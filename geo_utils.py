"""
Shared helpers for the radius-average feature: property-category
classification and OpenStreetMap Nominatim geocoding.

Category classification is a Bulgarian-keyword match against a listing's
title. Most portals already scope their search URL to apartments only
(alo.bg, homes.bg, imoti.bg), but imot.bg and olx.bg search "all real
estate for sale" and genuinely mix in houses/land/commercial - so every
portal runs the same classifier for consistency, rather than trusting
"apartments-only" portals to never contain a mislabeled listing.

Geocoding covers the 4 portals with no coordinates anywhere in their own
pages (static or JS-rendered) - homes.bg, olx.bg, imot.bg, imoti.bg,
confirmed by direct investigation (static HTML regex scan + a real headless
browser with cookie handling, WebGL enabled, and navigator.webdriver
patched, still found no map DOM node, no live google.maps.Map object, and
no maps iframe on either imot.bg or imoti.bg). Each of those 4 portals'
search is scoped to Sofia, and every listing carries a real neighborhood
name (the existing "area" field) - not a per-listing address, but real and
geocodable at neighborhood precision via OpenStreetMap's free Nominatim
API. Nominatim's usage policy caps free use at ~1 request/second and
expects heavy users to cache rather than re-request - and since Sofia has
only a few hundred distinct neighborhood names total (reused across
thousands of listings), caching by the query string itself (not per
listing) turns this into a one-time fixed cost rather than one geocode
call per listing: after the first run populates data/geocode_cache.json,
essentially every later call is a cache hit and costs nothing.

imoti.net, alo.bg, and bazar.bg need no geocoding at all - each embeds
real, listing-exact coordinates directly in its own listing page's HTML
(confirmed live): imoti.net as literal "latitude"/"longitude" JSON keys,
alo.bg as a plain <a href="https://maps.google.com/?q=LAT,LNG"> share
link, and bazar.bg as data-lat/data-long attributes on its #see_on_map
element - all present in the plain server-rendered HTML with no
JavaScript execution required, so a normal requests.get() picks them up.
"""

import gzip
import json
import math
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

CACHE_FILE = Path(__file__).parent / "data" / "geocode_cache.json"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
# Nominatim's usage policy requires a real identifying User-Agent (not a
# generic browser string) for the free public endpoint - see
# https://operations.osmfoundation.org/policies/nominatim/
GEOCODE_USER_AGENT = "bg-property-tracker/1.0 (personal deal-tracking tool, non-commercial)"
NOMINATIM_DELAY_SECONDS = 1.1

CATEGORY_KEYWORDS = {
    # Checked before "apartment" so e.g. "къща с 3 апартамента" (a house
    # subdivided into apartments) reads as a house, not an apartment.
    "land": ["парцел", "земеделска земя", "заведение за земя", "упи ", "имот за строеж", "терен"],
    "house": ["къща", "вила", "етаж от къща", "таунхаус"],
    "commercial": ["офис", "магазин", "склад", "хале", "заведение", "бизнес имот", "хотел", "ателие"],
    "apartment": [
        "апартамент", "едностаен", "двустаен", "тристаен", "четиристаен",
        "многостаен", "мезонет", "гарсониера", "стаен",
    ],
}


def classify_category(title):
    """Best-effort category from a Bulgarian listing title. Defaults to
    'apartment' when nothing matches, which is correct for every portal
    already scoped to an apartments-only search URL.

    KNOWN-BAD when called on a portal that ISN'T apartments-only: confirmed
    live for sales.bcpea.org (scraper_bcpea.py), whose auctions cover every
    property type - garages, farmland, production buildings, etc. - and
    whose own Bulgarian type words for those (e.g. "Гараж", "Земеделска
    земя") aren't all in CATEGORY_KEYWORDS below, so a majority of its own
    non-apartment listings silently default to "apartment" here. Never read
    a bcpea listing's raw "category" field expecting it to be accurate -
    type_filter_bucket() in sync_to_supabase.py (and typeFilterBucket() in
    index.html) already know this and use bcpea_type_match() against the
    title's own precise controlled vocabulary instead for that one portal;
    everything else in the app already goes through one of those two
    functions rather than raw category, so this is a dead-field trap for
    future code, not a live bug."""
    text = (title or "").lower()
    for category in ("land", "house", "commercial", "apartment"):
        if any(kw in text for kw in CATEGORY_KEYWORDS[category]):
            return category
    return "apartment"


# imoti.net embeds the listing's coordinates as plain JSON keys in the
# server-rendered detail page, e.g. "latitude":"42.72206626" - confirmed
# live via a plain (non-JS) HTTP fetch.
_IMOTI_NET_LAT_RE = re.compile(r'"latitude"\s*:\s*"?(-?\d{1,3}\.\d{3,10})"?')
_IMOTI_NET_LNG_RE = re.compile(r'"longitude"\s*:\s*"?(-?\d{1,3}\.\d{3,10})"?')


def extract_coords_imoti_net(html):
    lat_m = _IMOTI_NET_LAT_RE.search(html)
    lng_m = _IMOTI_NET_LNG_RE.search(html)
    if lat_m and lng_m:
        return {"lat": float(lat_m.group(1)), "lng": float(lng_m.group(1))}
    return None


# alo.bg embeds a plain "share this location" Google Maps link on the
# detail page, e.g. href="https://maps.google.com/?q=42.664,23.289&ll=...".
_ALO_MAPS_HREF_RE = re.compile(r"maps\.google\.com/\?q=(-?\d{1,3}\.\d{3,15}),(-?\d{1,3}\.\d{3,15})")


def extract_coords_alo(html):
    m = _ALO_MAPS_HREF_RE.search(html)
    if m:
        return {"lat": float(m.group(1)), "lng": float(m.group(2))}
    return None


# bazar.bg embeds the coordinates as data-lat/data-long attributes on its
# #see_on_map anchor, e.g. data-lat="42.698..." data-long="27.710...".
# Extracted as: find the whole tag, then find each attribute independently
# within it - not tied to a fixed attribute order or exact whitespace
# between them, since a first version requiring data-long to immediately
# follow data-lat undercounted real matches (~23% of a spot-checked sample
# vs. other portals' 88-100%) despite both attributes genuinely being
# present on the page.
_SEE_ON_MAP_TAG_RE = re.compile(r'<a\b[^>]*\bid="see_on_map"[^>]*>')
_DATA_LAT_RE = re.compile(r'data-lat="(-?\d{1,3}\.\d{3,15})"')
_DATA_LONG_RE = re.compile(r'data-long="(-?\d{1,3}\.\d{3,15})"')


def extract_coords_bazar(html):
    tag_match = _SEE_ON_MAP_TAG_RE.search(html)
    if not tag_match:
        return None
    tag = tag_match.group(0)
    lat_m = _DATA_LAT_RE.search(tag)
    lng_m = _DATA_LONG_RE.search(tag)
    if lat_m and lng_m:
        return {"lat": float(lat_m.group(1)), "lng": float(lng_m.group(1))}
    return None


# NOTE (backlog #9, 2026-09-23): `.obqva-block` was previously believed to
# hold alo.bg's real free-text description, but a later investigation
# sampling 200 real non-empty descriptions from data/leads_alo.json found
# 165/200 (82.5%) are literal substrings of that same listing's own `title`
# field - e.g. title "...Двустаен апартамент в к-с Суит хоум 2 Слънчев
# бряг, област Бургас" -> stored description "Двустаен апартамент в к-с
# Суит хоум 2". Re-sampling 300 records independently while fixing this
# reproduced the same shape at an even higher rate (265/300 = 88.3% exact
# substrings; nearly all of the remainder are still obvious near-duplicate
# title fragments, e.g. differing only by a trailing "!" or an emoji the
# title-truncation display cut off), never real seller-written prose. This
# is the same bug class as the already-fixed homes.bg case just above
# `extract_description_ldjson()`'s definition (see scraper_homes.py): a
# field that looks like a real description but is actually an echo of the
# title/heading blurb next to it.
#
# `.obqva-block` was very likely the wrong element - probably a heading/
# summary blurb rendered near the title (the site may reuse that CSS class
# for more than one unrelated box), not alo.bg's actual ad body. This
# couldn't be confirmed live at the time (alo.bg is blocked from this
# sandbox's network egress - both a plain HTTPS request and the WebFetch
# tool return a hard EGRESS_BLOCKED/403 for www.alo.bg), so a class-name
# fix was deferred pending live access and this function returned None
# unconditionally in the meantime (a title-echo is actively misleading - it
# looks like a real description, so a reader trusts it as one - so "no
# description available" was strictly better than a fake one).
#
# UPDATE (2026-09-24): network access is still blocked, but the user
# supplied real screenshots of a live detail page (alo_11319466,
# https://www.alo.bg/prodavam-atelie-v-zona-b-19-11319466). They show the
# real free-text ad body sitting under a fixed Bulgarian heading,
# "Допълнителна информация" ("Additional information") - a separate,
# clearly-labeled section, not the title/heading blurb `.obqva-block` was
# apparently grabbing. Rather than guess a new CSS class (unverifiable
# without live HTML, and exactly the mistake that caused the original bug),
# this targets the fixed heading TEXT instead: find the "Допълнителна
# информация" label wherever it sits in the DOM, then walk up from it
# looking for the first ancestor whose own text - once the heading itself
# is stripped off the front - is long enough to plausibly be real prose
# (see MIN_ALO_DESCRIPTION_LENGTH below). This is deliberately structure-
# agnostic: it works whether the heading and body are two sibling elements,
# or share one common wrapper (like `.obqva-block` might have), since
# either shape still has *some* ancestor of the heading whose text also
# contains the body. If the site's markup doesn't match this shape at all
# (heading missing, or every ancestor's text is still just the heading
# plus noise), this returns None rather than guess - never re-introduces
# the title-echo bug by falling back to some other unrelated element.
_ALO_DESC_HEADING_TEXT = "Допълнителна информация"
_ALO_DESC_PREFIX_RES = [
    re.compile(r"^Допълнителна информация\s*[:\-]?\s*"),
    re.compile(r"^За повече информация.*?в alo\.bg\.\s*"),
    re.compile(r"^Референтен номер:\s*\S+(?:\s+\S+)?\s*"),
    re.compile(r"^Отговорен брокер:\s*\S+(?:\s+\S+){0,1}\s*"),
]
# Trailing boilerplate that (per the screenshots) sits in the same visual
# card as the real description, immediately after it - a "write the first
# comment" prompt. Stripped from the end so it doesn't get concatenated
# onto the real ad text. ".*" with DOTALL so it also eats anything alo.bg
# renders after that prompt (e.g. a comment box placeholder) in the same
# container.
_ALO_DESC_TRAILING_RES = [
    re.compile(r"\s*Напиши(?:\s+първи)?\s+коментар.*$", re.DOTALL),
    re.compile(r"\s*Подобни обяви.*$", re.DOTALL),
    re.compile(r"\s*Контакт с подателя на обявата.*$", re.DOTALL),
]
# A real ad body is always at least a full short sentence - this rules out
# an ancestor whose text, after stripping the heading, is just leftover
# whitespace/punctuation noise (heading found but no real sibling/child
# content exists at all, e.g. an empty "Допълнителна информация" section)
# rather than genuine prose. Picked well below the shortest real samples
# seen in this project's other portals' ld+json descriptions (all >40
# chars) so this only rejects genuinely-empty matches, not just terse ones.
MIN_ALO_DESCRIPTION_LENGTH = 20
_ALO_ANCESTOR_SEARCH_LEVELS = 6


def _normalize_for_echo_compare(text):
    return re.sub(r"\s+", " ", text).strip().casefold()


def _looks_like_title_echo(text, title):
    # Production re-audit (2026-09-26) of the heading-based extractor above
    # found it still reproduces the exact bug it was meant to fix: sampling
    # 300 real non-empty descriptions produced by this function against
    # data/leads_alo.json, 233/300 (77.7%) are exact substrings of that same
    # listing's own grid-crawl title - down from the old `.obqva-block`
    # selector's 88.3%, but the same failure mode, not a different one. The
    # shared shape (extracted text is always a strict SUBSTRING of the
    # title, never unrelated to it) points at the ancestor walk climbing
    # past the real "Допълнителна информация" body and into a shared
    # container that also holds the page's own heading text - impossible to
    # confirm without live HTML (still blocked - see this function's other
    # comments), but confirmed wrong often enough in production that
    # propagating it is worse than returning None. This mirrors, rather than
    # replaces, the ancestor-walk's own MIN_ALO_DESCRIPTION_LENGTH guard: an
    # extra acceptance check using a signal (the listing's own known title)
    # this function didn't previously have, not a new guessed selector.
    if not title:
        return False
    norm_text = _normalize_for_echo_compare(text)
    norm_title = _normalize_for_echo_compare(title)
    return bool(norm_text) and norm_text in norm_title


def extract_description_alo(html, title=None):
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return None

    # Find the heading as a plain text node (not a tag lookup) - a tag
    # lookup by exact get_text() equality would also match every ancestor
    # that happens to contain ONLY that heading (e.g. a wrapper <div> around
    # a single <strong>Допълнителна информация</strong>), which is
    # ambiguous about which level is "the" heading element. A NavigableString
    # match has no such nesting ambiguity - there's exactly one text node
    # holding the label, whatever tag(s) wrap it.
    label_node = soup.find(string=re.compile(r"^\s*" + re.escape(_ALO_DESC_HEADING_TEXT) + r"\s*$"))
    if label_node is None or label_node.parent is None:
        return None

    node = label_node.parent
    for _ in range(_ALO_ANCESTOR_SEARCH_LEVELS):
        if node is None:
            break
        text = node.get_text(" ", strip=True)
        for pattern in _ALO_DESC_PREFIX_RES:
            text = pattern.sub("", text, count=1)
        for pattern in _ALO_DESC_TRAILING_RES:
            text = pattern.sub("", text)
        text = text.strip()
        if len(text) >= MIN_ALO_DESCRIPTION_LENGTH and not _looks_like_title_echo(text, title):
            return text
        node = node.parent

    # Heading found, but no ancestor within the search depth had enough
    # real content after stripping it (or every candidate was a title-echo
    # rejected by _looks_like_title_echo above) - a structural mismatch
    # (e.g. the site changed this section's markup), not a genuine empty
    # section. Returning None here is the same defensive choice as
    # everywhere else in this function: never guess, never fall back to
    # some other element.
    return None


# bazar.bg and olx.bg both embed the listing's real, agent/seller-written
# description as the "description" key of a <script type="application/
# ld+json"> block on the detail page (confirmed live via
# probe_descriptions.py) - takes the first non-empty one found, since a
# page can carry more than one ld+json block (e.g. bazar.bg also has an
# Organization block with no description at all).
def extract_description_ldjson(html):
    soup = BeautifulSoup(html, "html.parser")
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        blobs = data if isinstance(data, list) else [data]
        for blob in blobs:
            if isinstance(blob, dict) and blob.get("description"):
                return blob["description"].strip()
    return None


