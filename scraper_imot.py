"""
Scrapes current listings from imot.bg, nationwide.

imot.bg blocks plain requests-based fetching (a Cloudflare/Akamai-style bot
check returns a JS challenge page instead of real content), but a real
headless browser gets through cleanly - confirmed via Playwright/Chromium
against the live site. Each listing card is the smallest ancestor whose
text mentions the price ("<amount> €") exactly once, same "climb from the
link" approach as scraper.py and scraper_alo.py. Within that card the text
follows a consistent per-line layout, confirmed identical across Sofia,
Plovdiv, Varna and Burgas:
    line 0: title (e.g. "Продава 2-СТАЕН")
    line 1: "град <City>, <area>" (city listing) or "с. <Village>" (village
      listing - the village itself is both city and area, same convention
      olx.bg's own VILLAGE_LINE_RE uses; not independently live-sampled
      here the way the "град" shape was, since only actual cities were
      queryable before this module's own oblast-level crawl below - see
      that section's own comment)
    line 2: "<price> €"
    line 3: "<sqm> кв.м, <floor/description/phone...>"

Unlike homes.bg/alo.bg (a single URL switch drops the location filter for
a genuine nationwide result) or imoti.net (city segments requiring their
own English-spelled slug), imot.bg has no single "all of Bulgaria" URL at
all AND its own per-query pagination hits a real depth cap around page
27-28 (~1,080 listings at 40/page) regardless of scope - live-verified:
Sofia, Plovdiv, and even the bare /obiavi/prodazhbi URL (no city segment)
all cap at the same boundary independently. Extensive live probing (a
price-filter form turned out to be 82 hidden POST fields with no usable
GET param, and no district/kvartal URL segment exists either - see git
history for the full diagnostic trail) found no way to sub-slice a single
over-cap city further. City-only slicing (CITY_SLUGS below, each a
live-verified /obiavi/prodazhbi/grad-<slug> URL) was the first coverage
this module shipped: every city fits comfortably under the cap except
Sofia itself (confirmed 1000+ listings by the site's own UI), which is
accepted as still capped at ~1,080 rather than fully complete - a known,
documented limitation, not a bug. 24 of 29 canonical BG_CITIES resolved to
a real imot.bg city page; the other 5 (smaller towns - Asenovgrad,
Kazanlak, Dimitrovgrad, Dupnitsa, Svishtov) don't appear to have their own
page and are skipped.

docs/backlog.md item 31: CITY_SLUGS' 24 cities, however it's sliced, can
only ever cover a fixed, manually-curated set of towns - Bulgaria's ~230
other towns and ~5,000 villages were structurally never queried at all
(100% of every captured listing's own `city` field was confirmed to be one
of the 24 allowlisted names, zero exceptions). OBLAST_SLUGS below adds
real oblast (province)-level coverage on top - imot.bg does support this:
confirmed via web-search-indexed real imot.bg URLs (this sandbox's own
network egress to imot.bg is fully blocked, so unlike the CITY_SLUGS
verification above this could not be a direct Playwright/browser fetch;
every slug below was independently confirmed real by finding actual,
distinctly-titled imot.bg pages indexed for it - e.g. "Имоти в с. Велчево,
област Ловеч за продажба | Imot.bg" at
/obiavi/prodazhbi/oblast-lovech/s-velchevo - not guessed from a naming
pattern) - same URL shape as CITY_SLUGS
(/obiavi/prodazhbi/oblast-<slug>), reusing the identical oblast slug
spelling scraper_olx.py's own OBLAST_SLUGS already uses for 26 of 27
entries (imot.bg's own oblast pages are independently real, not copied
blind - each was confirmed to exist above; they just turn out to use the
same slug spelling olx.bg does for every oblast both portals cover). Two
confirmed differences from olx.bg's OBLAST_SLUGS, both content, not
copying error:
  - Sofia's slug is "sofiya", not olx.bg's "oblast-sofiya-grad" - imot.bg
    does not split Sofia city from Sofia Province into two separate oblast
    pages the way olx.bg (and Bulgaria's real 28-oblast administrative
    split) does. Confirmed live: /obiavi/prodazhbi/oblast-sofiya/gr-sofiya
    (Sofia city) AND /obiavi/prodazhbi/oblast-sofiya/gr-botevgrad,
    .../s-gradets (real Sofia Province towns/villages) are indexed under
    the SAME "oblast-sofiya" URL. One consequence: this single combined
    query shares imot.bg's ~1,080-listing depth cap across both Sofia city
    AND every Sofia Province town/village, so - unlike every other
    oblast - it cannot be trusted to carry full Sofia city coverage on its
    own; CITY_SLUGS' own dedicated "grad-sofiya" query (which already
    accepts being capped at ~1,080 for Sofia city alone, see above) is
    kept running in the city-level phase specifically so Sofia city
    coverage doesn't get diluted by the rest of Sofia Province sharing the
    same combined oblast query's cap.
  - Търговище/Targovishte is included - confirmed real on imot.bg
    (/obiavi/prodazhbi/oblast-targovishte, real listings indexed) even
    though scraper_olx.py's own docstring notes this oblast didn't resolve
    to a real page on olx.bg and is skipped there.
  27 oblast pages, covering all 28 of Bulgaria's real administrative
  oblasts (since imot.bg's single combined "oblast-sofiya" page already
  covers the same Sofia-city + Sofia-Province territory olx.bg needs two
  separate oblast pages for) - genuinely complete territorial coverage,
  not a curated subset like CITY_SLUGS.

Because an oblast query is coarser than a real city (it returns listings
from every settlement in the oblast, not just one queried city), each
listing's city AND area are now parsed from its own card text (see the
per-line layout above) rather than trusted from which CITY_SLUGS/
OBLAST_SLUGS entry produced it - same reasoning, and largely the same
regex shape, as olx.bg's own CITY_AREA_LINE_RE/VILLAGE_LINE_RE. This
replaces the old "tag city from the query slug" mechanism entirely
(including for CITY_SLUGS' own city-level queries, not just the new
oblast ones) rather than keeping two different tagging mechanisms
side by side. One known, deliberately NOT "fixed here" consequence:
docs/decisions.md's 2026-09-23 location-allocation entry found imot.bg's
own site groups Cherven Bryag (a real Pleven-oblast town) under a
"grad-lovech"/"oblast-lovech" URL and municipality claim
("...obshtina-lovech") that is factually wrong (Cherven Bryag is its own
separate municipality in Pleven oblast) - that wrong claim is imot.bg's
own site data, not a scraper misread, so if imot.bg's own rendered card
text for that listing also spells its city as "Ловеч" (not confirmed
either way - this sandbox has no live access to that specific card),
parsing straight from card text would faithfully reproduce imot.bg's own
error rather than silently paper over it, exactly as it should: a scraper
that returns what the source site actually says, not what the scraper
guesses the site "should" say. `sync_to_supabase.py`'s portal-agnostic
`CITY_AREA_OBLAST_OVERRIDE` (docs/decisions.md, same entry) already exists
specifically to correct this exact (city, area) pair downstream, past
scraper output - out of this module's own scope.

A page navigation retries a few times with backoff, and if all retries for
one page are exhausted, that page is skipped (not treated as the end of
the query's pagination) - only MAX_CONSECUTIVE_PAGE_FAILURES in a row
gives up on that city/oblast. Without this, one bad request mid-crawl
would silently truncate every remaining page for that query, identically
to the bug found in scraper.py/scraper_alo.py. Separately, since scrape.yml
runs several scrapers sequentially with a single git commit step at the
end, an uncaught exception here would otherwise silently discard every
other scraper's output for that run too.

docs/backlog.md item 31 (after item 30's fix on scraper_olx.py):
CITY_SLUGS' 24-city phase runs first, unbounded by design - it has
reliably finished well within the workflow's own 60-minute timeout on
every one of 6 consecutive recent runs even before OBLAST_SLUGS existed,
so it keeps that shape rather than gaining rotation machinery it doesn't
need (a deadline check is still present as a pure defensive safety net).
OBLAST_SLUGS' 27-oblast phase runs second and DOES reuse
scraper_olx.py's exact checkpointed/rotating-index mechanism wholesale
(deadline/on_checkpoint in fetch_listings(), GRID_STATE_FILE persisting
which OBLAST_SLUGS index to resume from next run so a bounded run's
leftover oblasts rotate to the front next time instead of the same tail
being starved forever) - adding a full second, oblast-scale crawl on top
of an already-timeout-respecting city crawl is exactly the shape item 30
already fixed for olx.bg, so this does not reinvent that mechanism.
Overlap between CITY_SLUGS and OBLAST_SLUGS (e.g. Sofia listings returned
by both "grad-sofiya" and "oblast-sofiya") is deduped the same way both
scrapers already dedup everything else: by listing id, via the single
`seen` dict shared across both phases within a run.

imot.bg genuinely carries no coordinates anywhere in its own pages -
confirmed by a real headless browser (cookie consent handled, WebGL
software rendering enabled, navigator.webdriver patched away) finding no
map DOM node, no live google.maps.Map object, and no maps iframe on a
real listing, plus a plain static-HTML fetch turning up nothing either.
So each listing's real area name is geocoded via OpenStreetMap Nominatim
(geo_utils.Geocoder) - but at nationwide scale, doing that live and inline
during the scrape doesn't fit in a single run (the same problem
scraper_homes.py hit first - see its module docstring), so this only does
a cache-only lookup and leaves the rest for backfill_geocode_imot.py to
fill in as a separate, decoupled pass. This portal's search also isn't
apartments-only (it's "all sales" per query), so each listing's category is
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
    Geocoder, extract_description_imot, extract_photos_imot, compute_motivation_score,
    listing_city_key, prune_snapshots, evict_stale_records, STALE_RECORD_RETENTION,
    load_json_any, save_json_any,
)

BASE_URL = "https://www.imot.bg"
SEARCH_BASE = "https://www.imot.bg/obiavi/prodazhbi"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# (city display name, URL slug) - each slug live-verified to return a real
# imot.bg city page (status 200, real listing links) before being trusted
# here; see the module docstring for the diagnostic trail. Sofia keeps its
# already-proven slug from the original Sofia-only scraper.
CITY_SLUGS = [
    ("София", "grad-sofiya"),
    ("Пловдив", "grad-plovdiv"),
    ("Варна", "grad-varna"),
    ("Бургас", "grad-burgas"),
    ("Русе", "grad-ruse"),
    ("Стара Загора", "grad-stara-zagora"),
    ("Плевен", "grad-pleven"),
    ("Сливен", "grad-sliven"),
    ("Добрич", "grad-dobrich"),
    ("Шумен", "grad-shumen"),
    ("Перник", "grad-pernik"),
    ("Хасково", "grad-haskovo"),
    ("Ямбол", "grad-yambol"),
    ("Пазарджик", "grad-pazardzhik"),
    ("Благоевград", "grad-blagoevgrad"),
    ("Велико Търново", "grad-veliko-tarnovo"),
    ("Враца", "grad-vratsa"),
    ("Габрово", "grad-gabrovo"),
    ("Видин", "grad-vidin"),
    ("Кюстендил", "grad-kyustendil"),
    ("Кърджали", "grad-kardzhali"),
    ("Монтана", "grad-montana"),
    ("Търговище", "grad-targovishte"),
    ("Ловеч", "grad-lovech"),
    ("Силистра", "grad-silistra"),
]

# (oblast display name, URL slug) - each slug confirmed real via
# web-search-indexed imot.bg pages (this sandbox has no live network access
# to imot.bg itself - see module docstring for the full diagnostic trail
# and the two confirmed differences from scraper_olx.py's own OBLAST_SLUGS,
# which this otherwise reuses the spelling of).
OBLAST_SLUGS = [
    ("София", "oblast-sofiya"),
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
    ("Търговище", "oblast-targovishte"),
]

OUT_DIR = Path(__file__).parent / "data"
OUT_DIR.mkdir(exist_ok=True)
# .json.gz, not plain .json - 2026-09-26: imot.bg was trending toward the
# same GH001 wall alo.bg/bazar.bg already hit (~68MB/~68MB) - migrated
# proactively in the same pass rather than firefighting it later. Same
# proven fix as homes.bg (2026-09-25 addendum)/alo.bg/bazar.bg/olx.bg -
# see geo_utils.py's "Compressed on-disk JSON storage" comment.
# load_json_any()/save_json_any() are extension-aware.
HISTORY_FILE = OUT_DIR / "history_imot.json.gz"
LEADS_FILE = OUT_DIR / "leads_imot.json.gz"
# Persists only which OBLAST_SLUGS index the oblast-level phase of the grid
# crawl should resume from next run - same tiny-file, loop-position-only
# pattern as scraper_olx.py's own GRID_STATE_FILE/olx_grid_state.json (see
# that file's own comment); deliberately its own file rather than folded
# into history_imot.json for the same reason.
GRID_STATE_FILE = OUT_DIR / "imot_grid_state.json"

MAX_CARD_TEXT_LENGTH = 800
MAX_PRICE_MENTIONS = 1
# The real per-query depth cap sits around page 27-28 (~1,080 listings at
# 40/page) - 30 gives a small safety margin before giving up on a
# city/oblast without wasting requests deep past the cap.
MAX_PAGES = 30
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 5
MAX_CONSECUTIVE_PAGE_FAILURES = 5

# docs/backlog.md item 31, mirroring scraper_olx.py's own TIME_BUDGET_SECONDS
# reasoning exactly: scrape.yml's imot.bg step also has timeout-minutes: 60,
# and CITY_SLUGS's own phase already reliably finishes fast within that cap
# on its own - this budget covers the WHOLE run (both phases), leaving a
# 10-minute buffer under the 60-minute cap for the oblast-level phase this
# item adds, the same margin scraper_olx.py's own fix already validated as
# safe for a single fetch already in flight plus a final checkpoint.
TIME_BUDGET_SECONDS = 50 * 60

LISTING_LINK_RE = re.compile(r"/obiava-(\d[a-z]\d{10,})-")
PRICE_RE = re.compile(r"[\d\s]{3,10}\s?€")
# Captures both city and area from "град <City>, <area>" (see module
# docstring for the confirmed per-line layout) - used to derive a
# listing's real city/area from its own card text instead of trusting the
# query slug (docs/backlog.md item 31), the same reasoning olx.bg's own
# CITY_AREA_LINE_RE uses.
CITY_AREA_LINE_RE = re.compile(r"^град\s+(\S.*?),\s*(.+)$")
# The comma+area shape above was only live-sampled against big cities
# (Sofia/Plovdiv/Varna/Burgas), which always have a named quarter. A
# smaller town's own card may have no comma at all (nothing to name as a
# separate "area" within it) - tried only as a fallback, after
# CITY_AREA_LINE_RE, so a real comma+area line is never short-circuited
# into this coarser match.
CITY_ONLY_LINE_RE = re.compile(r"^град\s+(.+)$")
# A village listing has no separate sub-area of its own - the settlement
# itself is both city and area, same convention olx.bg's VILLAGE_LINE_RE
# uses (see module docstring for why this specific shape isn't itself
# live-sampled the way the "град" one is).
VILLAGE_LINE_RE = re.compile(r"^с\.\s*(.+)$")
PRICE_LINE_RE = re.compile(r"^([\d\s]{3,10})\s?€$")
SQM_RE = re.compile(r"([\d.,]+)\s?кв\.?м")


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


def parse_listings_page(html, seen, geocoder, query_display):
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

        # city/area come from the card's own text (docs/backlog.md item 31),
        # not from which CITY_SLUGS/OBLAST_SLUGS entry produced this page -
        # an oblast query returns listings from many real cities/villages,
        # not just its own namesake. query_display (the city/oblast display
        # name that was actually queried) is kept only as a last-resort
        # fallback for the rare card that matches neither line shape below,
        # same defensive fallback olx.bg's own fetch_listings_page() uses.
        city = query_display
        area = query_display
        for l in lines:
            m = CITY_AREA_LINE_RE.match(l)
            if m:
                city, area = m.group(1).strip(), m.group(2).strip()
                break
            m2 = CITY_ONLY_LINE_RE.match(l)
            if m2:
                city = area = m2.group(1).strip()
                break
            m3 = VILLAGE_LINE_RE.match(l)
            if m3:
                city = area = m3.group(1).strip()
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
            candidate = img.get("data-src") or img.get("src")
            if candidate and "icons/" not in candidate:
                img_url = candidate
                break
        if img_url:
            if img_url.startswith("//"):
                img_url = "https:" + img_url
            elif img_url.startswith("/"):
                img_url = BASE_URL + img_url

        href = a["href"]
        full_url = "https:" + href if href.startswith("//") else (BASE_URL + href if href.startswith("/") else href)
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
            "id": "imot_" + listing_id,
            "url": full_url,
            "photo": img_url,
            "price_eur": price_eur,
            "sqm": sqm,
            "area": area,
            "city": city,
            "title": title[:150],
            "portal": "imot.bg",
            "lat": coords["lat"] if coords else None,
            "lng": coords["lng"] if coords else None,
            "category": category,
            "category_confidence": category_confidence,
        }
    return len(matching_links)


def goto_with_retries(page, url):
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
    # scraper.py/scraper_bcpea.py/scraper_alo.py use for their own detail
    # backfills.
    listing["detail_checked"] = True
    description = extract_description_imot(html)
    if description:
        listing["description"] = description
    photos = extract_photos_imot(html)
    if photos:
        listing["photos"] = photos
    return True


def fetch_listing_details(listings, on_checkpoint=None, checkpoint_every=150, deadline=None):
    # One shared browser/page reused across the whole batch (like the main
    # grid crawl already does across many page navigations) rather than a
    # fresh context per listing - much cheaper than scraper_bcpea.py's own
    # detail backfill, which needs a fresh context per listing for other
    # reasons (see its module docstring).
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


def _crawl_one_query(page, query_display, search_url, seen, geocoder, deadline):
    # Runs the page-by-page pagination loop for a single CITY_SLUGS/
    # OBLAST_SLUGS query, shared by both phases of fetch_listings() below so
    # the two don't drift into two subtly different pagination/retry
    # implementations. Mirrors scraper_olx.py's own fetch_listings_page()/
    # per-oblast page loop shape.
    #
    # Returns (new_listing_count, interrupted) - interrupted is True only
    # when `deadline` was hit mid-query (not yet at its own natural end),
    # so the caller can tell "genuinely done" apart from "cut off, resume
    # this same query next time" without inspecting seen sizes itself.
    before = len(seen)
    consecutive_failures = 0
    interrupted = False
    for page_num in range(1, MAX_PAGES + 1):
        if deadline is not None and time.monotonic() >= deadline:
            interrupted = True
            break
        url = search_url if page_num == 1 else f"{search_url}/p-{page_num}"
        html = goto_with_retries(page, url)
        if html is None:
            # A failed fetch (all in-page retries exhausted) is not the
            # same signal as a genuinely empty page - treating it as "end
            # of query" would silently truncate every remaining page on one
            # bad request (the same bug found in scraper.py/scraper_alo.py).
            # Skip it and keep going, only giving up after several in a row.
            consecutive_failures += 1
            print(f"DEBUG: {query_display} page {page_num} fetch failed "
                  f"({consecutive_failures}/{MAX_CONSECUTIVE_PAGE_FAILURES} consecutive)")
            if consecutive_failures >= MAX_CONSECUTIVE_PAGE_FAILURES:
                break
            continue
        consecutive_failures = 0

        link_count = parse_listings_page(html, seen, geocoder, query_display)
        print(f"DEBUG: {query_display} page {page_num} links matching listing URL pattern = {link_count}")
        if link_count == 0:
            break
    return len(seen) - before, interrupted


def fetch_listings(deadline=None, on_checkpoint=None):
    # docs/backlog.md item 31 - see module docstring for the full two-phase
    # design rationale. deadline/on_checkpoint mirror the exact pattern
    # fetch_listing_details() already uses (and scraper_olx.py's own
    # fetch_listings() uses for its single-phase oblast crawl): stop
    # cleanly, with everything fetched so far already saved via
    # on_checkpoint, before the workflow step's own timeout-minutes would
    # hard-kill the process mid-page instead.
    seen = {}
    geocoder = Geocoder()

    # OBLAST_SLUGS is a fixed list order, so a bounded oblast phase that
    # always started at index 0 would always run out of budget on the same
    # tail oblasts - GRID_STATE_FILE's next_start_index rotates which
    # oblast THIS run's oblast phase starts from to wherever the PREVIOUS
    # run left off, same reasoning as scraper_olx.py's own grid state (see
    # that module's fetch_listings() for the full comment this mirrors).
    state = load_grid_state()
    start_index = state.get("next_start_index", 0) % len(OBLAST_SLUGS)
    oblast_order = OBLAST_SLUGS[start_index:] + OBLAST_SLUGS[:start_index]
    if start_index:
        print(f"DEBUG: oblast-level phase resuming at index {start_index} "
              f"({oblast_order[0][0]}) per {GRID_STATE_FILE.name}'s rotation state")

    oblasts_completed = 0
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent=USER_AGENT, locale="bg-BG")
        page = context.new_page()

        # Phase 1: CITY_SLUGS (24 cities) - unbounded by design (see module
        # docstring: this phase alone has reliably finished well within the
        # workflow's own timeout on every recent run even before the
        # oblast-level phase existed). The deadline check here is a
        # defensive safety net only, not an expected/normal code path -
        # if it DOES fire, the oblast-level phase below is skipped entirely
        # for this run rather than starting already out of budget.
        city_phase_interrupted = False
        for city_display, slug in CITY_SLUGS:
            if deadline is not None and time.monotonic() >= deadline:
                print(f"DEBUG: stopping before city-level phase reaches {city_display} - "
                      f"approaching this run's time budget (unexpected - see module docstring)")
                city_phase_interrupted = True
                break
            search_url = f"{SEARCH_BASE}/{slug}"
            new_count, interrupted = _crawl_one_query(page, city_display, search_url, seen, geocoder, deadline)
            suffix = " (interrupted mid-page)" if interrupted else ""
            print(f"DEBUG: {city_display} done, {new_count} new listings{suffix}")
            if on_checkpoint:
                on_checkpoint(list(seen.values()), geocoder)
            if interrupted:
                city_phase_interrupted = True
                break

        # Phase 2: OBLAST_SLUGS (27 oblasts) - the new, heavier addition
        # that DOES need real checkpointing (see module docstring).
        if city_phase_interrupted:
            print("DEBUG: skipping oblast-level phase entirely this run - "
                  "city-level phase itself hit the time budget (unexpected)")
        else:
            for oblast_display, slug in oblast_order:
                if deadline is not None and time.monotonic() >= deadline:
                    print(f"DEBUG: stopping oblast-level phase before {oblast_display} - approaching this "
                          f"run's time budget, {len(oblast_order) - oblasts_completed} of {len(oblast_order)} "
                          f"oblasts left for a future run")
                    break
                search_url = f"{SEARCH_BASE}/{slug}"
                new_count, interrupted = _crawl_one_query(page, oblast_display, search_url, seen, geocoder, deadline)
                suffix = " (interrupted mid-page, will resume here next run)" if interrupted else ""
                print(f"DEBUG: {oblast_display} done, {new_count} new listings{suffix}")

                if interrupted:
                    # Doesn't count as completed - next_start_index below
                    # stays pointed at this same oblast so the next run
                    # resumes on it instead of skipping past it.
                    break

                oblasts_completed += 1
                if on_checkpoint:
                    on_checkpoint(list(seen.values()), geocoder)

        browser.close()

    # If every oblast in oblast_order completed, (start_index +
    # oblasts_completed) wraps back to start_index exactly - correct, since
    # a fully-completed lap makes the next run's starting point unimportant
    # either way.
    next_start_index = (start_index + oblasts_completed) % len(OBLAST_SLUGS)
    save_grid_state({"next_start_index": next_start_index})
    if oblasts_completed < len(oblast_order):
        print(f"DEBUG: oblast-level phase covered {oblasts_completed}/{len(oblast_order)} oblasts this run - "
              f"next run resumes at index {next_start_index} ({OBLAST_SLUGS[next_start_index][0]})")

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
# (parse_listings_page()) itself produces - never present on a fresh
# grid-only record. update_history() below must merge these in from the
# previous "latest" rather than let a fresh grid re-touch wipe them off an
# already-detail-checked, still-active listing every ~6 hours -
# docs/backlog.md item 9a.
_DETAIL_ONLY_FIELDS = ("description", "photos", "detail_checked")


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
        # duplicate snapshot for it. Same dedup shape as
        # scraper_olx.py's own main()/record_new(), and the same mechanism
        # that also absorbs CITY_SLUGS/OBLAST_SLUGS overlap (e.g. a Sofia
        # listing returned by both "grad-sofiya" and "oblast-sofiya") -
        # fetch_listings()'s own `seen` dict already dedups that within a
        # single run by listing id, so it never reaches here twice either.
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
    # completed* city/oblast, so a run interrupted mid-query (deadline hit
    # partway through that query's own page loop) can still be holding a
    # few pages' worth of listings that were never checkpointed.
    # record_new()'s id-delta tracking makes this a cheap no-op on a normal
    # run where nothing was interrupted. geocoder.save() isn't repeated here
    # - fetch_listings() already called it itself right before returning.
    record_new(listings)
    leads = compute_leads(history)
    print(f"Found {len(listings)} listings, {len(leads)} tracked leads")


if __name__ == "__main__":
    main()
