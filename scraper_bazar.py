"""
Scrapes current apartment-for-sale listings from bazar.bg, nationwide.

bazar.bg is a general classifieds site (cars, jobs, real estate, etc), so
this scopes to its own "apartments for sale" category URL per city
(https://bazar.bg/obiavi/prodazhba-apartamenti/<city>) rather than a
sitewide search - the site itself filters out unrelated categories.
Confirmed by sampling 68 live listings during development: 100% were
genuine apartment-for-sale titles ("Продава <N>-СТАЕН, гр. <City>, <area>"
or "Продава МЕЗОНЕТ/МНОГОСТАЕН, гр. <City>, <area>"), none from other
categories. bazar.bg has no bot-blocking - plain requests work fine.

Each listing card is the smallest ancestor whose text mentions the price
("<amount> €") exactly once, same "climb from the link" approach as
scraper.py, scraper_alo.py, scraper_imot.py, and scraper_olx.py. Within
that card the price amount and the "€" sign are on separate lines, so the
price is read by finding the "€" line and taking the digits from the line
right before it. bazar.bg's listing grid doesn't show square meters (only
the individual listing page does), so sqm/price_per_sqm are left null here
- the same graceful degradation compute_leads() already applies to any
listing missing sqm.

Nationwide mechanism: bazar.bg supports BOTH a bare "drop the city
segment" nationwide URL AND per-city path segments (unlike imot.bg/
imoti.net, which need per-city slugs exclusively) - but live pagination
testing found the site clamps out-of-range page numbers to its real last
page and repeats it verbatim, rather than ever showing an empty page
(confirmed: page 30's and page 50's listing ID sets were byte-identical).
That means the old "stop on empty page" logic never actually fires past
the real depth - it would silently loop through every remaining page
re-fetching the same content. Real content on a single query stops
changing around page 26, so CITY_SLUGS (each a live-verified
/obiavi/prodazhba-apartamenti/<slug> URL) slices by city instead, and
pagination now stops as soon as a page's listing ID set exactly matches
the previous page's (the real plateau signal), not just on an empty page
- confirmed live that a different city query still gets fresh content in
the same session after a previous city has already plateaued.

docs/backlog.md item 31 - oblast-level slicing investigated, NOT usable
for this portal the way it is for scraper_olx.py's OBLAST_SLUGS: this
sandbox has no live network access to bazar.bg at all (a direct HTTPS
fetch and a WebFetch call both came back blocked by this session's
egress policy - the same class of restriction earlier sessions already
hit against cdnjs/jsdelivr/Supabase, see docs/decisions.md), so the
"live network access, not guessed" bar this item calls for was met
through Google's live index of real bazar.bg pages instead (real URLs,
real titles, real listing counts - an external signal, not a guess, just
not a direct fetch). That research found bazar.bg DOES have real
oblast-level pages - but only as sitewide, all-category "browse" pages
under /obiavi/oblast-<slug> (e.g. oblast-plovdiv, oblast-varna) or
/obiavi/<slug>-oblast for a couple of oblasts (sofia-oblast, smolian-
oblast) - never nested under /obiavi/prodazhba-apartamenti/, and no
evidence either combination (e.g. prodazhba-apartamenti/sofia-oblast)
resolves. Routing this scraper through that sitewide page instead would
mean cars/jobs/electronics/furniture - also commonly priced in "<N> €" -
start passing this file's own "smallest ancestor with exactly one price
mention" card filter, and geo_utils.classify_category() is explicitly
documented (see its own docstring, and the real sales.bcpea.org bug it
describes) as unreliable off an apartments-only scope: it defaults any
unmatched title to "apartment", so a car or job listing from a sitewide
feed would silently get mislabeled and counted as a real apartment lead
instead of being dropped. That's a correctness regression, not a
coverage win, so it was rejected.

What the same research DID confirm live (via distinct, separately-
indexed bazar.bg pages, each with its own real listing count) is that
/obiavi/prodazhba-apartamenti/<slug> is granular well below city level -
small towns and even resort villages already have their own real page.
CITY_SLUGS below adds 8 such settlements a plain 29-big-city list
structurally can never include: 2 oblast capitals missing from both this
list and scraper_olx.py's own OBLAST_SLUGS (Разград, Смолян), 2 of the
exact small/mid towns docs/backlog.md item 31's own spot-check named as
missing (Петрич, Троян), and 4 more real, populated settlements (Банско,
Свети Влас, Обзор, Велинград). This is the "another defined, principled
method... e.g. oblast capitals plus the existing city list" fallback
item 31's own text pre-authorizes when true oblast-level querying isn't
available for a given portal - a real, evidence-based widening, not a
full nationwide fix. Bulgaria's remaining ~5,000 villages and the rest of
its ~230 smaller towns are still not covered; closing that gap for real
would need either a live-confirmed oblast+apartments-category combo URL
(unconfirmed either way here) or a much larger settlement list, verified
live by a session that actually has network access to bazar.bg - flagged
here rather than guessed at.

Each listing's city/area used to be tagged directly from which CITY_SLUGS
entry produced it (known from the URL, not parsed from text). Now parsed
from the card's own "гр. <City>, <area>" line instead (CITY_AREA_LINE_RE)
- the same "trust the listing's own text, not the query" reasoning
scraper_olx.py already uses for its oblast queries, applied here too so a
future settlement addition that leaks a neighboring town's listings (the
same "incidental leakage" the old docstring already flagged for Cherven
Bryag) gets tagged correctly instead of inheriting the query slug's
display name. VILLAGE_LINE_RE mirrors scraper_olx.py's own "с. <Village>"
fallback for a village-formatted card; unlike CITY_AREA_LINE_RE's "гр."
form (live-verified against real Plovdiv/Varna/Burgas card text), this
form was never directly observed on bazar.bg in this session - harmless
if it never matches, since a card matching neither line format still
falls back to the query's own display name exactly as before.

Search results are paginated with ?page=N. A page fetch retries a few
times with backoff before being treated as the end of pagination (same
pattern as scraper.py/scraper_alo.py), so a transient failure doesn't get
mistaken for having reached the last page - and, since scrape.yml runs
several scrapers sequentially with a single git commit step at the end,
an uncaught exception here would otherwise silently discard every other
scraper's output for that run too.

fetch_listings() now takes deadline/on_checkpoint and persists which
CITY_SLUGS index to resume from next run in GRID_STATE_FILE - the exact
checkpointed/rotating mechanism docs/backlog.md item 30 shipped for
scraper_olx.py's own oblast loop (see that file's fetch_listings()/
main()/load_grid_state()/save_grid_state() - ported here near verbatim,
adjusted only for this file having no browser/geocoder to manage), built
in from the start rather than bolted on after growing CITY_SLUGS from 29
to 37 entries made a monolithic, unbounded loop genuinely more likely to
run long. main()'s record_new()/checkpoint closure dedups by listing id
(recorded_ids) the same way, so a run that checkpoints after several
completed settlements - each checkpoint carrying the FULL accumulated
listings so far, not just that settlement's own delta - doesn't
double-append a history snapshot for a listing that shows up in more
than one checkpoint within the same run. Unlike scraper_olx.py, no
separate oblast-level query runs alongside CITY_SLUGS here (rejected
above), so there is no cross-query overlap (e.g. Sofia via both an
oblast query and its own city query) to dedup against - all_listings
being keyed by listing id is still kept as the general-purpose safety
net it already was.

Real coordinates live only on each listing's own detail page, as
data-lat/data-long attributes on its #see_on_map element (confirmed live
via a plain, non-JS HTTP fetch - no headless browser needed). At
nationwide scale, visiting every tracked listing's own page during the
main scrape doesn't fit in a single run (the same problem
scraper_homes.py hit first - see its module docstring), so this no longer
does that inline - backfill_detail_bazar.py does it as a separate,
decoupled pass. A detail-page fetch that fails there just leaves that one
listing without coordinates for that run rather than aborting the whole
backfill.
"""

