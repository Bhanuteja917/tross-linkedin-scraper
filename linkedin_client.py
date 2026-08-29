#!/usr/bin/env python3
"""
LinkedIn profile scraper: fetch with curl_cffi, parse the SDUI/Flight payload,
emit flat JSON.

    export LI_AT='your-li-at-cookie'
    export LI_USER_AGENT='<the exact UA of the browser your li_at came from>'
    python3 linkedin_client.py samyakj05
    python3 linkedin_client.py samyakj05 -o out.json
    python3 linkedin_client.py samyakj05 --dump   # also save raw responses

Raw responses are saved only when --dump is passed (or a dump_dir is given to
scrape()), for offline debugging. The API never saves them.

WHY curl_cffi:
    Plain HTTP clients (urllib, requests, Go net/http, Postman) get HTTP 999
    from LinkedIn even with a valid li_at cookie and perfect headers. The block
    happens at the TLS handshake — LinkedIn fingerprints the JA3 signature
    before it reads a single header. curl_cffi wraps curl-impersonate, which
    reproduces Chrome's actual TLS/JA3 signature, so the connection looks like
    Chrome at the transport layer.

    This is still a plain HTTP client — no browser, no Chromium, no automation
    framework — so it satisfies the "no browser" requirement.
"""

import argparse
import dataclasses
import datetime
import json
import os
import pathlib
import random
import re
import sys
import time

from curl_cffi import requests

import flight
import linkedin_profile as profile

# Chrome build to impersonate at the TLS layer. Keep this close to the UA you
# send — a Chrome 151 UA over a Firefox TLS fingerprint is itself a signal.
IMPERSONATE = os.environ.get("LI_IMPERSONATE", "chrome150")

# Fallback UA. Override via LI_USER_AGENT to match the browser your li_at came
# from — LinkedIn checks the UA against the cookie's origin session.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
)

BASE = "https://www.linkedin.com"
COMPONENT_ENDPOINT = f"{BASE}/flagship-web/rsc-action/actions/component"
PAGINATION_ENDPOINT = f"{BASE}/flagship-web/rsc-action/actions/pagination"
COMPONENT_PREFIX = "com.linkedin.sdui.generated.profile.dsl.impl."

# A vanity slug: letters, digits, hyphens (and %-escapes for non-ASCII names).
VANITY_RE = re.compile(r"^[A-Za-z0-9\-%]+$")


def vanity_from(value: str) -> str:
    """Accept either a bare slug or a full profile URL and return the slug.

        https://www.linkedin.com/in/samyakj05/                  -> samyakj05
        https://www.linkedin.com/in/foo-bar-123/details/skills/ -> foo-bar-123
        samyakj05                                               -> samyakj05
    """
    value = (value or "").strip().split("?")[0].split("#")[0]
    if "/in/" in value:
        # The slug is the FIRST segment after /in/, not the last — the rest
        # (details/skills, overlay/…) is sub-navigation.
        value = value.split("/in/", 1)[1]
    slug = value.strip("/").split("/")[0]
    if not VANITY_RE.match(slug):
        raise ValueError(f"could not parse a vanity slug from {value!r}")
    return slug

# The /details/<section>/ pages. These carry the COMPLETE list; the summary
# cards on the main profile are truncated to two or three rows plus a
# "Show all N" link, so these are the primary source for anything repeated.
DETAIL_SECTIONS = (
    "experience",
    "education",
    "skills",
    "certifications",
    "languages",
)

# The rsc-action component POSTs. Superseded by the detail pages for every
# repeated section — kept only for About, which has no /details/ page of its
# own and is absent from the main profile page's payload.
SECTIONS = {
    "about": "profileCardsAboveActivity",
    "experience": "profileCardsExperienceOnly",
    "education": "profileCardsBelowActivityPart1WithoutExp",  # + certifications
    "languages": "profileCardsBelowActivityPart4",  # + organizations
    "skills": "profileCardsBelowActivityPart7",
}