# imot.bg's real free-text description sits in a <div class="moreInfo">,
# prefixed with the fixed Bulgarian label "Описание на имота:" ("Property
# description:") - confirmed live via probe_descriptions.py (a Playwright
# fetch; imot.bg blocks plain requests-based fetching, same as its grid
# pages - see scraper_imot.py's module docstring).
_IMOT_BG_DESC_PREFIX_RE = re.compile(r"^Описание на имота:\s*")


def extract_description_imot(html):
    soup = BeautifulSoup(html, "html.parser")
    node = soup.find("div", class_="moreInfo")
    if not node:
        return None
    text = _IMOT_BG_DESC_PREFIX_RE.sub("", node.get_text(" ", strip=True)).strip()
    return text or None


# imot.bg's detail page embeds every gallery photo twice - once under
# ".../<dir>//big1/..." (double slash) and once under ".../<dir>/big1/..."
# (single slash), both resolving to the same image - confirmed live via
# probe_photos.py. Normalizing the double slash before deduping is what
# turns that raw list into the real, distinct photo set.
_IMOT_PHOTO_RE = re.compile(r'https://cdn3\.focus\.bg/imot/photosimotbg/[^\s"\'<>]+?\.jpg', re.IGNORECASE)


def extract_photos_imot(html):
    seen = []
    for url in _IMOT_PHOTO_RE.findall(html):
        normalized = url.replace("//big1/", "/big1/")
        if normalized not in seen:
            seen.append(normalized)
    return seen


# homes.bg detail-page description extraction - docs/backlog.md item 9d.
#
# homes.bg's own detail pages (offer["viewHref"]) have never been fetched or
# inspected by this codebase before this - this sandbox's egress to
# www.homes.bg is fully blocked (confirmed via both curl and WebFetch against
# several hosts, see item 9d's own investigation log), so none of the three
# tiers below could be verified against a real live page. Each was chosen
# because it's a technique already PROVEN to work on at least one other
# portal in this exact codebase (not a fresh guess pulled from nothing), and
# every one degrades to None on a markup mismatch rather than falling back to
# some other, unrelated element - same defensive standard as
# extract_description_alo()'s own fix:
#   1. A labeled HTML block keyed on the heading TEXT "Описание" ("
#      Description") - not a guessed CSS class/id, since homes.bg's own
#      classes/ids are completely unknown. "Описание" (or "Описание на
#      имота:") is the confirmed real label text this codebase has already
#      found on two OTHER Bulgarian real-estate portals for the exact same
#      purpose (imot.bg's <div class="moreInfo">, see
#      extract_description_imot() above; sales.bcpea.org's own
#      .label__group/.info block, see scraper_bcpea.py's label_info() call) -
#      real cross-portal evidence this is a common convention on this
#      category of site, not an arbitrary guess. Structure-agnostic ancestor
#      walk, same shape as extract_description_alo(): finds the heading as a
#      plain text node, then walks up looking for the first ancestor whose
#      own text (heading stripped off) is long enough to plausibly be real
#      prose.
#   2. <meta name="description">/<meta property="og:description"> - the
#      exact same proven pattern already used by scraper_imoti_bg.py's own
#      fetch_listing_detail() (94%+ live hit rate there).
#   3. application/ld+json "description" - extract_description_ldjson()
#      above, already proven live on olx.bg/bazar.bg.
# Deliberately NOT attempted: guessing at a different key inside the detail
# page's own window.__PRELOADED_STATE__ blob (item 9d's spec flags this as
# worth checking first, since it might mean no HTML parsing is needed at
# all). Doing that without live access would mean picking some field name
# (or the longest Cyrillic string anywhere in a large, deeply-nested JSON
# blob of unknown shape) out of pure speculation - real risk of confidently
# extracting the WRONG structured field (an address, an agent bio, an SEO
# blurb) as if it were the listing's own description, which is worse than
# finding nothing. This is the same "don't guess a selector that can't be
# verified live" line already drawn for imoti.net's untried Bulgarian-locale
# page and alo.bg's original selector search - left as a genuinely open
# follow-up for whoever next has live homes.bg access, not attempted here.
#
# Every candidate from every tier is rejected if it's shorter than
# MIN_HOMES_DESCRIPTION_LENGTH (comfortably above the known construction-tag
# lines' length - e.g. "Тухла/Бетон, Полуобзаведен" is 26 chars - so this
# also guards against the SAME wrong-field text PR #217 already fixed
# resurfacing from a different tier) or reads as a title echo (the same
# _looks_like_title_echo() guard alo.bg's own fix uses - homes.bg has no
# confirmed-different failure mode to expect instead, and a title echo is
# the single most common way a "looks real but isn't" description bug has
# shown up across this codebase's other portals).
# Anchored at both ends - matches ONLY a text node that is exactly the
# heading itself (whatever whitespace surrounds it), the same way
# extract_description_alo() locates its own heading.
_HOMES_DESC_HEADING_EXACT_RE = re.compile(r"^\s*Описание(?:\s+на\s+имота)?\s*[:\-]?\s*$")
# Prefix-only (no trailing "$") - strips just the leading heading text off
# the front of an ancestor's own flattened text, which also contains the
# real body after it.
_HOMES_DESC_HEADING_PREFIX_RE = re.compile(r"^\s*Описание(?:\s+на\s+имота)?\s*[:\-]?\s*")
MIN_HOMES_DESCRIPTION_LENGTH = 30
_HOMES_DESC_ANCESTOR_SEARCH_LEVELS = 6


def _homes_label_description(html, title=None):
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return None

    label_node = soup.find(string=_HOMES_DESC_HEADING_EXACT_RE)
    if label_node is None or label_node.parent is None:
        return None

    node = label_node.parent
    for _ in range(_HOMES_DESC_ANCESTOR_SEARCH_LEVELS):
        if node is None:
            break
        raw_text = node.get_text(" ", strip=True)
        stripped = _HOMES_DESC_HEADING_PREFIX_RE.sub("", raw_text, count=1).strip()
        # Only accept an ancestor whose text actually STARTS with the
        # heading - i.e. the prefix regex really matched something, not a
        # no-op on text that happens to already be long enough. Without
        # this, an ancestor broad enough to also contain unrelated content
        # BEFORE the heading (e.g. the page's own <h1> title, a specs
        # table) would have the heading buried in the middle of `raw_text`
        # instead of stripped off the front, and this would wrongly return
        # that whole mixed blob - title text and all - as if it were the
        # real description. Safer to keep climbing (and ultimately return
        # None) than risk that.
        if stripped == raw_text:
            node = node.parent
            continue
        if len(stripped) >= MIN_HOMES_DESCRIPTION_LENGTH and not _looks_like_title_echo(stripped, title):
            return stripped
        node = node.parent
    return None


def _homes_meta_description(html):
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return None
    meta = soup.find("meta", attrs={"name": "description"}) or soup.find(
        "meta", attrs={"property": "og:description"}
    )
    if not meta or not meta.get("content"):
        return None
    content = meta["content"].strip()
    return content or None


def extract_description_homes(html, title=None):
    """Best-effort real free-text description for a homes.bg detail page -
    see the module comment above for the full 3-tier search order and why
    each tier was chosen. Never raises; returns None (not a guess) when no
    tier finds a candidate that clears MIN_HOMES_DESCRIPTION_LENGTH and
    isn't a title echo."""
    for candidate in (
        _homes_label_description(html, title=title),
        _homes_meta_description(html),
        extract_description_ldjson(html),
    ):
        if not candidate:
            continue
        text = candidate.strip()
        if len(text) >= MIN_HOMES_DESCRIPTION_LENGTH and not _looks_like_title_echo(text, title):
            return text
    return None


# bazar.bg and olx.bg both embed the listing's full photo gallery as the
# "image" key of the same <script type="application/ld+json"> block
# extract_description_ldjson() already reads "description" from -
# confirmed live via probe_photos.py for bazar.bg (17 photos in one
# listing's "image" array); olx.bg couldn't be directly probed (blocked
# by the same edge check its own scraper already routes around via
# Playwright - see backfill_detail_olx.py) but shares the same ld+json
# "description" shape, so worth trying the same key there too - returns
# an empty list harmlessly if olx.bg's own blob has no "image" key.
def extract_photos_ldjson(html):
    soup = BeautifulSoup(html, "html.parser")
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        blobs = data if isinstance(data, list) else [data]
        for blob in blobs:
            if not isinstance(blob, dict) or not blob.get("image"):
                continue
            image = blob["image"]
            return image if isinstance(image, list) else [image]
    return []


# 2026-09-24 (docs/backlog.md - olx.bg spec-field extension): the same
# ld+json blob extract_description_ldjson()/extract_photos_ldjson() already
# read "description"/"image" from was checked for other Schema.org fields
# useful to this project's spec panel (property_type/construction_type/
# built_year/completion_status/floor_number/floor_qualifier/features/
# has_elevator/furnished/has_central_heating - see extract_specs_alo()),
# agency contact (agency_name/agency_website - see extract_contact_alo()),
# and coordinates. This sandbox's network egress to olx.bg is blocked (same
# as when extract_photos_ldjson() above was written, confirmed again live
# via WebFetch while doing this work) and no raw olx.bg detail-page HTML is
# saved anywhere in this repo (no probe_*.py output, no test fixture) to
# inspect instead, so none of that could be verified - and, unlike
# "description"/"image" (obviously listing-specific, only one plausible
# meaning), none of the remaining fields have a genuinely safe bet:
#   - Bulgarian-specific specs (property type, construction type, floor,
#     elevator/furnished/heating) have no standard Schema.org property at
#     all - representing them would need guessing site-specific
#     "additionalProperty" label strings with zero evidence any exist,
#     exactly the kind of fabricated selector this project's standing rule
#     (see extract_contact_alo()'s own comment on why phone numbers are
#     never guessed) forbids.
#   - Coordinates are NOT a "couldn't check" gap - this file's own module
#     docstring above already documents a direct prior investigation
#     (static HTML regex scan + a real headless browser checking for a map
#     DOM node/live google.maps object/iframe) that found olx.bg carries no
#     coordinates anywhere on its own pages at all, which is exactly why
#     Geocoder/Nominatim exists for this portal in the first place. Adding
#     a speculative geo/latitude/longitude ld+json reader here would
#     contradict that confirmed finding, not extend it.
#   - seller/author (for agency_name/agency_website) IS plausible in
#     principle, but genuinely ambiguous in a way "image" wasn't: Schema.org
#     "author"/top-level "seller" on a listing can just as easily name the
#     PUBLISHER (OLX Group / olx.bg itself) as the actual poster, and
#     getting that wrong wouldn't fail harmlessly the way an absent "image"
#     key would - it would silently write a wrong, misleading "agency" onto
#     real listings at nationwide scale. Not implemented without a real
#     sample to check which one it actually is.
#
# floorSize is the one field kept: a genuinely unambiguous, single-meaning
# standard Schema.org property (a QuantitativeValue holding the listing's
# own floor area) - same "worth trying, one more optional key on the same
# already-proven-real blob, degrades to None with zero downside if absent"
# reasoning extract_photos_ldjson() itself was written on above, before its
# own "image" key was confirmed working (see that function's comment) via
# real production data in data/leads_olx.json. The caller (scraper_olx.py's
# fetch_listing_detail()) only uses this as a GAP-FILLER, never overwriting
# an sqm the grid crawl's own SQM_RE already found - same restraint
# scraper_alo.py already applies to extract_specs_alo()'s own sqm result,
# since there's no live confirmation this ld+json value is actually more
# reliable than what the grid already provides.
def extract_sqm_ldjson(html):
    soup = BeautifulSoup(html, "html.parser")
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        blobs = data if isinstance(data, list) else [data]
        for blob in blobs:
            if not isinstance(blob, dict):
                continue
            size = blob.get("floorSize")
            if not isinstance(size, dict) or size.get("value") is None:
                continue
            try:
                sqm = round(float(str(size["value"]).replace(",", ".")))
            except (TypeError, ValueError):
                continue
            if sqm > 0:
                return sqm
    return None


# imoti.net's detail page embeds the gallery as separate numbered files -
# "main_image/thumb_<size>_wm_main_image_<id>_1.jpg" for the cover photo,
# then "images/thumb_<size>_wm_images_<id>_<n>.jpg" for the rest - each at
# more than one size variant. Confirmed live via probe_photos.py (7 total
# URLs across 2 size variants for a 5-photo gallery + cover). Keeps only
# the 1200x630 variant (present for every photo, unlike the smaller
# 620x349 one which was only seen for the cover) and dedupes by photo
# number so each real photo appears once.
_IMOTI_NET_PHOTO_RE = re.compile(
    r'https://www\.imoti\.net/web/files/obiavi/\d+/(main_image|images)/'
    r'thumb_1200x630_wm_(?:main_image|images)_(\d+)_(\d+)\.jpg'
)


def extract_photos_imoti_net(html):
    by_index = {}
    for kind, listing_id, n in _IMOTI_NET_PHOTO_RE.findall(html):
        key = (0, int(n)) if kind == "main_image" else (1, int(n))
        by_index[key] = (
            f"https://www.imoti.net/web/files/obiavi/{listing_id}/{kind}/"
            f"thumb_1200x630_wm_{kind}_{listing_id}_{n}.jpg"
        )
    return [by_index[key] for key in sorted(by_index)]


# alo.bg's detail page lists every gallery photo as an <a class="fancyimages"
# data-type="image" href="user_files/.../<n>_big.jpg"> - a relative URL, and
# the reason a plain URL-regex scan missed them all (per this project's
# history: a diagnostic probe script, referenced only by name in an earlier
# version of this comment, was never actually committed to this repo - no
# probe output, workflow, or captured HTML fixture survives anywhere in git
# history or in tests/ - so its "14 anchors" claim could not be
# independently re-verified this session).
#
# 2026-09-24 fix (see docs/missy-findings and this change's own commit for
# the investigation): a production sample of 29,792 real, independently-
# varied alo.bg listings that the CURRENT extractor has actually run
# against - i.e. every listing marked `_photos_checked: True` - had a 0.0%
# photos hit rate. That is not plausible as "alo.bg listings genuinely never
# have a gallery"; real-estate listings on a major portal virtually always
# carry more than one photo, and this project's OWN grid-crawl already
# proves single cover photos are routinely present (see `photo` in
# fetch_listings_page()). The root cause: the regex below required the
# anchor's `class`, `data-type`, and `href` attributes to appear in that
# EXACT order, with double quotes specifically, inside the same `<a ...>`
# tag - real HTML attribute order/quoting is not guaranteed to match
# whatever order was hand-transcribed into a regex, and unlike every other
# extractor in this file (extract_description_alo/extract_specs_alo/
# extract_contact_alo, all BeautifulSoup-based), this was the only one that
# parsed raw HTML text as a fixed-order string pattern instead of an actual
# parsed DOM - and it also shipped with zero test coverage (unlike its three
# siblings in tests/test_alo_detail_extraction.py), so this brittleness was
# never caught. Rewritten below to look up the anchor via BeautifulSoup
# instead - order/quote-independent by construction, exactly like every
# other alo.bg extractor already is. This still requires the same two
# identifying signals (a `fancyimages` class token AND `data-type="image"`)
# with no loosening of what counts as a gallery photo, only of the
# structural assumption (attribute order) that was demonstrably too narrow.
#
# One extra non-photo anchor with data-type="ajax" (a "more on Google"
# panel, per the original comment) is still excluded by requiring
# data-type="image".
_ALO_GALLERY_ANCHOR_CLASS_RE = re.compile(r"\bfancyimages\b", re.IGNORECASE)