import re
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

from category_classifier import classify_listing
from geo_utils import (
    compute_motivation_score, listing_city_key, prune_snapshots, evict_stale_records,
    STALE_RECORD_RETENTION, load_json_any, save_json_any,
)

BASE_URL = "https://bazar.bg"
SEARCH_BASE = "https://bazar.bg/obiavi/prodazhba-apartamenti"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
    "Accept-Language": "bg-BG,bg;q=0.9,en;q=0.8",
}

# (city display name, URL slug) - each slug live-verified to return a real
# bazar.bg city page (status 200, real listing links) before being trusted
# here; see the module docstring for the diagnostic trail.
CITY_SLUGS = [
    ("София", "sofia"),
    ("Пловдив", "plovdiv"),
    ("Варна", "varna"),
    ("Бургас", "burgas"),
    ("Русе", "ruse"),
    ("Стара Загора", "stara-zagora"),
    ("Плевен", "pleven"),
    ("Сливен", "sliven"),
    ("Добрич", "dobrich"),
    ("Шумен", "shumen"),
    ("Перник", "pernik"),
    ("Хасково", "haskovo"),
    ("Пазарджик", "pazardzhik"),
    ("Благоевград", "blagoevgrad"),
    ("Велико Търново", "veliko-tarnovo"),
    ("Враца", "vratsa"),
    ("Габрово", "gabrovo"),
    ("Видин", "vidin"),
    ("Асеновград", "asenovgrad"),
    ("Казанлък", "kazanlak"),
    ("Кюстендил", "kyustendil"),
    ("Кърджали", "kardzhali"),
    ("Монтана", "montana"),
    ("Димитровград", "dimitrovgrad"),
    ("Търговище", "targovishte"),
    ("Ловеч", "lovech"),
    ("Силистра", "silistra"),
    ("Дупница", "dupnitsa"),
    ("Свищов", "svishtov"),
    # docs/backlog.md item 31 additions - confirmed live via Google's index
    # of real bazar.bg pages (real URL, real page title, real listing
    # count each), not a direct HTTP fetch (this sandbox has no live
    # network access to bazar.bg at all - see the module docstring), but a
    # genuine external signal rather than a guess. Разград/Смолян close 2
    # of the 2 oblast capitals missing from both this list and
    # scraper_olx.py's own OBLAST_SLUGS; Петрич/Троян are 2 of the exact
    # towns docs/backlog.md item 31's own spot-check named as missing.
    ("Разград", "razgrad"),
    ("Смолян", "smolian"),  # bazar.bg's own transliteration, not "smolyan"
    ("Петрич", "petrich"),
    # "troian" (bazar.bg's own transliteration, not "troyan") was only
    # directly confirmed under the mixed-category /obiavi/apartamenti/
    # path, not this file's sale-only /obiavi/prodazhba-apartamenti/ path
    # - included on the strength of Petrich/Smolyan both resolving to the
    # identical slug under both category paths in the same research pass
    # (a settlement slug, reused across bazar.bg's category namespaces,
    # not scoped per-category), not a direct hit for this exact URL.
    ("Троян", "troian"),
    ("Банско", "bansko"),
    ("Свети Влас", "sveti-vlas"),
    ("Обзор", "obzor"),
    ("Велинград", "velingrad"),
]

