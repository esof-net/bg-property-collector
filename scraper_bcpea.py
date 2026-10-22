"""
Scrapes public property-auction listings ("публични продани") from
sales.bcpea.org, the official sales registry run by the Chamber of Private
Enforcement Agents (Камара на частните съдебни изпълнители). These are
court-ordered foreclosure auctions, not ordinary for-sale listings - the
price shown on the site is a legal minimum starting bid ("Начална цена"),
not a negotiable asking price. Tracked here the same way as every other
portal (price history, days-on-market, area averages) since a repeat
listing at a lower price genuinely does mean something concrete: Bulgarian
enforcement procedure re-lists a failed auction at a reduced price, so a
real "price_drop" here reflects a real second/third-round auction, not a
seller softening.

Unlike the other 6 portals (all scoped to Sofia via their own search URL),
this scraper pulls nationwide - the site's own court filter (`court=`)
covers all 28 Bulgarian judicial districts, and capping to Sofia would
throw away most of the ~1,300 total listings for very little request
savings. The frontend has no Sofia-only assumption baked in (area filters,
search, and the lead-generator feature all work off whatever `area` values
actually appear in the data), so this becomes the first real nationwide
data source without needing any frontend change.

Development note: sales.bcpea.org is blocked by this sandbox's network
egress policy (no live requests possible from here), so this scraper was
written entirely from two real saved HTML pages the user provided (a
`/properties` grid page and a `/properties/{id}` detail page, both viewed
live in a real browser) rather than from a live fetch during development,
unlike every other scraper in this repo. The parsing logic below matches
those saved pages exactly and was verified field-by-field against them.

A first live run (via workflow_dispatch) confirmed the one thing that
couldn't be checked from the sandbox: sales.bcpea.org bot-blocks a plain
requests.get() with a 403 on every attempt, retries included - the same
Akamai/Cloudflare-style edge check scraper_olx.py and scraper_imot.py
already document and solve on other portals. Fixed the same way here: a
real headless browser (Playwright/Chromium) gets through cleanly, reusing
one browser page across every grid and detail request rather than
relaunching Chromium per request (same approach as scraper_imot.py).

Listing grid (`/properties?p=N&perpage=36`): each result is a `.item__group`
div, direct child of `.item__container`, containing the property type as a
plain title ("Двустаен апартамент", "Гараж", "Парцел", ...), sqm, a price in
either EUR or BGN (both currencies appear - BGN converted at the fixed
1.95583 peg, same constant scraper.py and scraper_homes.py already use),
the settlement name, and a link to the detail page carrying the listing ID
(confirmed live: no "гр./с." prefix stripped from settlement here). The
grid card's own image is very often a shared "photo-placeholder.png" even
when the listing has real photos - the real gallery only exists on the
detail page.

Each tracked listing's own detail page carries two things not present in
the grid:
  - "Район" (district), a much more precise location than the grid's
    bare settlement name - for Sofia listings this is the same kind of
    neighborhood name (e.g. "Красно село") the other 5 Sofia-scoped
    scrapers already use as `area`, so bcpea listings can genuinely group
    and radius-average alongside them rather than sitting in their own
    unmatched "Sofia" bucket.
  - a real photo, when the grid only had the placeholder.
A detail-page fetch that fails just leaves that listing with the grid's
coarser settlement name and whatever photo the grid had, rather than
aborting the run - same "graceful degradation" pattern as scraper_bazar.py.

Visiting every listing's own page (a fresh browser context per request,
plus a live geocode call) is not affordable inline: at ~1,300 nationwide
listings this routinely took long enough to blow through the scrape's own
30-minute step timeout - and since the grid data is only saved after
fetch_listings() returns, that discarded the run's freshly-scraped grid
data too, not just the slow detail work (the same bug already found and
fixed in scraper.py/scraper_alo.py for their own inline detail-page work).
So, like those two, this now only does the fast grid crawl inline;
backfill_detail_bcpea.py is the other half, a separate resumable pass that
visits whatever hasn't been detail-checked yet.

No portal-embedded coordinates exist anywhere on this site (confirmed in
both saved pages - no data-lat/lng attributes, no map JS object, despite
leaflet.css being loaded sitewide for some other page), so coordinates
come from the same OpenStreetMap Nominatim geocoder every no-coordinate
portal already shares (geo_utils.Geocoder), queried at district+settlement
level (not full street address) so the disk cache is actually reused
across listings in the same district, same reasoning geo_utils.py already
documents for the other geocoded portals.
"""

