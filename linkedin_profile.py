#!/usr/bin/env python3
"""
Maps the decoded Flight trees (see flight.py) onto the flat profile shape.

    python3 linkedin_profile.py responses/samyakj05/20260829T081415Z

The SDUI payload has no field names — a position's title, employment type and
date range arrive as three sibling text nodes with hashed CSS classes and
nothing else to tell them apart. So the mapping is order-based within each
entity, anchored on the few strings that are self-identifying:

  * date ranges      "Aug 2024 - Present · 2 yrs 1 mo"   -> starts a position
  * bare durations   "2 yrs 8 mos"                       -> a company header,
                                                            i.e. a grouped
                                                            multi-role entity
  * "Issued …" / "Credential ID …" / "Associated with …" -> self-labelling

Everything else is positional relative to those anchors.
"""

import argparse
import dataclasses
import json
import pathlib
import re
import sys

import flight

# "Aug 2024 - Present · 2 yrs 1 mo" / "2020 - 2024 · 4 yrs"
DATE_RANGE_RE = re.compile(
    r"^(?:\w{3}\s+)?\d{4}\s*[-–]\s*(?:Present|(?:\w{3}\s+)?\d{4})(?:\s*·|$)"
)
# "2 yrs 8 mos", "7 mos" — a bare total duration. On the /details/ pages the
# same line carries the employment type too: "Full-time · 3 yrs 8 mos".
# Either form on line 2 marks a company group header (one company, many roles).
DURATION_RE = re.compile(r"^\d+\s+(?:yrs?|mos?)(?:\s+\d+\s+mos?)?$")
GROUP_HEADER_RE = re.compile(
    r"^(?:(?P<type>[A-Za-z-]+(?:\s+[A-Za-z-]+)?)\s+·\s+)?"
    r"\d+\s+(?:yrs?|mos?)(?:\s+\d+\s+mos?)?$"
)
# Workplace types stand alone as a "location" line on the detail pages.
WORKPLACE_TYPES = {"Remote", "On-site", "Hybrid"}
# "2025 – 2027" on an education row (no "·" tail).
YEAR_SPAN_RE = re.compile(r"^\d{4}\s*[-–]\s*(?:\d{4}|Present)$")
DEGREE_BADGE_RE = re.compile(r"^·\s*\d+(?:st|nd|rd|th)\+?$")
EMPLOYMENT_TYPES = {
    "Full-time", "Part-time", "Self-employed", "Freelance", "Contract",
    "Internship", "Apprenticeship", "Seasonal", "Temporary",
}
# LinkedIn qualifies the base type on many profiles: "Permanent Full-time",
# "Contract Full-time". Both halves have to be recognised as one type, or the
# whole string gets mistaken for a company name.
EMPLOYMENT_QUALIFIERS = {"Permanent", "Contract", "Temporary", "Seasonal"}

# Presentation tokens and affordance labels that survive flight.texts() but
# carry no profile data.
DROP_EXACT = {
    "WIDTH_AND_HEIGHT", "iconDisabled", "iconKnockout", "backgroundFaint",
    "embeddedWebView", "google.protobuf.Empty", "condensed", "topStart",
    "bottomStart", "bottom-end", "img", "text", "menuitem", "button",
    "circle", "light", "more", "Media", "Endorse", "Show credential",
    "position", "education", "auto", "stretch", "viewLink", "Message",
    "h1", "h2", "h3", "Premium", "Buffer",
    # <img> attribute values. They render ahead of the row's own text, so on a
    # certification they land in the name/issuer slots and push the real ones out.
    "low", "high", "lazy", "eager", "async", "sync", "ghost",
}
DROP_PREFIX = (
    "Thumbnail for ", "Skills for ", "Show credential for ", "Endorse ",
    "entity-collection-item", "expandable_text_block", "auto-binding",
    "profileEndorse", "actor_preview", "Manage notifications about ",
    "Message ", "state:invitation:",
)
# Image path fragments: "400_400/company-logo_400_400/0/…", "scale_100_100/…"
IMG_FRAGMENT_RE = re.compile(r"^(?:\d+(?:_\d+)?|scale_\d+_\d+|crop_\d+_\d+)/")
# preserveAspectRatio, the one image attribute whose value isn't a bare word:
# "xMidYMid slice", "xMinYMax meet".
SVG_ASPECT_RE = re.compile(r"^x(?:Mid|Min|Max)Y(?:Mid|Min|Max)\s+(?:meet|slice)$")