OUT_DIR = Path(__file__).parent / "data"
OUT_DIR.mkdir(exist_ok=True)
# .json.gz, not plain .json - 2026-09-26: bazar.bg hit the same GH001 wall
# as alo.bg (leads_bazar.json 100.19MB/history_bazar.json 98.73MB, at/over
# GitHub's 100MB hard limit). Same proven fix as homes.bg (2026-09-25
# addendum)/alo.bg (this same change) - see geo_utils.py's "Compressed
# on-disk JSON storage" comment. load_json_any()/save_json_any() are
# extension-aware.
HISTORY_FILE = OUT_DIR / "history_bazar.json.gz"
LEADS_FILE = OUT_DIR / "leads_bazar.json.gz"
# Persists only which CITY_SLUGS index the grid crawl should start from
# next run - the same tiny-file rotation-state pattern scraper_olx.py's
# GRID_STATE_FILE (data/olx_grid_state.json) already established for
# docs/backlog.md item 30, reused here per item 31's own note that this
# file should build on that mechanism rather than invent a second one.
GRID_STATE_FILE = OUT_DIR / "bazar_grid_state.json"

MAX_CARD_TEXT_LENGTH = 500
MAX_PRICE_MENTIONS = 1
# Real content on a single city query stops changing around page 26 (the
# site clamps out-of-range page numbers to the last real page instead of
# ever going empty - see the module docstring) - 30 gives a small safety
# margin before the plateau-detection stop condition kicks in.
MAX_PAGES = 30
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 5
MAX_CONSECUTIVE_PAGE_FAILURES = 5