import re
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup

from geo_utils import Geocoder, classify_category, compute_motivation_score, listing_city_key, prune_snapshots, evict_stale_records, STALE_RECORD_RETENTION

SEARCH_URL = "https://sales.bcpea.org/properties"
BASE_URL = "https://sales.bcpea.org"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

OUT_DIR = Path(__file__).parent / "data"
OUT_DIR.mkdir(exist_ok=True)
HISTORY_FILE = OUT_DIR / "history_bcpea.json"
LEADS_FILE = OUT_DIR / "leads_bcpea.json"

BGN_TO_EUR = 1.95583
PERPAGE = 36
MAX_PAGES = 100
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 5
REQUEST_DELAY_SECONDS = 1.0
MAX_CONSECUTIVE_PAGE_FAILURES = 5
MAX_CONSECUTIVE_DETAIL_FAILURES = 5

# Confirmed live (via debug_html_snippet): every request past the first in a
# session gets a bot-challenge interstitial instead of the real page - title
# "Един момент..." ("One moment..."), body text explicitly says this is a
# security service's automated check and "this page is shown while the
# website verifies [you]". That phrasing describes a self-clearing
# Cloudflare-style JS challenge, not a hard block or a CAPTCHA - Playwright's
# Chromium executes real JS, so waiting the challenge out (rather than
# giving up after a fixed 500ms) is a real fix for this specific pattern,
# not a blind guess.
CHALLENGE_TITLE_MARKERS = ("един момент", "just a moment", "checking your browser")
CHALLENGE_POLL_ATTEMPTS = 6
CHALLENGE_POLL_INTERVAL_MS = 1500

LISTING_ID_RE = re.compile(r"/properties/(\d+)")
PRICE_RE = re.compile(r"([\d][\d\s\xa0]*(?:[.,]\d{1,2})?)\s*(EUR|лв\.?)", re.IGNORECASE)
SQM_RE = re.compile(r"([\d.,]+)\s?кв\.?м")
PUBLISHED_RE = re.compile(
    r"Публикувано на\s+(\d{1,2})\s+(\S+)\s+(\d{4})\s*г\.?\s*в\s+(\d{1,2}):(\d{2})",
    re.IGNORECASE,
)

BG_MONTHS = {
    "януари": 1, "февруари": 2, "март": 3, "април": 4, "май": 5, "юни": 6,
    "юли": 7, "август": 8, "септември": 9, "октомври": 10, "ноември": 11, "декември": 12,
}


def parse_price_eur(text):
    m = PRICE_RE.search(text or "")
    if not m:
        return None
    amount_str, currency = m.groups()
    amount_str = amount_str.replace("\xa0", "").replace(" ", "")
    if "," in amount_str and "." not in amount_str:
        amount_str = amount_str.replace(",", ".")
    else:
        amount_str = amount_str.replace(",", "")
    try:
        amount = float(amount_str)
    except ValueError:
        return None
    if currency.upper().startswith("EUR"):
        return round(amount)
    return round(amount / BGN_TO_EUR)


def parse_published_at(text):
    m = PUBLISHED_RE.search(text or "")
    if not m:
        return None
    day, month_name, year, hour, minute = m.groups()
    month = BG_MONTHS.get(month_name.lower())
    if not month:
        return None
    try:
        return datetime(int(year), month, int(day), int(hour), int(minute), tzinfo=timezone.utc).isoformat()
    except ValueError:
        return None


def clean_settlement(text):
    return re.sub(r"^(гр\.|с\.)\s*", "", (text or "").strip()) or None


def is_challenge_title(title):
    title = (title or "").lower()
    return any(marker in title for marker in CHALLENGE_TITLE_MARKERS)


