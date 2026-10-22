"""
Scrapes current Bulgaria-wide listings from imoti.net, keeps a history of
every time each listing was seen, and works out:
  - price drops and days-on-market from that history
  - each listing's price per m2 vs the average for its area, as a %

Search results are paginated with ?page=N (confirmed via the site's own
paginator, which lists a "last page" link up to page 396 at 30 items/page
for the Sofia-only search). The scraper originally only fetched page 1
with no pagination loop at all - fixed by paging through page=2, page=3,
... until a page comes back with no listings, same "stop on empty page"
pattern as scraper_bazar.py, scraper_imot.py, and scraper_imoti_bg.py,
capped at MAX_PAGES as a safety limit. Confirmed against the live site
that imoti.net hard-blocks (HTTP 403) at page 200 regardless of pacing.

Nationwide conversion: imoti.net's URL is /en/obiavi/r/prodava/<city-slug>
- unlike homes.bg, there's no single "drop the filter" nationwide switch
(every guess without a city segment - no segment, "bulgaria", a bare query
string - 404s). Live-verified this is a required per-city path segment,
and that the page-200 block is per-QUERY, not per-session (paged Sofia to
the 403 wall, then immediately fetched a different city's page 1 in the
same requests.Session with no issue) - so, unlike what was assumed here
before, this doesn't need IP rotation/session-cycling to go further; it
just needs the depth cap sliced away per query, same idea as
scraper_homes.py's price-band bisection, but the site already hands us a
free slicing dimension via CITY_SLUGS instead of needing price bands.
CITY_SLUGS is the live-verified subset (23 of the 30 cities tracked
elsewhere in this project - index.html's BG_CITIES) whose lowercase-
transliterated slug actually resolves; the other 7 (Veliko Tarnovo,
Asenovgrad, Kazanlak, Kyustendil, Dimitrovgrad, Dupnitsa, Svishtov) 404 on
that guess and are left out rather than silently sending broken requests -
their real slugs weren't worth further reverse-engineering (the site's
location picker is JS-driven, not server-rendered, so there was no direct
way to read them off the page) for what's a small fraction of national
coverage. Each request's city is already known from which slug built the
URL, so it's attached to every listing directly - far more robust than
parsing a city name back out of scraped text, and generalizes
extract_area() away from its old Sofia-only ", Sofia" split.

If any single city's own listing count is still large enough to hit the
page-200 cap (most likely candidate: Sofia, the country's biggest market
by far), that city's data is truncated there for now, same graceful-
degradation as before - logged clearly rather than silently lost, so a
follow-up price-band slicing pass (mirroring scraper_homes.py's, if
imoti.net's URLs support a price filter param - not yet confirmed) can be
added specifically for whichever city actually needs it, once real per-
city counts from a live run show which ones do.

A page fetch retries a few times with backoff before being treated as the
end of pagination, so a one-off transient failure (a connect timeout, not
a real block) doesn't get mistaken for having reached the last page - the
persistent page-200 block above still stops the run the same way, just
after retries confirm it isn't transient.

days_on_market/coords: each listing's own page carries a genuine
schema.org "datePosted" field in its JSON-LD block (e.g. "2026-08-15" - a
real date, not "today") plus real "latitude"/"longitude" JSON keys,
confirmed live - previously fetched inline here, visiting every tracked
listing's own page once per scrape on top of the city-by-city grid crawl.
At nationwide scale (~21,700 listings, up from the old Sofia-only
estimate this was originally sized for) that inline pass alone exceeded
this workflow's 300-minute step timeout for real - confirmed live: a full
run's grid crawl finished in the usual time, but the detail-date pass was
still only 3,600/21,719 listings in when it got killed, discarding that
entire run's freshly-scraped grid data too, since nothing gets saved
until after both steps finish. Decoupled the same way homes.bg's/imot.bg's/
olx.bg's/alo.bg's own per-listing detail work already was: fetch_listings()
now only does the grid crawl (category is classified immediately there,
from the title, since that needs no network call), and
backfill_detail_imoti_net.py is a separate, resumable pass that visits
listing pages over time to fill in site_posted_at/lat,lng.
"""

