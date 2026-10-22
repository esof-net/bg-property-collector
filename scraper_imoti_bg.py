"""
Scrapes current for-sale listings from imoti.bg, nationwide, across all 6
property categories (flat/house/land/garage/shop/business) - the first
scraper converted for the nationwide expansion (see category_classifier.py
and the Supabase migration this builds on). Picked as the first portal to
convert because it's the smallest today (71 Sofia listings vs. thousands
on every other portal), so any pagination/field-extraction mistakes here
are cheap to find and fix before applying the same approach to the much
larger portals.

imoti.bg's homepage location filter is a select2 widget wrapping a hidden
native <select>, with the actual URL built client-side on search-button
click - there's no separate API call to intercept. But the resulting
filtered URL turned out to be stateless/bookmarkable, confirmed live via
plain requests with no need to repeat the dropdown-click simulation.
Removing the location filter entirely (di:софия) gives the nationwide,
all-cities URL; the old Sofia-only scraper was already scoping down to
apartments only after the fact (APARTMENT_SLUGS) even though the search
itself already mixed every property type - so going nationwide-and-
all-categories only means removing two filters that were already just
being discarded downstream, not a new capability the site lacks.

Each listing's permalink URL embeds both its property-type slug (e.g.
"тристаен-апартамент", "къща", "парцел", "гараж") and its city slug
(e.g. "софия", "пловдив", "варна") - LISTING_LINK_RE now captures both
generically instead of the old Sofia-only, apartment-only enum, since
every city and category shares the same URL shape on this site.

Each listing card is the smallest ancestor whose text mentions the price
("<amount> EUR") exactly once, same "climb from the link" approach as the
other portals' scrapers. A page fetch retries a few times with backoff
before being treated as the end of pagination, so a transient failure
doesn't get mistaken for having reached the last page.

Nationwide expansion also requires fields the Sofia-only card-only scrape
never captured: a full description and a real listing-posted date, both
only available on each listing's own detail page - fetch_listing_detail()
adds one extra request per listing for these (same pattern already used
by scraper_alo.py for site_updated_at/coordinates). This is the first
portal to pay that cost nationwide; whether the same approach scales to
the much larger portals (imoti.net, alo.bg, bazar.bg - tens of thousands
of listings each) is exactly what the staged rollout is meant to surface
before committing to it there.

fetch_listing_detail() (extended 2026-09-24) also pulls spec fields (sqm/
property type/features, incl. has_elevator/furnished/has_central_heating
derived from those features) and agency contact info (name + real
external website, never a phone number) out of the SAME application/
ld+json block its description extraction already successfully parses -
see its own docstring and geo_utils.extract_specs_imoti_bg()/
extract_contact_imoti_bg() for exactly which schema.org fields are read
and why. construction type/built year/completion status/floor number/
floor qualifier are deliberately NOT extracted: no core schema.org
vocabulary covers them, so there's no real evidence of where (or
whether) they'd live in this site's own ld+json - see
extract_specs_imoti_bg()'s own comment for what a future contributor
with live access should check first. No new photo or coordinate
extraction was added alongside these: this page embeds only one photo
total (already captured as "photo", see fetch_listing_detail()'s own
docstring) and carries no coordinates of its own anywhere (every listing
is already geocoded from area/city text at grid-crawl time below) - both
confirmed dead ends, not gaps this change left open.

imoti.bg genuinely carries no coordinates anywhere in its own pages
(confirmed by a real headless browser finding no map DOM node/object/
iframe, and a plain static fetch finding nothing either), so each
listing's area name is geocoded via OpenStreetMap Nominatim instead
(geo_utils.Geocoder), cached by query string.

fetch_listings_page() originally called Geocoder.geocode() directly - a
live, blocking Nominatim request on every cache miss. That was fine
Sofia-only (a few hundred distinct neighborhood names, one-time cost),
but a real nationwide production run (started before this fix existed)
took over 4 hours instead of its usual ~35 minutes - nationwide means far
more distinct city/area combinations, so most calls were live round-trips
rather than cache hits, the identical failure mode later confirmed and
fixed the same way for scraper_homes.py's own nationwide rewrite (see
its module docstring for the full story). Fixed the same way here:
fetch_listings_page() now does a cache-only lookup
(Geocoder.geocode_cached_only - no network call); backfill_geocode_imoti_bg.py
+ its workflow do the live, rate-limited lookups as a separate, resumable
pass. The "city" field is now persisted alongside "area" specifically so
that backfill script can rebuild the exact same query string later
without needing to re-derive it from the page HTML.
"""