def fetch_html(browser, url):
    # A fresh browser context per request, not one shared page reused for
    # the whole run - confirmed live via probe_bcpea_challenge.py (4
    # variants tested against the real site: a shared-context control, a
    # navigator.webdriver stealth patch, a long randomized delay, and a
    # fresh context per request - only the fresh-context variant got past
    # the challenge on a second request; the other three failed 100% of
    # the time, identically to the original bug). The site's bot-challenge
    # escalates based on session/context reuse, not raw timing or browser
    # fingerprinting, so a genuinely new context resets whatever
    # per-session signal triggers it - the previous shared-page approach
    # meant this scraper had been stuck at exactly one page (36 listings)
    # since it was first built, out of a real ~1,300 nationwide.
    for attempt in range(1, MAX_RETRIES + 1):
        context = browser.new_context(user_agent=USER_AGENT, locale="bg-BG")
        page = context.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(500)
            if is_challenge_title(page.title()):
                for _ in range(CHALLENGE_POLL_ATTEMPTS):
                    page.wait_for_timeout(CHALLENGE_POLL_INTERVAL_MS)
                    if not is_challenge_title(page.title()):
                        print(f"DEBUG: challenge cleared for {url}", flush=True)
                        break
                else:
                    print(f"DEBUG: challenge did not clear for {url} after "
                          f"{CHALLENGE_POLL_ATTEMPTS * CHALLENGE_POLL_INTERVAL_MS / 1000:.0f}s", flush=True)
            return page.content()
        except Exception as e:
            print(f"DEBUG: navigation failed for {url} (attempt {attempt}/{MAX_RETRIES}): {e}", flush=True)
            if attempt < MAX_RETRIES:
                page.wait_for_timeout(RETRY_BACKOFF_SECONDS * attempt * 1000)
        finally:
            context.close()
    return None


def label_info(scope, label_text):
    """Find a .label__group in scope whose .label text matches label_text
    (case-insensitive - the same site uses both "НАСЕЛЕНО МЯСТО" on the
    grid and "Населено място" on the detail page for the same field) and
    return its .info text, or None."""
    for group in scope.find_all(class_="label__group"):
        label = group.find(class_="label")
        info = group.find(class_="info")
        if label and info and label.get_text(strip=True).upper() == label_text.upper():
            return info.get_text(" ", strip=True)
    return None


# Every .label__group's label text found anywhere in `expanded`, already
# known to reliably contain "Район" and "Описание" (the two label_info()
# calls fetch_listing_detail() already makes, both confirmed against real
# saved pages). Investigated 2026-09-24 (bcpea detail gallery/specs task)
# for a genuine property-spec table (property type/sqm/construction/floor
# - see target schema in sync_to_supabase.py's SOURCE_FIELDS) the way
# alo.bg's detail page has one: none of the two real saved pages this
# scraper was originally built from carried any label beyond "Район" and
# "Описание". Note this module's own docstring only enumerates "Район"
# and a real photo as the two things the detail page adds over the grid -
# it predates and never mentions "Описание" (added later, per the
# separate label_info(expanded, "Описание") call and its own comment
# citing probe_descriptions.py), so the docstring alone isn't evidence
# against a spec table; the real evidence is that every "Описание" value
# seen in production data/history_bcpea.json is a single monolithic
# legal-auction announcement that already embeds the cadastral
# identifier/area/boundaries/auction date as free-running prose rather
# than separate structured fields - not a spec table this site simply
# doesn't render for a different kind of listing.
#
# This sandbox's network egress to sales.bcpea.org is blocked (confirmed
# again while investigating this - same as every other scraper's own
# egress-block note), so a live re-check against today's real markup
# wasn't possible, and CLAUDE.md rules out iterating that check via a live
# workflow_dispatch. So this function exists as a defensive, zero-cost
# tripwire rather than a guess: fetch_listing_detail() calls it and logs
# any label__group text it doesn't already recognize, so a future
# detail-checked run (or backfill_detail_bcpea.py's own logs) would
# surface a genuine new label the moment one actually starts appearing on
# a real page, without this code ever having to assume one exists today.
_KNOWN_BCPEA_LABELS = frozenset(["РАЙОН", "ОПИСАНИЕ", "НАСЕЛЕНО МЯСТО"])


def unrecognized_labels(expanded):
    """Every .label__group label text in `expanded` that isn't one of the
    two fields already extracted (or the grid's settlement label, which
    also uses this same markup) - see _KNOWN_BCPEA_LABELS' own comment.
    Returns a sorted list of the exact label text found, or [] when
    nothing unexpected is present (the expected/confirmed case on every
    page seen so far)."""
    found = set()
    for group in expanded.find_all(class_="label__group"):
        label = group.find(class_="label")
        if label:
            text = label.get_text(strip=True)
            if text.upper() not in _KNOWN_BCPEA_LABELS:
                found.add(text)
    return sorted(found)