import re
import json
import time
from collections import Counter, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

from category_classifier import classify_listing
from geo_utils import extract_coords_imoti_net, extract_photos_imoti_net, compute_motivation_score, listing_city_key, prune_snapshots, evict_stale_records, STALE_RECORD_RETENTION

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; PersonalDealTracker/1.0)"}
BASE_URL = "https://www.imoti.net/en/obiavi/r/prodava"

# Live-verified subset of index.html's BG_CITIES whose lowercase-
# transliterated slug actually resolves on imoti.net (23/30 - see module
# docstring for the 7 that don't and why they're left out). Each maps to
# the same real Cyrillic city name index.html/sync_to_supabase.py's
# BG_CITY_BY_NAME already expects in a listing's "city" field, so the
# city-key logic there (see index.html's listingCityKey()) works on these
# listings without any portal-specific handling.
CITY_SLUGS = [
    ("sofia", "София"), ("plovdiv", "Пловдив"), ("varna", "Варна"), ("burgas", "Бургас"),
    ("ruse", "Русе"), ("stara-zagora", "Стара Загора"), ("pleven", "Плевен"),
    ("sliven", "Сливен"), ("dobrich", "Добрич"), ("shumen", "Шумен"), ("pernik", "Перник"),
    ("haskovo", "Хасково"), ("yambol", "Ямбол"), ("pazardzhik", "Пазарджик"),
    ("blagoevgrad", "Благоевград"), ("vratsa", "Враца"), ("gabrovo", "Габрово"),
    ("vidin", "Видин"), ("kardzhali", "Кърджали"), ("montana", "Монтана"),
    ("targovishte", "Търговище"), ("lovech", "Ловеч"), ("silistra", "Силистра"),
]

OUT_DIR = Path(__file__).parent / "data"
OUT_DIR.mkdir(exist_ok=True)
HISTORY_FILE = OUT_DIR / "history.json"
LEADS_FILE = OUT_DIR / "leads.json"

BGN_TO_EUR = 1.95583
MAX_CARD_TEXT_LENGTH = 400
MAX_PRICE_MENTIONS = 2
MAX_PAGES = 420
REQUEST_DELAY_SECONDS = 1.0
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 5

# docs/backlog.md item 57's addendum (2026-10-10): a real production run
# (job 113770352507, 2026-10-09) showed Burgas paging 199 FULL pages (60
# raw <a> links/page, never short - PAGE_SIZE=30 listings x 2 anchors
# each) before the known page-200 403 block, but only 1,019 of the
# 11,940 raw link encounters were ever genuinely new (duplicate=10,919,
# no_container=2, no_bgn_match=0) - i.e. once ~34 pages' worth of real
# content (1,019 / PAGE_SIZE) had been seen, essentially every further
# page was re-serving listings already in `seen`, for 165 more pages, all
# the way to the hard block. Nationwide that same run: 6,113/51,966 raw
# links added (11.8%), duplicate=45,377 - a healthy small city that never
# needs to page this deep (e.g. that day's Стара Загора: 410/826, 49.6%,
# duplicate=410 - the expected ~50% split from each card's own thumbnail
# + title anchor both matching LISTING_LINK_RE) looks nothing like this.
#
# Paging a city all the way to MAX_PAGES/the 403 block once its results
# have become pure re-served duplicates burns real request time (and
# site load) for, on the evidence above, no plausible further yield -
# time this run's own fixed per-step budget would rather spend getting
# through every city at all (a slow/retry-heavy day risks the 300-minute
# step timeout, which discards the ENTIRE run's freshly-scraped data for
# every city, not just the slow one - see scrape-large.yml's own comment).
# DUPLICATE_STOP_WINDOW/DUPLICATE_STOP_MAX_NEW below stop a city's
# pagination once a long, unbroken run of still-FULL pages has added
# zero new listings - deliberately strict (a literal 0, not just "few",
# over 10 consecutive pages = 300 raw link encounters with nothing new)
# so a real trickle of genuinely new listings interspersed among
# duplicates - which this sandbox cannot rule out live, see the addendum
# for the honest caveat on what's actually confirmed about imoti.net's
# server-side behavior here - keeps the crawl going rather than risk
# cutting it off early. A healthy city's own ~50% duplicate split (two
# anchors per real, distinct card) never comes close to 10 pages of zero
# NEW listings - see tests/test_imoti_net_duplicate_pagination_stop.py.
DUPLICATE_STOP_WINDOW = 10
DUPLICATE_STOP_MAX_NEW = 0