import re
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from geo_utils import (
    Geocoder, compute_motivation_score, extract_contact_imoti_bg, extract_specs_imoti_bg,
    imoti_bg_ld_json_diagnostic, listing_city_key, prune_snapshots, evict_stale_records,
    STALE_RECORD_RETENTION,
)
from category_classifier import classify_listing

BASE_URL = "https://imoti.bg"
SEARCH_URL = "https://imoti.bg/продажби/cu:BGN"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
    "Accept-Language": "bg-BG,bg;q=0.9,en;q=0.8",
}

OUT_DIR = Path(__file__).parent / "data"
OUT_DIR.mkdir(exist_ok=True)
HISTORY_FILE = OUT_DIR / "history_imoti_bg.json"
LEADS_FILE = OUT_DIR / "leads_imoti_bg.json"

MAX_CARD_TEXT_LENGTH = 500
MAX_PRICE_MENTIONS = 1
# Nationwide, all-categories replaces the old Sofia-only MAX_PAGES=12 cap -
# stop condition is an empty page (see fetch_listings()), not a fixed cap,
# consistent with the "full pagination, no caps" requirement.
MAX_PAGES = 5000
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 5
REQUEST_DELAY_SECONDS = 0.5
MAX_CONSECUTIVE_PAGE_FAILURES = 5

# Captures (category_slug, city_slug, title_slug, id) generically - the old
# regex only matched a hardcoded apartment-slug enum under /софия/. Every
# city and every property-type slug shares this same URL shape.
LISTING_LINK_RE = re.compile(r"/продажби/([^/]+)/([^/]+)/([^/]+)-(\d{5,})\.htm")
PRICE_RE = re.compile(r"[\d\s]{3,10}\s?EUR")
PRICE_LINE_RE = re.compile(r"^([\d\s]{3,10})\s?EUR$")
# A card's location line is "<City>, <area>" - genuinely any city now, not
# just "София," - split on the first comma rather than anchoring to Sofia.
LOCATION_LINE_RE = re.compile(r"^([^,]+),\s*(.+)$")
SQM_RE = re.compile(r"([\d.,]+)\s?кв\.?м\.?")

# Common Bulgarian real-estate-site date-label patterns for a listing's own
# "posted on" date, tried against each detail page - same idea as
# scraper_alo.py's UPDATED_TEXT_RE. Not yet confirmed against imoti.bg's
# real detail-page markup (this sandbox has no network route to verify
# live) - first live run (see the nationwide-rollout verification workflow)
# confirms whether this actually matches, and gets corrected if not.
POSTED_DATE_RE = re.compile(
    r"(?:Публикувана|Публикувано|Дата на публикуване|Създадена)[^\d]{0,20}"
    r"(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{2,4})"
)


