#!/usr/bin/env python3
"""
Parser for LinkedIn's React Server Components (Flight) payloads.

Both response shapes we capture carry the *same* Flight stream:

  * the section POSTs (``responses/*/NN_<section>.txt``) return it as the
    response body directly;
  * the profile page HTML embeds it as a JSON array of string chunks in
    ``window.__como_rehydration__ = [...]`` — concatenate the chunks and you
    have the identical format.

Format: one row per line, ``<hexid>:<payload>``.

  ``1:I["<chunk-hash>",[],"ComponentName"]``   client-module reference
  ``0:["$","div",null,{...}]``                 a React element
  ``2:null``                                   an empty slot

Elements are ``["$", type, key, props]``. Any string value can be a *reference*
to another row: ``"$L1b"`` (lazy element) or ``"$1b"`` (plain value), plus the
sentinels ``"$undefined"`` and ``"$Sreact.fragment"``. Resolving those
references is the whole trick — parsing rows independently (which is what the
old ``parse_flight`` did) leaves the text stranded in unreferenced rows.

Human-readable content lives in ``props.children`` as plain strings, and in
``props.a11yText`` / ``aria-label`` / ``url`` on leaf components.
"""

import json
import re

# "$L1b" -> lazy element ref, "$1b" -> value ref. Row ids are lowercase hex.
REF_RE = re.compile(r"^\$L?([0-9a-f]+)$")

# Props that are pure styling/telemetry noise — never worth walking for text.
SKIP_PROPS = {"className", "style", "viewTrackingSpecs", "trackingSpecs"}

# Leaf props that hold real content rather than layout tokens.
TEXT_PROPS = ("a11yText", "altText", "accessibilityText", "aria-label", "text", "url")


def parse_rows(raw: str) -> dict:
    """Split a Flight stream into ``{row_id: value}``.

    ``I[...]`` module rows are kept as ``{"__module__": [...]}`` so a reference
    to one resolves to something inspectable instead of vanishing.
    """
    rows = {}
    for line in raw.split("\n"):
        line = line.rstrip("\r")
        if not line:
            continue
        i = line.find(":")
        if i < 0:
            continue
        rid, body = line[:i], line[i + 1 :]
        if body.startswith("I["):
            rows[rid] = {"__module__": json.loads(body[1:])}
        else:
            try:
                rows[rid] = json.loads(body)
            except json.JSONDecodeError:
                rows[rid] = {"__unparsed__": body}
    return rows


def resolve(node, rows, seen=frozenset(), depth=0):
    """Inline every ``$L<id>`` / ``$<id>`` reference, producing one whole tree.

    ``seen`` guards against reference cycles, which LinkedIn's payloads do
    contain (a component pointing back at its own subtree by path)."""
    if depth > 300:
        return node
    if isinstance(node, str):
        if node == "$undefined":
            return None
        if node.startswith("$S"):  # symbol, e.g. $Sreact.fragment
            return node[2:]
        m = REF_RE.match(node)
        if m and m.group(1) in rows:
            rid = m.group(1)
            if rid in seen:
                return f"<cycle:{rid}>"
            return resolve(rows[rid], rows, seen | {rid}, depth + 1)
        return node
    if isinstance(node, list):
        return [resolve(x, rows, seen, depth + 1) for x in node]
    if isinstance(node, dict):
        return {k: resolve(v, rows, seen, depth + 1) for k, v in node.items()}
    return node


def load_stream(raw: str):
    """Parse + resolve a raw section response into its root element tree.

    Section responses are rooted at row ``0``. Pagination responses are not
    rooted at all — they are a bag of rows — so fall back to resolving every
    row and letting the caller sweep the result."""
    rows = parse_rows(raw)
    if "0" in rows:
        return resolve(rows["0"], rows), rows
    return [resolve(v, rows) for v in rows.values()], rows


def load_page_html(html: str):
    """Pull the embedded Flight stream out of a profile page and resolve it."""
    i = html.find("window.__como_rehydration__")
    if i < 0:
        raise ValueError("no __como_rehydration__ payload in this HTML")
    j = html.index("[", i)
    chunks, _ = json.JSONDecoder().raw_decode(html[j:])
    return load_stream("".join(chunks))