def extract_photos_bcpea(expanded):
    """Every real (non-placeholder, non-logo/icon) <img> src found in
    `expanded`, with the `.head` image first (see this function's own
    2026-09-25 correction below for why "already-proven"/"confirmed real"
    is no longer an accurate description of that element) followed by any
    other distinct <img> elsewhere in `expanded`, in DOM order, deduped by
    resolved URL.

    CORRECTED 2026-09-25: a production audit found "photos" at a flat 0%
    (0/2,246) in data/history_bcpea.json - every single detail-checked
    listing, including the 64% whose "Описание"/district DID extract
    successfully from this exact same `expanded` object in the exact same
    fetch_listing_detail() call. That's conclusive that this function's
    `.head`-inside-`expanded` lookup never actually matches anything on a
    real sales.bcpea.org page, in production, for any of 2,246 samples -
    not a coincidence or a "rare shape" gap.

    This function's own prior docstring (and fetch_listing_detail()'s
    original single-photo extraction it was built to generalize, present
    since this scraper's very first commit) called `.head` inside
    `.item__expanded` "the already-proven"/"already-confirmed-real"
    element, on the strength of "two real saved HTML pages the user
    provided" during this scraper's initial build. Nothing in this repo's
    git history or docs/decisions.md actually quotes or otherwise
    preserves that raw HTML, so that claim can no longer be independently
    verified - and the flat, unmoved-since-before-this-function-existed
    "photo" (singular) coverage rate (43.6% pre-dating any detail-page
    photo work at all, per docs/backlog.md's 2026-09-22 figure; 43.4%
    (974/2,246) today, after 100% of listings have gone through this exact
    detail-page extraction unconditionally) is itself evidence AGAINST the
    claim: if `.head` genuinely matched real markup even some of the time,
    photo coverage should have moved up from the grid-only baseline, not
    stayed flat. Same shape of mistake as alo.bg's own photo regression
    (PR #279) and (per its own similarly-corrected comment) imoti.bg's
    specs/contact regression - a confident "this is already proven" claim
    that doesn't survive being traced to what actually produced the number
    it pointed to.

    This sandbox's network egress to sales.bcpea.org is blocked (confirmed
    again this session), so which of two real, independent causes this
    actually is - (a) `.head` genuinely doesn't exist inside
    `.item__expanded` on a real page (it might be a sibling section
    instead, e.g. a top-of-page gallery above an "expanded details"
    accordion, a common real-world layout this shape would produce), or
    (b) `.head` exists but some other assumption here (an attribute name,
    a wrapper level) doesn't match - could not be confirmed this session,
    and CLAUDE.md rules out iterating that check via a live
    workflow_dispatch. Deliberately NOT widening or changing this
    function's search scope without that evidence: guessing a new
    scope/selector here carries a real, different risk (a wrong guess
    could start attributing another section's images, e.g. a "related
    listings" strip if one exists, to the wrong listing - silently WRONG
    data, worse than the current honestly-empty list). See
    unrecognized_labels()'s own tripwire pattern just above, and this
    module's `expanded_image_diagnostic()`/its call site in
    fetch_listing_detail() below (added alongside this correction) for
    the zero-cost, guess-free way this now surfaces real evidence instead:
    logging exactly what <img> tags (class lists, not full URLs) actually
    exist on a real page whenever `expanded` was found (proving the page
    loaded and parsed fine) but this function still came up empty - the
    concrete information a future real production run needs to fix the
    selector for real, in `probe_bcpea_photos.py` (also added alongside
    this correction, NOT dispatched by this session per CLAUDE.md - for a
    human with live access, or a future session with real network access,
    to run once against a couple of real listing URLs)."""
    def is_real(src):
        if not src:
            return False
        low = src.lower()
        return not any(bad in low for bad in ("placeholder", "logo", "icon", "spacer"))

    photos = []
    seen = set()

    def add(img_tag):
        if img_tag is None:
            return
        src = img_tag.get("src")
        if not is_real(src):
            return
        full = src if src.startswith("http") else BASE_URL + src
        if full not in seen:
            seen.add(full)
            photos.append(full)

    head = expanded.find(class_="head")
    add(head.find("img") if head else None)
    for img in expanded.find_all("img"):
        add(img)
    return photos


