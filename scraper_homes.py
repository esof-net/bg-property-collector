"""
Scrapes current Bulgaria-wide apartment, house, and land listings from
homes.bg.

Unlike imoti.net and alo.bg, homes.bg's homepage embeds a structured JSON
blob (window.__PRELOADED_STATE__) with the listing data already parsed out
by their own frontend - no HTML/regex scraping needed, just pull the JSON.

Nationwide + all-categories mechanism (found via live diagnostic probing,
since neither is documented or guessable from the URL alone):
  - City filter: the homepage defaults to Sofia-only. Adding
    ?locationId=0 drops the location filter entirely (confirmed live:
    offersCount jumped from 12,351 Sofia-only to 44,111 nationwide, first
    result a non-Sofia city) - this is the one nationwide switch, used on
    every request below.
  - Property type: a plain ?type=<anything> query param is silently
    ignored - every guess (URL slugs, short codes, full business names)
    fell back to the same Sofia-dropped-but-still-apartment-only result.
    The real mechanism, found by reading homes.bg's own client JS bundle
    (static/js/client.*.js), is ?typeId=<PascalCase business name> - e.g.
    ?typeId=HouseSell. homes.bg has only 4 real for-sale property types
    total (no office/shop/garage/warehouse exist on this portal at all,
    unlike imoti.bg): ApartmentSell, HouseSell, LandParcel (regular land
    plots) and LandAgro (agricultural land) - confirmed nationwide counts
    44,111 / 9,625 / 13,406 / 2,115 respectively. TYPE_QUERIES below
    drives one full paginated pass per type.

Because the category comes straight from which portal search category
the listing was fetched under (ground truth from homes.bg's own search
index), not inferred from title/description text, category_confidence is
always "high" here - this is more reliable than the keyword-based
classifier used for portals that don't expose a real per-listing type.

The pagination loop stops on the API's own hasMoreItems=False; MAX_PAGES
is just a generous safety cap in case that flag ever misbehaves.

A page fetch retries a few times with backoff before being treated as the
end of pagination, so a transient failure doesn't get mistaken for having
reached the last page - and, since scrape.yml runs all scrapers
sequentially with a single git commit step at the end, an uncaught
exception here would otherwise silently discard every other scraper's
output for that run too.

Each offer's raw JSON also carries a "photos" array (not just the single
cover "photo") - captured and surfaced on the listing detail page. It also
has a key literally named "description", which despite the name is NOT a
free-text listing description - confirmed via a full raw-offer dump
(probe_deeper_history.py, Actions run 32700254970, 2026-08-24) and by
sampling data/leads_homes.json live (backlog #9): it's consistently a
short construction-material/furnishing tag line (e.g. "Тухла/Бетон,
Полуобзаведен" - "Brick/Concrete, Semi-furnished"), never prose, and this
JSON blob has no other field holding real description text. Surfacing it
as "description" actively misled users, so parse_offer() below leaves
that field unset rather than populated with the wrong content - see its
own comment for what a real fix would require.

There's also a "time" field, initially assumed
to be a real "last updated" signal (like olx.bg's, see scraper_olx.py) -
but sampling 280 live offers found 100% of them reporting "днес" (today)
with zero variation, meaning homes.bg apparently marks every actively
displayed listing as "today" regardless of true listing age. Using it
for days_on_market would make every homes.bg listing permanently show 0
days, which is worse than not using it at all - it would mask exactly
the stagnant, long-listed properties this tool exists to surface. So
days_on_market here stays purely tracking-based (time since we first
scraped the listing), same as before.

homes.bg carries no coordinates anywhere on its own pages, but every
offer's "location" string (neighborhood/settlement + city/region) is
genuinely geocodable via OpenStreetMap Nominatim (geo_utils.Geocoder).
Sofia-only scraping could afford a live geocode call per cache miss
in-line (a few hundred distinct neighborhood names total, one-time
cost) - nationwide, that assumption breaks: a bounded 1-page-per-type
trial run (~80 listings, almost all cache misses against the old
Sofia-only cache) took over 10 minutes and was still running when
cancelled, even after cutting Nominatim's request timeout from 15s to
8s. Blocking the actual scrape on live geocoding at nationwide scale
risks either starving it of runtime or hammering Nominatim's free,
rate-limited public endpoint well past what a single sequential 1
req/sec caller can clear in one run. So fetch_listings() now only does
a cache-only lookup (Geocoder.geocode_cached_only - no network call,
returns None on a miss) - existing cached areas (mostly Sofia, from
prior runs) resolve for free, everything new starts as lat=lng=None and
gets filled in over time by backfill_geocode_homes.py, a separate
workflow_dispatch job that does the live, rate-limited Nominatim calls
without competing with the scrape for time or blocking it on network
flakiness. extract_area() used to split specifically on ", София"
(Sofia-only scraping never needed anything else); now that every city
in Bulgaria shows up here, it generically splits on the last comma
instead, keeping the more specific neighborhood/settlement part as
"area" - the geocode query itself (built the same way here and in the
backfill script) uses the full location string rather than assuming a
Sofia suffix.

Pagination depth cap and the price-band slicing fix: a real nationwide
run found every type's pagination stopping at exactly page 49
(~980-1,000 results) regardless of the much larger offersCount each type
reports - a site-side depth cap, not a natural end of results (confirmed:
even a single narrow-enough slice paginates cleanly past where the cap
would otherwise bite, to a real partial last page with hasMoreItems=
False). Live diagnostics (probe_homes_slicing.py, kept for reference)
found priceFrom/priceTo are real, working filter params, and that the
real nationwide price distribution is heavily skewed - flat price bands
aren't fine-grained enough in the dense middle of the market, so
bisect_price_slices() recursively narrows each type's [0, 1,000,000)
price range, splitting any slice whose own offersCount is still over a
900-result safety margin (SLICE_THRESHOLD, comfortably under the ~1,000
cap), until every leaf slice is safely paginable to its true end;
everything above 1,000,000 (confirmed consistently small - 108-573
listings per type - across live checks) is scraped as one final tail
slice. This full scheme was verified live before being wired in here:
236 count-only requests found the real retrievable total across all 4
types is ~70,253 listings, versus the ~3,900 the old flat per-type loop
actually retrieved.
"""

