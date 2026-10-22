"""
Scrapes current listings from OLX.bg, nationwide.

OLX.bg blocks plain requests-based fetching (an Akamai-style edge check
returns a 403 before any content), but a real headless browser gets through
cleanly - confirmed via Playwright/Chromium against the live site. Each
listing card is the smallest ancestor whose text mentions the price
("<amount> €") exactly once, same "climb from the link" approach as
scraper.py, scraper_alo.py, and scraper_imot.py. Within that card the text
follows one of two layouts depending on whether the listing is in a city
or a village (confirmed live across Plovdiv/Varna/Burgas oblasts):
    line 0: title (free text)
    line 1: "<price> €"
    line 2 (city listing): "гр. <City>, <area> - Обновено на <date>"
    line 2 (village listing): "с. <Village> - Обновено на <date>"
      (villages have no separate sub-area - the settlement itself is both
      the city and the area)
    line 3: "<sqm> кв.м - <price per sqm>"

Unlike imot.bg/imoti.net (no single "nationwide" URL exists at all), OLX.bg
does have one: dropping the region path segment entirely
(/nedvizhimi-imoti/prodazhbi/, no oblast-<slug> suffix) returns real,
distinct listings - but it turned out to hit the SAME real per-query depth
cap (~page 26-27, ~1,000-1,400 listings) as any single oblast-scoped query,
live-verified against Sofia and Plovdiv independently in the same browser
session. So the bare nationwide URL alone can't carry full national
coverage; oblast-level slicing (OBLAST_SLUGS below, each a live-verified
/nedvizhimi-imoti/prodazhbi/oblast-<slug>/ URL) is used instead, the same
pattern imot.bg needed at city granularity. 26 of Bulgaria's 28
administrative oblasts resolved to a real olx.bg oblast page (Targovishte
and Sofia-oblast/Sofia-province - distinct from Sofia-city, which IS
covered - didn't resolve to a guessed slug and are skipped).

Because oblast slicing is coarser than a real city (an oblast query returns
listings from every settlement in it, not just its namesake city), each
listing's city/area is parsed from its own card text rather than trusted
from the query - same reasoning as alo.bg's LOCATION_RE, just with the
extra city-vs-village branch above.

Search results are paginated with ?page=N (confirmed via the site's own
paginator). A page navigation retries a few times with backoff before
being treated as the end of pagination, so a transient failure doesn't get
mistaken for having reached the last page - and, since scrape.yml runs
several scrapers sequentially with a single git commit step at the end, an
uncaught exception here would otherwise silently discard every other
scraper's output for that run too. Past the real depth cap boundary, the
site still renders a page with exactly one stray listing link (not zero) -
confirmed live - so pagination stops on <=1 real links, not only on 0.

Line 2's "Обновено на <date>" (confirmed live format: "20 август 2026 г.")
or bare "Днес"/"Вчера" reflects when the listing was actually last updated
on OLX, and is parsed into a real date so days_on_market/motivation score
are computed from that instead of purely from when we first scraped the
listing - otherwise a listing that's actually been up for months shows as
"0 days on market" just because we only just started tracking it.

olx.bg carries no coordinates anywhere on its own pages, but every
listing's real settlement/neighborhood name is genuinely geocodable -
resolved here via OpenStreetMap Nominatim (geo_utils.Geocoder). At
nationwide scale, doing that live and inline during the scrape doesn't fit
in a single run (the same problem scraper_homes.py hit first - see its
module docstring), so this only does a cache-only lookup and leaves the
rest for backfill_geocode_olx.py to fill in as a separate, decoupled pass.
This portal's search also isn't apartments-only (it's "all real estate for
sale"), so each listing's category (apartment/house/land/commercial) is
classified from its title too, for the frontend's same-category
radius-average feature.
"""

import re
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup

from category_classifier import classify_listing
from geo_utils import (
    Geocoder, extract_description_ldjson, extract_photos_ldjson, extract_sqm_ldjson,
    compute_motivation_score, listing_city_key, prune_snapshots, evict_stale_records,
    STALE_RECORD_RETENTION, load_json_any, save_json_any,
)