def clean(texts, vanity: str = "") -> list:
    """Strip presentation tokens, media URLs and affordance labels, leaving
    only strings that could plausibly be profile content."""
    out = []
    for t in texts:
        if (
            not t
            or t in DROP_EXACT
            or t == vanity
            or t.startswith(DROP_PREFIX)
            or t.startswith(("http://", "https://", "/", "urn:li:"))
            or t.endswith(" logo")
            or IMG_FRAGMENT_RE.match(t)
            or SVG_ASPECT_RE.match(t)
        ):
            continue
        out.append(t)
    return out


# -- record shapes --------------------------------------------------------


@dataclasses.dataclass
class Position:
    title: str = ""
    company: str = ""
    company_url: str = ""
    employment_type: str = ""
    date_range: str = ""
    duration: str = ""
    location: str = ""
    description: str = ""


@dataclasses.dataclass
class Education:
    school: str = ""
    school_url: str = ""
    degree: str = ""
    years: str = ""


@dataclasses.dataclass
class Certification:
    name: str = ""
    issuer: str = ""
    issued: str = ""
    credential_id: str = ""
    credential_url: str = ""


@dataclasses.dataclass
class Profile:
    """Exactly the fields the brief asks for, in the order it lists them."""

    name: str = ""
    headline: str = ""
    location: str = ""
    about: str = ""
    experience: list = dataclasses.field(default_factory=list)
    education: list = dataclasses.field(default_factory=list)
    skills: list = dataclasses.field(default_factory=list)
    certifications: list = dataclasses.field(default_factory=list)
    languages: list = dataclasses.field(default_factory=list)
    profile_photo_url: str = ""
    cover_photo_url: str = ""


# -- per-card parsers -----------------------------------------------------


def _first_url(texts, *, contains: str = "", prefix: str = "https://") -> str:
    for t in texts:
        if t.startswith(prefix) and (not contains or contains in t) and " " not in t:
            return t
    return ""


def _best_image_url(texts, marker: str) -> str:
    """Images arrive split into a rootUrl and per-size suffixUrls, and also as
    a pre-joined srcset. Take the longest single URL — that is the joined form
    — and skip srcsets, which are space-separated lists."""
    best = ""
    for t in texts:
        if t.startswith("https://media.licdn.com/") and marker in t and " " not in t:
            if len(t) > len(best):
                best = t
    return best


def parse_top_card(card, vanity: str) -> dict:
    """Headline / company / location sit between the connection-degree badge
    and the "Contact info" link. That bracket is what the old positional <p>
    scan was missing — it walked straight into the degree badges."""
    raw = flight.texts(card)
    c = clean(raw, vanity)

    out = {}

    for i, t in enumerate(c):
        if t.startswith("View ") and t.endswith("verifications") and i:
            out["name"] = c[i - 1]
            break

    badge = [i for i, t in enumerate(c) if DEGREE_BADGE_RE.match(t)]
    if badge:
        start = badge[-1] + 1
        try:
            stop = c.index("Contact info", start)
        except ValueError:
            stop = min(start + 3, len(c))
        middle = c[start:stop]
        if middle:
            out["headline"] = middle[0]
        # After the headline come the current company then the location. We
        # do not emit the company, but we still have to count it: with three
        # entries the location is the third, with two it is the second.
        if len(middle) >= 3:
            out["location"] = middle[2]
        elif len(middle) == 2:
            out["location"] = middle[1]

    out["profile_photo_url"] = _best_image_url(raw, "profile-displayphoto")
    out["cover_photo_url"] = _best_image_url(raw, "profile-displaybackgroundimage")
    return out


def parse_about(card, vanity: str) -> str:
    c = clean(flight.texts(card), vanity)
    if c and c[0] == "About":
        c = c[1:]
    return "\n\n".join(c)


MEDIA_FILE_RE = re.compile(r"\.(?:pdf|docx?|pptx?|png|jpe?g|gif)$", re.I)
SKILL_SUMMARY_RE = re.compile(r"\+\d+\s+skills?$")


def _artifacts(raw) -> set:
    """Trailing affordances that render after a position's description: the
    media attachment, and the "see associated skills" row. The modal titles
    duplicate a "Skills for <title>" string, which gives us an exact match to
    drop rather than a guess."""
    return {t[len("Skills for ") :] for t in raw if t.startswith("Skills for ")}


def _is_artifact(s: str, known: set) -> bool:
    return s in known or bool(MEDIA_FILE_RE.search(s)) or bool(SKILL_SUMMARY_RE.search(s))