def expanded_image_diagnostic(soup, expanded):
    """Zero-cost visibility for fetch_listing_detail()'s own tripwire (see
    its call site below and extract_photos_bcpea()'s own 2026-09-25
    correction for why this exists): every distinct <img> class attribute
    found INSIDE `expanded` versus found ANYWHERE ELSE on the whole page
    (`soup`), counted (not full URLs/src values - just enough to tell a
    future reader where a real photo/gallery element actually lives without
    dumping raw HTML into a log). A real page where `.head` genuinely lives
    inside `.item__expanded` would show `inside_expanded` non-empty; a page
    where it's a sibling section instead would show it empty while
    `outside_expanded` has real candidates - exactly the distinction needed
    to fix extract_photos_bcpea()'s scope for real, without guessing."""
    def class_signatures(tags):
        seen = []
        for img in tags:
            classes = tuple(img.get("class") or [])
            if classes not in seen:
                seen.append(classes)
        return [" ".join(c) if c else "(no class)" for c in seen]

    expanded_imgs = expanded.find_all("img") if expanded is not None else []
    all_imgs = soup.find_all("img")
    expanded_img_ids = {id(t) for t in expanded_imgs}
    outside_imgs = [t for t in all_imgs if id(t) not in expanded_img_ids]
    return {
        "inside_expanded": class_signatures(expanded_imgs),
        "outside_expanded": class_signatures(outside_imgs),
    }


def debug_html_snippet(html):
    """A short description of unexpected HTML - long enough to tell a bot
    challenge/block page (a title like "Just a moment..." or "Forbidden",
    little body text) apart from a genuine empty results page (the site's
    normal title, structured markup, just no matching content), without
    dumping the whole page into the log."""
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(strip=True) if soup.title else None
    body_text = soup.get_text(" ", strip=True)[:200] if soup.body else None
    return f"len={len(html)} title={title!r} body_start={body_text!r}"


def fetch_listings_page(browser, url):
    html = fetch_html(browser, url)
    if html is None:
        return None
    soup = BeautifulSoup(html, "html.parser")
    container = soup.find(class_="item__container")
    if container is None:
        print(f"DEBUG: no item__container for {url} - {debug_html_snippet(html)}", flush=True)
        return {}
    cards = container.find_all(class_="item__group", recursive=False)

    listings = {}
    for card in cards:
        link = card.find("a", href=LISTING_ID_RE)
        if not link:
            continue
        listing_id = LISTING_ID_RE.search(link["href"]).group(1)

        title_el = card.find(class_="title")
        category_el = card.find(class_="category")
        price_el = card.find(class_="price")
        date_el = card.find(class_="date")

        title = title_el.get_text(strip=True) if title_el else None
        price_eur = parse_price_eur(price_el.get_text(" ", strip=True)) if price_el else None
        if price_eur is None or price_eur < 100:
            continue

        sqm = None
        if category_el:
            m = SQM_RE.search(category_el.get_text(strip=True))
            if m:
                try:
                    sqm = round(float(m.group(1).replace(",", ".")))
                except ValueError:
                    pass

        settlement = clean_settlement(label_info(card, "НАСЕЛЕНО МЯСТО")) or "България"
        site_updated_at = parse_published_at(date_el.get_text(strip=True)) if date_el else None

        img = card.find("img")
        photo = None
        if img and img.get("src") and "placeholder" not in img["src"].lower():
            src = img["src"]
            photo = src if src.startswith("http") else BASE_URL + src

        href = link["href"]
        full_url = href if href.startswith("http") else BASE_URL + href

        listings[listing_id] = {
            "id": "bcpea_" + listing_id,
            "url": full_url,
            "photo": photo,
            "price_eur": price_eur,
            "sqm": sqm,
            "area": settlement,
            "title": (f"{title}, {settlement}" if title else settlement)[:150],
            "portal": "sales.bcpea.org",
            "site_updated_at": site_updated_at,
            # KNOWN-BAD for this portal - see classify_category()'s own
            # docstring in geo_utils.py. bcpea's auctions cover every
            # property type, not just apartments, so this column silently
            # mislabels most of this portal's own non-apartment listings as
            # "apartment". Never read this field directly for bcpea - every
            # real type check already goes through type_filter_bucket()/
            # typeFilterBucket(), which use bcpea_type_match() against this
            # same title's precise controlled vocabulary instead. Kept
            # (rather than removed) only because listing_sources/
            # merged_listings' schema still has a generic "category" column
            # shared by all 8 portals.
            "category": classify_category(title),
            "_settlement": settlement,
        }
    return listings