def extract_photos_alo(html):
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return []

    seen = []
    for a in soup.find_all("a", attrs={"data-type": "image"}):
        classes = a.get("class") or []
        # `class` is a list of tokens once BeautifulSoup parses it, so this
        # is deliberately checked per-token (order-independent) rather than
        # re-joining and regexing the whole attribute string, which would
        # reintroduce the same kind of exact-string assumption this fix is
        # replacing.
        if not any(_ALO_GALLERY_ANCHOR_CLASS_RE.search(c) for c in classes):
            continue
        href = a.get("href")
        if not href:
            continue
        href = href.strip()
        # Same relative-URL joining alo.bg's own grid-crawl photo already
        # needs (see fetch_listings_page()'s img_url handling in
        # scraper_alo.py) - handles a protocol-relative "//" URL and a
        # leading "/" without producing a "https://www.alo.bg//..."
        # double-slash, which a bare f"https://www.alo.bg/{href}" (the
        # previous join here) would have for any href starting with "/".
        if href.startswith("//"):
            url = "https:" + href
        elif href.startswith("http"):
            url = href
        else:
            url = "https://www.alo.bg/" + href.lstrip("/")
        if url not in seen:
            seen.append(url)
    return seen


# alo.bg's structured spec table (confirmed via real user-supplied
# screenshots of a live detail page, alo_11319466 - see extract_description_
# alo()'s own comment for why: this sandbox's network egress to alo.bg is
# blocked, so this is built from a real rendered page image, not a live
# HTML probe) is a fixed sequence of Bulgarian label/value rows:
# Местоположение (location - not extracted here, already covered by area/
# city/lat/lng elsewhere), Вид на имота (property type), Квадратура
# (size), Вид строителство (construction type - no "на", confirmed against
# the real screenshot and corroborated by the same phrasing already seen in
# scraped bazar.bg/olx.bg description text elsewhere in this codebase),
# Година на строителство
# (built year), Степен на завършеност (completion status), Номер на етажа
# (floor number), Етаж (floor qualifier - e.g. "Непоследен"/"Последен"/
# "Партер"), and Особености (feature checkboxes - the screenshot shows
# only the CHECKED features rendered as visible tags at all, e.g.
# "Асансьор"/"Необзаведен"/"ТЕЦ" - no visible "unchecked" state to parse).
#
# Same reasoning as extract_description_alo(): rather than guess this
# table's CSS classes/tag names (unverifiable without live HTML, and
# exactly the mistake that produced the `.obqva-block` bug), this locates
# each row by its fixed Bulgarian LABEL text and reads whatever text
# follows it, using BeautifulSoup's get_text("\n", strip=True) to flatten
# the page into one line per underlying text node. That works regardless
# of whether a row is a <tr><td> pair, a <dl><dt>/<dd> pair, or a pair of
# <div>s - in every one of those shapes the label and its value are still
# two separate text nodes next to each other. A value spanning more than
# one text node (e.g. "1980 г." plus a separate, dimmer "(годината може да
# е ориентировъчна)" hint span - both visible in the screenshot as one
# line of rendered text) is handled by joining lines until the next known
# label or a stop marker is reached, not just taking a single next line.
#
# Defensive by construction: a row whose label isn't found is simply
# absent from the result (never guessed), and if NO known label is found
# at all this returns None outright - a structural page change should
# shrink what gets extracted, never produce garbage under a
# plausible-looking key.
#
# 2026-09-24 investigation (see this change's own commit): production data
# shows only 3.9% of the 29,792 listings the current extractor has actually
# run against have any spec populated at all - much lower than a real
# structured spec table plausibly explains on its own (this project has no
# live evidence either way - the screenshot this was built from can't show
# what fraction of real listings fill in the table). One assumption in the
# label-matching below WAS provably too narrow regardless of that base
# rate, and is fixed here: `line == label` requires the label to be its own
# ENTIRE, isolated text node before it'll even look for a value - a
# screenshot can show "Label: Value" rendered as one visual row but cannot
# show whether that row is really two separate DOM text nodes (what every
# test fixture here assumed, since that's the only shape anyone could
# write a fixture for) or one flattened "Label: Value" text node (get_text
# would then produce a single line, e.g. "Вид на имота: Тристаен
# апартамент", which the old exact-equality check could never match at
# all). _alo_label_match() below now also accepts that second shape -
# still gated on the literal Bulgarian label text, never guessed, just no
# longer assuming which of the two DOM shapes carries it.
_ALO_SPEC_LABELS = [
    ("Вид на имота", "property_type_raw"),
    ("Квадратура", "_sqm_raw"),
    ("Вид строителство", "construction_type"),
    ("Година на строителство", "_built_year_raw"),
    ("Степен на завършеност", "completion_status"),
    ("Номер на етажа", "_floor_number_raw"),
    ("Етаж", "floor_qualifier"),
    ("Особености", "_features_raw"),
]
_ALO_SPEC_ALL_LABELS = frozenset(["Местоположение"] + [label for label, _ in _ALO_SPEC_LABELS])
# Lines that mark the end of the spec table (or of any one row's value) -
# whatever text alo.bg renders right after the table in the screenshots
# ("Актуализирана вчера. Валидна още 51 дни."), plus the later sections
# further down the same page that must never bleed into a spec value if a
# row's own value happens to be missing/empty on some listing.
_ALO_SPEC_STOP_PREFIXES = (
    "Актуализирана", "Публикувана", "Контакт с подателя", "Допълнителна информация",
    "Подобни обяви", "Обява №", "Обява от", "Цена",
)
# Guards against an unbounded join if stop-marker detection above somehow
# fails to fire (e.g. a genuinely new section label this list doesn't
# know about yet) - no real spec value in the screenshots spans more than
# one or two underlying text nodes.
_ALO_SPEC_MAX_VALUE_LINES = 4

_ALO_SQM_VALUE_RE = re.compile(r"(\d+(?:[.,]\d+)?)")
_ALO_YEAR_VALUE_RE = re.compile(r"(\d{4})")
_ALO_FLOOR_NUMBER_VALUE_RE = re.compile(r"(\d+)")
_ALO_FEATURE_ELEVATOR_RE = re.compile(r"асансьор", re.IGNORECASE)
_ALO_FEATURE_UNFURNISHED_RE = re.compile(r"необзаведен", re.IGNORECASE)
_ALO_FEATURE_FURNISHED_RE = re.compile(r"(?<!не)обзаведен", re.IGNORECASE)
_ALO_FEATURE_HEATING_RE = re.compile(r"\bтец\b", re.IGNORECASE)


def _alo_spec_value(lines, start_idx):
    """Joins the lines right after a label's own line, up to the next
    known label or a stop marker (or a hard cap) - the same defensive
    "collect until something else recognizable starts" shape used
    elsewhere in this project (e.g. smallest_container_with_price() in
    scraper_alo.py). Returns "" if the very next line is itself another
    known label/stop marker - i.e. this row's value is genuinely absent on
    this listing (not every alo.bg listing carries every field), not a
    parsing failure."""
    collected = []
    i = start_idx
    while i < len(lines) and len(collected) < _ALO_SPEC_MAX_VALUE_LINES:
        line = lines[i]
        if line in _ALO_SPEC_ALL_LABELS:
            break
        if any(line.startswith(p) for p in _ALO_SPEC_STOP_PREFIXES):
            break
        collected.append(line)
        i += 1
    return " ".join(collected).strip()


# Separators that could plausibly join a label to an inline value on the
# same flattened text node ("Вид на имота: Тристаен апартамент") - a colon
# or dash (incl. the two common Cyrillic-text dash characters), optionally
# surrounded by whitespace. The character right after the label must be one
# of these (not just "any character") specifically so a longer/differently
# -inflected Bulgarian label that happens to start with the same letters
# (e.g. a hypothetical "Етажа" row) is never mistaken for a match on the
# shorter "Етаж" label - see _ALO_SPEC_LABELS' own comment.
_ALO_SPEC_INLINE_SEPARATOR_CHARS = ":-–— \t"


def _alo_label_match(line, label):
    """Returns "" if `line` is exactly `label` alone (the original, already
    -tested "label is its own text node" shape - caller then reads the
    value from subsequent lines via _alo_spec_value()); the inline value
    text if `line` is `label` immediately followed by a separator and more
    text on the SAME line (the "flattened Label: Value" shape - see
    _ALO_SPEC_LABELS' own comment); or None if `line` doesn't match `label`
    at all."""
    if line == label:
        return ""
    if line.startswith(label):
        rest = line[len(label):]
        if rest and rest[0] in _ALO_SPEC_INLINE_SEPARATOR_CHARS:
            value = rest.lstrip(_ALO_SPEC_INLINE_SEPARATOR_CHARS).strip()
            if value:
                return value
    return None


def extract_specs_alo(html):
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return None
    text = soup.get_text("\n", strip=True)
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]

    raw = {}
    for i, line in enumerate(lines):
        for label, key in _ALO_SPEC_LABELS:
            if key in raw:
                continue
            inline_value = _alo_label_match(line, label)
            if inline_value is None:
                continue
            value = inline_value if inline_value else _alo_spec_value(lines, i + 1)
            if value:
                raw[key] = value
            break

    if not raw:
        return None

    specs = {}
    for key in ("property_type_raw", "construction_type", "completion_status", "floor_qualifier"):
        if raw.get(key):
            specs[key] = raw[key]

    if raw.get("_sqm_raw"):
        m = _ALO_SQM_VALUE_RE.search(raw["_sqm_raw"])
        if m:
            try:
                specs["sqm"] = round(float(m.group(1).replace(",", ".")))
            except ValueError:
                pass

    if raw.get("_built_year_raw"):
        m = _ALO_YEAR_VALUE_RE.search(raw["_built_year_raw"])
        if m:
            year = int(m.group(1))
            # Sanity bound - a matched 4-digit number that isn't plausibly
            # a construction year (e.g. accidentally grabbed from some
            # other nearby number) shouldn't be stored as one.
            if 1800 <= year <= 2100:
                specs["built_year"] = year

    if raw.get("_floor_number_raw"):
        m = _ALO_FLOOR_NUMBER_VALUE_RE.search(raw["_floor_number_raw"])
        if m:
            specs["floor_number"] = int(m.group(1))

    if raw.get("_features_raw"):
        # Each collected line under "Особености" is one checked feature
        # tag (the screenshots show only checked features rendered at
        # all) - _alo_spec_value() already joined them with " ", so split
        # back out. A feature label is a short single word or hyphenated
        # phrase in every real sample seen ("Асансьор", "Необзаведен",
        # "ТЕЦ"), so splitting on whitespace is safe here even though it
        # would be wrong for genuinely multi-word values.
        features = [f for f in raw["_features_raw"].split(" ") if f]
        if features:
            specs["features"] = features
            feature_text = " ".join(features)
            if _ALO_FEATURE_ELEVATOR_RE.search(feature_text):
                specs["has_elevator"] = True
            if _ALO_FEATURE_UNFURNISHED_RE.search(feature_text):
                specs["furnished"] = False
            elif _ALO_FEATURE_FURNISHED_RE.search(feature_text):
                specs["furnished"] = True
            if _ALO_FEATURE_HEATING_RE.search(feature_text):
                specs["has_central_heating"] = True

    return specs or None


# alo.bg's "Контакт с подателя на обявата" ("Contact the poster") box
# (confirmed via the same real screenshots as extract_description_alo() -
# see that function's comment for the network-access caveat) shows the
# poster's display name/agency name as plain text, followed by one or more
# links: an alo.bg-hosted storefront page for that poster (e.g.
# "endrevahouses.alo.bg") and, separately, the agency's own real external
# website (e.g. "https://endreva-houses.com"). Only the real external site
# is extracted as agency_website - the alo.bg-hosted one is just an
# internal profile page on this same portal, not independently useful
# contact info.
#
# The box also shows a partially-masked phone number ("08X XXX XXXX")
# behind a green "Виж" ("View") button. This is DELIBERATELY NOT extracted
# here. The masked format plus a click-to-reveal button is the standard
# shape of a JS/AJAX-revealed number - the real digits are normally
# fetched from the server only once the button is actually clicked,
# specifically so a plain page fetch can't harvest them (a common anti-
# scraping/lead-tracking pattern on Bulgarian classifieds sites) - rather
# than a value merely hidden by CSS while already sitting in the raw
# HTML/DOM. This project's network access to alo.bg is blocked in this
# sandbox (see extract_description_alo()'s own comment), so which of those
# two this actually is could not be confirmed by inspecting a real masked
# page's raw HTML. Per this project's own standing rule - never fabricate
# a working extraction for something that can't be verified - no phone
# extraction is implemented here rather than guess one that might return
# nothing (or garbage) against the real page. If a future contributor gets
# live access, the concrete thing to check is whether the full number (or
# a `data-phone`/similar attribute holding it) is already present
# somewhere in the raw HTML/a same-page <script> block for a masked
# listing; if so, add a real extractor. If the number is genuinely only
# returned by a follow-up XHR after the click, it can't be obtained from a
# plain page fetch at all, and this gap is a portal limitation, not a
# fixable scraper bug.
_ALO_CONTACT_HEADING_TEXT = "Контакт с подателя на обявата"
_ALO_CONTACT_SKIP_LINES = frozenset([
    _ALO_CONTACT_HEADING_TEXT, "Изпрати съобщение",
    "Вход в сайта", "Регистрация", "Виж", "Вижте",
])
_ALO_CONTACT_ANCESTOR_SEARCH_LEVELS = 6


def _alo_contact_from_container(container):
    """Extracts agency_name/agency_website from a single candidate
    container - the poster-name heuristic and the real-external-website
    link scan, unchanged from the original implementation (see
    extract_contact_alo()'s own comment for the reasoning behind each
    line-classification rule). Returns None if this container yields
    neither."""
    contact = {}

    website = None
    for a in container.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("mailto:", "tel:", "#")):
            continue
        if "alo.bg" in href.lower():
            # The poster's own storefront page on this portal, not their
            # real external site - see this function's own comment.
            continue
        website = href if href.startswith("http") else f"https://{href.lstrip('/')}"
        break
    if website:
        contact["agency_website"] = website

    lines = [ln.strip() for ln in container.get_text("\n", strip=True).split("\n") if ln.strip()]
    for line in lines:
        if line in _ALO_CONTACT_SKIP_LINES:
            continue
        if re.match(r"^\d", line):
            # Phone-number-shaped (or otherwise numeric) line, e.g. the
            # masked "08X XXX XXXX" - never the poster's name.
            continue
        if "." in line and " " not in line:
            # A bare domain/URL rendered as visible text (e.g.
            # "endrevahouses.alo.bg") rather than inside a proper href -
            # not a name either.
            continue
        contact["agency_name"] = line
        break

    return contact or None