# -- tree navigation ------------------------------------------------------


def is_element(n) -> bool:
    return isinstance(n, list) and len(n) == 4 and n[0] == "$"


def props_of(n) -> dict:
    return n[3] if is_element(n) and isinstance(n[3], dict) else {}


def find_components(node, predicate, depth=0):
    """Yield every element whose props satisfy ``predicate``. Does not recurse
    into a match, so nested collections come back as one item each."""
    if depth > 300:
        return
    if is_element(node):
        if predicate(props_of(node)):
            yield node
            return
        for k, v in props_of(node).items():
            if k not in SKIP_PROPS:
                yield from find_components(v, predicate, depth + 1)
        return
    if isinstance(node, list):
        for x in node:
            yield from find_components(x, predicate, depth + 1)
    elif isinstance(node, dict):
        for k, v in node.items():
            if k not in SKIP_PROPS:
                yield from find_components(v, predicate, depth + 1)


def _view_name(props) -> str:
    spec = props.get("viewTrackingSpecs")
    return str(spec.get("viewName", "")) if isinstance(spec, dict) else ""


def _is_card(props) -> bool:
    v = _view_name(props)
    return v.startswith("profile-card-") or v == "profile-top-card"


def cards(tree):
    """The profile cards in a response, keyed by their view name
    (``profile-top-card``, ``profile-card-experience``, ``profile-card-skills``,
    …). The page HTML carries the top card; the section POSTs carry the rest."""
    return {_view_name(props_of(el)): el for el in find_components(tree, _is_card)}


def _is_entity(props) -> bool:
    return isinstance(props.get("componentKey"), str) and props[
        "componentKey"
    ].startswith("entity-collection-item-")


def entities(node):
    """The repeated rows inside a card — one per position, school, licence,
    project or skill. LinkedIn tags each with ``componentKey`` of the form
    ``entity-collection-item-<hash>``."""
    return list(find_components(node, _is_entity))


# Cards that contain entity rows about *other people*, which must never be
# mistaken for the subject's own records.
FOREIGN_CARDS = (
    "profile-card-premium-browsemap-recommendation",
    "profile-card-browsemap",
    "profile-card-people-also-viewed",
    "profile-card-similar-profiles",
)


def page_entities(tree, skip=FOREIGN_CARDS):
    """Every entity row on a page, minus the recommendation carousels.

    A ``/details/<section>/`` page has one obvious list and no card wrapper we
    can rely on by name, so scope by exclusion instead of by inclusion. Rows
    are deduplicated by componentKey — a pagination response has no single
    root, so the same subtree can be reached by more than one path."""
    skip = set(skip)

    def walk(node, depth=0):
        if depth > 300:
            return
        if is_element(node):
            p = props_of(node)
            if _view_name(p) in skip:
                return
            if _is_entity(p):
                yield node
                return
            for k, v in p.items():
                if k not in SKIP_PROPS:
                    yield from walk(v, depth + 1)
            return
        if isinstance(node, list):
            for x in node:
                yield from walk(x, depth + 1)
        elif isinstance(node, dict):
            for k, v in node.items():
                if k not in SKIP_PROPS:
                    yield from walk(v, depth + 1)

    out, seen = [], set()
    for e in walk(tree):
        key = props_of(e).get("componentKey")
        if key in seen:
            continue
        seen.add(key)
        out.append(e)
    return out


URN_RE = re.compile(r"urn:li:fsd?_[A-Za-z]+:\(?[^\s\"',)]+\)?")
# "/in/<vanity>/overlay/2449734563/skill-associations-details/" and
# "/in/<vanity>/overlay/Position/2326825931/treasury/"
OVERLAY_ID_RE = re.compile(r"/overlay/(?:[A-Za-z]+/)?(\d{6,})/")


def entity_urn(entity) -> str:
    """A stable id for an entity. Prefers a real URN
    (``urn:li:fsd_profilePosition:(ACoAA…,2449734563)``), which the
    ``/details/`` pages carry; falls back to the numeric id embedded in the
    overlay links, which is all the summary cards expose."""
    blob = json.dumps(entity)
    m = URN_RE.search(blob)
    if m:
        return m.group(0)
    m = OVERLAY_ID_RE.search(blob)
    return m.group(1) if m else ""


