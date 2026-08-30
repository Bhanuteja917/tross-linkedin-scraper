# LinkedIn Profile API

Turn a LinkedIn profile URL into structured JSON — **without a browser**.

The service talks to LinkedIn's own web endpoints directly over HTTP, decodes
the React Server Components ("Flight") payloads they return, and folds every
response into one flat `Profile` object served over a small FastAPI app.

```
GET /profile?url=williamhgates  ->  { "name": …, "experience": [ … ], … }
```

| Module | Role |
|---|---|
| [`linkedin_client.py`](linkedin_client.py) | Transport — an authenticated `requests` session, plus a CLI |
| [`flight.py`](flight.py) | Decoder — RSC/Flight wire-format parser |
| [`linkedin_profile.py`](linkedin_profile.py) | Mapper — component trees → flat fields |
| [`cache.py`](cache.py) | SQLite TTL cache |
| [`api.py`](api.py) | HTTP API |

---

## Setup

Requires Python 3.10+ (3.12 in the image).

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### The cookie

Scraping a profile needs a logged-in member session. Grab the `li_at` cookie
from a browser where you are signed in (DevTools → Application → Cookies →
`https://www.linkedin.com` → `li_at`) and export it together with **that same
browser's** User-Agent — LinkedIn checks the UA against the session the cookie
came from.

```bash
export LI_AT='...'
export LI_USER_AGENT='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) …'
```

### Run the CLI

```bash
python3 linkedin_client.py williamhgates              # JSON to stdout
python3 linkedin_client.py williamhgates -o out.json
python3 linkedin_client.py williamhgates --section skills --section education
python3 linkedin_client.py williamhgates --dump       # also save raw responses
```

`--dump` writes the untouched upstream responses to `responses/<slug>/<timestamp>/`
for offline debugging. It is a **CLI-only** flag; the HTTP API never writes raw
responses to disk.

### Run the API

```bash
export LINKEDIN_API_KEY='choose-one'    # optional but recommended
uvicorn api:app --reload --no-server-header
```

Interactive docs at <http://127.0.0.1:8000/docs> — click **Authorize**, paste
the key once, and it is remembered across calls.

### Docker

```bash
docker build -t linkedin-api .
docker run -p 8080:8080 -e LI_AT="$LI_AT" -e LI_USER_AGENT="$LI_USER_AGENT" \
           -e LINKEDIN_API_KEY='choose-one' linkedin-api
```

The image runs as a non-root user and starts uvicorn with `--no-server-header`.
[`render.yaml`](render.yaml) is a ready-to-deploy Render blueprint; the cache
path there points at ephemeral storage, since the free plan has no disk.

### Configuration

All environment-driven, so the same image runs anywhere.

| Variable | Default | Meaning |
|---|---|---|
| `LI_AT` | — | cookie used when the caller doesn't supply one |
| `LI_USER_AGENT` | a Chrome 151 UA | must match the browser the cookie came from |
| `LINKEDIN_API_KEY` | *unset* | if set, callers must send `X-API-Key`; **if unset the endpoint is open** |
| `CACHE_TTL` | `3600` | seconds to cache a parsed profile; `0` disables |
| `CACHE_DB` | `./cache.db` | SQLite cache file — mount a volume to persist it |
| `CACHE_SWEEP_SECS` | `300` | how often expired rows are deleted |
| `REQUEST_DELAY` | `0.7` | base politeness delay between upstream calls |
| `CORS_ORIGINS` | *none* | comma-separated allowlist, or `*` |

---

## API

### `GET /profile`

| Param | | |
|---|---|---|
| `url` | query, required | a full profile URL or a bare vanity slug |
| `refresh` | query, default `false` | bypass the cache for this request |
| `X-API-Key` | header | required only when the server sets `LINKEDIN_API_KEY` |
| `X-LI-AT` | header | bring-your-own cookie; overrides the server's |
| `X-LI-User-Agent` | header | UA to pair with that cookie |