# Populated by fetch_listing_detail()'s unrecognized-label tripwire below -
# module-level so it dedupes across every listing in one process run (each
# scheduled run is a fresh process, so this naturally resets run to run).
_seen_unrecognized_labels = set()

# Capped counter for fetch_listing_detail()'s own extract_photos_bcpea()
# miss tripwire (see its call site) - module-level for the same
# reset-per-process reason as _seen_unrecognized_labels above.
_PHOTO_MISS_LOG_CAP = 5
_photo_miss_logged = 0


def _log_photo_miss(url, soup, expanded):
    """Zero-cost tripwire for extract_photos_bcpea()'s own 2026-09-25
    correction (see its docstring): logged for at most _PHOTO_MISS_LOG_CAP
    listings per run (not every miss - at ~1,300 nationwide listings this
    would otherwise spam the run's logs for what's very possibly the same
    root cause every time), capturing the real <img> class attributes this
    specific page actually has inside vs. outside `.item__expanded` - the
    concrete evidence a future real production run needs to fix this
    selector for real. Split out from fetch_listing_detail() (which calls
    this) so it's directly testable without a live Playwright browser -
    see tests/test_scraper_bcpea_photo_miss_tripwire.py."""
    global _photo_miss_logged
    if _photo_miss_logged >= _PHOTO_MISS_LOG_CAP:
        return
    _photo_miss_logged += 1
    diag = expanded_image_diagnostic(soup, expanded)
    print(f"DEBUG: extract_photos_bcpea() empty for {url} - {diag}", flush=True)


def fetch_listing_detail(browser, listing, geocoder):
    # Returns whether the page itself actually loaded - used by
    # fetch_listing_details() to detect a run of consecutive *fetch*
    # failures. A page that loads fine but simply has no matching markup
    # or no geocodable address is not a fetch failure - "no coords" can
    # happen for entirely legitimate reasons (a bad/rural address the
    # geocoder can't resolve) unrelated to site health, and treating it as
    # one let a run of ordinary no-coords listings falsely trip the
    # early-stop.
    html = fetch_html(browser, listing["url"])
    if html is None:
        # NOT marked detail_checked - fetch_html() already exhausted its
        # own in-page retries, but that failure could still be transient
        # (site throttling, a timeout), not a confirmed-gone page. This
        # used to mark detail_checked=True unconditionally before even
        # attempting the fetch, which meant a listing that hit one bad
        # request was silently and permanently skipped by every future
        # backfill run, losing its description/district/photo for good.
        # Leaving detail_checked unset lets a later run retry it.
        return False
    # Marked only once html is confirmed real, so a backlog scan can tell
    # "already attempted, got a real page, nothing more to gain" (even if
    # that real page had no matching markup) apart from "never
    # successfully visited yet" - same marker pattern
    # scraper.py/scraper_imot.py/scraper_alo.py use for their own detail
    # backfills.
    listing["detail_checked"] = True
    soup = BeautifulSoup(html, "html.parser")
    expanded = soup.find(class_="item__expanded")
    if expanded is None:
        print(f"DEBUG: no item__expanded for {listing['url']} - {debug_html_snippet(html)}", flush=True)
        return True

    settlement = listing.get("_settlement")
    district = label_info(expanded, "Район")
    if district:
        listing["area"] = district if (settlement or "").lower() in ("софия", "sofia") \
            else f"{settlement}, {district}" if settlement else district

    # Real per-listing legal/cadastral description (e.g. property
    # identifiers, cadastral references) - confirmed live via
    # probe_descriptions.py, same .label__group/.info markup label_info()
    # already reads District from, just a different label text.
    description = label_info(expanded, "Описание")
    if description:
        listing["description"] = description

    # Full photo set (see extract_photos_bcpea()'s own comment) - `photo`
    # is kept exactly as before (still the `.head` image, first in the
    # list) for backward compatibility with every existing reader of that
    # single field.
    photos = extract_photos_bcpea(expanded)
    if photos:
        listing["photos"] = photos
        if not listing.get("photo"):
            listing["photo"] = photos[0]
    else:
        _log_photo_miss(listing["url"], soup, expanded)

    # Zero-cost tripwire for a genuine new spec label (see
    # _KNOWN_BCPEA_LABELS' own comment) - logged once per distinct label
    # text per run, not per listing, so a real new label surfaces in a
    # run's logs without spamming them across ~1,300 listings.
    new_labels = [l for l in unrecognized_labels(expanded) if l not in _seen_unrecognized_labels]
    if new_labels:
        _seen_unrecognized_labels.update(new_labels)
        print(f"DEBUG: unrecognized label__group text on {listing['url']}: {new_labels}", flush=True)

    query_parts = [p for p in [district, settlement, "България"] if p]
    if query_parts:
        coords = geocoder.geocode(", ".join(query_parts))
        if coords:
            listing["lat"] = coords["lat"]
            listing["lng"] = coords["lng"]
    return True