class LinkedInClient:
    def __init__(self, li_at: str, user_agent: str, dump_dir: pathlib.Path):
        if not li_at:
            raise RuntimeError("LI_AT is required")
        self.user_agent = user_agent
        self.session = requests.Session(impersonate=IMPERSONATE)
        self.session.cookies.set("li_at", li_at, domain=".linkedin.com")
        self.csrf_token = None
        self.dump_dir = dump_dir
        self._dump_seq = 0
        self.last_dump = None

    # -- raw response capture ---------------------------------------------

    def dump(self, label: str, response, ext: str) -> pathlib.Path:
        """Write the response body verbatim, plus a sidecar .meta.json with the
        request/response metadata. Bodies are what we parse against, so they are
        saved before any decoding or heuristics touch them."""
        if not self.dump_dir:
            return None
        self.dump_dir.mkdir(parents=True, exist_ok=True)
        self._dump_seq += 1
        stem = f"{self._dump_seq:02d}_{label}"

        body_path = self.dump_dir / f"{stem}.{ext}"
        body_path.write_bytes(response.content)
        self.last_dump = body_path

        req = getattr(response, "request", None)
        meta = {
            "label": label,
            "fetched_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "impersonate": IMPERSONATE,
            "request": {
                "method": getattr(req, "method", None),
                "url": str(response.url),
                "headers": dict(getattr(req, "headers", None) or {}),
            },
            "status_code": response.status_code,
            "response_headers": dict(response.headers or {}),
            "encoding": response.encoding,
            "body_file": body_path.name,
            "body_bytes": len(response.content),
        }
        (self.dump_dir / f"{stem}.meta.json").write_text(
            json.dumps(meta, indent=2, default=str)
        )
        return body_path

    # -- session bootstrap ------------------------------------------------

    def fetch_profile_html(self, vanity: str, path: str = "", label: str = "profile_page") -> str:
        """GET a profile page. ``path`` appends a sub-page, e.g.
        ``details/experience``. Also picks up the JSESSIONID cookie that
        LinkedIn issues, which becomes the csrf-token for POSTs."""
        r = self.session.get(
            f"{BASE}/in/{vanity}/{path.strip('/') + '/' if path else ''}",
            headers={
                "User-Agent": self.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
            timeout=30,
        )
        self.dump(label, r, "html")
        if r.status_code == 999:
            raise RuntimeError(
                "HTTP 999 — LinkedIn blocked the request. If Chrome works but this "
                "doesn't, try a different LI_IMPERSONATE value (e.g. chrome146, "
                "chrome142). If Chrome is also blocked, wait before retrying."
            )
        r.raise_for_status()

        html = r.text
        if "authwall" in html or "Sign in to LinkedIn" in html:
            raise RuntimeError(
                "Got the auth wall — li_at is invalid/expired, or the User-Agent "
                "doesn't match the browser the cookie came from."
            )

        jsession = self.session.cookies.get("JSESSIONID")
        if jsession:
            self.csrf_token = jsession.strip('"')
        return html

    # -- section fetches --------------------------------------------------

    def fetch_section(self, vanity: str, component: str, label: str) -> str:
        if not self.csrf_token:
            raise RuntimeError("No csrf token — call fetch_profile_html() first")

        full = COMPONENT_PREFIX + component
        payload = {
            "clientArguments": {
                "payload": {"isSelfView": False, "vanityName": vanity},
                "states": [],
                "requestMetadata": {"$type": "proto.sdui.common.RequestMetadata"},
                "screenId": "com.linkedin.sdui.flagshipnav.home.Home",
                "knownTemplateIds": [],
            }
        }
        r = self.session.post(
            COMPONENT_ENDPOINT,
            params={"componentId": full, "sduiid": full},
            json=payload,
            headers={
                "User-Agent": self.user_agent,
                "Accept": "*/*",
                "Origin": BASE,
                "Referer": f"{BASE}/in/{vanity}/",
                "csrf-token": self.csrf_token,
                "x-li-rsc-stream": "true",
                "x-li-anchor-page-key": "d_flagship3_profile_view_base",
                "x-li-application-version": "0.2.6975",
            },
            timeout=30,
        )
        self.dump(label or component, r, "txt")
        r.raise_for_status()
        return r.text

    def fetch_pagination(
        self, vanity: str, request: dict, screen_id: str, start: int, count: int, label: str
    ) -> str:
        """Fetch one page of a lazily-loaded detail list.

        Most /details/ pages ship an empty list plus a ``nextPageRequest``
        descriptor; the rows only arrive from this POST. The body shape is the
        one the web client builds — {pagerId, clientArguments, paginationRequest}
        — with clientArguments being the request's own ``requestedArguments``
        plus states/screenId/knownTemplateIds."""
        if not self.csrf_token:
            raise RuntimeError("No csrf token — call fetch_profile_html() first")

        args = dict(request.get("requestedArguments") or {})
        payload = dict(args.get("payload") or {})
        payload["start"], payload["count"] = start, count
        args["payload"] = payload

        pager_id = request.get("pagerId", "")
        body = {
            "pagerId": pager_id,
            "clientArguments": {
                **args,
                "states": [],
                "screenId": screen_id,
                "knownTemplateIds": [],
            },
            "paginationRequest": {**request, "requestedArguments": args},
        }
        r = self.session.post(
            PAGINATION_ENDPOINT,
            params={"sduiid": pager_id},
            json=body,
            headers={
                "User-Agent": self.user_agent,
                "Accept": "*/*",
                "Origin": BASE,
                "Referer": f"{BASE}/in/{vanity}/{label}/",
                "csrf-token": self.csrf_token,
                "x-li-rsc-stream": "true",
                "x-li-anchor-page-key": "d_flagship3_profile_view_base",
                "x-li-application-version": "0.2.6975",
            },
            timeout=30,
        )
        self.dump(f"{label.replace('/', '_')}_page{start}", r, "txt")
        r.raise_for_status()
        return r.text


# -- end-to-end scrape ----------------------------------------------------


def scrape(
    vanity: str,
    li_at: str,
    user_agent: str,
    dump_dir: pathlib.Path = None,
    sections=None,
    delay: float = 0.7,
    max_pages: int = 20,
    log=None,
) -> "profile.Profile":
    """Fetch a profile and return it parsed.

    Three kinds of request, in order:

      1. GET /in/<vanity>/                — the top card (name, headline,
         company, location, photos). Nothing else lives here.
      2. POST the About component         — About has no /details/ page and is
         absent from the profile page payload, so this is the only way to it.
      3. GET /in/<vanity>/details/<s>/    — the complete list for each repeated
         section. These supersede the summary cards, which are truncated to a
         couple of rows plus a "Show all N" link.

    Every response is decoded as it arrives and folded into one Profile, so
    the raw dumps are a debugging aid rather than a required step.
    """
    log = log or (lambda *a: None)
    sections = tuple(DETAIL_SECTIONS if sections is None else sections)
    client = LinkedInClient(li_at, user_agent, dump_dir=dump_dir)

    def pause():
        time.sleep(delay + random.random() * 0.9)  # don't hammer

    log(f"GET  /in/{vanity}/")
    html = client.fetch_profile_html(vanity)
    log(f"     {len(html)} bytes, csrf_token={client.csrf_token}")

    p = profile.Profile()
    profile.ingest(p, flight.load_page_html(html)[0], vanity)

    # About: component POST, no detail page exists.
    pause()
    raw = client.fetch_section(vanity, SECTIONS["about"], label="about")
    log(f"POST about: {len(raw)} bytes")
    profile.ingest(p, flight.load_stream(raw)[0], vanity)

    for name in sections:
        pause()
        try:
            page = client.fetch_profile_html(
                vanity, path=f"details/{name}", label=f"details_{name}"
            )
        except Exception as exc:  # a section the member doesn't have 404s
            log(f"GET  /details/{name}/ failed: {exc}")
            continue

        tree, rows = flight.load_page_html(page)
        entities = flight.page_entities(tree)
        rendered = len(entities)

        # Only experience arrives server-rendered. The rest ship an empty list
        # plus a nextPageRequest, so the rows have to be paged in.
        request = flight.pagination_request(rows)
        pages = 0
        while request and pages < max_pages:
            pages += 1
            start = len(entities)
            count = int((request.get("requestedArguments") or {}).get("payload", {}).get("count") or 10)
            pause()
            try:
                raw = client.fetch_pagination(
                    vanity, request, flight.screen_id(rows), start, count,
                    label=f"details/{name}",
                )
            except Exception as exc:
                log(f"     page {pages} failed: {exc}")
                break
            more = flight.page_entities(flight.load_stream(raw)[0])
            fresh = [e for e in more if e not in entities]
            entities.extend(fresh)
            if len(fresh) < count:
                break  # short page: that was the last one

        n = profile.ingest_detail(p, entities, name, vanity)
        extra = f" ({rendered} inline + {len(entities) - rendered} paged)" if pages else ""
        log(f"GET  /details/{name}/: {len(page)} bytes -> {len(entities)} entities{extra} -> {n} parsed")

    return p


# -- cli ------------------------------------------------------------------


def main():
    ap = argparse.ArgumentParser(
        description="Fetch a LinkedIn profile and print it as JSON."
    )
    ap.add_argument("vanity", help="profile slug, e.g. bhanuteja917")
    ap.add_argument(
        "--section",
        choices=sorted(DETAIL_SECTIONS) + ["all"],
        default=None,
        action="append",
        dest="sections",
        help="limit which /details/ pages are fetched; repeatable (default: all)",
    )
    ap.add_argument("-o", "--out", help="write the JSON here instead of stdout")
    ap.add_argument(
        "--dump",
        nargs="?",
        const="",
        default=None,
        metavar="DIR",
        help="save the raw responses for offline debugging (off by default). "
        "With no value, writes to responses/<vanity>/<UTC timestamp>/; "
        "pass a directory to override.",
    )
    ap.add_argument("-q", "--quiet", action="store_true", help="no progress on stderr")
    args = ap.parse_args()

    vanity = vanity_from(args.vanity)
    li_at = os.environ.get("LI_AT", "")
    ua = os.environ.get("LI_USER_AGENT", DEFAULT_USER_AGENT)
    if not li_at:
        print("LI_AT env var is required", file=sys.stderr)
        sys.exit(1)

    dump_dir = None
    if args.dump is not None:  # flag given (with or without a directory)
        if args.dump:
            dump_dir = pathlib.Path(args.dump)
        else:
            stamp = datetime.datetime.now(datetime.timezone.utc).strftime(
                "%Y%m%dT%H%M%SZ"
            )
            dump_dir = pathlib.Path("responses") / vanity / stamp

    def log(msg):
        if not args.quiet:
            print(msg, file=sys.stderr)

    if dump_dir:
        log(f"raw responses -> {dump_dir}/")

    p = scrape(
        vanity,
        li_at,
        ua,
        dump_dir=dump_dir,
        sections=None if not args.sections or "all" in args.sections else args.sections,
        log=log,
    )

    text = json.dumps(dataclasses.asdict(p), indent=2, ensure_ascii=False)
    if args.out:
        pathlib.Path(args.out).write_text(text)
        log(f"wrote {args.out}")
    else:
        print(text)

    log(
        f"parsed: {len(p.experience)} positions, {len(p.education)} education, "
        f"{len(p.skills)} skills, {len(p.certifications)} certifications, "
        f"{len(p.languages)} languages"
    )


if __name__ == "__main__":
    main()