LISTING_LINK_RE = re.compile(r"^/en/obiava/prodava[^\"'#]*?/(\d+)/")
BGN_RE = re.compile(r"([\d\s]{3,12})\s?BGN")
SQM_RE = re.compile(r"(\d+)\s?\u043c\s?2")
DESC_RE = re.compile(r"for sale (.{5,90}?)\s+[\d\s]{2,10}\s?\u20ac")
DATE_POSTED_RE = re.compile(r'"datePosted"\s*:\s*"(\d{4}-\d{2}-\d{2})"')


def extract_area(title):
    # title is a free-text description snippet (see DESC_RE), consistently
    # shaped "<type>, <sqm> м 2 <City>, <area>" - the neighborhood/area is
    # always the last comma-separated segment, regardless of city. The old
    # Sofia-only version matched a literal "Sofia," split instead; trying
    # the same trick generically by re-matching each CITY_SLUGS name broke
    # on Burgas, live-confirmed: the site's own English text spells it
    # "Bourgas", not "burgas" - a real, silent split failure that dumped
    # the *entire* title into "area" instead of just "Lazur". Splitting on
    # the last comma sidesteps needing to know every city's exact English
    # spelling variant at all, same pattern scraper_homes.py's own
    # extract_area() already uses.
    if "," in title:
        area = title.rsplit(",", 1)[1].strip()
        return area or title.strip()
    return title.strip()


def smallest_container_with_price(link_tag, max_levels=6):
    node = link_tag
    for _ in range(max_levels):
        if node.parent is None:
            break
        node = node.parent
        text = node.get_text(" ", strip=True)
        matches = BGN_RE.findall(text)
        if len(matches) > MAX_PRICE_MENTIONS:
            return None
        if 1 <= len(matches) <= MAX_PRICE_MENTIONS and len(text) <= MAX_CARD_TEXT_LENGTH:
            return node
    return None