def extract_contact_alo(html):
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return None

    label_node = soup.find(string=re.compile(r"^\s*" + re.escape(_ALO_CONTACT_HEADING_TEXT) + r"\s*$"))
    if label_node is None or label_node.parent is None:
        return None

    # Walk up from the heading, trying extraction at each ancestor level and
    # stopping at the first one that actually yields a name and/or website -
    # the heading itself is normally just a bare label with no useful
    # content of its own, so this expands outward to whatever box wraps the
    # poster's name/website/phone-reveal button together, without assuming
    # a fixed nesting depth or class name.
    #
    # 2026-09-24 (see this change's own commit): this used to require the
    # candidate container to already contain a real `<a href>` before even
    # trying to read a name from it - reasonable for finding a real AGENCY's
    # box (which does carry an alo.bg storefront link), but this field's own
    # comment above always described it as extracting the poster's name in
    # general, agency or not. A private individual's contact box plausibly
    # has NO real `<a href>` at all (a phone-reveal control and a "send
    # message" control are both very plausibly `<button>`s, not links) - the
    # href-gated container search would then never even look at that box's
    # text, silently dropping the poster's name too, not just the (genuinely
    # absent) website. Production data backs this: only 3.7% of listings
    # this extractor has actually run against have any agency_name at all -
    # implausibly low for "the poster's own display name", which alo.bg's
    # contact box shows for every listing, agency or private. Trying
    # extraction at every level (name-or-website, no link required to even
    # look) and keeping the first productive one removes that unjustified
    # gate while still never guessing a name from unrelated page content
    # beyond the bounded ancestor search this already used.
    node = label_node.parent
    for _ in range(_ALO_CONTACT_ANCESTOR_SEARCH_LEVELS):
        if node is None:
            break
        contact = _alo_contact_from_container(node)
        if contact:
            return contact
        node = node.parent
    return None


# imoti.bg detail-page specs/contact extraction, added 2026-09-24, corrected
# 2026-09-25 after a production audit found property_type_raw/agency_name/
# agency_website at a flat 0% (0/912) despite every listing having gone
# through fetch_listing_detail(). The comment this replaced claimed
# fetch_listing_detail() "already has PROVEN, live-confirmed access to this
# page's own <script type="application/ld+json"> block(s) - that's exactly
# how its description extraction already works." That claim does not
# actually hold up: fetch_listing_detail() tries a <meta name="description">
# (or og:description) tag FIRST, and only falls back to scanning ld+json for
# a "description" field when that meta tag is missing/too short - see its
# own code in scraper_imoti_bg.py. Production's 94%+ description hit rate is
# perfectly explained by the meta-tag path alone (present on essentially
# every generic webpage) and is not evidence the ld+json fallback, let alone
# ld+json parsing in general, has EVER actually succeeded on a real imoti.bg
# page. This was the same shape of mistake as alo.bg's own regression
# (PR #279): a confident "this is already proven" claim that wasn't actually
# checked against what really drives the number it points to.
#
# SETTLED 2026-09-28, not still open: a fresh real-data audit (828 active
# imoti.bg listings, still a flat, literal 0% - not just low - for every one
# of property_type_raw/construction_type/built_year/floor_number/features/
# has_elevator/furnished/has_central_heating/agency_name/agency_website,
# confirming the 0% below never actually improved) traced this to real,
# live production evidence rather than guessing further from this sandbox's
# own still-blocked egress to imoti.bg (re-confirmed via WebFetch here too).
# scrape.yml run 36369847151 (2026-09-28, job 108763654513,
# `python scraper_imoti_bg.py` step) - a normal SCHEDULED run, nothing
# manually dispatched to get this, per CLAUDE.md - logged this file's own
# imoti_bg_ld_json_diagnostic() output for 5 real, live, currently-active
# imoti.bg detail pages (e.g. .../тристаен-апартамент/софия/връбница-
# 515941.htm/cu:BGN). All 5 read `{'script_tags': 0, 'parsed': 0,
# 'failed_to_parse': 0, 'types_seen': []}` - i.e. real confirmation of
# failure mode 1 below, not mode 2 (a parse failure would show a non-zero
# `failed_to_parse`, and any ld+json at all, even unrecognized, would show
# a non-zero `script_tags`). This is not "the wrong schema.org shape" - it's
# these pages genuinely carrying no <script type="application/ld+json">
# block at all. extract_specs_imoti_bg()/extract_contact_imoti_bg()
# themselves are not buggy (both correctly return None on exactly this
# input, and both are covered by tests/test_imoti_bg_detail_extraction.py's
# full synthetic-fixture suite) - their founding premise (this site
# publishes schema.org structured data, the same way its description also
# happens to be readable from a plain <meta name="description"> tag) was
# simply wrong for this portal. A real fix needs a DOM/text-based extractor
# (the same "read known labels off the real page" approach already proven
# for alo.bg/bazar.bg) built against this site's REAL markup, which this
# sandbox still can't fetch - imoti_bg_ld_json_diagnostic() now also reports
# 3 safe, generic (not label-guessing) structural facts about the rest of
# the page for whoever picks this up next with live access or a future
# scheduled run's logs: total <script> tag count of any type, the distinct
# "type" attribute values seen among them, and whether the body has any
# <table>/<dl> element at all (see that function's own comment for why
# these three and not a guessed label search).
#
# Two real, independent failure modes could produce a 0% pattern like this
# in general (now narrowed to mode 1, confirmed live, per the paragraph
# above - mode 2 is kept here as an already-hardened-against possibility,
# not because it's still an open question for THIS 0%):
#   1. imoti.bg's real ld+json (if it carries any at all) simply doesn't use
#      the RealEstateListing/Accommodation/Organization schema.org shape
#      assumed below - CONFIRMED as the real cause here (see above): these
#      pages carry no ld+json block at all, not merely an unrecognized one.
#   2. A common, well-documented real-world JSON-LD quirk: many sites
#      generate a script's JSON text from a raw user-submitted description
#      containing literal newline/control characters without escaping them,
#      which trips Python's DEFAULT strict `json.loads()` (control characters
#      are illegal inside a JSON string under strict mode) - silently
#      swallowed here by the bare `except (ValueError, TypeError): continue`,
#      exactly like it would be in fetch_listing_detail()'s own ld+json
#      fallback for description. This is not a guess about imoti.bg's
#      specific markup - it's a generic, well-known parser fragility this
#      code can safely harden against regardless of which (if either) cause
#      is the real one, so _imoti_bg_ld_json_candidates() below now retries
#      with `strict=False` before giving up on a block. It also now tracks
#      basic counts so a future run's logs (scraper_imoti_bg.py's own
#      diagnostic print, gated on real access to production) can tell these
#      two failure modes apart for real instead of guessing further here.
#
# What's extracted below is deliberately limited to CORE schema.org
# vocabulary terms with a fixed, documented meaning - not a guess about this
# specific site's markup:
#   - floorSize (a standard Accommodation/Place property, {"@type":
#     "QuantitativeValue", "value": ...}) -> sqm.
#   - amenityFeature (a standard Accommodation property, an array of
#     {"@type": "LocationFeatureSpecification", "name": ..., "value": true}
#     entries) -> features[]/has_elevator/furnished/has_central_heating,
#     using the same keyword-match approach extract_specs_alo() already
#     uses for its own (differently-sourced) feature tags.
#   - @type itself, when it's one of schema.org's own named Accommodation
#     subtypes (Apartment/House/SingleFamilyResidence/Room/Suite/...) ->
#     property_type_raw. This is the type schema.org itself defines for
#     "what kind of accommodation this is" - not a fabricated field.
#   - seller/offers.seller/provider/author (a standard Organization/Person
#     shape with "name" and, optionally, "url") -> agency_name/
#     agency_website, mirroring extract_contact_alo()'s exact same "only a
#     genuine external site, never the portal's own domain" restraint.
#
# What's deliberately NOT attempted here, for lack of any real evidence:
# construction_type, built_year, completion_status, floor_number, and
# floor_qualifier - schema.org has no core vocabulary term for any of
# these (unlike floorSize/amenityFeature/the Accommodation subtypes above,
# which are real, documented schema.org properties), so filling them in
# would mean guessing this specific site's own custom field/label names
# sight-unseen - exactly the "never fabricate a working extraction for
# something that can't be verified" mistake extract_contact_alo()'s own
# comment (phone numbers) already warns against. If a future contributor
# gets live access to a real imoti.bg detail page, the concrete thing to
# check is what (if anything) is in a candidate's "additionalProperty"
# array (schema.org's generic name/value escape hatch for exactly this
# kind of site-specific extra fact) - if these fields are there under some
# real, observed name, add a real extractor for them then.
_IMOTI_BG_ACCOMMODATION_TYPES = frozenset([
    "Apartment", "House", "SingleFamilyResidence", "Room", "Suite",
    "CampingPitch", "Campground", "Accommodation",
])
_IMOTI_BG_FEATURE_ELEVATOR_RE = re.compile(r"асансьор|elevator|lift", re.IGNORECASE)
_IMOTI_BG_FEATURE_UNFURNISHED_RE = re.compile(r"необзаведен|unfurnished", re.IGNORECASE)
_IMOTI_BG_FEATURE_FURNISHED_RE = re.compile(r"(?<!не)обзаведен|(?<!un)furnished", re.IGNORECASE)
_IMOTI_BG_FEATURE_HEATING_RE = re.compile(r"\bтец\b|central heating|отопление", re.IGNORECASE)


def _parse_ld_json_blocks(html):
    """Every dict-shaped entry across all application/ld+json blocks on the
    page, plus the raw counts (script tags found, blocks that failed to
    parse even with the strict=False retry) that let a caller tell "no
    ld+json on this page at all" apart from "ld+json is there but broken/
    unrecognized" - see this module's own comment above
    _imoti_bg_ld_json_candidates() for why that distinction matters here.

    Tries a normal strict `json.loads()` first, then retries with
    `strict=False` (the standard library's own documented way to allow
    literal control characters inside a JSON string) before giving up on a
    block - a generic hardening against a common real-world JSON-LD quirk
    (an unescaped newline from a raw user-submitted description), not a
    guess about this specific site's markup."""
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return [], 0, 0
    candidates = []
    scripts = soup.find_all("script", attrs={"type": "application/ld+json"})
    failed = 0
    for script in scripts:
        text = script.string or ""
        data = None
        try:
            data = json.loads(text)
        except (ValueError, TypeError):
            try:
                data = json.loads(text, strict=False)
            except (ValueError, TypeError):
                failed += 1
                continue
        candidates.extend(data if isinstance(data, list) else [data])
    return [c for c in candidates if isinstance(c, dict)], len(scripts), failed


def _imoti_bg_ld_json_candidates(html):
    """Every dict-shaped entry across all application/ld+json blocks on the
    page - see _parse_ld_json_blocks() above for the actual parse. Kept as
    a thin wrapper (just the candidates, no counts) since this is the
    signature extract_specs_imoti_bg()/extract_contact_imoti_bg() already
    call and test against."""
    candidates, _, _ = _parse_ld_json_blocks(html)
    return candidates


def imoti_bg_ld_json_diagnostic(html):
    """Zero-cost visibility for scraper_imoti_bg.py to log (see its own
    call site) when specs/contact extraction comes up empty: how many
    application/ld+json script tags the page actually had, how many failed
    to parse even with the strict=False retry, and - for whatever parsed
    fine - the distinct top-level "@type" values seen (a real page's own
    words for what a candidate IS, the single most useful thing to log for
    telling "no ld+json here" apart from "ld+json is here but not the
    Accommodation/Organization shape this file assumes"), all without
    dumping raw HTML/JSON into a log. Never raises.

    UPDATE 2026-09-28: the original hypothesis this diagnostic was built to
    distinguish is now settled, not still open - a real production audit
    (this repo's own scrape.yml run 36369847151, 2026-09-28, job
    108763654513) logged this exact diagnostic for 5 real, live imoti.bg
    detail pages and every single one came back `'script_tags': 0`. That is
    not "ld+json is here but the wrong shape" (which is what
    extract_specs_imoti_bg()/extract_contact_imoti_bg() were built to
    tolerate) - it's real, repeated, live confirmation that these pages
    carry NO application/ld+json block at all. The two extractor functions
    are not buggy (both are exercised by a full synthetic-fixture test
    suite in tests/test_imoti_bg_detail_extraction.py and correctly return
    None on exactly this input) - their founding assumption (this site
    publishes schema.org structured data the same way its description
    happens to also be readable from a plain <meta name="description">
    tag) was simply wrong for this portal. Fixing this for real needs a
    DOM/text-based extractor targeting whatever the real page actually
    looks like (the same "read known labels off the real page" approach
    already proven for alo.bg/bazar.bg), which needs real markup to read
    first - this sandbox's egress to imoti.bg is blocked (confirmed via
    WebFetch here too), so the 3 fields added below exist to capture that
    real markup's shape, safely and generically (no guess at Bulgarian
    label text, which is exactly the kind of guess this codebase's own
    history (see extract_specs_alo()/extract_specs_bazar()'s own comments)
    has repeatedly warned against making without live confirmation), the
    next time this already-scheduled workflow runs - no live dispatch
    needed to get it, it already runs every few hours on its own."""
    try:
        candidates, script_count, failed = _parse_ld_json_blocks(html)
    except Exception:
        return {
            "script_tags": 0, "parsed": 0, "failed_to_parse": 0, "types_seen": [],
            "any_script_tags": 0, "script_types_seen": [], "has_table_or_dl": False,
        }
    types_seen = sorted({
        c["@type"] for c in candidates
        if isinstance(c.get("@type"), str)
    })
    # Three generic, non-guessing structural facts about the REST of the
    # page - deliberately not a search for any specific Bulgarian label
    # text (that would be exactly the kind of blind guess this file's own
    # history warns against): how many <script> tags of ANY type exist (a
    # page that renders specs client-side from a JSON state blob would show
    # many, with an unfamiliar "type" - or none at all, e.g. "text/
    # javascript" - either fact narrows things down), which distinct "type"
    # attribute values they carry (a real "type" other than "application/
    # ld+json" - e.g. a framework's own state-blob convention - is the
    # single most actionable clue a future non-guessing fix could act on),
    # and whether the body has any <table>/<dl> element at all (the generic
    # shape a plain server-rendered spec table - the same shape bazar.bg's
    # own, already-proven extract_specs_bazar() reads - would take, versus
    # a page with no structured markup anywhere).
    any_script_tags = 0
    script_types_seen = []
    has_table_or_dl = False
    try:
        soup = BeautifulSoup(html, "html.parser")
        all_scripts = soup.find_all("script")
        any_script_tags = len(all_scripts)
        script_types_seen = sorted({
            s.get("type").strip() for s in all_scripts
            if isinstance(s.get("type"), str) and s.get("type").strip()
        })
        has_table_or_dl = bool(soup.find(["table", "dl"]))
    except Exception:
        pass
    return {
        "script_tags": script_count,
        "parsed": len(candidates),
        "failed_to_parse": failed,
        "types_seen": types_seen,
        "any_script_tags": any_script_tags,
        "script_types_seen": script_types_seen,
        "has_table_or_dl": has_table_or_dl,
    }