def fetch_listings():
    # Grid crawl only - no detail-page visits here (see
    # backfill_detail_bcpea.py and the module docstring: at ~1,300 listings,
    # each detail visit needing a fresh browser context plus a live geocode
    # call routinely pushed the combined grid+detail work well past this
    # step's 30-minute timeout, discarding that run's freshly-scraped grid
    # data too since main() never got past fetch_listings() to save
    # anything - the same bug already found and fixed in scraper.py/
    # scraper_alo.py for their own inline detail-page work).
    all_listings = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        consecutive_failures = 0
        for page_num in range(1, MAX_PAGES + 1):
            url = f"{SEARCH_URL}?perpage={PERPAGE}&p={page_num}"
            page_listings = fetch_listings_page(browser, url)
            count = len(page_listings) if page_listings is not None else None
            print(f"DEBUG: page {page_num} listings parsed = {count}", flush=True)
            if page_listings is None:
                # A failed fetch (all in-request retries exhausted) is not
                # the real end-of-results signal (an empty dict is) - giving
                # up here would silently truncate every remaining page after
                # one bad request, the same bug found in scraper.py/
                # scraper_alo.py/scraper_imot.py/scraper_homes.py/
                # scraper_olx.py/scraper_bazar.py/scraper_imoti_bg.py. Skip
                # it and keep going, only giving up after several failures
                # in a row.
                consecutive_failures += 1
                if consecutive_failures >= MAX_CONSECUTIVE_PAGE_FAILURES:
                    break
                continue
            consecutive_failures = 0
            if not page_listings:
                break
            all_listings.update(page_listings)

        browser.close()

    listings = list(all_listings.values())
    # _settlement is kept (not stripped here) - fetch_listing_details() still
    # needs it later, potentially in a separate run/process after this
    # listing has round-tripped through history JSON. lat/lng default to
    # None so every listing has a consistent schema before detail fetching
    # (a separate pass) fills some of them in.
    for l in listings:
        l.setdefault("lat", None)
        l.setdefault("lng", None)
    return listings


def fetch_listing_details(listings, on_checkpoint=None, checkpoint_every=150, deadline=None):
    """The other half of fetch_listings(): visits each given listing's own
    detail page for district/photo/coordinates, mutating each dict in
    place. Self-contained (launches its own browser + geocoder) so it can
    run as a separate, resumable pass - see backfill_detail_bcpea.py.

    deadline/on_checkpoint let the caller save progress as it goes and
    stop cleanly before the workflow's own timeout would hard-kill the
    process - same fix as backfill_detail_imoti_net.py/
    backfill_detail_alo.py's own timeout problems. A run of several
    consecutive *page-load* failures is treated the same way alo.bg's own
    fix treats a run of consecutive failures: a live site issue worth
    stopping this run for, not a reason to keep grinding through a batch
    that's currently doomed. A page that loads fine but simply has no
    coords is tracked for visibility only and never trips the early stop -
    a run that hit a stretch of hard-to-geocode addresses used to abort
    with zero further progress even though the site itself was fine."""
    geocoder = Geocoder()
    no_coords = 0
    consecutive_failures = 0
    processed = 0
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        for i, l in enumerate(listings, 1):
            if deadline is not None and time.monotonic() >= deadline:
                print(f"DEBUG: stopping at {i - 1}/{len(listings)} - approaching this run's time budget", flush=True)
                break
            time.sleep(REQUEST_DELAY_SECONDS)
            ok = fetch_listing_detail(browser, l, geocoder)
            processed = i
            if not ok:
                consecutive_failures += 1
                if consecutive_failures >= MAX_CONSECUTIVE_DETAIL_FAILURES:
                    print(f"DEBUG: {consecutive_failures} consecutive detail-page failures at "
                          f"{i}/{len(listings)} - looks like the site is throttling this run, stopping here", flush=True)
                    break
            else:
                consecutive_failures = 0
                if l.get("lat") is None:
                    no_coords += 1
                    if no_coords <= 3:
                        print(f"DEBUG: detail fetch produced no coords for {l['url']}", flush=True)
            if i % 200 == 0:
                print(f"DEBUG: fetched detail for {i}/{len(listings)} listings ({no_coords} with no coords so far)", flush=True)
            if on_checkpoint and i % checkpoint_every == 0:
                geocoder.save()
                on_checkpoint()
        browser.close()
    print(f"DEBUG: detail fetch finished, {no_coords}/{processed} listings got no coords", flush=True)
    geocoder.save()
    for l in listings:
        l.pop("_settlement", None)


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