def fetch_with_retries(url):
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=20)
            resp.raise_for_status()
            return resp.text
        except requests.HTTPError as e:
            # 404/410 mean the page is permanently gone - retrying can
            # never succeed. A real production run of
            # backfill_detail_imoti_net.py's detail-page pass (which hits
            # a much older, less-checked backlog than the grid crawl ever
            # does) confirmed this wastes real time: every retry here was
            # still paying the full RETRY_BACKOFF_SECONDS sleep before
            # trying again, up to ~15s burned per gone listing for nothing,
            # which was the main reason that backfill kept exceeding its
            # workflow's 45-minute timeout on every single run.
            status = e.response.status_code if e.response is not None else None
            if status in (404, 410):
                print(f"DEBUG: {url} permanently gone ({status}) - not retrying")
                return None
            print(f"DEBUG: request failed for {url} (attempt {attempt}/{MAX_RETRIES}): {e}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)
        except requests.RequestException as e:
            print(f"DEBUG: request failed for {url} (attempt {attempt}/{MAX_RETRIES}): {e}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)
    return None


def fetch_listings_page(url, seen, city_name, skip_stats=None):
    # skip_stats (optional): a collections.Counter (or any dict-like with
    # __getitem__/__setitem__ defaulting missing keys to 0, i.e. an actual
    # Counter) that this function increments once per raw <a> match that
    # does NOT become a saved listing, keyed by which check rejected it -
    # see fetch_listings()'s own per-city yield summary for why this
    # exists (2026-09-26 imoti.net active-ratio collapse investigation,
    # docs/backlog.md item 57): every rejection below was previously a
    # bare, unlogged `continue`, so a real drop in how many of a page's
    # genuine listing cards actually survive this extraction pipeline -
    # confirmed live: ~30/page on 2026-09-22 vs. ~5/page on 2026-09-24
    # onward, with the raw per-page <a>-tag count unchanged - was
    # completely invisible in this scraper's own logs. This doesn't fix
    # that regression (its real cause needs live imoti.net HTML this
    # sandbox's egress proxy blocks, see the docstring note above), it
    # only makes the NEXT occurrence immediately diagnosable instead of
    # requiring the same kind of after-the-fact GitHub Actions log
    # archaeology this investigation needed.
    html = fetch_with_retries(url)
    if html is None:
        return None
    soup = BeautifulSoup(html, "html.parser")

    all_links = soup.find_all("a", href=True)
    matching_links = [a for a in all_links if LISTING_LINK_RE.search(a["href"])]

    for a in matching_links:
        match = LISTING_LINK_RE.search(a["href"])
        listing_id = match.group(1)
        if listing_id in seen:
            if skip_stats is not None:
                skip_stats["duplicate"] += 1
            continue

        container = smallest_container_with_price(a)
        if container is None:
            if skip_stats is not None:
                skip_stats["no_container"] += 1
            continue

        text = container.get_text(" ", strip=True)

        bgn_match = BGN_RE.search(text)
        sqm_match = SQM_RE.search(text)
        desc_match = DESC_RE.search(text)
        if not bgn_match:
            if skip_stats is not None:
                skip_stats["no_bgn_match"] += 1
            continue

        price_bgn = int(re.sub(r"\D", "", bgn_match.group(1)))
        if price_bgn < 1000:
            if skip_stats is not None:
                skip_stats["price_under_1000"] += 1
            continue
        price_eur = round(price_bgn / BGN_TO_EUR)
        sqm = int(sqm_match.group(1)) if sqm_match else None

        img = container.find("img")
        img_url = None
        if img:
            img_url = img.get("src") or img.get("data-src")
        if img_url and img_url.startswith("/"):
            img_url = "https://www.imoti.net" + img_url

        full_url = a["href"] if a["href"].startswith("http") else "https://www.imoti.net" + a["href"]
        title = desc_match.group(1).strip() if desc_match else None
        if not title:
            if skip_stats is not None:
                skip_stats["no_title_match"] += 1
            continue

        # Real bug, confirmed against live committed data (docs/backlog.md
        # item 5): this used to call geo_utils.classify_category(title),
        # whose keyword table is Bulgarian-only. Every imoti.net title is
        # scraped from the site's /en/ (English) path (see module
        # docstring), so it could never match a single keyword there and
        # 100% of imoti.net's 26,804+ listings silently fell through to
        # that function's own documented "apartment" default - even though
        # imoti.net's own search isn't apartment-scoped (its scraped
        # listing URLs carry 20+ distinct Bulgarian property-type slugs:
        # kashta, parcel, garaj, magazin, ofis, etc. - real houses/land/
        # garages/shops/offices all bucketed "apartment" on the live site).
        # category_classifier.classify_listing() is the shared
        # nationwide-expansion classifier already used by scraper_alo.py/
        # scraper_imoti_bg.py for this exact reason - it scores both the
        # title AND url (imoti.net's own listing URL embeds that same
        # Bulgarian type slug, e.g. ".../kashta/1234/") as independent
        # signals, so a listing still classifies correctly even when only
        # one of the two carries a recognizable word for it.
        category, category_confidence, _ = classify_listing(title=title, url=full_url)

        seen[listing_id] = {
            "id": listing_id,
            "url": full_url,
            "photo": img_url,
            "price_eur": price_eur,
            "sqm": sqm,
            "area": extract_area(title),
            "city": city_name,
            "title": title,
            "portal": "imoti.net",
            "category": category,
            "category_confidence": category_confidence,
        }
        if skip_stats is not None:
            skip_stats["added"] += 1
    return len(matching_links)


def parse_date_posted(html):
    m = DATE_POSTED_RE.search(html)
    return m.group(1) if m else None


# No longer called from fetch_listings() - see the module docstring for
# why (nationwide scale made the inline detail pass exceed this workflow's
# timeout, discarding the whole run). Kept here, reused by
# backfill_detail_imoti_net.py's separate, resumable pass.
#
# deadline (a time.monotonic() cutoff) and on_checkpoint let the caller
# save progress as it goes and stop cleanly before the workflow's own
# timeout would hard-kill the process: a real production run of this
# backfill kept exceeding its 45-minute workflow timeout on every single
# scheduled run (confirmed via job logs - killed mid-batch with only
# ~150-300/1500 listings done), which both discarded that run's entire
# batch (nothing was saved until the very end) and reported as a failed
# run every time even though nothing was actually broken. Checking the
# deadline once per listing (not on some coarser interval) keeps a run
# that's genuinely almost out of time from overshooting it by much.
def fetch_listing_dates(seen, on_checkpoint=None, checkpoint_every=150, deadline=None):
    total = len(seen)
    for i, (listing_id, l) in enumerate(seen.items(), 1):
        if deadline is not None and time.monotonic() >= deadline:
            print(f"DEBUG: stopping at {i - 1}/{total} - approaching this run's time budget")
            break
        time.sleep(REQUEST_DELAY_SECONDS)
        html = fetch_with_retries(l["url"])
        if html is None:
            # NOT marked detail_checked here - fetch_with_retries() already
            # exhausted its own in-page retries, but that failure could
            # still be transient (a momentary block/timeout, not a
            # confirmed-gone 404/410, which fetch_with_retries() itself
            # already treats as a real page load returning None-with-no-
            # further-retry rather than reaching here). Marking this done
            # anyway - as this used to, unconditionally - meant a listing
            # that just got unlucky once was silently and permanently
            # skipped by every future backfill run, losing its
            # datePosted/coords/photos for good with nothing to show for
            # it. Leaving detail_checked unset lets a later run retry it.
            continue
        # Marked only once html is confirmed real - some listings
        # genuinely have no datePosted/coords on their own page even when
        # it loads fine, and without an explicit marker those would get
        # needlessly re-visited by every future backfill run instead of
        # being treated as done.
        l["detail_checked"] = True
        date_posted = parse_date_posted(html)
        if date_posted:
            l["site_posted_at"] = date_posted
        coords = extract_coords_imoti_net(html)
        if coords:
            l["lat"] = coords["lat"]
            l["lng"] = coords["lng"]
        photos = extract_photos_imoti_net(html)
        if photos:
            l["photos"] = photos
        if i % 200 == 0:
            print(f"DEBUG: fetched detail dates for {i}/{total} listings")
        if on_checkpoint and i % checkpoint_every == 0:
            on_checkpoint()


# imoti.net shows 30 listings/page (see module docstring) - a city whose
# last fetched page still came back full when pagination stopped (either
# a fetch failure - most likely the confirmed page-200 block - or hitting
# MAX_PAGES) means real listings were probably left uncollected, unlike a
# city that stopped on a genuinely partial or empty page.
PAGE_SIZE = 30


def fetch_listings():
    seen = {}
    # Nationwide-total counterpart to each city's own skip_stats below -
    # see fetch_listings_page()'s own comment for why this exists.
    nationwide_stats = Counter()
    nationwide_raw_links = 0
    for city_slug, city_name in CITY_SLUGS:
        search_url = f"{BASE_URL}/{city_slug}"
        city_start_count = len(seen)
        city_raw_links = 0
        city_stats = Counter()
        # Rolling count of how many NEW listings each of the last
        # DUPLICATE_STOP_WINDOW full pages added - see that constant's
        # own comment above for the real evidence this is built from.
        recent_added = deque(maxlen=DUPLICATE_STOP_WINDOW)
        stop_reason = None
        for page_num in range(1, MAX_PAGES + 1):
            if page_num > 1:
                time.sleep(REQUEST_DELAY_SECONDS)
            url = search_url if page_num == 1 else f"{search_url}?page={page_num}"
            added_before_page = city_stats["added"]
            link_count = fetch_listings_page(url, seen, city_name, skip_stats=city_stats)
            if link_count is None:
                print(f"DEBUG: {city_name} page {page_num} fetch failed (likely the page-200 "
                      f"block) - stopping this city here, {len(seen) - city_start_count} listings collected")
                stop_reason = "fetch_failed"
                break
            print(f"DEBUG: {city_name} page {page_num} links = {link_count}")
            city_raw_links += link_count
            if not link_count:
                stop_reason = "exhausted"
                break
            recent_added.append(city_stats["added"] - added_before_page)
            if len(recent_added) == DUPLICATE_STOP_WINDOW and sum(recent_added) <= DUPLICATE_STOP_MAX_NEW:
                print(f"DEBUG: {city_name} page {page_num} stopping early - the last "
                      f"{DUPLICATE_STOP_WINDOW} full pages added {sum(recent_added)} new listings "
                      f"between them (duplicate-saturated - see docs/backlog.md item 57's addendum) - "
                      f"{len(seen) - city_start_count} listings collected")
                stop_reason = "duplicate_saturated"
                break
        else:
            stop_reason = "max_pages"
        if stop_reason == "duplicate_saturated":
            print(f"DEBUG: {city_name} pagination stopped intentionally once content was "
                  f"duplicate-saturated, not a fetch failure - further pages were overwhelmingly "
                  f"re-serving listings already collected, so this is NOT treated as truncation "
                  f"the way a fetch failure or hitting MAX_PAGES still full would be")
        elif stop_reason in ("fetch_failed", "max_pages"):
            print(f"DEBUG: WARNING - {city_name} may be truncated (last page was still full or "
                  f"a fetch failed) - real total could be higher than the "
                  f"{len(seen) - city_start_count} listings collected")
        # Per-city extraction-yield summary: what fraction of this city's
        # raw <a>-tag matches actually became a saved listing, broken down
        # by which check rejected the rest. A healthy small city's yield
        # should track roughly 1/(anchors per listing card) - e.g. ~50%,
        # all in `duplicate` (two matching <a> tags per real card -
        # thumbnail + title). docs/backlog.md item 57's original 2026-09-
        # 26 entry guessed a sustained yield drop here (with raw link
        # count unchanged) would mean an imoti.net card-markup change
        # breaking smallest_container_with_price()/BGN_RE/DESC_RE - that
        # guess is CORRECTED by this item's 2026-10-10 addendum: real job
        # logs show no_container/no_bgn_match near zero even during the
        # active-ratio collapse this was investigating. The real signature
        # is `duplicate` dominating (83-99% of rejections) for any city
        # that needs to page deep - see DUPLICATE_STOP_WINDOW's own
        # comment above for the real evidence and the fix built from it.
        added = city_stats["added"]
        yield_pct = (added / city_raw_links * 100) if city_raw_links else 0.0
        print(f"DEBUG: {city_name} yield - {added}/{city_raw_links} raw links became listings "
              f"({yield_pct:.1f}%) - skipped: no_container={city_stats['no_container']}, "
              f"no_bgn_match={city_stats['no_bgn_match']}, "
              f"price_under_1000={city_stats['price_under_1000']}, "
              f"no_title_match={city_stats['no_title_match']}, "
              f"duplicate={city_stats['duplicate']}")
        print(f"DEBUG: finished {city_name}, {len(seen) - city_start_count} listings, "
              f"{len(seen)} total so far")
        nationwide_stats.update(city_stats)
        nationwide_raw_links += city_raw_links
    added = nationwide_stats["added"]
    yield_pct = (added / nationwide_raw_links * 100) if nationwide_raw_links else 0.0
    print(f"DEBUG: nationwide yield - {added}/{nationwide_raw_links} raw links became listings "
          f"({yield_pct:.1f}%) - skipped: no_container={nationwide_stats['no_container']}, "
          f"no_bgn_match={nationwide_stats['no_bgn_match']}, "
          f"price_under_1000={nationwide_stats['price_under_1000']}, "
          f"no_title_match={nationwide_stats['no_title_match']}, "
          f"duplicate={nationwide_stats['duplicate']}")
    return list(seen.values())


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


# Fields fetch_listing_dates() (run separately via
# backfill_detail_imoti_net.py - main() never calls it inline, see that
# function's own comment) adds on top of what the grid crawl itself
# produces - never present on a fresh grid-only record. update_history()
# below must merge these in from the previous "latest" rather than let a
# fresh grid re-touch wipe them off an already-detail-checked, still-active
# listing every ~6 hours - docs/backlog.md item 9a. (imoti.net has no
# "description" field at all, deliberately - docs/backlog.md item 9 task 1
# investigated this: backfill_detail_imoti_net.py's own docstring already
# documents a live-confirmed finding, predating that backlog task, that
# imoti.net's own /en/ detail page - the only page this scraper ever
# fetches - carries no free-text description anywhere, in meta tags,
# ld+json, or any labeled HTML block. A genuine per-portal data gap, not a
# missing-selector bug, so there is nothing for this list to merge-protect
# unless a different page (e.g. imoti.net's Bulgarian-language equivalent,
# untried - this sandbox also has no live imoti.net access to check it)
# turns up a real source for it.)
_DETAIL_ONLY_FIELDS = ("site_posted_at", "lat", "lng", "photos", "detail_checked")


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
# scrape cycle due to pagination timing/site load. This scraper runs on its
# OWN 24h schedule (scrape-large.yml, not the 6-hourly scrape.yml the other
# portals use - see that workflow's own comment for why), unlike the 20h
# threshold detect_relistings.py uses for the 6-hourly portals - a 20h cutoff
# here would flag every single listing "removed" the moment a day passes
# without a scrape, confirmed live against this scraper's own committed
# history.json (100% of listings came back "removed" at 20h). 48h gives a
# full extra cycle of slack for an occasionally slow/delayed run before
# concluding a listing is genuinely gone. Once removed, days_on_market/score
# freeze at the day it was last confirmed live instead of continuing to
# climb forever against an ad that's no longer there.
GONE_AFTER = timedelta(hours=48)


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
        site_posted_at = latest.get("site_posted_at")
        reference_date = (
            datetime.fromisoformat(site_posted_at).replace(tzinfo=timezone.utc)
            if site_posted_at
            else datetime.fromisoformat(rec["first_seen"])
        )
        days_on_market = max((effective_now - reference_date).days, 0)
        price_per_sqm = round(last_price / latest["sqm"]) if latest.get("sqm") else None

        leads.append({
            **latest,
            # Records from before this nationwide conversion never had an
            # "area"/"city" key at all (the old Sofia-only scraper didn't
            # set one) - every listing tracked back then genuinely was
            # Sofia, so that's a correct fallback, not a guess, and keeps
            # the area-average loop below from crashing on a missing key
            # until each pre-existing listing is next re-scraped and gets
            # a real area/city from fetch_listings_page().
            "area": latest.get("area") or "Sofia",
            "city": latest.get("city") or "София",
            "price_eur": last_price,
            "price_per_sqm": price_per_sqm,
            "price_history": price_history,
            "price_drop_count": price_drop_count,
            "drop_pct": drop_pct,
            "days_on_market": days_on_market,
            "source_status": source_status,
            "removed_at": last_seen.isoformat() if source_status == "removed" else None,
        })

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