import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

import requests

from geo_utils import (
    Geocoder, compute_motivation_score, listing_city_key, prune_snapshots,
    evict_stale_records, STALE_RECORD_RETENTION, load_json_any, save_json_any,
    extract_description_homes,
)

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; PersonalDealTracker/1.0)"}
BASE_URL = "https://www.homes.bg"
MAX_PAGES = 5000
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 5
MAX_CONSECUTIVE_PAGE_FAILURES = 5

# (typeId query value, our 6-category bucket). LandParcel and LandAgro are
# two distinct homes.bg search categories (regular plots vs agricultural
# land) that both map onto our single "land" bucket.
TYPE_QUERIES = [
    ("ApartmentSell", "flat"),
    ("HouseSell", "house"),
    ("LandParcel", "land"),
    ("LandAgro", "land"),
]

# Safety margin under homes.bg's confirmed ~1,000-result pagination depth
# cap - a slice at or under this is trusted to paginate cleanly to its real
# end. MAX_SLICE_PRICE is where price-band bisection stops and the rest
# (confirmed consistently small - 108-573 listings/type live) is scraped as
# one final tail slice instead of being bisected further.
SLICE_THRESHOLD = 900
MAX_SLICE_PRICE = 1_000_000
MAX_BISECT_DEPTH = 12
MIN_SLICE_WIDTH = 500

OUT_DIR = Path(__file__).parent / "data"
OUT_DIR.mkdir(exist_ok=True)
# .json.gz, not plain .json - 2026-09-25 addendum to the GH001 incident fix
# (see geo_utils.py's own "Compressed on-disk JSON storage" comment for the
# real numbers): homes.bg's own record count jumped ~1.90x (dd83178's
# tracking-ID collision fix unmasking previously-hidden listings), and even
# aggressive photo-capping couldn't get that back under GitHub's 100MB
# limit without gzip - every read/write of these two files must go through
# load_json_any()/save_json_any(), never raw .read_text()/.write_text().
HISTORY_FILE = OUT_DIR / "history_homes.json.gz"
LEADS_FILE = OUT_DIR / "leads_homes.json.gz"

BGN_TO_EUR = 1.95583

STATE_RE = re.compile(r"window\.__PRELOADED_STATE__\s*=\s*(\{.*?\});", re.DOTALL)
SQM_RE = re.compile(r"(\d+)\s?m²")