# Fields fetch_listing_detail() (run separately via backfill_detail_bcpea.py
# - main() never calls it inline, see fetch_listings()'s own comment) adds
# on top of what the grid crawl (fetch_listings_page()) itself produces:
# "description" and "detail_checked" are never set by the grid at all;
# "lat"/"lng" ARE present on a fresh grid record but only ever as the
# None placeholder fetch_listings() defaults them to - real coordinates
# only ever come from this detail pass's own geocode call. update_history()
# below must merge these in from the previous "latest" rather than let a
# fresh grid re-touch wipe them off an already-detail-checked, still-active
# listing every ~6 hours - docs/backlog.md item 9a.
#
# NOTE: "area" (district enrichment) is deliberately NOT included here -
# unlike the fields above, the grid crawl always supplies a real
# (non-empty) value for it, just a less complete one before a detail visit
# (settlement-only area rather than settlement+district), so the
# "missing/falsy in the fresh record" merge rule below can't safely tell
# "grid's own value" apart from "should keep the richer detail-page value"
# without guessing. Left as a known smaller-severity gap for a follow-up,
# not guessed at here.
#
# "photo" IS included, unlike "area": fetch_listings_page() (the grid
# crawl) sets it to None whenever the listing card's own image is the
# shared "photo-placeholder.png" (see this module's docstring - very
# common, since "the real gallery only exists on the detail page"), so a
# fresh grid record's falsy photo is unambiguously "grid has nothing
# useful here", exactly like a fresh lat/lng always being the None
# placeholder above. No guessing needed - same merge rule already applied
# to lat/lng applies identically to photo.
#
# "photos" (2026-09-24, added alongside "photo" above) is never set by the
# grid crawl at all - extract_photos_bcpea() only ever runs from a detail
# visit - so it's an unconditional detail-only field, same shape as
# "description"/"detail_checked" above, not the "photo"/lat/lng merge-rule
# case. update_history()'s generic "missing/falsy in the fresh record"
# merge rule (an empty list counts as falsy, same as None/"") already
# handles it correctly without any extra code here.
_DETAIL_ONLY_FIELDS = ("description", "detail_checked", "lat", "lng", "photo", "photos")


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

        price_history = []
        last_hist_price = None
        for s in rec["snapshots"]:
            p = s.get("price_eur")
            if not p or p == last_hist_price:
                continue
            price_history.append({"date": s["seen_at"], "price_eur": p})
            last_hist_price = p
        price_drop_count = sum(
            1 for i in range(1, len(price_history)) if price_history[i]["price_eur"] < price_history[i - 1]["price_eur"]
        )

        last_seen = datetime.fromisoformat(rec["snapshots"][-1]["seen_at"])
        source_status = "active" if (datetime.now(timezone.utc) - last_seen) <= GONE_AFTER else "removed"
        effective_now = last_seen if source_status == "removed" else datetime.now(timezone.utc)

        latest = rec["latest"]
        site_updated_at = latest.get("site_updated_at")
        reference_date = datetime.fromisoformat(site_updated_at) if site_updated_at else datetime.fromisoformat(rec["first_seen"])
        days_on_market = max((effective_now - reference_date).days, 0)
        price_per_sqm = round(last_price / latest["sqm"]) if latest.get("sqm") else None

        entry = dict(latest)
        entry["price_eur"] = last_price
        entry["price_per_sqm"] = price_per_sqm
        entry["price_history"] = price_history
        entry["price_drop_count"] = price_drop_count
        entry["drop_pct"] = drop_pct
        entry["source_status"] = source_status
        entry["removed_at"] = last_seen.isoformat() if source_status == "removed" else None
        entry["days_on_market"] = days_on_market
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