# -- text extraction ------------------------------------------------------

# State ids, tracking hashes and design tokens all arrive as bare strings
# alongside the real content; these filter them out.
NOISE_EXACT = {
    "id", "stringValue", "intValue", "booleanValue", "floatValue", "expression",
    "booleanExpression", "notExpression", "floatExpression", "booleanBinding",
    "bindableBoolean", "sans", "serif", "xsmall", "small", "medium", "large",
    "xlarge", "normal", "bold", "default", "start", "center", "end", "open",
    "none", "underline", "horizontal", "vertical", "block", "inlineBlock",
    "isolate", "screen", "modal", "fullPage", "url", "icon", "link",
    "linkHover", "linkActive", "click", "fitContent", "fillAvailable",
    "flexStart", "secondary", "primary", "ghostCompact", "rounded", "square",
    "presentation", "Column", "Row", "LazyColumn", "en_US", "Loading",
    "Visible", "Hidden", "Collapsed", "Expanded", "MemoryNamespace",
    "CollectionNamespace", "onComponentAppear", "react.fragment",
}
NOISE_RE = re.compile(
    r"^(?:"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"  # uuid
    r"|[0-9a-f]{6,}"                       # css class / hash
    r"|[\d.]+x"                            # spacing token, "1x"
    r"|--[0-9a-z]+"                        # css variable
    r"|[A-Za-z0-9+/]{20,}={0,2}"           # base64 tracking id
    r"|(?:auto-component|expandable_text_block|entity-collection-item"
    r"|profile[-_][a-z0-9_-]*|[a-z-]+-(?:footer|divider)[a-z0-9-]*"
    r"|ACoAA[\w-]+|urn:li:[\w:.-]+"
    r"|(?:Presentation|Modal|Position|BlockPosition|ColorScheme|SortOrder"
    r"|LinkFormatting|Profile)[A-Za-z_]*"
    r")"
    r")$"
)


def texts(node, depth=0, out=None):
    """Human-readable strings in document order, deduplicated consecutively."""
    if out is None:
        out = []
    if depth > 300:
        return out
    if isinstance(node, str):
        s = node.strip()
        if (
            s
            and s not in NOISE_EXACT
            and not NOISE_RE.match(s)
            and not s.startswith(("com.linkedin.", "proto.sdui.", "$", "<cycle:"))
            and not (len(s) == 1 and not s.isalnum())
        ):
            if not out or out[-1] != s:
                out.append(s)
        return out
    if is_element(node):
        p = props_of(node)
        for attr in TEXT_PROPS:
            v = p.get(attr)
            if isinstance(v, str):
                texts(v, depth + 1, out)
        for k, v in p.items():
            if k not in SKIP_PROPS and k not in TEXT_PROPS:
                texts(v, depth + 1, out)
        return out
    if isinstance(node, list):
        for x in node:
            texts(x, depth + 1, out)
    elif isinstance(node, dict):
        for k, v in node.items():
            if k not in SKIP_PROPS:
                texts(v, depth + 1, out)
    return out


# -- images ---------------------------------------------------------------

MEDIA_PREFIX = "https://media.licdn.com/"
# A suffixUrl: the per-size tail of a media URL, e.g.
# "scale_100_100/B4DZ…/0/1785…?e=…&t=…" or "400_400/company-logo_400_400/0/…".
IMG_SUFFIX_RE = re.compile(r"^(?:\d+(?:_\d+)?|(?:scale|crop|shrink)_\d+_\d+)/")
# The size token inside a rendition: scale_100_100, crop_800_800, shrink_200_800.
IMG_SIZE_RE = re.compile(r"(?:scale|crop|shrink)_(\d+)_(\d+)")


def _is_image_root(s, marker: str) -> bool:
    return (
        isinstance(s, str)
        and s.startswith(MEDIA_PREFIX)
        and marker in s
        and " " not in s  # a srcset is a space-separated list, not a URL
    )


def _strings_under(node, out, depth=0):
    if depth > 8:
        return
    if isinstance(node, str):
        out.append(node)
    elif isinstance(node, list):
        for x in node:
            _strings_under(x, out, depth + 1)
    elif isinstance(node, dict):
        for k, v in node.items():
            if k not in SKIP_PROPS:
                _strings_under(v, out, depth + 1)