# homes.bg's own URL scheme type-prefixes every numeric offer id (hs=
# HouseSell, as=ApartmentSell, lp=LandParcel, la=LandAgro) precisely because
# those ids are only unique *within* a type, not across the whole site - the
# last path segment of every offer's viewHref is exactly "<prefix><id>"
# (confirmed against the full leads_homes.json dataset: as/hs/lp/la are the
# only prefixes that appear, and each maps 1:1 onto one of the 4
# TYPE_QUERIES types). build_tracking_id() below previously dropped this
# prefix (just "homes_" + the bare numeric id), which let two completely
# unrelated listings of different types silently collide onto one tracking
# key whenever homes.bg happened to reuse the same numeric id across types -
# confirmed to have actually happened and corrupted real records (see
# docs/backlog.md item 23 and docs/decisions.md for the confirmed cases and
# the one-time backfill/split plan for the listings already corrupted under
# the old, unprefixed scheme).
OFFER_URL_ID_RE = re.compile(r"/([a-z]{2})(\d+)$")


def build_tracking_id(offer):
    """Builds this offer's tracking id the same way homes.bg itself scopes
    its numeric ids: by type prefix, taken from the offer's own viewHref
    (the ground truth homes.bg assigns), not guessed from our own
    category/type_id bucketing. Falls back to the old unprefixed id (with a
    DEBUG log) only if viewHref doesn't match the expected shape - matching
    this module's existing "never crash the whole run over one malformed
    offer" philosophy - so a future homes.bg URL format change degrades
    gracefully instead of raising.
    """
    offer_id = offer.get("id")
    href = offer.get("viewHref") or ""
    match = OFFER_URL_ID_RE.search(href)
    if match:
        return "homes_" + match.group(1) + match.group(2)
    print(f"DEBUG: could not extract a type prefix from viewHref={href!r} for offer id={offer_id} - "
          f"falling back to the unprefixed tracking id (collision risk)")
    return "homes_" + str(offer_id)


def parse_price_eur(price):
    # A "price on request" listing reports "value": false (a bool, not a
    # numeric string) instead of omitting the field - found live on a real
    # nationwide run (a HouseSell listing crashed the whole scrape here,
    # discarding everything already fetched since fetch_listings() only
    # returns its results at the very end). Any other unparseable value
    # gets the same treatment: no price to report, not a crash.
    raw = price.get("value")
    if not isinstance(raw, str):
        return None
    try:
        value = float(raw.replace(",", ""))
    except ValueError:
        return None
    if price.get("currency") == "BGN":
        return round(value / BGN_TO_EUR)
    return round(value)


def extract_area(location):
    if not location:
        return "Bulgaria"
    if "," in location:
        area = location.rsplit(",", 1)[0].strip()
        return area or location.strip()
    return location.strip()


def extract_city(location):
    # location is "<neighborhood/settlement>, <city>" - extract_area() above
    # keeps the more specific first part; the city name after the last comma
    # was previously discarded entirely, which is what left every homes.bg
    # listing's "city" field empty and made the frontend's city filter/tabs
    # fall back to always assuming Sofia (see index.html's listingCityKey()).
    # A location with no comma (rare) has no separate city to extract.
    if not location or "," not in location:
        return None
    city = location.rsplit(",", 1)[1].strip()
    return city or None