Responses carry `X-Cache: HIT` or `MISS`.

```bash
curl -H 'X-API-Key: choose-one' \
     'http://127.0.0.1:8000/profile?url=https://www.linkedin.com/in/williamhgates/'
```

| Status | When |
|---|---|
| 200 | the parsed profile |
| 400 | the url/slug could not be parsed |
| 401 | missing or wrong `X-API-Key` |
| 429 | LinkedIn is rate-limiting the server (upstream HTTP 999) |
| 502 | the scrape failed, or the server's cookie is expired |
| 503 | no cookie available — set `LI_AT` or send `X-LI-AT` |

### `GET /health`

Liveness plus a config summary: whether a cookie and an API key are configured,
cache size and TTL, and the last successful upstream fetch.

### Output shape (11 fields)

`name, headline, location, about, experience[], education[], skills[],
certifications[], languages[], profile_photo, cover_photo`

```jsonc
{
  "name": "Ada Lovelace",
  "headline": "Software Engineer at Example",
  "location": "London, England, United Kingdom",
  "about": "First paragraph.\n\nSecond paragraph.",
  "experience": [{
    "title": "Software Engineer",
    "company": "Example",
    "company_url": "https://www.linkedin.com/company/example/",
    "employment_type": "Full-time",
    "date_range": "Mar 2026 - Present",   // as LinkedIn displays it
    "start_date": "2026-03",              // machine-readable
    "end_date": null,                     // null while current
    "is_current": true,
    "duration": "6 mos",
    "location": "London · Hybrid",
    "description": "Line one\nLine two"
  }],
  "education":      [{ "school": …, "school_url": …, "degree": …, "years": "2019 - 2023" }],
  "skills":         ["Python", "Distributed Systems"],
  "certifications": [{ "name": …, "issuer": …, "issued": "Jun 2023",
                       "issued_date": "2023-06", "credential_id": …,
                       "credential_url": "https://coursera.org/verify/…" }],
  "languages":      ["English", "French"],
  "profile_photo":  { "url": "…crop_800_800…",
                      "renditions": { "100": …, "200": …, "400": …, "800": … } },
  "cover_photo":    { "url": "…shrink_350_1400…",
                      "renditions": { "200": …, "350": … } }
}
```

Conventions worth knowing:

- **Absent values are `null`, never `""`.** An empty string would mean the field
  exists and is blank; empty collections stay `[]`.
- **Dates come twice.** `date_range` / `issued` are the strings LinkedIn renders;
  `start_date`, `end_date`, `is_current`, `issued_date` are the machine-readable
  form (`"2026-03"`, or `"2026"` where LinkedIn published no month). No consumer
  should have to parse English.
- **Outbound links are unwrapped.** LinkedIn wraps every off-site link in a
  `/safety/go/?url=…` interstitial; `credential_url` is the real credential.
- **Images come at full size.** `url` is the largest rendition. `renditions` is
  keyed by **width**, ascending — width rather than the larger edge, because a
  cover's 200×800 and a photo's 800×800 would otherwise both answer to `"800"`.
  Each rendition carries its own signed token, so they are read off the payload,
  never produced by rewriting one URL.

### Fidelity

The API returns **what LinkedIn displays, including inconsistencies entered by
the profile owner**. Overlapping date ranges, two roles at one company for the
same two months, a degree with no years, a URL typed into the credential-ID
field — these are reproduced as they appear rather than reconciled. A field is
`null` only because LinkedIn showed nothing there.

---

## Approach

### Why a plain HTTP client

The requirement was "no browser", so the transport is `requests` with a real
`li_at` cookie and Chrome-consistent headers. LinkedIn's profile page is
server-rendered as an RSC/Flight stream, so everything needed is in the
response body — there is nothing to execute, and no headless browser to run.

The cookie is what unlocks the member view. Measured against a live session

### Authentication