BASE_URL = "https://www.olx.bg"
SEARCH_BASE = "https://www.olx.bg/nedvizhimi-imoti/prodazhbi"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# (oblast display name, URL slug) - each slug live-verified to return a
# real olx.bg oblast page (status 200, real listing links) before being
# trusted here; see the module docstring for the diagnostic trail.
OBLAST_SLUGS = [
    ("София", "oblast-sofiya-grad"),
    ("Пловдив", "oblast-plovdiv"),
    ("Варна", "oblast-varna"),
    ("Бургас", "oblast-burgas"),
    ("Русе", "oblast-ruse"),
    ("Стара Загора", "oblast-stara-zagora"),
    ("Плевен", "oblast-pleven"),
    ("Сливен", "oblast-sliven"),
    ("Добрич", "oblast-dobrich"),
    ("Шумен", "oblast-shumen"),
    ("Перник", "oblast-pernik"),
    ("Хасково", "oblast-haskovo"),
    ("Ямбол", "oblast-yambol"),
    ("Пазарджик", "oblast-pazardzhik"),
    ("Благоевград", "oblast-blagoevgrad"),
    ("Велико Търново", "oblast-veliko-tarnovo"),
    ("Враца", "oblast-vratsa"),
    ("Габрово", "oblast-gabrovo"),
    ("Видин", "oblast-vidin"),
    ("Кюстендил", "oblast-kyustendil"),
    ("Кърджали", "oblast-kardzhali"),
    ("Монтана", "oblast-montana"),
    ("Ловеч", "oblast-lovech"),
    ("Силистра", "oblast-silistra"),
    ("Разград", "oblast-razgrad"),
    ("Смолян", "oblast-smolyan"),
]

OUT_DIR = Path(__file__).parent / "data"
OUT_DIR.mkdir(exist_ok=True)
# .json.gz, not plain .json - 2026-09-26: olx.bg was trending toward the
# same GH001 wall alo.bg/bazar.bg already hit (~83MB/~82MB, closing in on
# GitHub's 100MB hard limit) - migrated proactively in the same pass
# rather than firefighting it later. Same proven fix as homes.bg
# (2026-09-25 addendum)/alo.bg/bazar.bg - see geo_utils.py's "Compressed
# on-disk JSON storage" comment. load_json_any()/save_json_any() are
# extension-aware.
HISTORY_FILE = OUT_DIR / "history_olx.json.gz"
LEADS_FILE = OUT_DIR / "leads_olx.json.gz"
# Persists only which OBLAST_SLUGS index the grid crawl should start from
# next run (see fetch_listings()'s own comment) - deliberately its own tiny
# file rather than folded into history_olx.json, since it's loop-position
# state, not per-listing state, and item 31 (bazar.bg/imot.bg oblast-level
# coverage, docs/backlog.md) is expected to reuse this exact small-file
# pattern for its own rotation state rather than inventing a new one.
GRID_STATE_FILE = OUT_DIR / "olx_grid_state.json"

MAX_CARD_TEXT_LENGTH = 500
MAX_PRICE_MENTIONS = 1
# The real per-query depth cap sits around page 26-27 (~1,000-1,400
# listings) - 30 gives a small safety margin before giving up on an oblast
# without wasting requests deep past the cap.
MAX_PAGES = 30
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 5
MAX_CONSECUTIVE_PAGE_FAILURES = 5

# docs/backlog.md item 30: all 6 most recent scheduled runs hit the
# workflow step's full 60-minute timeout-minutes cap exactly (60m12s-
# 60m13s each, confirmed via the real GitHub Actions logs), getting through
# only ~15 of OBLAST_SLUGS' 26 oblasts (~4min/oblast average) before being
# hard-killed mid-page - continue-on-error then reported the workflow green
# anyway. 50 minutes leaves a 10-minute buffer under that 60-minute cap -
# comfortably more than the worst-case overshoot of one single page fetch
# already in flight when the deadline is checked (fetch_html_with_retries()
# tops out around 3 attempts * 30s nav timeout + <=15s backoff, ~1.75min)
# plus the final checkpoint (a fast, local JSON write) - while keeping
# real headroom over the ~15 oblasts/run the unbounded loop used to get
# through before being killed.
TIME_BUDGET_SECONDS = 50 * 60

LISTING_LINK_RE = re.compile(r"/d/ad/[^\"'#]*-ID(\w+)\.html")
PRICE_RE = re.compile(r"[\d\s]{3,10}\s?€")
PRICE_LINE_RE = re.compile(r"^([\d\s]{3,10})\s?€$")
CITY_AREA_LINE_RE = re.compile(r"^гр\.\s*(.+?),\s*(.+?)\s-\s")
VILLAGE_LINE_RE = re.compile(r"^с\.\s*(.+?)\s-\s")
UPDATED_RE = re.compile(r"-\s*(Днес|Вчера|Обновено на\s+(\d{1,2})\s+(\S+)\s+(\d{4})\s*г\.?)", re.IGNORECASE)
SQM_RE = re.compile(r"([\d.,]+)\s?кв\.?м")