def _is_employment_type(s: str) -> bool:
    """A bare type ("Freelance") or a qualified one ("Permanent Full-time")."""
    if s in EMPLOYMENT_TYPES:
        return True
    head, _, tail = s.partition(" ")
    return head in EMPLOYMENT_QUALIFIERS and tail in EMPLOYMENT_TYPES


def _looks_like_location(s: str) -> bool:
    """Locations are short and comma-separated, often with a workplace-type
    suffix. Description bullets start with a marker, or run long. On the
    detail pages a bare workplace type ("Remote") can stand in for one."""
    if s.startswith(("•", "-", "–")) or len(s) > 90:
        return False
    return "," in s or " · " in s or s in WORKPLACE_TYPES


def parse_experience(entities, vanity: str) -> list:
    """Two lockup shapes. A single-role entity leads with the title and a
    "<company> · <type>" subtitle. A multi-role entity leads with the company
    and a total duration, then repeats title/type/dates per role beneath it —
    the sub-roles carry no stable key, so each date range starts a new one."""
    positions = []
    for entity in entities:
        raw = flight.texts(entity)
        c = clean(raw, vanity)
        known = _artifacts(raw)
        company_url = _first_url(raw, contains="linkedin.com/company/") or _first_url(
            raw, contains="linkedin.com/school/"
        )

        starts = [i for i, t in enumerate(c) if DATE_RANGE_RE.match(t)]
        if not starts:
            continue

        m = GROUP_HEADER_RE.match(c[1]) if len(c) > 1 else None
        grouped = bool(m)
        group_company = c[0] if grouped else ""
        group_type = (m.group("type") or "") if grouped else ""
        if group_type and not _is_employment_type(group_type):
            group_type = ""

        # Consume the group header: company, "<type> · <total duration>", then
        # any location lines. What follows is the first role's own lead, which
        # must not be mistaken for part of the header.
        header_end, group_location = 0, ""
        if grouped:
            header_end = 2
            while header_end < starts[0] and _looks_like_location(c[header_end]):
                group_location = group_location or c[header_end]
                header_end += 1

        # Title and employment type sit immediately above each date range.
        # Walk back over at most two lines, stopping at anything that is
        # clearly description or a trailing affordance. Resolve every role's
        # lead up front: a role's body runs to where the *next* role's lead
        # begins, not to the next date range, or it swallows that role's
        # title and type.
        leads = []
        for n, start in enumerate(starts):
            floor = starts[n - 1] if n else header_end - 1
            lead = []
            i = start - 1
            while i > floor and len(lead) < 2:
                s = c[i]
                if _is_artifact(s, known) or s.startswith(("•", "-", "–")) or len(s) > 120:
                    break
                lead.insert(0, s)
                i -= 1
            leads.append((i + 1, lead))

        for n, start in enumerate(starts):
            lead = leads[n][1]
            stop = leads[n + 1][0] if n + 1 < len(starts) else len(c)

            body = []
            for s in c[start + 1 : stop]:
                if _is_artifact(s, known):
                    break  # everything past the first affordance is chrome
                body.append(s)
            body = [s for s in body if s not in lead]

            p = Position(company=group_company, company_url=company_url,
                         employment_type=group_type)
            p.date_range = c[start]
            if "·" in p.date_range:
                p.date_range, _, p.duration = (
                    x.strip() for x in p.date_range.partition("·")
                )

            for x in lead:
                if _is_employment_type(x):
                    p.employment_type = x
                elif " · " in x and _is_employment_type(x.rsplit(" · ", 1)[1]):
                    p.company, p.employment_type = x.rsplit(" · ", 1)
                elif not p.title:
                    p.title = x
                elif not p.company:
                    p.company = x

            if body and _looks_like_location(body[0]):
                p.location = body[0]
                body = body[1:]
            p.location = p.location or group_location
            p.description = "\n".join(body).strip()
            positions.append(p)
    return positions


def parse_education(entities, vanity: str) -> list:
    out = []
    for entity in entities:
        raw = flight.texts(entity)
        c = clean(raw, vanity)
        e = Education(school_url=_first_url(raw, contains="linkedin.com/school/"))
        if c:
            e.school = c[0]
        rest = c[1:]
        years = [x for x in rest if YEAR_SPAN_RE.match(x)]
        if years:
            e.years = years[0]
            rest = rest[: rest.index(years[0])]
        if rest:
            e.degree = rest[0]
        out.append(e)
    return out