| Item | Source | Purpose |
|---|---|---|
| `li_at` cookie | env `LI_AT`, or per-request `X-LI-AT` | member session |
| `JSESSIONID` cookie | issued by LinkedIn on the first GET | becomes the `csrf-token` for every POST |
| User-Agent | env `LI_USER_AGENT` | must match the browser the cookie came from |

The first page GET does double duty: it returns the top-card data **and** sets
`JSESSIONID`, whose value (minus quotes) is sent back as the `csrf-token` header
on the component and pagination POSTs.

### The endpoints hit, in order

All under `https://www.linkedin.com`.

**1. `GET /in/<vanity>/` — the top card.** The profile page HTML, with the data
embedded as an RSC/Flight stream (`window.__como_rehydration__`). Yields name,
headline, location, profile photo, cover photo. Also sets `JSESSIONID`.

**2. `POST /flagship-web/rsc-action/actions/component` — About.** About has no
`/details/` page of its own and is *not* in the profile-page payload, so it has
to be requested as an SDUI component.

```
POST /flagship-web/rsc-action/actions/component?componentId=<full>&sduiid=<full>
full = com.linkedin.sdui.generated.profile.dsl.impl.profileCardsAboveActivity

body: { "clientArguments": {
          "payload": { "isSelfView": false, "vanityName": "<vanity>" },
          "states": [], "requestMetadata": {…},
          "screenId": "com.linkedin.sdui.flagshipnav.home.Home",
          "knownTemplateIds": [] } }
headers: csrf-token, x-li-rsc-stream: true,
         x-li-anchor-page-key: d_flagship3_profile_view_base, …
```

**3. `GET /in/<vanity>/details/<section>/` — the full lists**, for each of
experience, education, skills, certifications, languages. The summary cards on
the main page are truncated ("Show all N"); these pages carry the complete list,
in the same embedded-Flight-in-HTML shape as step 1.

- **Experience** arrives **server-rendered** here — done after this GET.
- **Education / skills / certifications / languages** ship an **empty list** plus
  a `nextPageRequest` descriptor. The rows only arrive from step 4.

**4. `POST /flagship-web/rsc-action/actions/pagination` — lazy list pages.**
Fires only when step 3 returned a `nextPageRequest`; loops until a short page
(fewer rows than requested) or `max_pages` (20).

```
POST /flagship-web/rsc-action/actions/pagination?sduiid=<pagerId>
body: { "pagerId": <pagerId>,
        "clientArguments": { …requestedArguments, states, screenId, knownTemplateIds },
        "paginationRequest": { …request, requestedArguments } }
```

The `pagerId` is extracted from step 3's stream, filtered to the profile pager
namespace `com.linkedin.sdui.pagers.profile` so the home-feed pager is never
grabbed by mistake. `start`/`count` are injected per page.

```
GET  /in/<vanity>/                         -> name, headline, location, photos + JSESSIONID
POST /rsc-action/actions/component (About) -> about
GET  /in/<vanity>/details/experience/      -> experience (server-rendered)
GET  /in/<vanity>/details/education/       -> empty + nextPageRequest
  POST /rsc-action/actions/pagination         -> education rows (looped)
GET  /in/<vanity>/details/skills/          -> empty + nextPageRequest
  POST /rsc-action/actions/pagination         -> skills rows (looped)
GET  /in/<vanity>/details/certifications/  -> empty + nextPageRequest
  POST /rsc-action/actions/pagination         -> certification rows (looped)
GET  /in/<vanity>/details/languages/       -> empty + nextPageRequest
  POST /rsc-action/actions/pagination         -> language rows (looped)
```

A randomized `delay + random()*0.9s` pause sits between every call.

### Decoding the Flight payload

RSC responses are **not** clean REST JSON. The wire format is one row per line,
`<hexid>:<payload>`, where elements look like `["$", type, key, props]` and
strings reference other rows (`$L1b`, `$1b`, `$undefined`, `$Sreact.fragment`).
[`flight.py`](flight.py):