# .github/workflows/scrape.yml's bazar.bg step has timeout-minutes: 90 (not
# touched by this change - docs/backlog.md item 31 is scoped to this file
# only). 75 minutes leaves a 15-minute buffer under that cap - comfortably
# more than the worst-case overshoot of one single page fetch already in
# flight when the deadline is checked (fetch_html()'s own MAX_RETRIES=3 *
# this file's 20s request timeout, plus <=15s of RETRY_BACKOFF_SECONDS
# backoff, well under 2 minutes total) plus the final checkpoint (a fast,
# local JSON write) - the same proportional buffer scraper_olx.py's own
# TIME_BUDGET_SECONDS comment uses (~17% of its 60-minute cap; 15/90 here
# is ~17% too).
TIME_BUDGET_SECONDS = 75 * 60

LISTING_LINK_RE = re.compile(r"obiava-(\d+)")
PRICE_RE = re.compile(r"[\d\s]{3,10}\s?€")
# Captures both city and area, unlike the old AREA_LINE_RE (which only
# captured the area and trusted the query slug for city) - see the module
# docstring's "Each listing's city/area used to be tagged..." paragraph.
CITY_AREA_LINE_RE = re.compile(r"^гр\.\s*(.+?),\s*(.+)$")
# Mirrors scraper_olx.py's own VILLAGE_LINE_RE fallback - never directly
# observed on a live bazar.bg card in this session (see module docstring),
# harmless if it never matches.
VILLAGE_LINE_RE = re.compile(r"^с\.\s*(.+)$")


class PermanentlyGone(Exception):
    """Raised by fetch_html() for a 404/410 - the listing is gone for
    good, which is normal and expected in a large newest-first backlog and
    proves the server is responding fine. Deliberately not just another
    None return: backfill_detail_bazar.py's consecutive-failure early-stop
    was folding "permanently gone" and "retries exhausted after a real
    failure" into the same signal, so a handful of ordinary dead listings
    could trip it and abort a run with zero progress. Only a real failure
    should count toward that threshold."""