def fetch_with_retries(session, url):
    for attempt in range(1, MAX_RETRIES + 1):
        resp = None
        try:
            resp = session.get(url, headers=HEADERS, timeout=20)
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as e:
            # Logging the real HTTP status (when there is one) distinguishes
            # "homes.bg is throttling us" (429/403) from a plain timeout/
            # connection error (no response at all) - both look identical
            # as a generic "request failed" message otherwise, and a real
            # multi-hour run needs this to be diagnosable after the fact.
            status = resp.status_code if resp is not None else "no response (timeout/connection error)"
            print(f"DEBUG: request failed for {url} (attempt {attempt}/{MAX_RETRIES}, status={status}): {e}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)
    return None


# Rate limit for backfill_detail_homes.py's own detail-page visits - the
# main grid crawl above has no need for one (each search-page fetch already
# returns dozens of listings, so its own natural page-to-page cadence is
# already far below one request/listing), but a detail backfill makes one
# plain HTTP request per LISTING, the same shape backfill_detail_bazar.py
# already rate-limits with the same value, for the same reason: be polite to
# a site that (unlike imot.bg/olx.bg) has shown no anti-bot blocking so far,
# rather than risk becoming the reason it starts.
DETAIL_REQUEST_DELAY_SECONDS = 1.0


def fetch_listing_detail(session, listing):
    """Visits this listing's own detail page (offer["url"]) and fills in a
    real free-text description via extract_description_homes() - see that
    function's own module comment in geo_utils.py for the full 3-tier
    search order and why this is the best that could be built without live
    homes.bg access (docs/backlog.md item 9d).

    Returns whether the page actually loaded (used by
    fetch_listing_details() below to detect a run of consecutive
    failures) - mirrors scraper_imot.py's own fetch_listing_detail()
    exactly: "detail_checked" is set only once a real page was fetched, so
    a transient failure (site throttling, a timeout) doesn't permanently
    skip a listing that was never actually looked at."""
    html = fetch_with_retries(session, listing["url"])
    if html is None:
        return False
    listing["detail_checked"] = True
    description = extract_description_homes(html, title=listing.get("title"))
    if description:
        listing["description"] = description
    return True


def fetch_listing_details(listings, on_checkpoint=None, checkpoint_every=150, deadline=None):
    """Batch driver for backfill_detail_homes.py - same deadline/
    on_checkpoint/consecutive-failure-stop shape as scraper_imot.py's own
    fetch_listing_details(), adapted for a plain requests.Session() (homes.bg
    has shown no anti-bot blocking on its grid crawl, unlike imot.bg/olx.bg,
    so no Playwright browser is needed here)."""
    session = requests.Session()
    consecutive_failures = 0
    for i, listing in enumerate(listings, 1):
        if deadline is not None and time.monotonic() >= deadline:
            print(f"DEBUG: stopping at {i - 1}/{len(listings)} - approaching this run's time budget")
            break
        time.sleep(DETAIL_REQUEST_DELAY_SECONDS)
        ok = fetch_listing_detail(session, listing)
        if ok:
            consecutive_failures = 0
        else:
            consecutive_failures += 1
            if consecutive_failures >= MAX_CONSECUTIVE_PAGE_FAILURES:
                print(f"DEBUG: {consecutive_failures} consecutive detail-page failures at "
                      f"{i}/{len(listings)} - looks like the site is throttling this run, stopping here")
                break
        if i % 200 == 0:
            print(f"DEBUG: checked {i}/{len(listings)} listings")
        if on_checkpoint and i % checkpoint_every == 0:
            on_checkpoint()


def build_url(type_id, page, price_from=None, price_to=None):
    params = {"locationId": "0", "typeId": type_id}
    if price_from is not None:
        params["priceFrom"] = str(price_from)
    if price_to is not None:
        params["priceTo"] = str(price_to)
    if page > 1:
        params["page"] = str(page)
    return f"{BASE_URL}/?{urlencode(params)}"


def get_offers_count(session, type_id, price_from, price_to):
    text = fetch_with_retries(session, build_url(type_id, 1, price_from, price_to))
    if text is None:
        return 0
    match = STATE_RE.search(text)
    if not match:
        return 0
    state = json.loads(match.group(1))
    return state.get("data", {}).get("offers", {}).get("offersCount", 0) or 0


def bisect_price_slices(session, type_id, lo, hi, depth=0):
    """Recursively narrows [lo, hi) until each leaf slice's own offersCount
    is under the depth-cap safety threshold, so it can paginate to its real
    end instead of being truncated. See the module docstring for the live
    verification this mirrors."""
    count = get_offers_count(session, type_id, lo, hi)
    if count <= SLICE_THRESHOLD or depth >= MAX_BISECT_DEPTH or hi - lo < MIN_SLICE_WIDTH:
        return [(lo, hi)]
    mid = lo + (hi - lo) // 2
    return (bisect_price_slices(session, type_id, lo, mid, depth + 1)
            + bisect_price_slices(session, type_id, mid, hi, depth + 1))


def parse_offer(offer, category, geocoder):
    # A single malformed offer (an unexpected field shape homes.bg's own
    # JSON hasn't shown before) must not crash the whole run - the caller
    # only returns its results at the very end, so an uncaught exception
    # here would discard every page already fetched. Confirmed as a real
    # failure mode live: a "price on request" listing reports "value":
    # false (a bool, not a numeric string) instead of omitting the field,
    # which crashed parse_price_eur() and lost 49 ApartmentSell + 14
    # HouseSell pages of already-fetched results in one real nationwide run.
    sqm_match = SQM_RE.search(offer.get("title", ""))
    sqm = int(sqm_match.group(1)) if sqm_match else None

    photo = offer.get("photo")
    photo_url = None
    if photo:
        photo_url = f"https://g1.homes.bg/{photo['path']}{photo['name']}b.jpg"

    photos = []
    for p in offer.get("photos") or []:
        if isinstance(p, dict) and p.get("path") and p.get("name"):
            photos.append(f"https://g1.homes.bg/{p['path']}{p['name']}b.jpg")
    if not photos and photo_url:
        photos = [photo_url]

    price_eur = parse_price_eur(offer["price"])
    if price_eur is None:
        return None

    location = offer.get("location", "")
    area = extract_area(location)
    city = extract_city(location)
    title = f"{offer.get('title', '')}, {location}".strip(", ")
    geo_query = f"{location}, България" if location else f"{area}, България"
    coords = geocoder.geocode_cached_only(geo_query)

    return {
        "id": build_tracking_id(offer),
        "url": BASE_URL + offer["viewHref"],
        "photo": photo_url,
        "photos": photos,
        # homes.bg's own JSON key "description" is NOT a free-text listing
        # description, despite the name - confirmed via a full raw-offer
        # dump (probe_deeper_history.py, Actions run 32700254970, 2026-08-24):
        # the complete field set on a search-page offer is id/type/
        # viewHref/time/location/title/"description"/status/photos/photo/
        # price/isFav, and "description" itself is consistently a short
        # construction-material/furnishing tag line (e.g. "Тухла/Бетон,
        # Полуобзаведен" - "Brick/Concrete, Semi-furnished"), never prose -
        # also reconfirmed live by sampling data/leads_homes.json (backlog
        # #9). There is no other candidate field in this JSON blob holding
        # real description text; a genuine free-text description, if
        # homes.bg has one at all, would only live on each listing's own
        # detail page (offer["viewHref"]) - which this scraper never
        # fetches, since it only ever calls the paginated search/listing
        # endpoint (see module docstring), not each offer's page. Left
        # unset (None) rather than populated with the wrong tag line, which
        # was actively misleading users into thinking they were reading a
        # real description. Adding a real description would mean an extra
        # per-listing HTTP fetch across 74,000+ listings - that's a new
        # capability and a real scale/runtime tradeoff, not a selector fix,
        # so it's deliberately left to a separate follow-up (the
        # backfill_detail_alo.py/backfill_detail_bazar.py/etc. pattern
        # already used for other large portals: a separate, resumable
        # backfill pass rather than paying that cost inline in the main
        # scrape) instead of being done here.
        "description": None,
        "price_eur": price_eur,
        "sqm": sqm,
        "area": area,
        "city": city,
        "title": title,
        "portal": "homes.bg",
        "lat": coords["lat"] if coords else None,
        "lng": coords["lng"] if coords else None,
        "category": category,
        "category_confidence": "high",
    }


def scrape_slice(session, geocoder, type_id, category, lo, hi, seen, start_time):
    consecutive_failures = 0
    for page in range(1, MAX_PAGES + 1):
        url = build_url(type_id, page, lo, hi)
        text = fetch_with_retries(session, url)
        if text is None:
            # A failed fetch (all in-request retries exhausted) is not the
            # real end-of-results signal (that's hasMoreItems below) - giving
            # up on the whole slice here would silently truncate every
            # remaining page after one bad request, the same bug found in
            # scraper.py/scraper_alo.py/scraper_imot.py. Skip it and keep
            # going, only giving up on the slice after several in a row.
            consecutive_failures += 1
            print(f"DEBUG: {type_id} price[{lo}-{hi}] page {page} fetch failed ({consecutive_failures}/{MAX_CONSECUTIVE_PAGE_FAILURES} consecutive)")
            if consecutive_failures >= MAX_CONSECUTIVE_PAGE_FAILURES:
                break
            continue
        consecutive_failures = 0

        match = STATE_RE.search(text)
        if not match:
            print(f"DEBUG: no __PRELOADED_STATE__ found on {type_id} price[{lo}-{hi}] page {page}")
            continue
        state = json.loads(match.group(1))
        offers = state.get("data", {}).get("offers", {})
        results = offers.get("result", [])
        # t= is wall-clock elapsed since fetch_listings() started, not this
        # page/slice alone - a real multi-hour run with no way to see live
        # logs needs this to tell "still making progress" from "stuck
        # retrying the same page", after the fact from the final log.
        elapsed = time.monotonic() - start_time
        print(f"DEBUG: {type_id} price[{lo}-{hi}] page {page} offers count = {len(results)} "
              f"(t={elapsed:.0f}s, {len(seen)} listings so far)")

        for offer in results:
            listing_id = build_tracking_id(offer)
            if listing_id in seen:
                continue
            try:
                parsed = parse_offer(offer, category, geocoder)
                if parsed is not None:
                    seen[parsed["id"]] = parsed
            except Exception as e:
                print(f"DEBUG: skipping malformed offer {listing_id} on {type_id} price[{lo}-{hi}] page {page}: {e}")
                continue

        if not offers.get("hasMoreItems"):
            break


def fetch_listings():
    start_time = time.monotonic()
    session = requests.Session()
    seen = {}
    geocoder = Geocoder()

    for type_id, category in TYPE_QUERIES:
        type_start = time.monotonic()
        print(f"DEBUG: starting {type_id} at t={type_start - start_time:.0f}s, {len(seen)} listings so far")

        slices = bisect_price_slices(session, type_id, 0, MAX_SLICE_PRICE)
        tail_count = get_offers_count(session, type_id, MAX_SLICE_PRICE, None)
        if tail_count > SLICE_THRESHOLD:
            print(f"DEBUG: {type_id} tail slice ({MAX_SLICE_PRICE}+) has {tail_count} offers, "
                  f"over the {SLICE_THRESHOLD} safety threshold - pagination may truncate it")
        slices.append((MAX_SLICE_PRICE, None))
        print(f"DEBUG: {type_id} split into {len(slices)} price slices for full pagination")

        for lo, hi in slices:
            scrape_slice(session, geocoder, type_id, category, lo, hi, seen, start_time)

        print(f"DEBUG: finished {type_id} in {time.monotonic() - type_start:.0f}s, {len(seen)} listings so far")

    geocoder.save()
    return list(seen.values())


def load_history():
    if HISTORY_FILE.exists():
        return load_json_any(HISTORY_FILE)
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
    save_json_any(HISTORY_FILE, history)


# Fields fetch_listing_detail() adds on top of what the grid crawl
# (parse_offer()) itself produces - never present on a fresh grid-only
# record, which always sets "description": None (see parse_offer()'s own
# comment). update_history() below must merge these in from the previous
# "latest" rather than let a fresh grid re-touch wipe them off an
# already-detail-checked, still-active listing every ~6 hours - the same
# bug class docs/backlog.md items 9a/9c already fixed for the other seven
# scrapers. This scraper was *correctly* left out of that original fix (9c
# investigated it directly and found it genuinely not at risk at the time:
# photos come straight off the grid JSON every run, and lat/lng self-heal
# via the shared geocode cache) - that stopped being true the moment
# backfill_detail_homes.py shipped (item 9d), since description/
# detail_checked are now real detail-only fields on "latest" the grid crawl
# never produces. Same merge-not-replace pattern already used by
# scraper_imot.py/scraper_alo.py/etc., copied here rather than reinvented.
_DETAIL_ONLY_FIELDS = ("description", "detail_checked")


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
        days_on_market = (effective_now - first_seen).days
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

        latest = rec["latest"]
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

    # Keyed by (city_key, area), not area alone - "Център" ("center") is
    # the same literal text in dozens of Bulgarian towns, so a plain area
    # key silently averaged Sofia's Center together with Dobrich's,
    # Plovdiv's, etc. Same root cause as group_listings()'s own cross-city
    # merge bug (see sync_to_supabase.py) - a generic area name needs a
    # real, known city agreeing before it means anything on its own. A
    # listing whose city can't be resolved is excluded from every area's
    # average (same "leave unclassified rather than guess" rule
    # listing_city_key() already follows) and gets no area average itself.
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
    save_json_any(LEADS_FILE, leads)
    print(f"Found {len(listings)} listings, {len(leads)} tracked leads")


if __name__ == "__main__":
    main()