def fetch_html(url):
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.get(url, headers=HEADERS, timeout=20)
            r.raise_for_status()
            return r.text
        except requests.RequestException as e:
            print(f"DEBUG: request failed for {url} (attempt {attempt}/{MAX_RETRIES}): {e}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)
    return None


def smallest_container_with_price(link_tag, max_levels=9):
    # imoti.bg's price sits in a deeper single-purpose element than the area
    # line, so unlike some other scrapers the first ancestor with exactly
    # one price mention is too small to also contain the area - keep
    # climbing and return the largest ancestor that still satisfies the
    # constraints.
    node = link_tag
    best = None
    for _ in range(max_levels):
        if node.parent is None:
            break
        node = node.parent
        text = node.get_text(" ", strip=True)
        matches = PRICE_RE.findall(text)
        if len(matches) > MAX_PRICE_MENTIONS:
            break
        if 1 <= len(matches) <= MAX_PRICE_MENTIONS and len(text) <= MAX_CARD_TEXT_LENGTH:
            best = node
    return best


def fetch_listing_detail(url):
    """Best-effort description + posted-date + specs + agency contact from a
    listing's own page. Returns (description, site_posted_at_iso, specs,
    contact) - description/site_posted_at may be None, specs/contact may be
    None or a partial dict (whatever fields were actually found), never
    raises (a missing field shouldn't drop an otherwise-good listing).

    specs/contact (added 2026-09-24, root-caused 2026-09-25 after a
    production audit found them at 0% - see geo_utils.py's own comment
    above _imoti_bg_ld_json_candidates() for the full story) call
    geo_utils.extract_specs_imoti_bg()/extract_contact_imoti_bg(), which
    parse this page's own application/ld+json block(s) independently of the
    description extraction below - NOT "the exact same already-parsed
    block" a previous version of this comment claimed. The description
    extraction below tries a <meta name="description"> tag FIRST and only
    ever falls back to ld+json when that tag is missing/short, so its high
    hit rate is not evidence ld+json parsing itself works here. See
    geo_utils.extract_specs_imoti_bg()'s own comment for exactly which
    schema.org fields are read and why (and which fields were deliberately
    left out for lack of any real evidence of where they'd live on this
    site).

    UPDATE 2026-09-28: what was "genuinely unverified" about this page's
    real ld+json shape is now settled, via real production log evidence
    (scrape.yml run 36369847151, no live dispatch needed - see geo_utils.py's
    own comment for the exact numbers): these pages carry NO
    application/ld+json block at all, so specs/contact will keep coming up
    empty here until a real DOM/text-based extractor replaces this ld+json
    approach entirely - see _log_ld_json_miss()'s own updated comment below
    for what a future fix needs next.

    Does not extract a photo gallery: confirmed live via probe_photos.py/
    probe_photos_round2.py (one land-parcel and one apartment sample) that
    imoti.bg's own detail page embeds only one photo (as "large"/"small"
    size variants of the same image, both under /assets/offers/) - a
    genuine per-portal limitation, same as imoti.net's missing description
    (see backfill_detail_imoti_net.py's docstring). The single photo it
    does have is already captured below as "photo".

    Also does not add any new coordinate source: fetch_listings_page()
    already geocodes every listing from its area/city text at grid-crawl
    time (Geocoder.geocode_cached_only(), see this module's own docstring) -
    imoti.bg carries no coordinates of its own anywhere on the page, so
    there's nothing for a detail-page extractor to add here."""
    html = fetch_html(url)
    if html is None:
        return None, None, None, None

    description = None
    soup = BeautifulSoup(html, "html.parser")
    meta = soup.find("meta", attrs={"name": "description"}) or soup.find("meta", attrs={"property": "og:description"})
    if meta and meta.get("content"):
        content = meta["content"].strip()
        # A generic site-wide meta description (not this listing's own
        # text) is worse than none - short boilerplate rarely exceeds a
        # few words, a real per-listing description is a real paragraph.
        if len(content) > 40:
            description = content
    if description is None:
        for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
            text = script.string or ""
            try:
                data = json.loads(text)
            except (ValueError, TypeError):
                # Same strict=False retry as geo_utils._parse_ld_json_blocks()
                # (see its own comment) - a raw control character inside a
                # JSON string is a common real-world JSON-LD quirk, not
                # necessarily a reason to give up on this block entirely.
                try:
                    data = json.loads(text, strict=False)
                except (ValueError, TypeError):
                    continue
            candidates = data if isinstance(data, list) else [data]
            for c in candidates:
                if isinstance(c, dict) and isinstance(c.get("description"), str) and len(c["description"]) > 40:
                    description = c["description"].strip()
                    break
            if description:
                break

    site_posted_at = None
    m = POSTED_DATE_RE.search(html)
    if m:
        day, month, year = m.groups()
        year = int(year) if len(year) == 4 else 2000 + int(year)
        try:
            site_posted_at = datetime(year, int(month), int(day), tzinfo=timezone.utc).isoformat()
        except ValueError:
            site_posted_at = None

    specs = extract_specs_imoti_bg(html)
    contact = extract_contact_imoti_bg(html)

    if not specs and not contact:
        _log_ld_json_miss(url, html)

    return description, site_posted_at, specs, contact


# Populated by _log_ld_json_miss() below - module-level so it caps/dedupes
# across every listing in one process run (each scheduled run is a fresh
# process, so this naturally resets run to run), same pattern
# scraper_bcpea.py's own _seen_unrecognized_labels tripwire already
# established for an analogous "confirm a production assumption for real,
# next time this scraper actually runs" need.
_LD_JSON_MISS_LOG_CAP = 5
_ld_json_miss_logged = 0


def _log_ld_json_miss(url, html):
    """Zero-cost visibility into WHY specs/contact came up empty for this
    listing - logged for at most _LD_JSON_MISS_LOG_CAP listings per run (not
    every listing; at nationwide scale this would otherwise spam the run's
    logs for what's very possibly the same root cause every time). See
    geo_utils.py's own comment above _imoti_bg_ld_json_candidates() for why
    this diagnostic exists: distinguishing "no ld+json on this page at all"
    from "ld+json is there but not the shape assumed" from "ld+json is there
    but fails to parse" is exactly what a future real fix needs.

    UPDATE 2026-09-28: that distinction is now answered for real, via this
    exact diagnostic's own real production output (see geo_utils.py's own
    comment for the full story) - every real, live imoti.bg page sampled so
    far reads `script_tags: 0`, i.e. no ld+json at all, not an unrecognized
    shape. What's still open is what these pages DO carry instead (a plain
    HTML spec table, a non-ld+json JSON state blob, or genuinely nothing
    machine-readable at all) - imoti_bg_ld_json_diagnostic() now also
    reports 3 safe, generic structural facts (any_script_tags,
    script_types_seen, has_table_or_dl) toward answering that on this
    already-scheduled workflow's own next run, without guessing at
    Bulgarian label text sight-unseen."""
    global _ld_json_miss_logged
    if _ld_json_miss_logged >= _LD_JSON_MISS_LOG_CAP:
        return
    _ld_json_miss_logged += 1
    diag = imoti_bg_ld_json_diagnostic(html)
    print(f"DEBUG: imoti.bg specs/contact empty for {url} - {diag}", flush=True)


def fetch_listings_page(url, geocoder):
    html = fetch_html(url)
    if html is None:
        return None
    soup = BeautifulSoup(html, "html.parser")

    all_links = soup.find_all("a", href=True)
    matching_links = [a for a in all_links if LISTING_LINK_RE.search(a["href"])]

    listings = {}
    for a in matching_links:
        match = LISTING_LINK_RE.search(a["href"])
        category_slug, city_slug, title_slug, listing_id = match.groups()
        if listing_id in listings:
            continue

        container = smallest_container_with_price(a)
        if container is None:
            continue

        lines = [l.strip() for l in container.get_text("\n", strip=True).split("\n") if l.strip()]
        if not lines:
            continue

        price_eur = None
        for l in lines:
            m = PRICE_LINE_RE.match(l)
            if m:
                price_eur = int(re.sub(r"\D", "", m.group(1)))
                break
        if price_eur is None or price_eur < 1000:
            continue

        city = city_slug.replace("-", " ").strip().title()
        area = city
        for l in lines:
            m = LOCATION_LINE_RE.match(l)
            if m:
                city = m.group(1).strip() or city
                area = m.group(2).strip() or area
                break

        sqm = None
        for l in lines:
            m = SQM_RE.search(l)
            if m:
                try:
                    sqm = round(float(m.group(1).replace(",", ".")))
                except ValueError:
                    pass
                break

        img_url = None
        for img in container.find_all("img"):
            candidate = img.get("src") or img.get("data-src")
            if candidate and "icons/" not in candidate.lower():
                img_url = urljoin(BASE_URL + "/", candidate)
                break

        href = a["href"]
        full_url = href if href.startswith("http") else urljoin(BASE_URL, href)
        raw_type = lines[1] if len(lines) > 1 else category_slug.replace("-", " ")
        full_title = f"{raw_type}, {area}"[:150]
        coords = geocoder.geocode_cached_only(f"{area}, {city}, България")

        listings[listing_id] = {
            "id": "imotibg_" + listing_id,
            "url": full_url,
            "photo": img_url,
            "price_eur": price_eur,
            "sqm": sqm,
            "area": area,
            "city": city,
            "title": full_title,
            "portal": "imoti.bg",
            "lat": coords["lat"] if coords else None,
            "lng": coords["lng"] if coords else None,
            # category/confidence/reason filled in by fetch_listings() once
            # the detail page's description is available too, since the
            # classifier scores description as a third signal alongside
            # title and URL.
            "_category_slug": category_slug,
        }
    return listings


def fetch_listings():
    all_listings = {}
    geocoder = Geocoder()
    consecutive_failures = 0
    for page in range(1, MAX_PAGES + 1):
        if page > 1:
            time.sleep(REQUEST_DELAY_SECONDS)
        url = SEARCH_URL if page == 1 else f"{SEARCH_URL}/page:{page}"
        page_listings = fetch_listings_page(url, geocoder)
        print(f"DEBUG: page {page} listings found = {len(page_listings) if page_listings else 0}")
        if page_listings is None:
            # A failed fetch (all in-request retries exhausted) is not the
            # real end-of-results signal (an empty dict is) - giving up here
            # would silently truncate every remaining page after one bad
            # request, the same bug found in scraper.py/scraper_alo.py/
            # scraper_imot.py/scraper_homes.py/scraper_olx.py/
            # scraper_bazar.py. Skip it and keep going, only giving up after
            # several failures in a row.
            consecutive_failures += 1
            if consecutive_failures >= MAX_CONSECUTIVE_PAGE_FAILURES:
                break
            continue
        consecutive_failures = 0
        if not page_listings:
            break
        all_listings.update(page_listings)
    geocoder.save()

    low_confidence_count = 0
    low_confidence_reasons = {}
    for l in all_listings.values():
        time.sleep(REQUEST_DELAY_SECONDS)
        description, site_posted_at, specs, contact = fetch_listing_detail(l["url"])
        l["description"] = description
        l["site_posted_at"] = site_posted_at
        # sqm is special-cased like scraper_alo.py's own equivalent merge:
        # the grid crawl's own SQM_RE already sets sqm when a card happens
        # to show "... кв.м" text, so this only fills it in when that came
        # up empty, never overwrites a value the grid crawl already found.
        if specs:
            if specs.get("sqm") and not l.get("sqm"):
                l["sqm"] = specs["sqm"]
            # Only the fields extract_specs_imoti_bg() can actually
            # produce (see its own comment in geo_utils.py) - unlike
            # scraper_alo.py's identically-shaped loop, this deliberately
            # does NOT list construction_type/built_year/completion_status/
            # floor_number/floor_qualifier: no core schema.org vocabulary
            # covers them, so extract_specs_imoti_bg() never sets them,
            # and listing them here would be dead, misleading code -
            # claiming a copy this scraper never actually performs.
            for field in ("property_type_raw", "features", "has_elevator", "furnished", "has_central_heating"):
                if field in specs:
                    l[field] = specs[field]
        # Agency name + real external agency website - deliberately no
        # phone number, see geo_utils.extract_contact_imoti_bg()'s own
        # comment (mirrors extract_contact_alo()'s reasoning).
        if contact:
            for field in ("agency_name", "agency_website"):
                if field in contact:
                    l[field] = contact[field]

        category, confidence, reason = classify_listing(
            title=l["title"], description=description, url=l["url"] + " " + l.pop("_category_slug")
        )
        l["category"] = category
        l["category_confidence"] = confidence
        if confidence == "low":
            low_confidence_count += 1
            low_confidence_reasons[reason] = low_confidence_reasons.get(reason, 0) + 1

    total = len(all_listings)
    print(f"DEBUG: category confidence - {total - low_confidence_count}/{total} high confidence, "
          f"{low_confidence_count}/{total} low confidence {low_confidence_reasons}")

    return list(all_listings.values())


def load_history():
    if HISTORY_FILE.exists():
        return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    return {}


def save_history(history):
    # evict_stale_records() before prune_snapshots() - see its own
    # docstring/geo_utils.py's STALE_RECORD_RETENTION comment (2026-09-25
    # incident: unbounded history/leads growth from never removing
    # long-gone listings). Runs every save, not just as a one-off
    # migration, so history_*.json/leads_*.json (leads via this same
    # `history` object feeding this run's own compute_leads() call right
    # after) stay bounded going forward instead of recurring.
    evicted = evict_stale_records(history)
    if evicted:
        print(f"DEBUG: evicted {evicted} history record(s) not seen in over {STALE_RECORD_RETENTION.days} days")
    prune_snapshots(history)
    HISTORY_FILE.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")


# Fields fetch_listing_detail() supplies on top of what the grid crawl
# (fetch_listings_page()) itself produces. Unlike the other portals'
# scrapers, fetch_listings() here calls fetch_listing_detail() inline for
# EVERY listing on EVERY run (not a separate, one-time backfill pass) -
# but per its own docstring it's best-effort and "never raises... a
# missing field shouldn't drop a listing", so a single transient per-run
# failure (timeout, a missing meta tag/ld+json block that day, a page
# render hiccup) legitimately returns (None, None, None, None) for a
# listing that had real detail-page fields on a previous run. update_history()
# must merge these in from the previous "latest" rather than let a fresh
# record's None wipe an already-captured value - docs/backlog.md item 9a
# (originally fixed in six other scrapers, this one was missed since it
# has the identical bug but wasn't part of that investigation's "all six
# scrapers with this function" premise).
#
# specs/contact fields (added 2026-09-24, same reasoning scraper_alo.py's
# own equivalent list already documents for its identically-shaped merge):
# property_type_raw/features/has_elevator/furnished/has_central_heating/
# agency_name/agency_website are all detail-only (the grid crawl never
# produces them) and best-effort (not every listing's ld+json carries
# every field), so they get the same merge-not-replace protection
# description/site_posted_at already have.
#
# "sqm" IS also listed here, matching what scraper_alo.py's own
# _DETAIL_ONLY_FIELDS actually does (not the opposite - a previous version
# of this comment wrongly claimed alo.bg excludes it). sqm is normally a
# GRID field (SQM_RE, set directly from card text), which is why it isn't
# detail-only in the sense the fields above are - a fresh grid crawl that
# DOES find real sqm text must still be able to overwrite an old value
# (real edits happen). But now that fetch_listing_detail() can ALSO fill
# sqm in (via extract_specs_imoti_bg()'s floorSize, for whatever fraction
# of listings whose card never showed "... кв.м" text), the same
# merge-not-replace protection every other detail-only field gets is
# needed here too - otherwise a later grid-only re-touch that (per this
# function's own docstring above) hits a transient detail-fetch failure
# and again finds no sqm text on the card would silently wipe a real sqm
# value a previous run's detail fetch had already filled in - exactly the
# bug class this field list exists to prevent, and exactly the scenario
# this module's own fetch_listing_detail() docstring describes ("a single
# transient per-run failure... legitimately returns None... for a listing
# that had real detail-page fields on a previous run"). Being in this list
# still means "prefer the fresh value when the fresh value is non-empty"
# (see update_history() below) - so a genuine grid-parsed sqm change still
# wins; this only stops a grid MISS from clobbering a real detail-page
# value.
_DETAIL_ONLY_FIELDS = (
    "description", "site_posted_at", "sqm",
    "property_type_raw", "features", "has_elevator", "furnished",
    "has_central_heating", "agency_name", "agency_website",
)


def update_history(history, listings):
    now = datetime.now(timezone.utc).isoformat()
    for l in listings:
        lid = l["id"]
        if lid not in history:
            history[lid] = {"first_seen": now, "snapshots": []}
        history[lid]["snapshots"].append({"seen_at": now, "price_eur": l["price_eur"]})
        prev_latest = history[lid].get("latest") or {}
        merged = dict(l)
        for field in _DETAIL_ONLY_FIELDS:
            prev_value = prev_latest.get(field)
            if merged.get(field) in (None, "", []) and prev_value not in (None, "", []):
                merged[field] = prev_value
        history[lid]["latest"] = merged
    return history


# A listing not seen in a scrape for at least this long is treated as no
# longer actually posted on this portal ("removed"), not just skipped by one
# scrape cycle due to pagination timing/site load - same threshold
# detect_relistings.py already uses for the same reason. Once removed,
# days_on_market/score freeze at the day it was last confirmed live instead
# of continuing to climb forever against an ad that's no longer there.
GONE_AFTER = timedelta(hours=20)


def compute_leads(history):
    leads = []
    for lid, rec in history.items():
        prices = [s["price_eur"] for s in rec["snapshots"] if s["price_eur"]]
        if not prices:
            continue
        first_price, last_price = prices[0], prices[-1]
        drop_pct = round((first_price - last_price) / first_price * 100, 1) if first_price else 0
        first_seen = datetime.fromisoformat(rec["first_seen"])
        last_seen = datetime.fromisoformat(rec["snapshots"][-1]["seen_at"])
        source_status = "active" if (datetime.now(timezone.utc) - last_seen) <= GONE_AFTER else "removed"
        effective_now = last_seen if source_status == "removed" else datetime.now(timezone.utc)

        latest = rec["latest"]
        site_posted_at = latest.get("site_posted_at")
        reference_date = datetime.fromisoformat(site_posted_at) if site_posted_at else first_seen
        days_on_market = max((effective_now - reference_date).days, 0)
        price_history = []
        last_hist_price = None
        for s in rec["snapshots"]:
            p = s.get("price_eur")
            # detect_relistings.py/detect_relistings_by_photo.py inject a
            # synthetic snapshot for the OLD (delisted) listing's own last
            # real price at the moment this listing is believed to be its
            # relisting - tagged "source": "relisted_from" rather than a
            # normal scraped snapshot. That tag has to survive into
            # price_history (not just live in the raw snapshots list) or
            # nothing downstream - Supabase, the frontend's price chart, the
            # eventual motivation-score rework - can tell an off-market gap
            # apart from an ordinary same-ad price edit. Kept even when the
            # relisted price happens to equal the prior entry's (still a
            # real, meaningful event - the property came back on the market,
            # whether or not the price moved) - the plain dedup rule below
            # would otherwise silently drop it like any other unchanged
            # price point.
            is_relisting = s.get("source") == "relisted_from"
            if not p or (p == last_hist_price and not is_relisting):
                continue
            entry = {"date": s["seen_at"], "price_eur": p}
            if is_relisting:
                entry["source"] = "relisted_from"
                entry["relisted_from"] = s["relisted_from"]
                if s.get("came_back_at"):
                    entry["came_back_at"] = s["came_back_at"]
                if s.get("came_back_price") is not None:
                    entry["came_back_price"] = s["came_back_price"]
            price_history.append(entry)
            last_hist_price = p
        price_drop_count = sum(
            1 for i in range(1, len(price_history)) if price_history[i]["price_eur"] < price_history[i - 1]["price_eur"]
        )

        price_per_sqm = round(last_price / latest["sqm"]) if latest.get("sqm") else None

        entry = dict(latest)
        entry["price_eur"] = last_price
        entry["price_per_sqm"] = price_per_sqm
        entry["price_history"] = price_history
        entry["price_drop_count"] = price_drop_count
        entry["drop_pct"] = drop_pct
        entry["days_on_market"] = days_on_market
        entry["source_status"] = source_status
        entry["removed_at"] = last_seen.isoformat() if source_status == "removed" else None
        leads.append(entry)

    # Keyed by (city_key, area), not area alone - see geo_utils.py's
    # listing_city_key() comment / sync_to_supabase.py's group_listings()
    # comment for the full "Център" cross-city collision story.
    area_totals = {}
    for l in leads:
        city_key = listing_city_key(l)
        if l["price_per_sqm"] and city_key:
            area_totals.setdefault((city_key, l["area"]), []).append(l["price_per_sqm"])
    area_avg = {key: sum(v) / len(v) for key, v in area_totals.items()}

    for l in leads:
        city_key = listing_city_key(l)
        area_key = (city_key, l["area"]) if city_key else None
        if l["price_per_sqm"] and area_key in area_avg:
            avg = area_avg[area_key]
            l["area_avg_price_per_sqm"] = round(avg)
            l["pct_vs_area_avg"] = round((l["price_per_sqm"] - avg) / avg * 100, 1)
        else:
            l["area_avg_price_per_sqm"] = None
            l["pct_vs_area_avg"] = None
        # Computed here, not in the loop above, because the motivation
        # score's area-average component needs pct_vs_area_avg, which
        # isn't known until this second pass over "leads" completes its
        # own area_totals aggregation - see geo_utils.compute_motivation_
        # score()'s own docstring for the full formula and the real-data
        # reasoning behind every cap.
        l["score"] = compute_motivation_score(
            l["drop_pct"], l["price_drop_count"], l["days_on_market"], l["pct_vs_area_avg"], l["price_history"]
        )

    leads.sort(key=lambda x: x["score"], reverse=True)
    return leads


def main():
    listings = fetch_listings()
    history = load_history()
    history = update_history(history, listings)
    save_history(history)
    leads = compute_leads(history)
    LEADS_FILE.write_text(json.dumps(leads, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Found {len(listings)} listings, {len(leads)} tracked leads")


if __name__ == "__main__":
    main()