def parse_certifications(entities, vanity: str) -> list:
    out = []
    for entity in entities:
        raw = flight.texts(entity)
        c = clean(raw, vanity)
        cert = Certification(credential_url=_first_url(raw, contains="/safety/go/"))
        body = []
        for t in c:
            if t.startswith("Issued "):
                cert.issued = t
            elif t.startswith("Credential ID "):
                cert.credential_id = t[len("Credential ID ") :]
            else:
                body.append(t)
        if body:
            cert.name = body[0]
        if len(body) > 1:
            cert.issuer = body[1]
        out.append(cert)
    return out


def parse_simple_list(entities, vanity: str) -> list:
    """Skills and languages: one entity per item, name first."""
    out = []
    for entity in entities:
        c = clean(flight.texts(entity), vanity)
        if c:
            out.append(c[0])
    return out


# Cards whose parser needs the whole card, not its entity rows.
CARD_TEXT_HANDLERS = {
    "profile-card-about": ("about", parse_about),
}

# Cards parsed from their entity rows. The same parsers serve the
# /details/ pages, which supply the rows from a page-wide sweep instead.
CARD_HANDLERS = {
    "profile-card-experience": ("experience", parse_experience),
    "profile-card-education": ("education", parse_education),
    "profile-card-licenses-and-certifications": ("certifications", parse_certifications),
    "profile-card-skills": ("skills", parse_simple_list),
    "profile-card-languages": ("languages", parse_simple_list),
}


# -- driver ---------------------------------------------------------------


def ingest(p: Profile, tree, vanity: str) -> Profile:
    """Fold one decoded response tree into a Profile. Safe to call repeatedly —
    a card only ever overwrites a field when it actually produced a value, so
    an empty section can't wipe one that an earlier response filled."""
    for view_name, card in flight.cards(tree).items():
        if view_name == "profile-top-card":
            for k, v in parse_top_card(card, vanity).items():
                if v:
                    setattr(p, k, v)
        elif view_name in CARD_TEXT_HANDLERS:
            field, fn = CARD_TEXT_HANDLERS[view_name]
            value = fn(card, vanity)
            if value:
                setattr(p, field, value)
        elif view_name in CARD_HANDLERS:
            field, fn = CARD_HANDLERS[view_name]
            value = fn(flight.entities(card), vanity)
            if value:
                setattr(p, field, value)
    return p


# /details/<section>/ -> the Profile field it fills and the parser for its rows.
DETAIL_HANDLERS = {
    "experience": ("experience", parse_experience),
    "education": ("education", parse_education),
    "certifications": ("certifications", parse_certifications),
    "skills": ("skills", parse_simple_list),
    "languages": ("languages", parse_simple_list),
}


def ingest_detail(p: Profile, entities, section: str, vanity: str) -> int:
    """Fold the rows of a ``/details/<section>/`` list into a Profile,
    replacing whatever the truncated summary card had put there.

    Takes the entity rows rather than a tree because they may have been
    assembled from several responses: the page itself plus any pagination
    POSTs needed to load the list."""
    if section not in DETAIL_HANDLERS or not entities:
        return 0
    field, fn = DETAIL_HANDLERS[section]
    value = fn(entities, vanity)
    if value:
        setattr(p, field, value)
    return len(value)


def from_trees(trees, vanity: str) -> Profile:
    """Build a Profile from already-decoded trees (the live-fetch path)."""
    p = Profile()
    for tree in trees:
        ingest(p, tree, vanity)
    return p


def from_dump(dump_dir: pathlib.Path, vanity: str = "") -> Profile:
    """Build a Profile from a captured responses/<vanity>/<timestamp> dir."""
    vanity = vanity or dump_dir.parent.name
    p = Profile()

    for path in sorted(dump_dir.iterdir()):
        if path.suffix == ".html":
            tree, _ = flight.load_page_html(path.read_text(errors="replace"))
        elif path.suffix == ".txt":
            tree, _ = flight.load_stream(path.read_text(errors="replace"))
        else:
            continue
        ingest(p, tree, vanity)
    return p


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dump_dir", help="a responses/<vanity>/<timestamp> directory")
    ap.add_argument("--vanity", default="", help="override the slug inferred from path")
    args = ap.parse_args()

    d = pathlib.Path(args.dump_dir)
    if not d.is_dir():
        print(f"not a directory: {d}", file=sys.stderr)
        sys.exit(1)

    print(json.dumps(dataclasses.asdict(from_dump(d, args.vanity)), indent=2,
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
