#!/usr/bin/env python3
"""
Public HTTP API over the LinkedIn scraper.

    GET /profile?url=<profile url or vanity slug>   -> the profile as JSON
    GET /health                                     -> liveness + config summary

Run locally:
    export LI_AT='...'                # the cookie the server scrapes with
    export LINKEDIN_API_KEY='...'     # require this in the X-API-Key header
    uvicorn api:app --reload

Configuration is entirely environment-driven so the same image runs anywhere:

    LI_AT             cookie used when the caller doesn't supply one (below)
    LI_USER_AGENT     UA to match that cookie's browser (falls back to default)
    LINKEDIN_API_KEY  if set, callers must send it as X-API-Key; if unset the
                      endpoint is OPEN and logs a warning on startup
    CACHE_TTL         seconds to cache a parsed profile by slug (default 3600);
                      set to 0 to disable caching entirely
    CACHE_DB          path to the SQLite cache file (default ./cache.db). Put it
                      on a mounted volume if you want it to survive redeploys.
    CACHE_SWEEP_SECS  how often expired rows are deleted (default 300)
    REQUEST_DELAY     base politeness delay between upstream calls (default 0.7)
    CORS_ORIGINS      comma-separated allowlist, or * (default: none)

Cookie resolution per request: the X-LI-AT header (bring-your-own-key) wins;
otherwise the server's LI_AT is used. Raw upstream responses are NEVER written
to disk here — that is a local-debug-only feature of the CLI.
"""

import asyncio
import contextlib
import dataclasses
import os
import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader

import linkedin_client as lc
from cache import ProfileCache

API_KEY = os.environ.get("LINKEDIN_API_KEY", "")
SERVER_LI_AT = os.environ.get("LI_AT", "")
SERVER_UA = os.environ.get("LI_USER_AGENT", lc.DEFAULT_USER_AGENT)
CACHE_TTL = float(os.environ.get("CACHE_TTL", "3600"))
CACHE_DB = os.environ.get("CACHE_DB", "cache.db")
CACHE_SWEEP_SECS = float(os.environ.get("CACHE_SWEEP_SECS", "300"))
REQUEST_DELAY = float(os.environ.get("REQUEST_DELAY", "0.7"))
CORS_ORIGINS = [o.strip() for o in os.environ.get("CORS_ORIGINS", "").split(",") if o.strip()]

# -- persistent TTL cache --------------------------------------------------
# Keyed by vanity slug: a public profile's parsed form is the same whichever
# valid cookie fetched it, so entries are shared across callers. Backed by a
# SQLite file so it survives restarts and is shared by every worker on the
# box; swap for Redis if you ever need it shared across machines.
_cache = ProfileCache(CACHE_DB, CACHE_TTL)

# One upstream scrape per slug at a time, so a burst of duplicate requests
# collapses into a single set of LinkedIn calls instead of hammering them.
# In-process only — it de-dupes within a worker, which is where bursts land.
_locks: dict[str, asyncio.Lock] = {}

# Track when the server last reached LinkedIn successfully, for /health.
_last_success_at: float | None = None


async def _sweeper() -> None:
    """Delete expired rows on a timer so the cache file stays bounded."""
    while True:
        await asyncio.sleep(CACHE_SWEEP_SECS)
        with contextlib.suppress(Exception):
            await asyncio.to_thread(_cache.purge_expired)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if _cache.enabled:
        await asyncio.to_thread(_cache.purge_expired)
        task = asyncio.create_task(_sweeper())
    else:
        task = None
    yield
    if task is not None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


app = FastAPI(
    title="LinkedIn Profile API",
    version="1.0.0",
    description=(
        "Fetch a public LinkedIn profile as structured JSON.\n\n"
        "**Try it here:** if the server was started with `LINKEDIN_API_KEY`, click "
        "**Authorize** and paste the key once. If the server has no `LI_AT` cookie "
        "configured, supply your own in the `X-LI-AT` field per request."
    ),
    lifespan=lifespan,
)