1. `parse_rows` — split the stream into id→payload rows.
2. `resolve` — recursively splice every `$`-reference into one tree
   (cycle-guarded). `load_stream` falls back to resolving all rows when there is
   no root row `0` — the shape pagination responses take.
3. `load_page_html` — pull the embedded stream out of the profile HTML first.
4. Walk the tree for the pieces that matter: `cards` (`viewTrackingSpecs.viewName`
   starting `profile-card-` / `profile-top-card`), `entities` (`componentKey`
   starting `entity-collection-item-`), `texts`, `image_renditions`,
   `find_by_identifier`, and the `pagination_request` / `screen_id` descriptors
   that drive step 4.

### Mapping to fields

Because SDUI ships **presentation, not a schema**, there is no field named
`title` to read. Fields are recovered by **order, anchored on self-identifying
strings** — date ranges, durations, `Issued `, `Credential ID `, workplace types
(Remote / On-site / Hybrid), degree badges. Handlers: `parse_top_card`,
`parse_about`, `parse_experience` (which handles grouped multi-role companies),
`parse_education`, `parse_certifications`, `parse_simple_list` (skills,
languages).

Two places need more than ordering. `parse_about` scopes to the About text's own
subtree via its `observabilityIdentifier` and then stops at the first widget
heading, because the About *card* is a container whose siblings (top-skills,
featured) would otherwise be swallowed into the text. `image_renditions` groups
per-dict so two images sharing a URL marker can't be mixed.

### Serving

- SQLite TTL cache keyed by vanity slug — a public profile's parsed form is the
  same whichever valid cookie fetched it, so entries are shared across callers.
  Backed by a file so it survives restarts and is shared by every worker on the
  box. Expired rows are dropped lazily on read and swept on a timer.
- A per-slug `asyncio.Lock` collapses a burst of duplicate requests into one
  upstream scrape.
- `requests` is synchronous, so `scrape` runs off the event loop via
  `asyncio.to_thread`.

---

## Known limitations

- **Needs a real member cookie.** No `li_at`, no data — and "no data" can arrive
  as a plausible-looking `HTTP 200`: logged out, LinkedIn serves a crawler page
  with job titles and past employers asterisk-masked and no RSC stream. Cookies
  expire, and one used from a very different IP or UA than it was issued to can
  be invalidated — the API surfaces that as `502`.
- **Unofficial and undocumented.** These are LinkedIn's internal SDUI endpoints,
  not a public API. Payload shapes can change without notice; a redesign of the
  About card or a renamed `observabilityIdentifier` breaks a field, not the
  service. Extraction degrades rather than crashes: a handler that finds nothing
  yields `null`.
- **Rate limiting is real.** Sustained scraping earns HTTP 999 (surfaced as
  `429`). The politeness delay and the cache reduce the pressure; they do not
  remove it. There is no retry/backoff loop and no proxy rotation.
- **Scraping LinkedIn is against its Terms of Service.** This is a technical
  demonstration; running it at scale is your call and your risk.
- **English display strings assumed.** The anchors (`Issued`, `Present`,
  `Full-time`, month abbreviations) are the English UI's. A cookie whose account
  is set to another language will still return text, but the structured date
  fields and `employment_type` will not populate.
- **Ordering-based mapping is inherently fragile** where a profile omits fields.
  Unusual layouts — no dates on a role, a company with many stacked positions —
  are handled explicitly, but the space of layouts is not closed.
- **Public profile data only.** No connections, posts, recommendations,
  endorsements, contact info, or anything behind a privacy setting. Sections
  beyond the nine mapped fields are not fetched.
- **Pagination stops at 20 pages** per section, a guard against a runaway loop.
- **The cache is per-box.** SQLite, not Redis — fine for one instance, not shared
  across machines. Cached JSON is written by whichever version of the mapper was
  running, so an output-shape change wants a `?refresh=true` or a TTL expiry
  before consumers see it.