def _imoti_bg_nested_dicts(candidate):
    """The candidate itself, plus one level of nesting under "about"/
    "itemOffered" - the canonical schema.org shape for a RealEstateListing
    wrapping the actual Accommodation it's advertising (RealEstateListing
    itself carries no floorSize/amenityFeature/subtype of its own; those
    live on the Accommodation it points to). Checking both, rather than
    just the top level, costs nothing when the nesting isn't there (dict.get
    on a dict that doesn't have it is just None) and covers either shape
    without assuming which one this specific site actually uses."""
    nested = [candidate]
    for key in ("about", "itemOffered", "mainEntity"):
        inner = candidate.get(key)
        if isinstance(inner, dict):
            nested.append(inner)
    return nested


def extract_specs_imoti_bg(html):
    try:
        candidates = _imoti_bg_ld_json_candidates(html)
    except Exception:
        return None
    if not candidates:
        return None

    specs = {}
    for c in candidates:
        for node in _imoti_bg_nested_dicts(c):
            if "sqm" not in specs:
                floor_size = node.get("floorSize")
                if isinstance(floor_size, dict):
                    value = floor_size.get("value")
                elif isinstance(floor_size, (int, float, str)):
                    value = floor_size
                else:
                    value = None
                if value is not None:
                    try:
                        specs["sqm"] = round(float(str(value).replace(",", ".")))
                    except (ValueError, TypeError):
                        pass

            if "property_type_raw" not in specs:
                type_value = node.get("@type")
                if isinstance(type_value, str) and type_value in _IMOTI_BG_ACCOMMODATION_TYPES:
                    specs["property_type_raw"] = type_value

            if "features" not in specs:
                amenities = node.get("amenityFeature")
                if isinstance(amenities, list):
                    names = []
                    for a in amenities:
                        if not isinstance(a, dict):
                            continue
                        name = a.get("name")
                        value = a.get("value")
                        # Only a feature explicitly marked present (true/
                        # "true"/1) counts as a checked tag - same
                        # "checked features only" shape extract_specs_alo()
                        # already applies to its own (differently-shaped)
                        # feature list.
                        if isinstance(name, str) and name.strip() and value in (True, "true", "True", 1):
                            names.append(name.strip())
                    if names:
                        specs["features"] = names
                        feature_text = " ".join(names)
                        if _IMOTI_BG_FEATURE_ELEVATOR_RE.search(feature_text):
                            specs["has_elevator"] = True
                        if _IMOTI_BG_FEATURE_UNFURNISHED_RE.search(feature_text):
                            specs["furnished"] = False
                        elif _IMOTI_BG_FEATURE_FURNISHED_RE.search(feature_text):
                            specs["furnished"] = True
                        if _IMOTI_BG_FEATURE_HEATING_RE.search(feature_text):
                            specs["has_central_heating"] = True

    return specs or None


# bazar.bg's structured spec table (confirmed via the user's own real
# screenshots of a live detail page, bazar.bg/obiava-55691101/
# prodava-2-staen-gr-sofiia-lyulin-1 - this sandbox has no live network
# access to bazar.bg either, same as alo.bg - see extract_coords_bazar()'s
# neighboring comment) is a fixed sequence of Bulgarian label/value rows:
# Тип сделка (a combined transaction+property-type sentence, e.g. "Продава
# Апартамент в гр. София" - not extracted here, too generic to be useful
# and superseded by Тип апартамент below), Тип апартамент (the specific
# room-count property type, e.g. "2-стаен" - the actually useful value,
# stored as property_type_raw), Квадратура (size, e.g. "50 кв. м."), Цена
# на кв.м. (price per sqm, e.g. "2240 €/кв. м." - not extracted; this
# project already computes price_per_sqm downstream from price/sqm
# elsewhere, no need to also trust the portal's own pre-computed figure),
# Вид строителство (construction type, e.g. "ЕПК" - no "на", same
# phrasing already confirmed for alo.bg's own row of the same name), and
# Етаж (a bare floor NUMBER on bazar.bg, e.g. "4" - unlike alo.bg, where
# "Етаж" is a floor QUALIFIER like "Непоследен" and the number lives under
# a separate "Номер на етажа" row; bazar.bg's own screenshot shows no
# qualifier row at all, so this maps straight to floor_number).
#
# Same reasoning as extract_specs_alo() (see its own comment): rather than
# guess this table's CSS classes/tag names, this locates each row by its
# fixed Bulgarian LABEL text and reads whatever text follows it, using
# BeautifulSoup's get_text("\n", strip=True) to flatten the page into one
# line per underlying text node - works regardless of whether a row is a
# <tr><td> pair, a <dl><dt>/<dd> pair, or a pair of <div>s. Every real
# value in the screenshot is a single short line (unlike alo.bg's built_
# year row, which had a separate trailing hint span), so the per-row value
# cap here is deliberately tighter (2 lines, not alo's 4) - just enough
# margin for a value split across two text nodes without risking Етаж (the
# last known row, with no confirmed heading/stop-text right after it on
# this portal) pulling a chunk of the free-text description paragraph
# underneath it into its own value. floor_number is still safe even if
# that happens: it's read out with a digit-only regex search that matches
# the FIRST number in the collected text, which is always the real "4"
# itself (collected before any description text could be appended), never
# a number appearing later in that description.
#
# Defensive by construction, same as every other extractor in this file:
# a row whose label isn't found is simply absent from the result, and if
# NO known label is found at all this returns None outright.
_BAZAR_SPEC_LABELS = [
    ("Тип апартамент", "property_type_raw"),
    ("Квадратура", "_sqm_raw"),
    ("Вид строителство", "construction_type"),
    ("Етаж", "_floor_number_raw"),
]
# "Тип сделка" and "Цена на кв.м." are recognized labels purely so they
# correctly terminate whatever row precedes them (same role "Местоположение"
# plays in _ALO_SPEC_ALL_LABELS) - neither is ever stored, see the comment
# above.
_BAZAR_SPEC_ALL_LABELS = frozenset(
    ["Тип сделка", "Цена на кв.м."] + [label for label, _ in _BAZAR_SPEC_LABELS]
)
_BAZAR_SPEC_MAX_VALUE_LINES = 2

_BAZAR_SQM_VALUE_RE = re.compile(r"(\d+(?:[.,]\d+)?)")
_BAZAR_FLOOR_NUMBER_VALUE_RE = re.compile(r"(\d+)")


def _bazar_spec_value(lines, start_idx):
    """Same "collect until the next known label" shape as _alo_spec_value()
    - see that function's own comment. Returns "" if the very next line is
    itself another known label - i.e. this row's value is genuinely absent
    on this listing, not a parsing failure."""
    collected = []
    i = start_idx
    while i < len(lines) and len(collected) < _BAZAR_SPEC_MAX_VALUE_LINES:
        line = lines[i]
        if line in _BAZAR_SPEC_ALL_LABELS:
            break
        collected.append(line)
        i += 1
    return " ".join(collected).strip()


def extract_specs_bazar(html):
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return None
    text = soup.get_text("\n", strip=True)
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]

    raw = {}
    for i, line in enumerate(lines):
        for label, key in _BAZAR_SPEC_LABELS:
            if line == label and key not in raw:
                value = _bazar_spec_value(lines, i + 1)
                if value:
                    raw[key] = value
                break

    if not raw:
        return None

    specs = {}
    for key in ("property_type_raw", "construction_type"):
        if raw.get(key):
            specs[key] = raw[key]

    if raw.get("_sqm_raw"):
        m = _BAZAR_SQM_VALUE_RE.search(raw["_sqm_raw"])
        if m:
            try:
                specs["sqm"] = round(float(m.group(1).replace(",", ".")))
            except ValueError:
                pass

    if raw.get("_floor_number_raw"):
        m = _BAZAR_FLOOR_NUMBER_VALUE_RE.search(raw["_floor_number_raw"])
        if m:
            specs["floor_number"] = int(m.group(1))

    return specs or None


def _imoti_bg_org_candidates(node):
    """Every plausible "who's behind this listing" object on one ld+json
    node, in priority order - offers.seller first (the most specific: this
    exact offer's seller), then the node's own seller/provider/author. Same
    idea as extract_contact_alo() walking outward to find "whatever box
    wraps the poster's name" without assuming one fixed shape."""
    orgs = []
    offers = node.get("offers")
    if isinstance(offers, dict):
        seller = offers.get("seller")
        if isinstance(seller, dict):
            orgs.append(seller)
    for key in ("seller", "provider", "author"):
        val = node.get(key)
        if isinstance(val, dict):
            orgs.append(val)
    return orgs


def extract_contact_imoti_bg(html):
    try:
        candidates = _imoti_bg_ld_json_candidates(html)
    except Exception:
        return None
    if not candidates:
        return None

    contact = {}
    for c in candidates:
        for node in _imoti_bg_nested_dicts(c):
            for org in _imoti_bg_org_candidates(node):
                name = org.get("name")
                if "agency_name" not in contact and isinstance(name, str) and name.strip():
                    contact["agency_name"] = name.strip()
                url = org.get("url")
                if "agency_website" not in contact and isinstance(url, str) and url.strip():
                    # Only a genuine external site, never the portal's own
                    # domain - same restraint as extract_contact_alo(). A
                    # real hostname check (not a raw substring test) so a
                    # genuinely different domain that merely CONTAINS
                    # "imoti.bg" as a substring (e.g. "imperial-imoti.bg")
                    # is never mistaken for the portal's own domain.
                    host = (urlparse(url.strip()).hostname or "").lower()
                    if host and host != "imoti.bg" and not host.endswith(".imoti.bg"):
                        contact["agency_website"] = url.strip()
                if "agency_name" in contact and "agency_website" in contact:
                    break

    return contact or None
# bazar.bg's agency contact box (confirmed via the same real screenshots as
# extract_specs_bazar() - see its own comment for the network-access
# caveat) shows the agency's name as plain text, followed by a line "Още
# оферти на <url>" ("More offers at <url>") linking to what looks like the
# agency's own storefront/listing page (e.g. "https://sntbg.imot.bg") -
# plausibly an imot.bg-hosted agency page rather than the agency's own
# independent domain, but stored as agency_website either way, same "don't
# over-engineer telling them apart" scope this project already applies to
# alo.bg's own real-vs-hosted-site distinction (see extract_contact_alo()'s
# comment) - just with less certainty here about which this actually is.
#
# The box also shows a masked phone number, "08XX XXX XXX (покажи)"
# ("покажи" = "show") behind a click-to-reveal button - the identical
# anti-scraping pattern already documented at length in
# extract_contact_alo()'s own comment (alo.bg's "Виж" button). Same
# conclusion, same reason: DELIBERATELY NOT extracted here either. Never
# guess a masked number.
#
# Same reasoning as extract_contact_alo(): rather than guess this box's CSS
# classes/tag names (unverifiable without live HTML), this keys off the
# "Още оферти на" phrase - the one piece of this box confirmed word-for-
# word in the screenshots - and walks up from wherever that text sits to
# the smallest ancestor whose own text ALSO holds a plausible agency-name
# line (not just the offers phrase and/or its URL) - the same "keep
# climbing until there's real content beyond the label itself" shape
# extract_description_alo() uses, rather than requiring a real <a href>
# specifically (unlike extract_contact_alo()'s equivalent search): the
# name and the "Още оферти на" line could plausibly sit as two different-
# depth siblings, and the website itself might not be a real anchor at all
# (the screenshot doesn't show it as a distinguishably-styled link, unlike
# alo.bg's own contact box) - a bare domain rendered as plain text is
# handled by the website extraction below regardless of which shape wins.
_BAZAR_OFFERS_LINK_TEXT = "Още оферти на"
_BAZAR_PHONE_REVEAL_RE = re.compile(r"покажи", re.IGNORECASE)
_BAZAR_CONTACT_ANCESTOR_SEARCH_LEVELS = 6


def _bazar_contact_name_candidate(lines):
    """First line that plausibly reads as an agency/poster name - not the
    offers phrase itself, not the masked-phone reveal line, not a bare
    phone number, not a bare domain/URL shown as plain text. Same filter
    shape as extract_contact_alo()'s own name search."""
    for line in lines:
        if _BAZAR_OFFERS_LINK_TEXT in line:
            continue
        if _BAZAR_PHONE_REVEAL_RE.search(line):
            continue
        if re.match(r"^\d", line):
            continue
        if "." in line and " " not in line:
            continue
        return line
    return None


def extract_contact_bazar(html):
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return None

    label_node = soup.find(string=re.compile(re.escape(_BAZAR_OFFERS_LINK_TEXT)))
    if label_node is None or label_node.parent is None:
        return None

    node = label_node.parent
    container = None
    name = None
    for _ in range(_BAZAR_CONTACT_ANCESTOR_SEARCH_LEVELS):
        if node is None:
            break
        lines = [ln.strip() for ln in node.get_text("\n", strip=True).split("\n") if ln.strip()]
        candidate = _bazar_contact_name_candidate(lines)
        if candidate:
            container = node
            name = candidate
            break
        node = node.parent
    if container is None:
        # Heading found, but no ancestor within the search depth had a
        # plausible name line beyond the offers phrase itself - a
        # structural mismatch, not a genuine empty box. Never guess.
        return None

    contact = {"agency_name": name}

    website = None
    for a in container.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("mailto:", "tel:", "#")):
            continue
        website = href if href.startswith("http") else f"https://{href.lstrip('/')}"
        break
    if website is None:
        # The site may render this as plain text rather than a real
        # anchor - fall back to whatever token sits right after the "Още
        # оферти на" phrase in the container's own text.
        text = container.get_text(" ", strip=True)
        m = re.search(re.escape(_BAZAR_OFFERS_LINK_TEXT) + r"\s*(\S+)", text)
        if m and "." in m.group(1):
            candidate = m.group(1).strip(").,;")
            website = candidate if candidate.startswith("http") else f"https://{candidate.lstrip('/')}"
    if website:
        contact["agency_website"] = website

    return contact


# "жк."/"ж.к." (жилищен комплекс - "residential complex") is a common
# Bulgarian prefix on neighborhood names (e.g. "жк. Лозенец") that, left
# in the query, made Nominatim return zero results ~95% of the time
# (56/59 in a spot-check of the real geocode cache) - while the exact same
# neighborhood names with the "кв." prefix or no prefix at all succeeded
# ~95-100% of the time. Stripped here, at the one call site every scraper
# shares, rather than in each scraper individually.
_ZHK_PREFIX_RE = re.compile(r"^ж\.?\s*к\.?\s+", re.IGNORECASE)


def _clean_query(query):
    return _ZHK_PREFIX_RE.sub("", query.strip())