def image_renditions(node, marker: str) -> dict:
    """Every size LinkedIn offers for one image, keyed by ``(width, height)``.

    Both dimensions are kept because the size token is a width×height pair and
    the two are not always equal: profile photos are square (``crop_800_800``)
    but cover photos are not (``shrink_200_800``, ``shrink_350_1400``), so a
    single number names the rendition ambiguously.

    An image arrives as a ``rootUrl`` plus a list of per-size ``suffixUrls``,
    each carrying its own signed ``t=`` token — so the sizes cannot be derived
    by rewriting one URL, they have to be read off the payload and joined. A
    whole URL is usually present too (the one rendition the page renders), and
    both forms are folded together here.

    Grouping is per-dict so two images sharing a marker can't be mixed: the
    deepest group wins, being the tightest scope around a single image.
    """
    groups = []

    def walk(n, depth=0):
        if depth > 300:
            return
        if isinstance(n, dict):
            if any(_is_image_root(v, marker) for v in n.values()):
                strs = []
                _strings_under(n, strs)
                groups.append((depth, strs))
            for k, v in n.items():
                if k not in SKIP_PROPS:
                    walk(v, depth + 1)
        elif isinstance(n, list):
            for x in n:
                walk(x, depth + 1)

    walk(node)

    out = {}

    def add(url):
        m = IMG_SIZE_RE.search(url)
        if m:
            out.setdefault((int(m.group(1)), int(m.group(2))), url)

    for _, strs in sorted(groups, key=lambda g: -g[0]):
        # The shortest root is the bare prefix the suffixes attach to; a longer
        # one is an already-joined rendition, which `add` takes as it stands.
        base = min((s for s in strs if _is_image_root(s, marker)), key=len)
        for s in strs:
            if _is_image_root(s, marker):
                add(s)
            elif IMG_SUFFIX_RE.match(s):
                add(base + s)
    return out


def find_by_identifier(node, identifier: str):
    """The first component tagged with ``identifier`` — LinkedIn's
    ``observabilityIdentifier``, the one stable name a card's own subtree has.
    Returns None when the payload doesn't carry it, so callers can fall back
    to the whole card."""

    def tagged(props) -> bool:
        return any(v == identifier for v in props.values() if isinstance(v, str))

    return next(find_components(node, tagged), None)


# -- pagination -----------------------------------------------------------

PAGINATION_TYPE = "proto.sdui.actions.requests.PaginationRequest"


def find_dicts(node, predicate, depth=0):
    """Yield every dict in the payload satisfying ``predicate``."""
    if depth > 300:
        return
    if isinstance(node, dict):
        if predicate(node):
            yield node
            return
        for v in node.values():
            yield from find_dicts(v, predicate, depth + 1)
    elif isinstance(node, list):
        for x in node:
            yield from find_dicts(x, predicate, depth + 1)


PROFILE_PAGER_PREFIX = "com.linkedin.sdui.pagers.profile"


def pagination_request(rows, pager_prefix=PROFILE_PAGER_PREFIX):
    """The ``nextPageRequest`` a lazily-loaded list leaves behind.

    Most /details/ pages ship an EMPTY list plus this descriptor; the rows
    themselves only arrive from a follow-up POST. It carries the pagerId and
    the full payload (vanityName, profileId, start, count, and the section's
    replaceable-component ref), so the follow-up needs nothing invented."""
    for d in find_dicts(rows, lambda x: x.get("$type") == PAGINATION_TYPE):
        # Every page also prefetches the main feed's pager; only the profile
        # ones are ours.
        if str(d.get("pagerId", "")).startswith(pager_prefix):
            return d
    return None


def screen_id(rows, prefix="com.linkedin.sdui.flagshipnav.profile.") -> str:
    """The screenId a /details/ page reports, e.g.
    ``…profile.ProfileEducationDetails``. The generic ``…profile.Profile`` is
    also present, so prefer the specific one."""
    found = {
        v
        for d in find_dicts(rows, lambda x: isinstance(x.get("screenId"), str))
        for v in [d["screenId"]]
        if v.startswith(prefix)
    }
    specific = [v for v in found if v.endswith("Details")]
    return max(specific or found or [""], key=len)