# Declared as a security scheme (rather than a plain header param) purely so
# Swagger UI grows an Authorize button and remembers the key across calls.
# auto_error=False keeps it optional: when LINKEDIN_API_KEY is unset the
# endpoint is open, and _require_api_key does the actual enforcing.
_api_key_scheme = APIKeyHeader(
    name="X-API-Key",
    auto_error=False,
    description="Required only when the server sets LINKEDIN_API_KEY.",
)

if CORS_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=CORS_ORIGINS,
        allow_methods=["GET"],
        allow_headers=["*"],
    )

def _require_api_key(x_api_key: str | None) -> None:
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="invalid or missing X-API-Key")


def _status_for(exc: RuntimeError) -> int:
    """Map the scraper's RuntimeErrors onto HTTP codes."""
    msg = str(exc).lower()
    if "auth wall" in msg or "csrf" in msg or "li_at" in msg:
        return 502  # our cookie is bad — a server-side problem, not the caller's
    if "999" in msg:
        return 429  # LinkedIn is rate-limiting us
    return 502


@app.get("/health", summary="Liveness and config summary", tags=["meta"])
async def health():
    return {
        "ok": True,
        "server_cookie_configured": bool(SERVER_LI_AT),
        "api_key_required": bool(API_KEY),
        "cache_entries": await asyncio.to_thread(_cache.count),
        "cache_ttl_seconds": CACHE_TTL,
        "cache_db": CACHE_DB,
        "last_success_at": _last_success_at,
        "impersonate": lc.IMPERSONATE,
    }


@app.get(
    "/profile",
    summary="Fetch a profile as JSON",
    tags=["profile"],
    responses={
        200: {"description": "The parsed profile. `X-Cache: HIT|MISS` says whether it was cached."},
        400: {"description": "The url/slug could not be parsed"},
        401: {"description": "Missing or wrong X-API-Key"},
        429: {"description": "LinkedIn is rate-limiting the server (upstream 999)"},
        502: {"description": "Upstream scrape failed, or the server's cookie is expired"},
        503: {"description": "No cookie available: set LI_AT on the server or send X-LI-AT"},
    },
)
async def profile(
    url: str = Query(
        ...,
        description="A profile URL or a bare vanity slug",
        examples=["williamhgates", "https://www.linkedin.com/in/williamhgates/"],
    ),
    refresh: bool = Query(False, description="Bypass the cache for this request"),
    x_api_key: str | None = Depends(_api_key_scheme),
    x_li_at: str | None = Header(default=None, description="BYO cookie override"),
    x_li_user_agent: str | None = Header(default=None),
):
    global _last_success_at
    _require_api_key(x_api_key)

    try:
        slug = lc.vanity_from(url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    li_at = x_li_at or SERVER_LI_AT
    if not li_at:
        raise HTTPException(
            status_code=503,
            detail="no cookie available: set LI_AT on the server or send X-LI-AT",
        )
    user_agent = x_li_user_agent or SERVER_UA

    if not refresh:
        cached = await asyncio.to_thread(_cache.get, slug)
        if cached is not None:
            return JSONResponse(cached, headers={"X-Cache": "HIT"})

    lock = _locks.setdefault(slug, asyncio.Lock())
    async with lock:
        # Another request may have filled the cache while we waited.
        if not refresh:
            cached = await asyncio.to_thread(_cache.get, slug)
            if cached is not None:
                return JSONResponse(cached, headers={"X-Cache": "HIT"})
        try:
            # curl_cffi is synchronous; keep it off the event loop.
            result = await asyncio.to_thread(
                lc.scrape, slug, li_at, user_agent, None, None, REQUEST_DELAY
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=_status_for(exc), detail=str(exc))
        except Exception as exc:  # network/parse failure
            raise HTTPException(status_code=502, detail=f"scrape failed: {exc}")

    data = dataclasses.asdict(result)
    _last_success_at = time.time()
    await asyncio.to_thread(_cache.set, slug, data, _last_success_at)
    return JSONResponse(data, headers={"X-Cache": "MISS"})