def _haversine_km(lat1, lng1, lat2, lng2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _load_cache():
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


class Geocoder:
    """Caches by the exact query string on disk (data/geocode_cache.json,
    shared across all scrapers and committed to the repo like the other
    data files), so the same neighborhood name reused across thousands of
    listings - and across scraper runs - triggers one real Nominatim
    request total, not one per listing per run."""

    def __init__(self):
        self.cache = _load_cache()
        self._dirty = False

    def geocode_cached_only(self, query):
        """Cache lookup with no network call, for callers that can't afford
        to block on a live Nominatim round-trip (or its up-to-8s timeout)
        per listing - e.g. a scraper covering thousands of distinct
        nationwide locations for the first time, where the existing cache
        barely helps yet. Returns None on any cache miss instead of
        fetching; a separate backfill pass (see backfill_geocode_homes.py)
        does the live lookups on its own schedule."""
        query = _clean_query(query or "")
        return self.cache.get(query)

    def _geocode_raw(self, query, limit=1):
        """One real Nominatim lookup, no caching - callers manage the cache
        themselves so a single query() call can make more than one raw
        lookup (see geocode()'s cross-check) without double-charging the
        rate limit delay per cache write. limit>1 (used only by the
        confidence check below) doesn't cost an extra request - Nominatim
        returns up to `limit` ranked results in the one response."""
        time.sleep(NOMINATIM_DELAY_SECONDS)
        try:
            resp = requests.get(
                NOMINATIM_URL,
                params={"q": query, "format": "json", "limit": limit},
                headers={"User-Agent": GEOCODE_USER_AGENT},
                timeout=8,
            )
            resp.raise_for_status()
            data = resp.json()
            points = [{"lat": float(d["lat"]), "lng": float(d["lon"])} for d in data]
        except Exception as e:
            print(f"DEBUG: geocode failed for {query!r}: {e}")
            points = []
        return points[0] if (points and limit == 1) else points

    def _bare_name_is_confident(self, query):
        """A bare settlement name is only trustworthy as independent ground
        truth when Nominatim itself is confident about it - i.e. its top
        few ranked results agree on roughly one place, not scattered
        across the country. A GENUINE distinct settlement name (like
        "Червен бряг") resolves this way. A generic word reused as a
        district name in many unrelated towns (Център/"Center", Дружба/
        "Friendship", Изток/"East"...) does not - its top results are
        each a real place, just different, unrelated ones - so trusting
        the single top hit as "the" answer for those would be wrong.
        This replaces an earlier attempt at this that tried to hand-list
        which cities/words are "safe" to skip - that approach doesn't
        scale (an early version only excluded Bulgaria's 4 biggest
        cities, and still wrongly overrode dozens of ordinary districts
        in smaller cities like Ruse and Haskovo, corrupting real data
        before being caught and reverted). Checking the bare name's own
        result spread works for any settlement name, known in advance or
        not, without needing a list of exceptions at all."""
        top = self._geocode_raw(query, limit=3)
        if len(top) < 2:
            return len(top) == 1
        ref = top[0]
        return all(_haversine_km(ref["lat"], ref["lng"], p["lat"], p["lng"]) <= 30 for p in top[1:])

    def geocode(self, query):
        query = _clean_query(query or "")
        if not query:
            return None
        if query in self.cache:
            return self.cache[query]

        result = self._geocode_raw(query)

        # Independent verification, not blind trust: a query like "<area>,
        # <city>, България" only ever resolves correctly if <city> is
        # really that area's own city/region - and for a city/oblast-
        # sliced scraper, <city> is often just whichever search page a
        # listing happened to turn up on, not a fact about the listing
        # itself (confirmed live: an imot.bg "Lovech" city search
        # returned a real Cherven Bryag listing, an entirely different
        # town in a different oblast - geocoding "Червен бряг, Ловеч,
        # България" resolved ~62km from the real town, propagating a
        # wrong coordinate to every listing that shared the query).
        # The bare settlement name is only trusted as the independent
        # answer when it's ALSO independently confident on its own (see
        # _bare_name_is_confident) - a generic district name disagreeing
        # with its qualifier is not evidence the qualifier is wrong, it's
        # just an ambiguous word; only override when the bare name is
        # both different from the qualified result AND unambiguous by
        # itself.
        parts = [p.strip() for p in query.split(",")]
        if result and len(parts) >= 3 and parts[0]:
            bare_query = _clean_query(f"{parts[0]}, България")
            if bare_query != query:
                bare_result = self.cache.get(bare_query)
                if bare_query not in self.cache:
                    bare_result = self._geocode_raw(bare_query)
                    self.cache[bare_query] = bare_result
                    self._dirty = True
                if bare_result:
                    dist_km = _haversine_km(result["lat"], result["lng"], bare_result["lat"], bare_result["lng"])
                    if dist_km > 30 and self._bare_name_is_confident(bare_query):
                        print(f"DEBUG: geocode mismatch for {query!r} vs {bare_query!r} "
                              f"({dist_km:.0f}km apart, bare name confident) - trusting the bare settlement name")
                        result = bare_result
                    elif dist_km > 30:
                        print(f"DEBUG: geocode mismatch for {query!r} vs {bare_query!r} "
                              f"({dist_km:.0f}km apart, bare name AMBIGUOUS) - keeping the qualified result")

        self.cache[query] = result
        self._dirty = True
        return result

    def save(self):
        if self._dirty:
            CACHE_FILE.write_text(json.dumps(self.cache, ensure_ascii=False, indent=2), encoding="utf-8")
            self._dirty = False


def prune_snapshots(history):
    # Every scraper appends one {seen_at, price_eur} snapshot per listing
    # per run regardless of whether the price changed - the real driver of
    # history_*.json's size at nationwide scale (a listing scraped every 6h
    # for 8 months with 2 real price changes stores ~970 raw snapshots
    # before this, 3 after). Shrinks each listing's snapshot list to its
    # first snapshot, every point where the price actually changed, and
    # the single most recent snapshot - kept unconditionally, even when
    # its price repeats the one before it, so last-seen/removed_at/
    # days_on_market (all read off the last snapshot's timestamp) stay
    # exactly as accurate as before this ran. Never drops a listing or a
    # real price change, only redundant same-price snapshots in between.
    for rec in history.values():
        snapshots = rec.get("snapshots") or []
        if not snapshots:
            continue
        pruned = [snapshots[0]]
        for s in snapshots[1:]:
            if s.get("price_eur") != pruned[-1].get("price_eur"):
                pruned.append(s)
        if pruned[-1] is not snapshots[-1]:
            pruned.append(snapshots[-1])
        rec["snapshots"] = pruned
    return history


# --- Compressed on-disk JSON storage --------------------------------------
# 2026-09-25 addendum to this same incident (see STALE_RECORD_RETENTION's
# own comment just below): evict_stale_records() alone was found NOT to
# unblock the very next scrape.yml run. Root cause (found by reading git
# history, not guessed): dd83178 (2026-09-23, "Fix homes.bg tracking-ID
# type collision") fixed build_tracking_id() to stop dropping homes.bg's
# hs/as/lp/la type prefix - before that fix, listings of DIFFERENT types
# sharing the same bare numeric id silently collided onto one tracking key
# and overwrote each other on alternating scrapes, so many real, distinct
# listings were invisibly suppressed for a long time (only one "side" of
# each collision ever visible at a time). Once fixed, every collision
# pair's previously-hidden "other side" starts appearing as a genuinely
# new record the next time it's freshly scraped - a real, wanted, one-time
# correction (not a bug in dd83178, not stale data), but it means
# evict_stale_records() evicting 0 records today is not "nothing to fix
# yet": the very next real crawl was confirmed (real job-log output:
# check_scrape_freshness.py's own leads count) to produce 140,337 total
# homes.bg leads, ~1.90x today's committed 74,012 - large enough on its
# own to blow through the 100MB limit again immediately, independent of
# long-term eviction.
#
# What was checked and rejected first: capping the `photos` array (41.6%
# of leads_homes.json's bytes, field-by-field measured) looked like the
# obvious lever, but sync_to_supabase.py's SOURCE_FIELDS/MERGED_FIELDS
# copies the FULL `photos` list straight from leads_homes.json into
# Supabase's listing_sources/merged_listings columns, and index.html's own
# detail-page gallery (`sourcePhotos`/`mergedPhotos` in its `showDetail()`
# path) renders every one of them - not dead weight checked only for
# truthiness/count, a real, live call site. Real measurement against
# homes.bg's actual 74,012-record leads_homes.json: even capping every
# record to a single photo (a severe, real functional loss - no more
# multi-photo gallery for 45-66% of listings depending on the cap chosen)
# combined with compact (no-indent) serialization only reaches ~111.7MB
# projected at 140,337 records - STILL over the 100MB limit, for a real
# product regression bought and not even enough on its own.
#
# Gzip compression, by contrast, recovers far more with ZERO data loss
# (full round-trip fidelity - every photo, every field, byte-identical
# after decompression) because these files are enormously repetitive:
# the same ~30 JSON keys and shared URL domains/path prefixes repeated
# across tens of thousands of near-identical records is exactly what
# gzip is built for. Measured directly on real homes.bg data: leads_
# homes.json, 74,012 records, 79.48MB compact-serialized -> 6.41MB
# gzipped (level 9, ~91.9% smaller); history_homes.json: 73.97MB compact
# -> 6.08MB gzipped. Projected at the real 140,337-record scale (linear
# scaling validated against scrape.yml's own real quoted incident numbers
# - 182.01MB/179.26MB pretty-printed at that same scale, matching this
# module's projection from the 74,012-record baseline to within ~1.5%):
# ~11.6MB (leads) / ~11.0MB (history) - roughly 8.5x headroom under
# GitHub's 100MB hard limit, not a razor's-edge fix that recurs the next
# time record count ticks up again.
#
# load_json_any()/save_json_any() below are the single read/write path
# every history_*.json/leads_*.json consumer (every scraper, sync_to_
# supabase.py, evict_stale_history.py, detect_relistings.py, verify_
# geocode_qualifiers.py, check_scrape_freshness.py, backfill_split_homes_
# id_collision.py) goes through, so a portal's on-disk format (plain vs.
# gzip) is a pure file-extension choice at that portal's own HISTORY_FILE/
# LEADS_FILE/PORTAL_FILES constant, not something every call site has to
# special-case. Only homes.bg's own HISTORY_FILE/LEADS_FILE were switched
# to `.json.gz` here - this incident is homes.bg-specific (dd83178 only
# touched homes.bg's build_tracking_id()); every other portal's record
# count didn't just jump, so they stay plain `.json`, unchanged, rather
# than an unverified blanket format change across all 8 portals.
def load_json_any(path):
    """Reads `path` as UTF-8 JSON, transparently gzip-decompressing when
    its name ends in `.gz`. See this section's own module-level comment
    for why."""
    path = Path(path)
    if path.name.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8") as f:
            return json.load(f)
    return json.loads(path.read_text(encoding="utf-8"))


def save_json_any(path, obj):
    """Writes `obj` as UTF-8 JSON to `path`. When `path`'s name ends in
    `.gz`, gzip-compresses (level 9) with compact separators - no
    indent=2 pretty-printing, which costs ~5% extra post-gzip on real
    leads_homes.json data for zero readability benefit once compressed
    (these files were never meant to be hand-read, and gzip'd JSON can't
    be diffed line-by-line either way). Every other path is written
    exactly as before this change: plain, pretty-printed (indent=2),
    unchanged for every non-`.gz` portal file."""
    path = Path(path)
    if path.name.endswith(".gz"):
        with gzip.open(path, "wt", encoding="utf-8", compresslevel=9) as f:
            json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
    else:
        path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


# --- Bounded history retention -------------------------------------------
# 2026-09-25 incident: scrape.yml (homes.bg/imot.bg/olx.bg/bazar.bg/
# imoti.bg/bcpea.org, committed as one atomic commit) failed its last 7
# consecutive scheduled runs on a GH001 hard file-size rejection -
# data/history_homes.json and data/leads_homes.json over GitHub's 100MB
# push limit. prune_snapshots() above already collapses each listing's
# snapshot list (that was the 2026-09-24 incident's own, different, root
# cause - a relisting-detector bug injecting synthetic snapshots into
# EXISTING records, fixed separately by RELISTING_GUARD_ABS/RATIO below).
# This incident's driver is different and structural: measured directly
# against the real committed data (homes.bg, 74,012 tracked records,
# ~94.9MB/history_homes.json, ~1282 bytes/record average, ~2.0 snapshots/
# record average post-prune) - per-record payload is roughly constant
# (dominated by the "photos" array, ~42% of leads_homes.json's bytes per a
# field-by-field measurement), so record COUNT is what actually drives
# size, and nothing has ever removed a record once its listing is
# confirmed sold/delisted: update_history() only ever adds keys via
# history[lid][...] = ..., across every one of the 8 portal scrapers.
# Two step-function events compound this: a portal's coverage widening
# from Sofia-only to nationwide/oblast-level (confirmed real - homes.bg/
# imot.bg/olx.bg's 2026-08-25 nationwide switch alone added 66,030 new
# homes.bg records in a single day per real first_seen timestamps; bazar.
# bg/imot.bg's 2026-09-23 oblast-capital coverage commits landed within
# the hour of this incident's own first failure) permanently raises the
# floor, since none of those newly-tracked listings are ever evicted once
# they eventually sell or get delisted either.
#
# Retention window (180 days) chosen from real relisting-gap data, not a
# guessed round number: detect_relistings.py's detect_portal() (matching
# a newly-active listing back to a since-removed one by photo/address) has
# no age cap on its candidate gone_ids, so an eviction window has to stay
# well clear of any real relisting delay or it starts silently breaking
# that matching. Measured every real "source": "relisted_from" pair
# already recorded across every portal's history_*.json (261 real pairs,
# all portals combined): delisted-to-relisted gap is 0.2-31.7 days, median
# 10.7, mean 12.8, 96% (250/261) within 30 days, none past 32 days - but
# this dataset has only been tracking any listings since 2026-08-21 (~35
# days as of this writing), so that 31.7-day max is itself left-censored;
# a real relisting after a longer real-world gap simply hasn't had time to
# be observed yet. 180 days gives ~5.6x headroom over the longest gap
# actually observed and leaves substantial room for longer gaps this young
# a dataset can't yet rule out, while still bounding growth to a fixed
# multiple of steady-state daily volume instead of forever. Evicted
# records are dropped outright rather than kept as a lighter-weight trace
# for relisting matching past this cutoff: the single largest per-record
# cost (the photos array, needed for exactly that matching) is what a
# "lightweight" archive would still have to keep to remain useful for it,
# so a partial archive wouldn't meaningfully help the size problem this
# exists to fix, and a relisting matched only after 180+ days off-market
# is both unobserved in this data so far and low-value to catch even when
# it happens. index.html's frontend reads Supabase, not these JSON files,
# and sync_to_supabase.py's own stale-row cleanup (MIN_PORTAL_RATIO=0.5,
# MIN_PORTAL_ABSOLUTE=10) already treats a portal's mirrored Supabase rows
# as tied to what's in the current leads_*.json, not a permanent archive -
# nothing downstream needs these long-gone records kept around forever.
STALE_RECORD_RETENTION = timedelta(days=180)


def evict_stale_records(history, retention=STALE_RECORD_RETENTION, now=None):
    """Removes (in place) every history record whose most recent snapshot
    is older than `retention` - a listing that has not been seen in any
    scrape for that long is treated as permanently gone (sold/delisted),
    not just temporarily off the current commit due to e.g. a scrape.yml
    outage (this project's worst real outage to date, this same incident,
    was ~40 hours - three orders of magnitude below the default retention,
    so there is no realistic false-eviction risk from an ordinary commit-
    pipeline gap). Call this before prune_snapshots()/save() in every
    scraper's own save_history() so history_*.json (and, via the same
    `history` object, that run's own compute_leads()-derived leads_*.json)
    stay bounded to a fixed multiple of steady-state daily volume instead
    of growing forever - see this section's own module-level comment for
    the real measurements and the 180-day retention's reasoning. Returns
    the number of records evicted."""
    now = now or datetime.now(timezone.utc)
    stale_ids = [
        lid
        for lid, rec in history.items()
        if rec.get("snapshots") and now - datetime.fromisoformat(rec["snapshots"][-1]["seen_at"]) > retention
    ]
    for lid in stale_ids:
        del history[lid]
    return len(stale_ids)


# --- Relisting chain-storm guard ----------------------------------------
# 2026-09-24 incident: a 3-consecutive-scrape.yml-run commit/push outage
# (git push rejected with GH001, data/leads_homes.json and data/
# history_homes.json at 182.01MB/179.26MB vs GitHub's 100MB hard limit -
# real numbers off job logs for runs 35883682311/35918395367/35945698190)
# meant homes.bg's checked-out history kept comparing detect_relistings.py's
# GONE_AFTER cutoff against an increasingly stale committed baseline. The
# next run's crawl found huge swaths of its own backlog crossing that
# cutoff while simultaneously being freshly re-scraped under new listing
# IDs, and detect_relistings.py's chain logic (see its own module
# docstring) misread that as 61,862 simultaneous delisted-then-relisted
# pairs in one run - 83.6% of homes.bg's entire 74,012-listing tracked
# backlog - injecting that many synthetic snapshots and ballooning both
# files past the push limit, discarding every portal's real data on every
# failed atomic push (scrape.yml commits data/ as one commit).
#
# Calibrated from real committed history, not a guessed round number.
# **Correction (Missy's review caught this): the original version of this
# comment divided bazar.bg's cumulative 1,873 relisting-tagged snapshots
# by 9 runs to get "~208/run" - that's wrong, because 1,726 of those 1,873
# were a one-time bulk backfill written by the go-live commit itself
# (2026-09-20 23:59 UTC), not steady per-run behavior.** Diffing each of
# the 10 real "Update listings" runs since go-live individually (not the
# cumulative total) gives bazar.bg's real steady-state per-run injection
# rate: 6-28 relistings/run, 0.01%-0.06% of its 51,860-listing backlog -
# never close to the originally-claimed 208/0.4%. That means the real
# margin under these thresholds is far larger than first estimated
# (roughly 70-300x the busiest real per-run rate, not ~10x) - the
# threshold VALUES below don't need to change, they were already safe and
# are safer than originally believed; only this narrative was wrong. The
# incident's 61,862-in-one-run/83.6%-of-backlog event remains ~300x even
# the highest real per-run count on this corrected basis, and two orders
# of magnitude past its per-run backlog fraction - comfortable headroom
# for a portal's genuinely busiest real day, with no realistic risk of
# missing an actual storm.
RELISTING_GUARD_ABS = 2000
RELISTING_GUARD_RATIO = 0.05


def relisting_chain_guard_tripped(portal, matched_count, total_tracked):
    """True (and loudly ::error::-logged) when a single run's relisting-
    chain detector matched an implausibly large slice of a portal's
    tracked backlog as simultaneous delisted-then-relisted pairs - the
    signature of stale/broken upstream data (a scraper outage, or a prior
    run's failed commit leaving GONE_AFTER's cutoff comparing against a
    stale baseline - see this module's own 2026-09-24 incident comment
    above) being misread as a mass relisting event, not real relisting
    behavior. Callers must skip chain-injection entirely for this portal
    this run when this returns True - per this project's "fail loud,
    never silent" standing rule, silently injecting the synthetic
    snapshots anyway is exactly the failure mode this guards against."""
    if matched_count <= 0:
        return False
    ratio = matched_count / total_tracked if total_tracked else 0.0
    if matched_count >= RELISTING_GUARD_ABS or ratio >= RELISTING_GUARD_RATIO:
        print(
            f"::error::relisting chain-storm guard tripped for {portal}: this run's "
            f"detector matched {matched_count}/{total_tracked} tracked listings "
            f"({ratio:.1%}) as simultaneous delisted-then-relisted pairs - past both "
            f"the {RELISTING_GUARD_ABS}-absolute and {RELISTING_GUARD_RATIO:.0%}-of-"
            f"backlog guard thresholds (calibrated from real per-run history - see "
            f"RELISTING_GUARD_ABS's own comment in geo_utils.py). This is not real "
            f"relisting behavior; it is the signature of stale/broken upstream data "
            f"being misread as a mass relisting event (docs/backlog.md's 2026-09-24 "
            f"scrape.yml commit-failure incident). Skipping chain-injection for "
            f"{portal} this run rather than silently injecting {matched_count} "
            f"synthetic snapshots - investigate why so much of this portal's backlog "
            f"crossed GONE_AFTER in one pass before the next run."
        )
        return True
    return False


# --- City-key derivation - shared by every scraper's own compute_leads()
# (for area averages) and sync_to_supabase.py's cross-portal merge/city
# filter. Used to live only in sync_to_supabase.py; moved here so a
# scraper doesn't need its own separate copy (import sync_to_supabase.py
# from a scraper would be backwards - this module already flows the other
# way) and so there is exactly one implementation to keep correct, not two
# that can quietly drift apart.
#
# sales.bcpea.org's own listing-type lookup lives here too, since
# listing_city_key() needs it for that portal's settlement-from-title
# extraction (bcpea has no separate "city" field - the settlement name IS
# the title, once the type prefix is stripped).
BCPEA_RAW_TYPES = [
    ("flat", ["Едностаен апартамент", "Двустаен апартамент", "Тристаен апартамент",
              "Многостаен апартамент", "Мезонет", "Ателие, Таван", "Стая"]),
    ("house", ["Вила", "Етаж от къща", "Къща", "Жилищна сграда", "Къща с парцел"]),
    ("land", ["Парцел", "Земеделска земя", "Земеделски имот", "Парцел с къща"]),
    ("garage", ["Гараж", "Паркомясто"]),
    ("shop", ["Магазин", "Заведение"]),
    ("business", ["Офис", "Склад", "Фабрика", "Хотел", "Търговски имот",
                  "Производствен имот", "Бензиностанция", "Газстанция", "Автомивка"]),
]
# Longest raw type first, so a type string that's a prefix of another (e.g.
# "Къща" vs "Къща с парцел") always resolves to the more specific one.
BCPEA_TYPE_LOOKUP = sorted(
    ((raw_type, key) for key, raw_types in BCPEA_RAW_TYPES for raw_type in raw_types),
    key=lambda pair: -len(pair[0]),
)


def bcpea_type_match(title):
    if not title:
        return None
    for raw_type, key in BCPEA_TYPE_LOOKUP:
        if title.startswith(raw_type):
            return raw_type, key
    return None


def bcpea_settlement_from_title(title):
    match = bcpea_type_match(title)
    if match:
        raw_type, _ = match
        rest = title[len(raw_type):]
        return re.sub(r"^,\s*", "", rest).strip() or None
    # "Други" ("Other") is a real bcpea.org category label outside
    # BCPEA_RAW_TYPES' controlled vocabulary - deliberately NOT added there,
    # since bcpea_type_match() returning None for it is exactly what makes
    # type_filter_bucket() correctly bucket these listings "other" (backlog
    # item 21). But the settlement name still follows the same
    # "<category label>, <settlement>" shape every other bcpea title uses
    # (e.g. "Други, Брезово"), so settlement extraction alone needs its own
    # narrow handling of this one label to not lose the location signal.
    if title and title.startswith("Други"):
        rest = title[len("Други"):]
        return re.sub(r"^,\s*", "", rest).strip() or None
    return None


BG_CITIES = [
    ("sofia", "София"), ("plovdiv", "Пловдив"), ("varna", "Варна"), ("burgas", "Бургас"),
    ("ruse", "Русе"), ("stara_zagora", "Стара Загора"), ("pleven", "Плевен"), ("sliven", "Сливен"),
    ("dobrich", "Добрич"), ("shumen", "Шумен"), ("pernik", "Перник"), ("haskovo", "Хасково"),
    ("yambol", "Ямбол"), ("pazardzhik", "Пазарджик"), ("blagoevgrad", "Благоевград"),
    ("veliko_tarnovo", "Велико Търново"), ("vratsa", "Враца"), ("gabrovo", "Габрово"),
    ("vidin", "Видин"), ("asenovgrad", "Асеновград"), ("kazanlak", "Казанлък"),
    ("kyustendil", "Кюстендил"), ("kardzhali", "Кърджали"), ("montana", "Монтана"),
    ("dimitrovgrad", "Димитровград"), ("targovishte", "Търговище"), ("lovech", "Ловеч"),
    ("silistra", "Силистра"), ("dupnitsa", "Дупница"), ("svishtov", "Свищов"),
]
BG_CITY_BY_NAME = {name: key for key, name in BG_CITIES}


# 2026-09-27 (Placy, Sofia/Plovdiv count investigation): Sofia is the only
# one of these 30 cities with real, distinct villages formally absorbed
# into its own municipality (Stolichna) that still go by their own
# settlement name in scraped listing text (e.g. "с.Владая", "Войнеговци,
# София") rather than "София" itself - every other BG_CITIES entry's own
# surrounding villages are a genuinely separate town/municipality, not a
# quarter of the city itself, so this asymmetry is real, not a
# special-casing shortcut (see this file's own city_key_from_name() comment
# for the parallel "София област is a different oblast" asymmetry). A
# listing naming only the village (no "София" anywhere in city/title) was
# real-world-live-confirmed to resolve to city_key=None while an identical
# listing of the SAME real property on another portal (whose own city field
# says "София" directly) resolves to "sofia" - blocking group_listings()'s
# cross-portal merge (its hard city_key-must-agree gate can't merge "sofia"
# with None) and showing as an un-mergeable duplicate card, plus silently
# undercounting Sofia's own city-level listing count.
#
# Reuses data/bg_settlements_to_oblast.json - the SAME already-audited,
# ambiguous-name-excluded gazetteer backlog item 4 built (see
# sync_to_supabase.py's own BG_SETTLEMENT_TO_OBLAST comment for the full
# ambiguity-exclusion process) - filtered to just the names that dataset
# already resolved to oblast "sofia_grad" (Sofia city's own single-
# municipality oblast), minus "София" itself (handled separately, above).
# Deliberately NOT a new, separately-maintained list: any settlement name
# that dataset's own automatic ambiguity check ever excludes (spans more
# than one oblast) never reaches this set either, so this can't introduce
# a new guessed/ambiguous mapping of its own - it only ever narrows an
# already-vetted single-oblast set down to city-key granularity.
# Purely additive: only ever fills in city_key when it would otherwise be
# unresolved (None) below - never overrides an existing field/title match,
# the same "additive only" rule BG_SETTLEMENT_TO_OBLAST itself follows.
_SOFIA_SATELLITE_SETTLEMENTS_PATH = Path(__file__).parent / "data" / "bg_settlements_to_oblast.json"


def _load_sofia_satellite_settlements():
    if not _SOFIA_SATELLITE_SETTLEMENTS_PATH.exists():
        return frozenset()
    data = json.loads(_SOFIA_SATELLITE_SETTLEMENTS_PATH.read_text(encoding="utf-8"))
    return frozenset(name for name, oblast in data.items() if oblast == "sofia_grad" and name != "София")


SOFIA_SATELLITE_SETTLEMENTS = _load_sofia_satellite_settlements()

# Portals prefix a settlement-type label onto the name itself ("с.Владая" -
# village, "кв. Курило" - quarter) - same normalization principle
# city_key_from_name() already applies for its own trailing-suffix case,
# mirrored here for this leading-prefix shape (matches
# sync_to_supabase.py's own _MUNICIPALITY_PREFIX_RE; duplicated rather than
# imported since sync_to_supabase.py imports FROM this module, not the
# other way around - see this module's own top-of-file comment on that
# one-directional import architecture).
_SETTLEMENT_PREFIX_RE = re.compile(r"^[\s,]*(?:гр\.?|с\.?|кв\.?|ж\.?к\.?)\s*", re.IGNORECASE)


def _sofia_satellite_city_key(*values):
    for value in values:
        if not value:
            continue
        normalized = _SETTLEMENT_PREFIX_RE.sub("", value.strip()).strip()
        if normalized in SOFIA_SATELLITE_SETTLEMENTS:
            return "sofia"
    return None


def city_key_from_name(name):
    if not name:
        return None
    # Sofia is the one BG_CITIES name where this "strip a trailing 'област'"
    # normalization below is actively wrong, not just a no-op: "София
    # област" (Sofia Province/Софийска област) is a REAL, DIFFERENT oblast
    # from Sofia city itself (city_key "sofia" -> oblast "sofia_grad";
    # Sofia Province is oblast "sofia" - see BG_OBLASTS/CITY_KEY_TO_OBLAST's
    # own comments for the same sofia/sofia_grad split). Every other
    # BG_CITIES name's own oblast happens to share that city's exact name
    # (e.g. Plovdiv city sits in an oblast that's also just called
    # "Пловдив"), so stripping "област" there is harmless - only Sofia has
    # two differently-named oblasts where blindly stripping "област" turns
    # a real reference to the SURROUNDING region into a false match for the
    # CAPITAL city. Confirmed live: 67 imoti.bg listings literally tagged
    # city="София област" (backlog item 20-22-adjacent audit) were all
    # resolving to sofia_grad instead of the correct "sofia" (province)
    # oblast before this fix.
    stripped = name.strip()
    if re.match(r"^софия\s*област$", stripped, flags=re.IGNORECASE):
        return None
    # Strips a trailing settlement-type suffix a portal's own title text can
    # tack on after the real city name - "област" (region), or homes.bg's
    # own "<City> - град"/"- село" (town/village) convention, live-sampled
    # from real currently-active homes.bg titles like "София, София - град"
    # (the second "София" is the last comma segment the title fallback
    # reads, but " - град" made it fail to match "София" exactly).
    normalized = re.sub(r"\s*(?:област|-\s*град|-\s*село)$", "", stripped, flags=re.IGNORECASE).strip()
    return BG_CITY_BY_NAME.get(normalized)


# alo.bg's title has "<area>, <city>" but sometimes runs the price straight
# into the city with no separating comma - "...Дианабад, София Цена : 480
# 000 €" - so the last comma segment is "София Цена : 480 000 €", not
# "София" alone, and the exact match above fails even though the city name
# is right there. Live-sampled: every currently-active alo.bg listing with
# a null city_key that still had a comma in its title matched this shape.
# Longest names first so "Стара Загора" doesn't prefix-match as "Стара"
# alone (not a real entry, but keeps the general principle safe).
BG_CITY_PREFIX_RE = re.compile(
    r"^(" + "|".join(re.escape(name) for _, name in sorted(BG_CITIES, key=lambda c: -len(c[1]))) + r")\b"
)


def city_key_from_name_prefix(name):
    if not name:
        return None
    stripped = name.strip()
    # Same Sofia-specific guard as city_key_from_name() above - a name
    # starting with "София област" must not prefix-match "София" the city.
    if re.match(r"^софия\s*област\b", stripped, flags=re.IGNORECASE):
        return None
    normalized = re.sub(r"\s*(?:област|-\s*град|-\s*село)$", "", stripped, flags=re.IGNORECASE).strip()
    match = BG_CITY_PREFIX_RE.match(normalized)
    return BG_CITY_BY_NAME.get(match.group(1)) if match else None


# imoti.net's own titles render the city in English/Latin script ("... Sofia,
# Lyulin Center" - the city is the SECOND-to-last comma segment there, not
# the last, so the generic last-comma fallback above can never recover it).
# This bit imoti.net hardest: a live sample of currently-active,
# freshly-scraped imoti.net listings with no "city" field found ~5,800 of
# them (28% of all active merged listings, and the single largest unmatched
# bucket of any portal) were genuinely Sofia listings whose title plainly
# says so in Latin script - e.g. "Shop, 44 m2 Sofia, Lyulin Center". Reuses
# the same slugs scraper.py's own CITY_SLUGS already live-verified against
# imoti.net's real city pages, just keyed by the natural-language spelling
# (space, not the URL slug's hyphen) since this searches free-form title
# text, not a URL.
LATIN_CITY_TO_KEY = {
    "sofia": "sofia", "plovdiv": "plovdiv", "varna": "varna", "burgas": "burgas", "bourgas": "burgas",
    "ruse": "ruse", "stara zagora": "stara_zagora", "pleven": "pleven", "sliven": "sliven",
    "dobrich": "dobrich", "shumen": "shumen", "pernik": "pernik", "haskovo": "haskovo",
    "yambol": "yambol", "pazardzhik": "pazardzhik", "blagoevgrad": "blagoevgrad",
    "veliko tarnovo": "veliko_tarnovo", "vratsa": "vratsa", "gabrovo": "gabrovo", "vidin": "vidin",
    "kardzhali": "kardzhali", "montana": "montana", "targovishte": "targovishte", "lovech": "lovech",
    "silistra": "silistra",
}
LATIN_CITY_RE = re.compile(
    r"\b(" + "|".join(sorted((k.replace(" ", r"\s+") for k in LATIN_CITY_TO_KEY), key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)


def latin_city_key_from_text(text):
    if not text:
        return None
    match = LATIN_CITY_RE.search(text)
    if not match:
        return None
    normalized = re.sub(r"\s+", " ", match.group(1).lower())
    return LATIN_CITY_TO_KEY.get(normalized)


# bazar.bg's own title format has the same "city buried mid-string, not in
# the last comma segment" problem as imoti.net, just in Cyrillic: "Продава
# 3-СТАЕН, гр. София, Левски Г" - the city is the "гр. <City>" segment in
# the middle, area (last segment) is the neighborhood. A live sample of
# currently-active bazar.bg listings with no "city" field (stale rows
# scraped before bazar.bg's nationwide city-tagging merged today) confirmed
# this "гр. <City>," shape holds consistently, matching bazar.bg's own
# AREA_LINE_RE ("^гр\.\s*\S.*?,\s*(.+)$") which already relies on the same
# "гр. " prefix convention to find the area line at all.
CYR_CITY_TITLE_RE = re.compile(
    r"гр\.?\s*(" + "|".join(re.escape(name) for _, name in BG_CITIES) + r")"
)


def cyr_city_key_from_text(text):
    if not text:
        return None
    match = CYR_CITY_TITLE_RE.search(text)
    if not match:
        return None
    return BG_CITY_BY_NAME.get(match.group(1))


def _title_derived_city_key_strict(title):
    # The structured half of _title_derived_city_key() below: a title
    # formatted "<description>, <City>" (the shape every portal that
    # ever motivated the "title wins" override in listing_city_key() below
    # actually has) reliably names the real city in its own last comma
    # segment. Kept separate because this is the ONLY half of title-derived
    # resolution trustworthy enough to override an already-present, valid
    # "city" field - see listing_city_key()'s own comment for the evidence
    # this split is based on.
    if not title or "," not in title:
        return None
    last_segment = title.rsplit(",", 1)[1].strip()
    key = city_key_from_name(last_segment)
    if key:
        return key
    return city_key_from_name_prefix(last_segment)


def _title_derived_city_key(title):
    # The title-only half of listing_city_key()'s resolution, kept separate
    # so listing_city_key() can compute it independently of the "city"
    # field and compare the two - see that function's own comment for why.
    # Used only to RECOVER a city when the field itself is missing/
    # unresolvable - the loose latin_city_key_from_text()/
    # cyr_city_key_from_text() "anywhere in the title" fallbacks below are
    # deliberately NOT trusted enough to override an already-present field
    # value (see _title_derived_city_key_strict() above and
    # listing_city_key()'s own comment) - only to fill in a real gap.
    if not title:
        return None
    key = _title_derived_city_key_strict(title)
    if key:
        return key
    key = latin_city_key_from_text(title)
    if key:
        return key
    return cyr_city_key_from_text(title)


def listing_city_key(l):
    # Ported 1:1 from index.html's listingCityKey() - see that function's
    # comment for the full story (this used to unconditionally return
    # "sofia" for every non-bcpea portal, silently miscounting every real
    # non-Sofia listing from homes.bg/imoti.bg as Sofia).
    if l.get("portal") == "sales.bcpea.org":
        settlement = bcpea_settlement_from_title(l.get("title"))
        return city_key_from_name(settlement) if settlement else None

    title = l.get("title")

    city = l.get("city")
    field_key = None
    if city:
        field_key = city_key_from_name(city)
        if not field_key:
            field_key = city_key_from_name_prefix(city)

    # A live report found a listing whose "city" field disagreed with its
    # own title (a stale/wrong scrape-time field vs. a title formatted
    # "<description>, <City>" that plainly, unambiguously names a different
    # real city in its own last comma segment) - trusting the field
    # unconditionally let two listings that don't actually share a city
    # slip through a downstream cross-portal match on area+price alone
    # (see group_listings()'s own comment). The title is the thing a human
    # reader would trust in that situation, so when both resolve and
    # disagree via this STRUCTURED signal, the title wins.
    #
    # 2026-09-23 (Placy, full-population allocation audit): narrowed this
    # override from "any title-derived key" to only the structured
    # comma-segment signal above, after live-sampling every real
    # field-vs-title cross-OBLAST disagreement nationwide (32 active
    # listings) and finding 31 of 32 were the loose latin_city_key_from_text()/
    # cyr_city_key_from_text() "anywhere in the title" fallbacks matching a
    # city name that ISN'T the listing's own real location at all - e.g.
    # alo.bg's own titles are scraped as "<Agency Name> преди N дни <real ad
    # title>" (a real, common shape - roughly 73-76% of active alo.bg
    # titles under a couple of reasonable regexes, but alo.bg's own titles
    # are visibly left-truncated in the scraped data, so any single precise
    # percentage here overstates how exactly this is known - a Missy review
    # independently reproducing this got anywhere from 27.5% to 86.3%
    # depending on matching strictness; the shape itself, and the concrete
    # per-record fix below, don't depend on the exact prevalence figure),
    # and an agency literally named "Varna North Properties" made
    # every listing it manages elsewhere in Bulgaria (15 confirmed active:
    # real Dobrich-oblast coastal towns - Балчик/Каварна/Топола) wrongly
    # override a correct city="Добрич" field with "varna" (the agency's own
    # name, not the property's location) - 7 of those currently have no
    # lat/lng to be rescued by geo (which always wins over city_key when
    # present), so were live-resolving to the WRONG oblast in production
    # today. Same shape independently confirmed on olx.bg (12 cases - e.g.
    # a real Sofia listing, city="София", genuine Sofia district "Хладилника",
    # whose own title text contains an unrelated "гр. Пловдив" fragment,
    # likely a multi-branch agency's mistemplated ad) and bazar.bg (3 of its
    # 4 cross-oblast cases). Only 1 of the 32 came from the structured
    # comma-segment method (a bazar.bg title "2-стаен апартамент кк.Камчия,
    # Варна" with a wrong city="Габрово" field - independently confirmed via
    # k.k. Kamchia being a real Varna-oblast coastal resort, unrelated to
    # inland Габрово - exactly the shape the original override was designed
    # for), so the structured method is kept as override-authoritative and
    # the loose methods are demoted to fill-a-gap-only (see
    # _title_derived_city_key() above), not discarded entirely.
    title_key_strict = _title_derived_city_key_strict(title)
    if field_key and title_key_strict and field_key != title_key_strict:
        return title_key_strict
    title_key = _title_derived_city_key(title)
    if field_key or title_key:
        return field_key or title_key
    # Fill-a-gap-only, same rule as the loose title fallbacks above: only
    # runs once every other signal has already failed to resolve anything -
    # see SOFIA_SATELLITE_SETTLEMENTS' own comment for why this is scoped to
    # Sofia specifically and can't introduce a new ambiguous guess.
    return _sofia_satellite_city_key(city, l.get("area"))


# --- Motivation score (backlog item, reworked after relisting detection was
# built) ------------------------------------------------------------------
#
# A single shared implementation, unlike most of this project's per-scraper
# compute_leads() logic (deliberately duplicated there - see e.g.
# scraper.py's own comments - because those tweaks are a few inline lines
# woven into each scraper's own loop). This is different: a self-contained
# function with plain inputs and no scraper-specific state, exactly the
# kind of logic this module already centralizes for the same reason
# listing_city_key() lives here instead of copy-pasted 8 times - one
# implementation that can't drift, not eight that could.
#
# Original formula (still what every scraper computes today, before this
# rework ships) was two inputs only: min(drop_pct,0..20)/20*50 +
# min(days_on_market,0..180)/180*50. Reworked per direct user request once
# relisting detection existed to feed it: five inputs, weighted and capped
# from real distributions measured against the live committed dataset
# (all 8 portals, deduped through the same merge sync_to_supabase.py's
# group_listings() does) - not guessed. See each cap's own comment for the
# specific percentile that justified it.
#
# Matches index.html's own UNVERIFIED_PCT_THRESHOLD (computeUnverified()) -
# a price/m² this far from its area average is flagged there as a likely
# data error rather than a real bargain, and the same judgment applies
# here: an "unverified" pct_vs_area_avg must not earn motivation-score
# points, or a parsing error could outscore a genuine below-market deal.
UNVERIFIED_PCT_THRESHOLD = 60

# Below this many points, the caller couldn't compute a real distinct-
# reduction/drop-size/days-on-market picture at all (see the docstring on
# compute_motivation_score for when that happens) - not currently used to
# gate anything here, kept only as a named constant so the 85/100 rescale
# factor below has a name instead of a bare magic number.
_RESCALE_WITHOUT_AREA_AVG = 85


def _relisting_score_component(price_history):
    """Up to 25 points: 10 base for having been delisted and relisted at
    all (a real, behavioral "this deal stalled and the ad restarted"
    event - the strongest single signal this formula has, per the user's
    own framing), plus up to 15 more scaled to how much cheaper it came
    back. Real data: of 112 relistings where both the old and new price
    are known, 74% came back lower, median cut 3.9%, 90th percentile
    17.5% - capped at a 15% price cut for full marks on the bonus, so it's
    reachable by genuinely large capitulations without needing an extreme
    outlier to max out. A relisting at the same or a higher price still
    earns the 10-point base (something changed enough for the ad to be
    pulled and restarted) but no bonus.

    price_history is chronological (see every compute_leads()'s own
    price_history construction) - a listing relisted more than once uses
    its LAST (most recent) tagged event, the freshest signal, rather than
    whichever event happened to produce the highest score.
    """
    before = after = None
    for entry in price_history:
        if not isinstance(entry, dict) or entry.get("source") != "relisted_from":
            continue
        before = entry.get("price_eur")
        after = entry.get("came_back_price")
    if before is None:
        return 0
    points = 10
    if before and after and after < before:
        drop_on_relist = (before - after) / before * 100
        points += min(drop_on_relist, 15) / 15 * 15
    return points


def compute_motivation_score(drop_pct, price_drop_count, days_on_market, pct_vs_area_avg, price_history):
    """The five components, each capped, summed, and rescaled to 0-100.

    Every component except the area-average one is always computable - a
    listing that was never reduced or never relisted legitimately scores 0
    there, which isn't missing data, it's a real answer. pct_vs_area_avg is
    the one genuine gap: None when the listing (or too few of its area
    peers) has no sqm/price_per_sqm to compare, and treated as unusable
    (not just missing, actively excluded) when it's flagged unverified -
    see UNVERIFIED_PCT_THRESHOLD above. When that component isn't usable,
    the other four are rescaled from their 0-85 raw sum up to 0-100 rather
    than leaving every affected listing capped below the real top end -
    the deliberate "rescale" choice over "cap at 85", made because the gap
    is a data-collection artifact (alo.bg in particular has sparse sqm
    coverage) rather than a real signal about the listing, and capping
    would systematically punish whichever portals/areas happen to have
    thinner coverage rather than reflecting anything about the property
    itself.
    """
    relist_pts = _relisting_score_component(price_history)
    drop_count_pts = min(price_drop_count or 0, 3) / 3 * 20  # real p90=1, p95=2, p99=3 among ever-reduced listings - a cap of 4+ would be unreachable
    drop_pct_pts = min(max(drop_pct or 0, 0), 20) / 20 * 20  # unchanged cap from the original formula - real data confirms it still sits near the ~96th percentile of reduced listings
    days_pts = min(days_on_market or 0, 180) / 180 * 20  # unchanged cap - kept for when tracking history matures rather than fitted to today's artificially compressed (~23-28 day) distribution

    area_available = pct_vs_area_avg is not None and abs(pct_vs_area_avg) < UNVERIFIED_PCT_THRESHOLD
    area_pts = 0
    if area_available and pct_vs_area_avg < 0:
        area_pts = min(abs(pct_vs_area_avg), 40) / 40 * 15  # real data (verified listings only): cap sits near the 90th percentile of below-average listings

    raw_total = relist_pts + drop_count_pts + drop_pct_pts + days_pts + (area_pts if area_available else 0)
    denom = 100 if area_available else _RESCALE_WITHOUT_AREA_AVG
    return round(raw_total * 100 / denom)