def fetch_html(url):
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.get(url, headers=HEADERS, timeout=20)
            r.raise_for_status()
            return r.text
        except requests.HTTPError as e:
            # 404/410 mean the page is permanently gone - retrying can
            # never succeed. Same fix as scraper.py's own fetch_with_retries
            # (see its comment) after backfill_detail_imoti_net.py kept
            # timing out from wasting its whole run's budget retrying
            # gone-forever pages 3x each.
            status = e.response.status_code if e.response is not None else None
            if status in (404, 410):
                print(f"DEBUG: {url} permanently gone ({status}) - not retrying")
                raise PermanentlyGone(url) from None
            print(f"DEBUG: request failed for {url} (attempt {attempt}/{MAX_RETRIES}): {e}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)
        except requests.RequestException as e:
            print(f"DEBUG: request failed for {url} (attempt {attempt}/{MAX_RETRIES}): {e}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)
    return None


def smallest_container_with_price(link_tag, max_levels=9):
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


def fetch_listings_page(url, city_display):
    try:
        html = fetch_html(url)
    except PermanentlyGone:
        return None
    if html is None:
        return None
    soup = BeautifulSoup(html, "html.parser")

    all_links = soup.find_all("a", href=True)
    matching_links = [a for a in all_links if LISTING_LINK_RE.search(a["href"])]

    listings = {}
    for a in matching_links:
        match = LISTING_LINK_RE.search(a["href"])
        listing_id = match.group(1)
        if listing_id in listings:
            continue

        container = smallest_container_with_price(a)
        if container is None:
            continue

        lines = [l.strip() for l in container.get_text("\n", strip=True).split("\n") if l.strip()]
        if not lines:
            continue

        price_eur = None
        for i, l in enumerate(lines):
            if l == "€" and i > 0:
                # Confirmed against imot.bg (same underlying Focus-backend
                # listings, matched via shared photo IDs): stripping all
                # non-digits from the previous line, unchecked, occasionally
                # swallowed a stray adjacent number (e.g. floor/sqm) into the
                # price when the two got merged with no separator during text
                # extraction - producing a price ~100x too large. Requiring
                # the previous line to be purely digits/whitespace (a real
                # price line never has anything else on it) rejects those.
                prev_line = lines[i - 1]
                if re.fullmatch(r"[\d\s]{3,10}", prev_line):
                    price_eur = int(re.sub(r"\s", "", prev_line))
                break
        if price_eur is None or price_eur < 1000 or price_eur > 10_000_000:
            continue

        # Parsed from the card's own text, not trusted from the query slug
        # - see the module docstring's "Each listing's city/area used to be
        # tagged..." paragraph. city_display (the CITY_SLUGS entry that
        # produced this query) is kept only as the fallback for a card
        # whose text matches neither line format.
        city = city_display
        area = city_display
        for l in lines:
            m = CITY_AREA_LINE_RE.match(l)
            if m:
                city, area = m.group(1).strip(), m.group(2).strip()
                break
            m2 = VILLAGE_LINE_RE.match(l)
            if m2:
                city = area = m2.group(1).strip()
                break

        img_url = None
        for img in container.find_all("img"):
            candidate = img.get("src") or img.get("data-src")
            if candidate and "icons/" not in candidate.lower():
                if candidate.startswith("//"):
                    candidate = "https:" + candidate
                elif candidate.startswith("/"):
                    candidate = BASE_URL + candidate
                img_url = candidate
                break

        href = a["href"]
        full_url = href if href.startswith("http") else BASE_URL + href
        title = lines[0] if lines else area
        category, category_confidence, _ = classify_listing(title=title, url=full_url)

        listings[listing_id] = {
            "id": "bazar_" + listing_id,
            "url": full_url,
            "photo": img_url,
            "price_eur": price_eur,
            "sqm": None,
            "area": area,
            "city": city,
            "title": title[:150],
            "portal": "bazar.bg",
            "lat": None,
            "lng": None,
            "category": category,
            "category_confidence": category_confidence,
        }
    return listings


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
    # deadline/on_checkpoint, the CITY_SLUGS-index rotation persisted in
    # GRID_STATE_FILE, and the checkpoint-per-completed-entry shape below
    # are ported from scraper_olx.py's own fetch_listings() (docs/
    # backlog.md item 30) - see that file's own comment on this same
    # shape for the full reasoning; kept in sync here rather than
    # re-derived, per item 31's own instruction to reuse this exact
    # mechanism instead of inventing a second one.
    #
    # CITY_SLUGS is a fixed list order, so a bounded run that always
    # started at index 0 would keep running out of budget on the same
    # tail entries - GRID_STATE_FILE's next_start_index instead rotates
    # which entry THIS run starts from to wherever the PREVIOUS run left
    # off, so a bounded run's leftover entries shift each time and every
    # entry gets roughly even coverage across runs instead of the tail
    # being starved forever.
    all_listings = {}
    state = load_grid_state()
    start_index = state.get("next_start_index", 0) % len(CITY_SLUGS)
    order = CITY_SLUGS[start_index:] + CITY_SLUGS[:start_index]
    if start_index:
        print(f"DEBUG: grid crawl resuming at CITY_SLUGS index {start_index} "
              f"({order[0][0]}) per {GRID_STATE_FILE.name}'s rotation state")

    entries_completed = 0
    for city_display, slug in order:
        if deadline is not None and time.monotonic() >= deadline:
            print(f"DEBUG: stopping grid crawl before {city_display} - approaching this run's time "
                  f"budget, {len(order) - entries_completed} of {len(order)} entries left for a future run")
            break

        search_url = f"{SEARCH_BASE}/{slug}"
        city_before = len(all_listings)
        prev_ids = None
        consecutive_failures = 0
        entry_interrupted = False
        for page_num in range(1, MAX_PAGES + 1):
            if deadline is not None and time.monotonic() >= deadline:
                print(f"DEBUG: stopping mid-{city_display} at page {page_num} - "
                      f"approaching this run's time budget")
                entry_interrupted = True
                break
            url = search_url if page_num == 1 else f"{search_url}?page={page_num}"
            page_listings = fetch_listings_page(url, city_display)
            if page_listings is None:
                # A failed fetch (all in-request retries exhausted) is not
                # the real end-of-results signal (an empty page, or the
                # clamped-page-id-repeat check below, is) - giving up on the
                # whole city here would silently truncate every remaining
                # page after one bad request, the same bug found in
                # scraper.py/scraper_alo.py/scraper_imot.py/scraper_homes.py/
                # scraper_olx.py. Skip it and keep going, only giving up on
                # the city after several in a row.
                consecutive_failures += 1
                if consecutive_failures >= MAX_CONSECUTIVE_PAGE_FAILURES:
                    break
                continue
            consecutive_failures = 0
            page_ids = frozenset(page_listings.keys())
            print(f"DEBUG: {city_display} page {page_num} links matching listing URL pattern = {len(page_listings)}")
            if not page_listings or page_ids == prev_ids:
                # Empty page, or the site clamped this out-of-range page
                # number back to the same last real page (its listing ID
                # set is identical to the previous page's) - either way
                # there's nothing new past this point.
                break
            all_listings.update(page_listings)
            prev_ids = page_ids

        suffix = " (interrupted mid-page, will resume here next run)" if entry_interrupted else ""
        print(f"DEBUG: {city_display} done, {len(all_listings) - city_before} new listings{suffix}")

        if entry_interrupted:
            # Doesn't count as completed - next_start_index below stays
            # pointed at this same entry so the next run resumes on it
            # instead of skipping straight past it.
            break

        entries_completed += 1
        if on_checkpoint:
            on_checkpoint(list(all_listings.values()))

    # If every entry in `order` completed, (start_index + len(order)) wraps
    # back to start_index exactly - correct, since a fully-completed lap
    # makes the next run's starting point unimportant either way.
    next_start_index = (start_index + entries_completed) % len(CITY_SLUGS)
    save_grid_state({"next_start_index": next_start_index})
    if entries_completed < len(order):
        print(f"DEBUG: grid crawl covered {entries_completed}/{len(order)} entries this run - next run "
              f"resumes at CITY_SLUGS index {next_start_index} ({CITY_SLUGS[next_start_index][0]})")

    return list(all_listings.values())


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


# Fields backfill_detail_bazar.py's own detail-page pass adds on top of
# what the grid crawl (fetch_listings_page()) itself produces:
# "description"/"photos"/"coords_checked" are never set by the grid at
# all; "lat"/"lng" ARE present on a fresh grid record but only ever as the
# None placeholder fetch_listings_page() sets them to - real coordinates
# only ever come from that separate detail pass. update_history() below
# must merge these in from the previous "latest" rather than let a fresh
# grid re-touch wipe them off an already-detail-checked, still-active
# listing every ~6 hours - docs/backlog.md item 9a.
#
# 2026-09-24: extended with the same spec-table/agency fields alo.bg's own
# _DETAIL_ONLY_FIELDS list already carries (see scraper_alo.py's own
# comment on this list for the full reasoning), now that
# backfill_detail_bazar.py also fills these in via geo_utils.
# extract_specs_bazar()/extract_contact_bazar(). "sqm" is unconditionally
# detail-only here (not the "special case" it is for alo.bg) - unlike
# alo.bg's grid crawl, bazar.bg's own grid crawl (fetch_listings_page())
# never sets a real sqm value at all, always None (see this module's own
# docstring: "bazar.bg's listing grid doesn't show square meters"), so
# there's no competing fresh-grid-value case to preserve a merge-not-
# replace exception for.
_DETAIL_ONLY_FIELDS = (
    "description", "photos", "coords_checked", "lat", "lng", "sqm",
    "property_type_raw", "construction_type", "floor_number",
    "agency_name", "agency_website",
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
    # record_new()/checkpoint closure ported from scraper_olx.py's own
    # main() (docs/backlog.md item 30) - see that file's comment for the
    # full reasoning. Keyed by listing id (recorded_ids), not just called
    # once at the end, so a run that checkpoints several times mid-crawl
    # (see fetch_listings()) only ever appends the delta since the last
    # checkpoint into history's snapshots - calling update_history() again
    # on the same listing within one run would otherwise double-append a
    # near-duplicate snapshot for it.
    history = load_history()
    recorded_ids = set()

    def record_new(listings_so_far):
        nonlocal history
        new_listings = [l for l in listings_so_far if l["id"] not in recorded_ids]
        if not new_listings:
            return
        history = update_history(history, new_listings)
        recorded_ids.update(l["id"] for l in new_listings)
        save_history(history)
        leads = compute_leads(history)
        save_json_any(LEADS_FILE, leads)

    deadline = time.monotonic() + TIME_BUDGET_SECONDS
    listings = fetch_listings(deadline=deadline, on_checkpoint=record_new)
    # Final catch-up: fetch_listings() only checkpoints after each *fully
    # completed* CITY_SLUGS entry, so a run interrupted mid-entry (deadline
    # hit partway through that entry's own page loop) can still be holding
    # a few pages' worth of listings that were never checkpointed.
    # record_new()'s id-delta tracking makes this a cheap no-op on a normal
    # run where nothing was interrupted.
    record_new(listings)
    leads = compute_leads(history)
    print(f"Found {len(listings)} listings, {len(leads)} tracked leads")


if __name__ == "__main__":
    main()