BG_MONTHS = {
    "януари": 1, "февруари": 2, "март": 3, "април": 4, "май": 5, "юни": 6,
    "юли": 7, "август": 8, "септември": 9, "октомври": 10, "ноември": 11, "декември": 12,
}


def parse_site_updated_at(line):
    m = UPDATED_RE.search(line)
    if not m:
        return None
    now = datetime.now(timezone.utc)
    label = m.group(1).lower()
    if label.startswith("днес"):
        return now.isoformat()
    if label.startswith("вчера"):
        return (now - timedelta(days=1)).isoformat()
    day, month_name, year = m.group(2), m.group(3).lower(), m.group(4)
    month = BG_MONTHS.get(month_name)
    if not month:
        return None
    try:
        return datetime(int(year), month, int(day), tzinfo=timezone.utc).isoformat()
    except ValueError:
        return None


def fetch_html(page, url):
    page.goto(url, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(1500)
    # Listing photos are lazy-loaded as cards enter the viewport, so scroll
    # all the way to the bottom (slowly, so each batch has time to actually
    # finish loading, not just get requested) to force them all in before
    # reading src.
    for _ in range(35):
        page.mouse.wheel(0, 1200)
        page.wait_for_timeout(500)
        at_bottom = page.evaluate(
            "window.innerHeight + window.scrollY >= document.body.scrollHeight - 10"
        )
        if at_bottom:
            break
    try:
        page.wait_for_load_state("networkidle", timeout=5000)
    except Exception:
        pass
    return page.content()


def smallest_container_with_price(link_tag, max_levels=8):
    node = link_tag
    for _ in range(max_levels):
        if node.parent is None:
            break
        node = node.parent
        text = node.get_text(" ", strip=True)
        matches = PRICE_RE.findall(text)
        if len(matches) > MAX_PRICE_MENTIONS:
            return None
        if 1 <= len(matches) <= MAX_PRICE_MENTIONS and len(text) <= MAX_CARD_TEXT_LENGTH:
            return node
    return None


def fetch_html_with_retries(page, url):
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return fetch_html(page, url)
        except Exception as e:
            print(f"DEBUG: navigation failed for {url} (attempt {attempt}/{MAX_RETRIES}): {e}")
            if attempt < MAX_RETRIES:
                page.wait_for_timeout(RETRY_BACKOFF_SECONDS * attempt * 1000)
    return None


def fetch_listings_page(page, url, seen, geocoder, oblast_display):
    html = fetch_html_with_retries(page, url)
    if html is None:
        return None
    soup = BeautifulSoup(html, "html.parser")

    all_links = soup.find_all("a", href=True)
    matching_links = [a for a in all_links if LISTING_LINK_RE.search(a["href"])]

    for a in matching_links:
        match = LISTING_LINK_RE.search(a["href"])
        listing_id = match.group(1)
        if listing_id in seen:
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

        city = oblast_display
        area = oblast_display
        site_updated_at = None
        for l in lines:
            m = CITY_AREA_LINE_RE.match(l)
            if m:
                city, area = m.group(1).strip(), m.group(2).strip()
                site_updated_at = parse_site_updated_at(l)
                break
            m2 = VILLAGE_LINE_RE.match(l)
            if m2:
                city = area = m2.group(1).strip()
                site_updated_at = parse_site_updated_at(l)
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
            if candidate and candidate.startswith("http") and "icon" not in candidate.lower():
                img_url = candidate
                break

        href = a["href"]
        full_url = href if href.startswith("http") else BASE_URL + href
        title = f"{lines[0]}, {area}" if lines else area
        coords = geocoder.geocode_cached_only(f"{area}, {city}, България")
        # Classified from the raw first card line (not the fuller `title`
        # with area appended) - same field classify_category() was always
        # given here, deliberately kept to avoid a new false-positive class
        # this migration didn't audit: an area/district name matching a
        # category keyword (e.g. "Промишлена зона"/"Бизнес хотел" are real
        # place names, already a confirmed false-positive source for other
        # portals - see category_classifier.py's own "industrial property"/
        # "commercial property" keyword comments and docs/decisions.md).
        category, category_confidence, _ = classify_listing(
            title=lines[0] if lines else title, url=full_url
        )

        seen[listing_id] = {
            "id": "olx_" + listing_id,
            "url": full_url,
            "photo": img_url,
            "price_eur": price_eur,
            "sqm": sqm,
            "area": area,
            "city": city,
            "title": title[:150],
            "portal": "olx.bg",
            "site_updated_at": site_updated_at,
            "lat": coords["lat"] if coords else None,
            "lng": coords["lng"] if coords else None,
            "category": category,
            "category_confidence": category_confidence,
        }
    return len(matching_links)


def goto_with_retries(page, url):
    # Lighter-weight than fetch_html()/fetch_html_with_retries() above - those
    # scroll the whole page to force lazy-loaded card photos into the DOM,
    # which only matters on a search-results grid page, not a single
    # listing's own detail page (nothing here is lazy-loaded behind scroll).
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(1500)
            return page.content()
        except Exception as e:
            print(f"DEBUG: navigation failed for {url} (attempt {attempt}/{MAX_RETRIES}): {e}")
            if attempt < MAX_RETRIES:
                page.wait_for_timeout(RETRY_BACKOFF_SECONDS * attempt * 1000)
    return None


def fetch_listing_detail(page, listing):
    # Returns whether the page actually loaded (used by
    # fetch_listing_details() to detect a run of consecutive failures).
    html = goto_with_retries(page, listing["url"])
    if html is None:
        # NOT marked detail_checked - goto_with_retries() already exhausted
        # its own in-page retries, but that failure could still be
        # transient (site throttling, a timeout), not a confirmed-gone
        # page. This used to mark detail_checked=True unconditionally
        # before even attempting the fetch, which meant a listing that hit
        # one bad request was silently and permanently skipped by every
        # future backfill run, losing its description/photos for good.
        # Leaving detail_checked unset lets a later run retry it.
        return False
    # Marked only once html is confirmed real, so a backlog scan can tell
    # "already attempted, got a real page, nothing more to gain" apart
    # from "never successfully visited yet" - same marker pattern
    # scraper.py/scraper_bcpea.py/scraper_imot.py/scraper_alo.py use for
    # their own detail backfills.
    listing["detail_checked"] = True
    description = extract_description_ldjson(html)
    if description:
        listing["description"] = description
    # Best-effort - olx.bg wasn't directly reachable to confirm its ld+json
    # blob carries an "image" key the way bazar.bg's does (see
    # geo_utils.extract_photos_ldjson()'s docstring); harmlessly returns
    # nothing if it doesn't.
    photos = extract_photos_ldjson(html)
    if photos:
        listing["photos"] = photos
    # Gap-filler only, from the same ld+json blob's "floorSize" - never
    # overwrites an sqm the grid crawl's own SQM_RE already found (see
    # geo_utils.extract_sqm_ldjson()'s own comment for why: unconfirmed
    # live, no evidence yet it's more reliable than the grid). Every other
    # spec/coordinate/agency field this project's other portals expose
    # (property_type_raw, construction_type, built_year, completion_status,
    # floor_number, floor_qualifier, features, has_elevator, furnished,
    # has_central_heating, agency_name, agency_website, lat/lng) was
    # investigated for olx.bg too and deliberately NOT added here - see
    # extract_sqm_ldjson()'s own comment in geo_utils.py for the field-by-
    # field reasoning (no standard Schema.org vocabulary for the Bulgarian
    # specs, a prior confirmed investigation already ruling out coordinates
    # anywhere on olx.bg's own pages, and real ambiguity - publisher vs.
    # actual seller - for ld+json "seller"/"author").
    if not listing.get("sqm"):
        sqm = extract_sqm_ldjson(html)
        if sqm:
            listing["sqm"] = sqm
    return True


def fetch_listing_details(listings, on_checkpoint=None, checkpoint_every=150, deadline=None):
    # One shared browser/page reused across the whole batch, same as
    # scraper_imot.py's own detail backfill.
    #
    # deadline/on_checkpoint let the caller save progress as it goes and
    # stop cleanly before the workflow's own timeout would hard-kill the
    # process, and a run of MAX_CONSECUTIVE_PAGE_FAILURES consecutive
    # failed page loads stops the run early - same fix as
    # backfill_detail_imoti_net.py/backfill_detail_alo.py's own timeout
    # problems.
    consecutive_failures = 0
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent=USER_AGENT, locale="bg-BG")
        page = context.new_page()
        for i, listing in enumerate(listings, 1):
            if deadline is not None and time.monotonic() >= deadline:
                print(f"DEBUG: stopping at {i - 1}/{len(listings)} - approaching this run's time budget")
                break
            ok = fetch_listing_detail(page, listing)
            if ok:
                consecutive_failures = 0
            else:
                consecutive_failures += 1
                if consecutive_failures >= MAX_CONSECUTIVE_PAGE_FAILURES:
                    print(f"DEBUG: {consecutive_failures} consecutive detail-page failures at "
                          f"{i}/{len(listings)} - looks like the site is throttling this run, stopping here")
                    break
            if on_checkpoint and i % checkpoint_every == 0:
                on_checkpoint()
        browser.close()


def load_grid_state():
    if GRID_STATE_FILE.exists():
        try:
            return json.loads(GRID_STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_grid_state(state):
    GRID_STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def fetch_listings(deadline=None, on_checkpoint=None):
    # deadline/on_checkpoint mirror the exact pattern fetch_listing_details()
    # already uses for its own detail-fetch phase (see that function's own
    # comment, and backfill_detail_olx.py for a real caller) - stop cleanly,
    # with everything fetched so far already saved, before the workflow
    # step's own timeout-minutes would hard-kill the process mid-page
    # instead (docs/backlog.md item 30: all 6 most recent scheduled runs hit
    # that 60-minute cap exactly and lost whatever wasn't committed yet).
    #
    # OBLAST_SLUGS is a fixed list order, so a bounded run that always
    # started at index 0 would always run out of budget on the same ~10
    # tail oblasts (confirmed: 15/26 covered every time, always the same
    # first 15) - GRID_STATE_FILE's next_start_index instead rotates which
    # oblast THIS run starts from to wherever the PREVIOUS run left off, so
    # a bounded run's leftover oblasts shift each time and every oblast gets
    # roughly even coverage across runs instead of the tail being starved
    # forever.
    #
    # No checkpoint_every here (unlike fetch_listing_details()'s fixed
    # item-count one) - a completed oblast (up to MAX_PAGES pages) is
    # already this loop's natural unit of progress, so on_checkpoint fires
    # once per completed oblast instead. A future caller with a different
    # natural batch size (e.g. item 31's bazar.bg/imot.bg oblast-level
    # crawl, docs/backlog.md) can add its own checkpoint_every-style
    # throttling on top of on_checkpoint without changing this shape.
    seen = {}
    geocoder = Geocoder()
    state = load_grid_state()
    start_index = state.get("next_start_index", 0) % len(OBLAST_SLUGS)
    order = OBLAST_SLUGS[start_index:] + OBLAST_SLUGS[:start_index]
    if start_index:
        print(f"DEBUG: grid crawl resuming at oblast index {start_index} "
              f"({order[0][0]}) per {GRID_STATE_FILE.name}'s rotation state")

    oblasts_completed = 0
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent=USER_AGENT, locale="bg-BG")
        page = context.new_page()

        for oblast_display, slug in order:
            if deadline is not None and time.monotonic() >= deadline:
                print(f"DEBUG: stopping grid crawl before {oblast_display} - approaching this run's time "
                      f"budget, {len(order) - oblasts_completed} of {len(order)} oblasts left for a future run")
                break

            search_url = f"{SEARCH_BASE}/{slug}/"
            oblast_before = len(seen)
            consecutive_failures = 0
            oblast_interrupted = False
            for page_num in range(1, MAX_PAGES + 1):
                if deadline is not None and time.monotonic() >= deadline:
                    print(f"DEBUG: stopping mid-{oblast_display} at page {page_num} - "
                          f"approaching this run's time budget")
                    oblast_interrupted = True
                    break
                url = search_url if page_num == 1 else f"{search_url}?page={page_num}"
                link_count = fetch_listings_page(page, url, seen, geocoder, oblast_display)
                print(f"DEBUG: {oblast_display} page {page_num} links matching listing URL pattern = {link_count}")
                if link_count is None:
                    # A failed fetch (all in-page retries exhausted) is not
                    # the real end-of-results signal (link_count <= 1 below
                    # is) - giving up on the whole oblast here would silently
                    # truncate every remaining page after one bad request,
                    # the same bug found in scraper.py/scraper_alo.py/
                    # scraper_imot.py/scraper_homes.py. Skip it and keep
                    # going, only giving up on the oblast after several in a
                    # row.
                    consecutive_failures += 1
                    if consecutive_failures >= MAX_CONSECUTIVE_PAGE_FAILURES:
                        break
                    continue
                consecutive_failures = 0
                if link_count <= 1:
                    break

            suffix = " (interrupted mid-page, will resume here next run)" if oblast_interrupted else ""
            print(f"DEBUG: {oblast_display} done, {len(seen) - oblast_before} new listings{suffix}")

            if oblast_interrupted:
                # Doesn't count as completed - next_start_index below stays
                # pointed at this same oblast so the next run resumes on it
                # instead of skipping straight past it.
                break

            oblasts_completed += 1
            if on_checkpoint:
                on_checkpoint(list(seen.values()), geocoder)

        browser.close()

    # If every oblast in `order` completed, (start_index + len(order)) wraps
    # back to start_index exactly - correct, since a fully-completed lap
    # makes the next run's starting point unimportant either way.
    next_start_index = (start_index + oblasts_completed) % len(OBLAST_SLUGS)
    save_grid_state({"next_start_index": next_start_index})
    if oblasts_completed < len(order):
        print(f"DEBUG: grid crawl covered {oblasts_completed}/{len(order)} oblasts this run - next run "
              f"resumes at oblast index {next_start_index} ({OBLAST_SLUGS[next_start_index][0]})")

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
# (fetch_listings_page()) itself produces - never present on a fresh
# grid-only record. update_history() below must merge these in from the
# previous "latest" rather than let a fresh grid re-touch wipe them off an
# already-detail-checked, still-active listing every ~6 hours -
# docs/backlog.md item 9a.
#
# "sqm" is a special case, same reasoning as scraper_alo.py's own
# _DETAIL_ONLY_FIELDS comment for the identical field: it's normally a GRID
# field (fetch_listings_page()'s own SQM_RE sets it directly from card
# text), which is why it isn't detail-only in the sense the other two
# fields above are - a fresh grid crawl that DOES find a real sqm value
# must still be able to overwrite an old one (real edits happen). But now
# that fetch_listing_detail() can ALSO fill sqm in via extract_sqm_ldjson()
# (for listings whose card text never showed "кв.м"), the same merge-not-
# replace protection every other detail-only field already gets is needed
# here too - otherwise a later grid-only re-touch that finds no sqm on the
# card would silently wipe a real detail-filled value. Being in this list
# still means "prefer the fresh value when the fresh value is non-empty"
# (see update_history() below) - a genuine grid-parsed sqm change still
# wins, this only stops a grid MISS from clobbering a real detail-page
# value.
_DETAIL_ONLY_FIELDS = ("description", "photos", "detail_checked", "sqm")


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
    history = load_history()
    recorded_ids = set()

    def record_new(listings_so_far):
        # Keyed by listing id (not just called once at the end) so a run
        # that checkpoints several times mid-crawl (see fetch_listings())
        # only ever appends the delta since the last checkpoint into
        # history's snapshots - calling update_history() again on the same
        # listing within one run would otherwise double-append a near-
        # duplicate snapshot for it.
        nonlocal history
        new_listings = [l for l in listings_so_far if l["id"] not in recorded_ids]
        if not new_listings:
            return
        history = update_history(history, new_listings)
        recorded_ids.update(l["id"] for l in new_listings)
        save_history(history)
        leads = compute_leads(history)
        save_json_any(LEADS_FILE, leads)

    def checkpoint(listings_so_far, geocoder):
        record_new(listings_so_far)
        geocoder.save()

    deadline = time.monotonic() + TIME_BUDGET_SECONDS
    listings = fetch_listings(deadline=deadline, on_checkpoint=checkpoint)
    # Final catch-up: fetch_listings() only checkpoints after each *fully
    # completed* oblast, so a run interrupted mid-oblast (deadline hit
    # partway through that oblast's own page loop) can still be holding a
    # few pages' worth of listings that were never checkpointed.
    # record_new()'s id-delta tracking makes this a cheap no-op on a normal
    # run where nothing was interrupted. geocoder.save() isn't repeated here
    # - fetch_listings() already called it itself right before returning.
    record_new(listings)
    leads = compute_leads(history)
    print(f"Found {len(listings)} listings, {len(leads)} tracked leads")


if __name__ == "__main__":
    main()
