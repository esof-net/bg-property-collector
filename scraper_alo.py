"""
Scrapes current Bulgaria-wide apartment listings from alo.bg.

Search results are paginated with &page=N. The scraper originally only
fetched page 1 with no pagination loop at all - fixed by paging through
page=2, page=3, ... until a page comes back with no listings, same "stop
on empty page" pattern as scraper_bazar.py, scraper_imot.py, and
scraper_imoti_bg.py, capped at MAX_PAGES as a safety limit. Learned from
scraper.py's imoti.net fix: a delay between requests is needed to avoid
getting rate-limited (HTTP 403) partway through, and a failed request is
treated as "no more listings" (stop and keep what was collected) rather
than crashing the whole run.

Found in production (not in earlier live testing) that a single request can
also fail transiently - e.g. a connect timeout with no HTTP response at all,
unrelated to any real block - and treating that identically to "no more
listings" cut a real run off at page 21 of ~166, keeping only 613 of the
old Sofia-only ~9995 listings. A page fetch retries a few times with
backoff first, but at nationwide scale (2,800 pages instead of 166) even a
low per-page failure rate means SOME page exhausting all retries becomes
likely over a full run, not a rare fluke - confirmed live as the real
cause of alo.bg's chronic nationwide undercount (~15,000 tracked against
this scraper's own ~80,424-listing live-verified total): fetch_listings()
still couldn't tell "this one page failed after retries" (a transient
blip - the crawl should skip it and keep going) apart from "a real empty
page" (genuinely reached the end - the crawl should stop), so the first
page anywhere in a 2,800-page run to exhaust its retries silently ended
the entire crawl right there. Now tracked separately: a page that
exhausts retries is skipped (losing only that one page's ~60 listings,
not the rest of the run) and only enough CONSECUTIVE page-level failures
in a row (suggesting a real, sustained outage rather than one-off
flakiness) actually stops the crawl early.

Nationwide conversion: the Sofia scope was two URL params, ?region_id=22
&location_ids=4342 - live-verified that dropping them entirely (or setting
region_id=0, same effect) gives nationwide results, jumping the site's own
stated total from ~10,009 to ~80,424 (apartments only; this scraper still
doesn't cover other property types, same as before). Paged all the way to
~2600 with real content and no block of any kind (unlike homes.bg/
imoti.net's confirmed depth caps) before hitting a genuine 404 past the
real last page - alo.bg's own per-page listing count is also higher
nationwide (60/page vs 30/page Sofia-only), so the real total is closer to
~156,000. MAX_PAGES raised well past that with real margin.

CORRECTION (2026-09-27, investigating backlog item 61's alo.bg-vs-Sofia
finding): the "~156,000" estimate two paragraphs above is wrong, and wrong
in a way worth flagging clearly rather than quietly fixing, because it
was the reason a real coverage gap was suspected here in the first place.
It came from multiplying MAX_PAGES-scale page count by "60/page" - but
that 60 is fetch_listings_page()'s own `link_count` return value, which
counts every LISTING_LINK_RE-matching <a> TAG on the page (each real
listing card has two - see smallest_container_with_price()'s own use of
this), not 60 unique listings. Confirmed directly against two full,
real, complete production runs (workflow run 35973606926, 2026-09-24, and
workflow run 36113518576, 2026-09-25 - both scraper_alo.py job steps,
full logs read page-by-page, not just the tail): `link_count` prints
exactly 60 on essentially every real page in both runs, but the actual
NEW-listing yield per page (the delta in "N listings so far" between
consecutive pages) is consistently ~29-30 the entire way through both
2.25-hour crawls, page 1 through page ~2706, with no degradation over
time. So nationwide is ~30 unique listings/page, the SAME rate as the
old Sofia-only crawl, not double it - there's no real basis for a
~156,000-listing nationwide total. Both runs instead crawled cleanly to
a genuine 404 wall (page 2707-2711, never anywhere near MAX_PAGES=2800)
and landed within ~100 listings of each other (77,843 and 77,935) - i.e.
this scraper is capturing close to the FULL real current nationwide
inventory under this search query, not roughly half of it. This means
the Sofia/Plovdiv/Burgas imbalance backlog item 61 found in alo.bg's own
raw `city` field (Бургас 18,128 / Пловдив 17,632 / София 9,906) is not
explained by an uneven partial crawl - the crawl is close to complete -
so it reflects either a genuine fact about alo.bg's own current listing
mix by city, or a URL/query-construction issue independent of pagination
depth (e.g. whether region_id=0 truly applies no server-side region bias
at all, vs. some session/cookie-driven default) that this sandbox's
alo.bg egress block prevents confirming live. See docs/decisions.md's
2026-09-27 entry for the full investigation and what a human/agent with
live alo.bg access should check next.

LOCATION_RE replaces the old Sofia-only AREA_RE (which matched "<area
words>, София" specifically) - live samples of real non-Sofia cards found
the consistent shape "<settlement>, [област ]<city>" immediately before
"Цена :" (note: alo.bg's "област" - region - is a PREFIX before the city/
region name here, unlike imoti.bg's own listings elsewhere in this project
where it's a trailing suffix - these are two different portals' own text
conventions, not the same rule). Captures settlement as "area" (unchanged
meaning) and the city/region name (with any "област " prefix stripped) as
a new "city" field, matching what index.html's/sync_to_supabase.py's
city-key logic already expects.

days_on_market/coords/category: previously fetched inline during the main
scrape by visiting every listing's own page (the search grid carries no
date signal at all). At nationwide scale (~156,000 listings vs. the old
~10,000 Sofia-only) that's no longer affordable in a single scrape pass,
so it's decoupled the same way homes.bg's/imoti.bg's geocoding was:
fetch_listings() now only does the fast grid crawl, and
backfill_detail_alo.py (a separate, resumable, prioritized-by-newest-first
job) visits listing pages over time to fill in site_updated_at/lat,lng/
description/specs/contact via fetch_update_dates() (kept here, now unused
by the main scrape path but still imported and reused by the backfill
script).

description is extracted via geo_utils.extract_description_alo() - as of
backlog #9 (2026-09-23) this deliberately returned None unconditionally
rather than `.obqva-block` text, which turned out to be a title echo, not
a real free-text description (see the NOTE above that function in
geo_utils.py for the full investigation). As of 2026-09-24, that function
has a real implementation, built from real user-supplied screenshots of a
live detail page rather than a live HTML probe (this sandbox's network
egress to alo.bg is still blocked) - see its own comment for the full
selector strategy. A 2026-09-26 production re-audit found that
implementation still reproduces the same title-echo bug on 77.7% of what
it extracts (down from the old selector's 88.3%, not actually fixed) -
extract_description_alo() now also takes this listing's own `l["title"]`
and rejects a candidate that's a substring of it, per that function's own
comment. The same screenshots are also the source for two new
detail-page extractors wired in below: geo_utils.extract_specs_alo()
(property type/size/construction type/built year/completion status/
floor/features from the page's structured spec table - sqm from here
feeds the existing sqm/price_per_sqm fields, which were null for
essentially every alo.bg listing before this) and geo_utils.
extract_contact_alo() (agency name + real external agency website -
deliberately NOT the masked phone number; see that function's own comment
for why no phone extraction is implemented).
"""

import re
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

from geo_utils import (
    extract_coords_alo, extract_description_alo, extract_photos_alo, extract_specs_alo,
    extract_contact_alo, compute_motivation_score, listing_city_key, prune_snapshots, evict_stale_records,
    STALE_RECORD_RETENTION, load_json_any, save_json_any,
)
from category_classifier import classify_listing

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; PersonalDealTracker/1.0)"}
SEARCH_URL = "https://www.alo.bg/obiavi/imoti-prodajbi/apartamenti-stai/?region_id=0"
BASE_URL = "https://www.alo.bg"

OUT_DIR = Path(__file__).parent / "data"
OUT_DIR.mkdir(exist_ok=True)
# .json.gz, not plain .json - 2026-09-26 GH001 incident (backfill-detail-
# alo.yml's push rejected: leads_alo.json 104.29MB/history_alo.json
# 101.95MB, both over GitHub's 100MB hard limit). Same fix already proven
# for homes.bg (2026-09-25 addendum) - gzip these enormously repetitive
# files instead of trimming data. Every reader/writer goes through
# load_json_any()/save_json_any(), which are extension-aware.
HISTORY_FILE = OUT_DIR / "history_alo.json.gz"
LEADS_FILE = OUT_DIR / "leads_alo.json.gz"

MAX_CARD_TEXT_LENGTH = 1500
MAX_PRICE_MENTIONS = 1
# Real ~30 unique listings/page (not the 60 raw <a>-tag matches/page this
# comment originally assumed - see the module docstring's 2026-09-27
# correction) puts the real nationwide total around ~78,000-80,000, not
# ~156,000; two full real production runs (2026-09-24, 2026-09-25) both
# crawled cleanly to a genuine 404 wall around page 2707-2711, nowhere
# near this cap. Left at 2800 anyway - real margin above the confirmed
# real page count costs nothing (the crawl already stops cleanly on its
# own real 404 well before reaching it) and protects against genuine
# future site growth.
MAX_PAGES = 2800
REQUEST_DELAY_SECONDS = 1.0
# A run of this many CONSECUTIVE page-level failures (each already having
# exhausted its own retries) is treated as a real, sustained outage worth
# stopping for - anything less is just skipped, one bad page at a time, so
# a handful of scattered transient blips across a 2,800-page run can't
# truncate the rest of it the way a single one used to.
MAX_CONSECUTIVE_PAGE_FAILURES = 5
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 5
# Connection-level failures (the TCP handshake itself never completed, or a
# connection attempt was refused/never resolved - requests.ConnectTimeout,
# or a plain requests.ConnectionError raised before any response existed)
# get far fewer attempts and no escalating backoff than a real HTTP-level
# failure. Live job logs (run 36085734587, backfill-detail-alo.yml,
# 2026-09-25 02:18-02:55 UTC) showed 55 distinct URLs in one 35-minute
# backfill_detail_alo.py run that never completed a TCP handshake at all
# ("Connection to www.alo.bg timed out. (connect timeout=8)"). Under the
# old MAX_RETRIES=3 with escalating RETRY_BACKOFF_SECONDS backoff, each one
# cost up to 3*8s (connect timeouts) + 5s + 10s (backoff after attempts 1
# and 2) = 39s worst case - 55 * 39s =~2,145s, i.e. almost the entire
# 35-minute TIME_BUDGET_SECONDS budget in backfill_detail_alo.py, spent on
# URLs that were never going to answer, before a single successful fetch.
# A dead network path to the same host is very unlikely to start accepting
# connections again one or two retries later within the same run -
# especially plausible (though not confirmed here) if this is IP-level
# rate-limiting/blocking of GitHub Actions runner IP ranges by alo.bg, which
# no amount of same-run backoff would clear. See fetch_with_retries()'s own
# comment for the resulting worst-case math under the new limits.
CONNECT_FAILURE_MAX_RETRIES = 2
CONNECT_FAILURE_BACKOFF_SECONDS = 0

# Same reasoning as MAX_CONSECUTIVE_PAGE_FAILURES above, applied to
# fetch_update_dates()'s per-listing detail-page visits: a live production
# run hit a burst of 429 Too Many Requests across many different listing
# URLs in a row (confirmed via job logs) - the site actively throttling
# this run, not any one listing being bad. Grinding through the rest of a
# ~1,000-listing batch at 3 retries each in that state wastes the entire
# run on requests that were never going to succeed either; stopping early
# here leaves the rest of the batch for a later run once the throttling
# (usually tied to a request-rate window) has likely cleared.
MAX_CONSECUTIVE_DETAIL_FAILURES = 5


class PermanentlyGone(Exception):
    """Raised by fetch_with_retries() for a 404/410 - the listing is gone
    for good, which is normal and expected in a large newest-first backlog
    and proves the server is responding fine. Deliberately not just
    another None return: a live run hit MAX_CONSECUTIVE_DETAIL_FAILURES
    after only 5 ordinary dead listings and aborted with zero progress
    despite a full time budget, because the old code folded "permanently
    gone" and "retries exhausted after a real failure" into the same
    signal. Only the latter should count toward that threshold."""

LISTING_LINK_RE = re.compile(r"^/[a-z0-9\-]+-(\d{6,9})$")
UPDATED_TEXT_RE = re.compile(r">((?:Актуализирана|Публикувана)[^<]{0,40})<")
DAYS_AGO_RE = re.compile(r"преди\s+(\d+)\s+д")
# alo.bg's regular listing cards ("listtop-item") format this "Цена:" tight,
# but its promoted/VIP cards ("listvip-item" - the template used on paginated
# pages, which is most of them) format it "Цена :" with a space before the
# colon. That mismatch alone caused the extraction to silently drop ~85-95%
# of listings on every page past page 1 - allow optional whitespace here.
PRICE_RE = re.compile(r"Цена\s*:\s*([\d\s]+)\s?€")
SQM_RE = re.compile(r"Квадратура:\s*([\d.,]+)\s?кв\.?м")
AREA_WORD = r"[А-Я][а-я]*"
# Replaces the old Sofia-only "<area words>, София" match - live samples of
# real non-Sofia cards found the consistent shape "<settlement>,
# [област ]<city>" right before "Цена :" (see module docstring). Group 1 is
# the settlement/neighborhood (kept as "area", same meaning as before);
# group 2 is the city/region name with any "област " prefix already
# stripped by the non-capturing group ahead of it.
LOCATION_RE = re.compile(
    "((?:" + AREA_WORD + r"\s+){0,3}" + AREA_WORD + r"(?:\s+\d+)?),\s*"
    r"(?:област\s+)?(" + AREA_WORD + r"(?:\s+" + AREA_WORD + r")?)\s*Цена\s*:"
)


# AREA_WORD matches any capitalized Cyrillic word with no semantic filter,
# so a property-type label sitting right before the real area in the card's
# raw text (no comma between them - "Двустаен Лазур Лазур, Бургас Цена :")
# gets swept into the same match. Live-sampled: 347/14,983 currently-active
# listings had an area value starting with one of these.
AREA_TYPE_PREFIXES = (
    "Едностаен", "Двустаен", "Тристаен", "Четиристаен", "Многостаен",
    "Мезонет", "Ателие", "Стая",
)


def dedup_area(area):
    words = area.split()
    while words and words[0] in AREA_TYPE_PREFIXES:
        words = words[1:]
    if not words:
        # Nothing left after stripping the type label(s) - the real area
        # apparently wasn't part of this match at all (a different card
        # layout LOCATION_RE doesn't fully cover). Keep the original rather
        # than return an empty string.
        return area
    if len(words) > 1:
        first = words[0]
        for i in range(len(words) - 1, 0, -1):
            if words[i] == first:
                return " ".join(words[i:])
        # The site's own card markup sometimes repeats just the area name
        # itself, immediately, with no other duplication ("Лазур Лазур",
        # "Витоша Витоша") - the loop above only catches the *first* word
        # recurring later, so check the simpler last-word-repeats-once case
        # separately.
        if words[-1] == words[-2]:
            return words[-1]
    return " ".join(words)


def smallest_container_with_price(link_tag, max_levels=6):
    node = link_tag
    for i in range(max_levels):
        if node.parent is None:
            break
        node = node.parent
        text = node.get_text(" ", strip=True)
        matches = PRICE_RE.findall(text)
        if len(matches) > MAX_PRICE_MENTIONS:
            return None
        if len(matches) == 1 and len(text) <= MAX_CARD_TEXT_LENGTH:
            return node
    return None


def fetch_with_retries(url):
    # requests.exceptions.ConnectTimeout is a subclass of both
    # requests.exceptions.ConnectionError and requests.exceptions.Timeout
    # (verified against the installed requests version, not assumed): it's
    # raised specifically when the TCP handshake itself never completed
    # within the connect timeout. A plain requests.exceptions.ConnectionError
    # (not a ConnectTimeout) covers the sibling case of a connection that
    # was refused or never resolved at all (e.g. DNS failure) - requests
    # wraps urllib3's NewConnectionError the same way, also with no response
    # ever received. Catching ConnectionError (which includes ConnectTimeout)
    # ahead of the generic RequestException catch-all cleanly separates both
    # of those "never got a response at all" cases from:
    #   - requests.exceptions.ReadTimeout, a *sibling* of ConnectionError
    #     (Timeout, not ConnectionError) - the connection itself succeeded,
    #     the server was just slow to answer, so a retry has a real chance.
    #   - requests.HTTPError (handled above) - a real response (e.g. a 5xx)
    #     came back, so the connection is fine and retrying is worthwhile.
    # Only the true connection-level case gets the fast-fail treatment.
    attempt = 1
    while True:
        try:
            # (connect, read) rather than one 20s timeout for both - live
            # job logs showed backfill_detail_alo.py runs burning most of
            # their 35-minute time budget on ConnectTimeoutErrors (TCP
            # handshake never completing, not a slow response), at up to
            # 20s x 3 retries = 60s per dead URL, and only clearing ~200 of
            # the ~73,000-listing description backlog per run as a result.
            # A connect that hasn't succeeded in 8s isn't going to.
            resp = requests.get(url, headers=HEADERS, timeout=(8, 15))
            resp.raise_for_status()
            return resp.text
        except requests.HTTPError as e:
            # 404/410 mean the page is permanently gone - retrying can
            # never succeed. Same fix as scraper.py/scraper_bazar.py's own
            # fetch_with_retries/fetch_html (see their comments).
            status = e.response.status_code if e.response is not None else None
            if status in (404, 410):
                print(f"DEBUG: {url} permanently gone ({status}) - not retrying")
                raise PermanentlyGone(url) from None
            max_attempts = MAX_RETRIES
            backoff = RETRY_BACKOFF_SECONDS * attempt
            print(f"DEBUG: request failed for {url} (attempt {attempt}/{max_attempts}): {e}")
        except requests.exceptions.ConnectionError as e:
            # Connection-level failure - see the fast-fail comment above and
            # CONNECT_FAILURE_MAX_RETRIES's own comment for the time-budget
            # math. Worst case here: CONNECT_FAILURE_MAX_RETRIES * 8s connect
            # timeout with zero backoff between attempts, e.g. 2*8s = 16s -
            # versus the old 3*8s+5s+10s = 39s, a ~59% cut per dead URL. For
            # the 55-dead-URL figure observed in one real run (not a
            # guaranteed constant, just the best evidence available): old
            # cost ~=55*39s=2,145s (~35.75 min, i.e. effectively the whole
            # TIME_BUDGET_SECONDS budget); new cost ~=55*16s=880s (~14.7
            # min) - freeing roughly 1,265s (~21 minutes) per run for URLs
            # that actually complete a connection.
            max_attempts = CONNECT_FAILURE_MAX_RETRIES
            backoff = CONNECT_FAILURE_BACKOFF_SECONDS
            print(
                f"DEBUG: connection-level failure for {url} "
                f"(attempt {attempt}/{max_attempts}, fast-fail path): {e}"
            )
        except requests.RequestException as e:
            max_attempts = MAX_RETRIES
            backoff = RETRY_BACKOFF_SECONDS * attempt
            print(f"DEBUG: request failed for {url} (attempt {attempt}/{max_attempts}): {e}")

        if attempt >= max_attempts:
            return None
        if backoff:
            time.sleep(backoff)
        attempt += 1


def fetch_listings_page(url, seen):
    # PermanentlyGone (404/410) is deliberately NOT caught here - it must
    # propagate to fetch_listings()'s own caller, which treats "hit a real
    # 404 past the last page" as a distinct, confirmed-end-of-pagination
    # signal, not a generic failure. See fetch_listings()'s own comment for
    # why conflating the two (as this function used to, returning None for
    # both) was a real bug, confirmed against real job logs.
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
            continue

        container = smallest_container_with_price(a)
        if container is None:
            continue

        text = container.get_text(" ", strip=True)

        price_match = PRICE_RE.search(text)
        sqm_match = SQM_RE.search(text)
        location_match = LOCATION_RE.search(text)
        if not price_match:
            continue

        price_eur = int(re.sub(r"\D", "", price_match.group(1)))
        if price_eur < 1000:
            continue
        sqm = None
        if sqm_match:
            sqm_str = sqm_match.group(1).replace(",", ".")
            try:
                sqm = round(float(sqm_str))
            except ValueError:
                sqm = None

        if location_match:
            area = dedup_area(location_match.group(1).strip())
            city = dedup_area(location_match.group(2).strip())
        else:
            # Used to write area, city = "Bulgaria", None here - a literal
            # placeholder, not a real signal, that ended up user-visible
            # (index.html's neighborhood filter dropdown pulls straight
            # from every listing's own l.area) and, worse, fed straight
            # into sync_to_supabase.py's listing_oblast_key() as if it
            # were real location text to try matching - it never matches
            # anything (it isn't a real Bulgarian place name), so it
            # never actively caused a WRONG match, but it dressed up "we
            # have no location text for this card" as if it were a
            # (wrong) answer instead of a plain unknown. None/None is the
            # honest value: sync_to_supabase.py's own field loop already
            # treats a falsy city/area as "try the next source" (the
            # title-based fallback, or - since backfill_others_alo_
            # detail.py exists specifically for this LOCATION_RE-miss
            # case - a later coordinate backfill), same as it does for
            # every other portal's genuinely-missing location text. See
            # docs/backlog.md item 4 and docs/decisions.md's 2026-09-22
            # entry for the full investigation (confirmed independent of
            # item 3's "grid crawl looked dead" bug - different root
            # cause, same file, pure coincidence).
            area, city = None, None

        # An agency-posted card has TWO images in this container: its own
        # branding/avatar (class "listtop-logo") *before* the real property
        # photo (class "listtop-image-img") in DOM order - confirmed live
        # against real cards via probe_alo_photos.py. Plain container.find(
        # "img") grabbed whichever came first, which was the agency's logo
        # for every agency-posted listing (93.8% of currently tracked
        # listings, one avatar reused across 1,201 unrelated ones) - the
        # actual property photo was sitting right there, just second.
        # Individually-posted listings (no agency) only ever have the one,
        # correctly-classed image, so this still works for them unchanged.
        img = container.find("img", class_="listtop-image-img")
        if img is None:
            # Fall back to the first non-avatar/non-logo image rather than
            # blindly taking whatever's first, in case the class name ever
            # changes - never show an avatar as if it were the property.
            img = next(
                (i for i in container.find_all("img")
                 if i.get("src") and "avatar" not in i["src"].lower()
                 and (not i.get("class") or "listtop-logo" not in i.get("class"))),
                None,
            )
        img_url = img.get("src") if img else None
        if img_url:
            if img_url.startswith("//"):
                img_url = "https:" + img_url
            elif img_url.startswith("/"):
                img_url = BASE_URL + img_url
            elif not img_url.startswith("http"):
                img_url = BASE_URL + "/" + img_url

        full_url = BASE_URL + a["href"]
        title = a.get_text(" ", strip=True)
        if not title:
            # The <a> tag itself carries no text on this card layout (the
            # common case - see LOCATION_RE's own docstring for how often
            # LOCATION_RE alone already misses this shape). Used to fall
            # back to a blind text[:100] here, which kept whatever
            # happened to sit at the very START of the container's own
            # text (agency name, "преди N дни", other boilerplate) and
            # cut off before the location phrase, which this project's
            # own real samples confirm sits at the very END, immediately
            # before "Цена :" (same shape LOCATION_RE matches on - see
            # its docstring). Since price_match already matched above,
            # "Цена" is guaranteed present in text at least once, so
            # slicing up to it and keeping the TAIL nearest to it (not
            # the head, which is only guaranteed to be non-empty, not
            # short - the "Bulgaria"/None-area fix above already stopped
            # LOCATION_RE misses from being silently mislabeled, but the
            # stored title itself needs the location words too, both for
            # a human glancing at the listing and for sync_to_supabase.
            # py's own title-based oblast fallback) survives the [:120]
            # cut just below. Real example this fixes: alo_11149326 used
            # to truncate mid-word right before "Слънчев бряг ...
            # Бургас" would have appeared - see docs/backlog.md item 4.
            price_pos = text.find("Цена")
            pre_price = text[:price_pos].strip() if price_pos != -1 else text
            title = pre_price[-100:] if len(pre_price) > 100 else pre_price

        listing_title = title[:120]
        category, category_confidence, _ = classify_listing(title=listing_title, url=full_url)
        seen[listing_id] = {
            "id": "alo_" + listing_id,
            "url": full_url,
            "photo": img_url,
            "price_eur": price_eur,
            "sqm": sqm,
            "area": area,
            "city": city,
            "title": listing_title,
            "portal": "alo.bg",
            "category": category,
            "category_confidence": category_confidence,
        }
    return len(matching_links)


def parse_days_ago(html):
    m = UPDATED_TEXT_RE.search(html)
    if not m:
        return None
    text = m.group(1)
    if "днес" in text:
        return 0
    if "вчера" in text:
        return 1
    m2 = DAYS_AGO_RE.search(text)
    return int(m2.group(1)) if m2 else None


def fetch_update_dates(seen, on_checkpoint=None, checkpoint_every=150, deadline=None):
    consecutive_failures = 0
    for i, (listing_id, l) in enumerate(seen.items(), 1):
        if deadline is not None and time.monotonic() >= deadline:
            print(f"DEBUG: stopping at {i - 1}/{len(seen)} - approaching this run's time budget")
            break
        time.sleep(REQUEST_DELAY_SECONDS)
        try:
            html = fetch_with_retries(l["url"])
        except PermanentlyGone:
            # A clean 404/410 proves the server is responding normally -
            # it's not the site-health signal this early-stop exists for,
            # and there's nothing left to gain from ever revisiting this
            # URL, so mark it done same as a successful visit would.
            consecutive_failures = 0
            l["_detail_fetched"] = True
            l["_photos_checked"] = True
            l["_gallery_specs_rechecked"] = True
            l["_description_title_echo_rechecked"] = True
            continue
        if html is None:
            consecutive_failures += 1
            if consecutive_failures >= MAX_CONSECUTIVE_DETAIL_FAILURES:
                print(f"DEBUG: {consecutive_failures} consecutive detail-page failures at "
                      f"{i}/{len(seen)} - looks like the site is throttling this run, stopping here")
                break
            continue
        consecutive_failures = 0
        days_ago = parse_days_ago(html)
        if days_ago is not None:
            l["site_updated_at"] = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
        coords = extract_coords_alo(html)
        if coords:
            l["lat"] = coords["lat"]
            l["lng"] = coords["lng"]
        description = extract_description_alo(html, title=l.get("title"))
        if description:
            l["description"] = description
        photos = extract_photos_alo(html)
        if photos:
            l["photos"] = photos
        # Structured specs table (property type/construction type/built
        # year/completion status/floor/features) - see geo_utils.
        # extract_specs_alo()'s own comment. sqm feeds straight into the
        # existing sqm field (and, downstream, price_per_sqm in
        # compute_leads()) - the grid crawl's own SQM_RE already sets sqm
        # when a card happens to show "Квадратура: ..." text, so this only
        # fills it in when the grid pass came up empty (the overwhelming
        # majority of listings - see docs/backlog.md), never overwrites a
        # value the grid crawl already found on this same run.
        specs = extract_specs_alo(html)
        if specs:
            if specs.get("sqm") and not l.get("sqm"):
                l["sqm"] = specs["sqm"]
            for field in (
                "property_type_raw", "construction_type", "built_year", "completion_status",
                "floor_number", "floor_qualifier", "features", "has_elevator", "furnished",
                "has_central_heating",
            ):
                if field in specs:
                    l[field] = specs[field]
        # Agency name + real external agency website - deliberately no
        # phone number, see geo_utils.extract_contact_alo()'s own comment
        # for why.
        contact = extract_contact_alo(html)
        if contact:
            for field in ("agency_name", "agency_website"):
                if field in contact:
                    l[field] = contact[field]
        # category is now classified at grid-crawl time (title/url only,
        # no detail-page visit needed - see fetch_listings_page()), so this
        # backfill pass no longer touches it. "_detail_fetched" is the new
        # not-yet-enriched marker for backfill_detail_alo.py - site_updated_at/
        # lat,lng can genuinely stay unset even after a real visit (the site
        # doesn't always show them), so presence of an actual field can't be
        # used as the "was this visited" signal; this explicit marker can.
        l["_detail_fetched"] = True
        # Separate from _detail_fetched (not just folded into it) because
        # extract_photos_alo() was added to this function well after it had
        # already been visiting pages for weeks - every listing marked
        # _detail_fetched before that point permanently looked "done" to
        # backfill_detail_alo.py's missing-listing filter even though it was
        # never actually checked for photos, so its gallery would never get
        # backfilled at all. Live-confirmed: 18,010 _detail_fetched listings
        # with a real visit already done, still 0 with photos. Set
        # unconditionally (like _detail_fetched) so a real "this listing
        # genuinely has no gallery" result still counts as checked and isn't
        # retried forever - only listings visited before this flag existed
        # get the one-time re-check backfill_detail_alo.py now does.
        l["_photos_checked"] = True
        # 2026-09-24: the SAME "flag says checked, extractor didn't actually
        # work" problem _photos_checked was created to solve recurred, this
        # time within extract_photos_alo() itself rather than in whether it
        # ran at all - a production sample of every _photos_checked listing
        # (29,792 of them) found a 0.0% photos hit rate, root-caused to
        # _ALO_GALLERY_ANCHOR_RE requiring one exact HTML attribute order
        # that real markup had no reason to honor (see geo_utils.py's own
        # comment on the fix). extract_specs_alo()/extract_contact_alo()
        # also had overly-narrow structural assumptions loosened the same
        # day (see their own comments). Every _photos_checked: True listing
        # was visited under the OLD, broken photo extractor at least once,
        # so - same fix as _photos_checked's own creation - a new, separate
        # marker distinguishes "visited under today's fixed extractors" from
        # "visited (possibly under the old broken one)". Set unconditionally
        # on every real visit (like its two siblings) so a listing that
        # genuinely has no gallery under the fixed extractor still counts as
        # done and isn't retried forever - only listings visited before
        # today get backfill_detail_alo.py's new one-time re-check.
        l["_gallery_specs_rechecked"] = True
        # 2026-09-26: SAME "flag says checked, extractor didn't actually
        # work" problem again, this time for description specifically - a
        # production sample of 300 real non-empty descriptions produced by
        # the 2026-09-24 heading-based extract_description_alo() found
        # 233/300 (77.7%) are still exact title echoes, the same failure
        # mode the 2026-09-24 fix was meant to replace (see that function's
        # own comment on _looks_like_title_echo()). Every
        # _gallery_specs_rechecked: True listing was visited under that
        # still-flawed extractor at least once, so - same fix pattern as
        # _photos_checked's and _gallery_specs_rechecked's own creation - a
        # new, separate marker distinguishes "visited under today's
        # title-echo-aware extractor" from "visited (possibly under the
        # still-flawed one)". Set unconditionally on every real visit (like
        # its three siblings) so a listing that's genuinely just a short,
        # title-like description even under the new guard still counts as
        # done - only listings visited before today get backfill_detail_
        # alo.py's new one-time re-check.
        l["_description_title_echo_rechecked"] = True
        if on_checkpoint and i % checkpoint_every == 0:
            on_checkpoint()


def fetch_listings():
    # Grid crawl only - no detail-page visits here, see module docstring
    # (backfill_detail_alo.py handles site_updated_at/coords/category as a
    # separate, resumable pass; nationwide scale made doing it inline no
    # longer affordable in a single run).
    start_time = time.monotonic()
    seen = {}
    consecutive_failures = 0
    consecutive_permanently_gone = 0
    for page_num in range(1, MAX_PAGES + 1):
        if page_num > 1:
            time.sleep(REQUEST_DELAY_SECONDS)
        url = SEARCH_URL if page_num == 1 else f"{SEARCH_URL}&page={page_num}"
        elapsed = time.monotonic() - start_time
        try:
            link_count = fetch_listings_page(url, seen)
        except PermanentlyGone:
            # Real production job logs (runs 35973606926 on 2026-09-24 and
            # 36113518576 on 2026-09-25, both scraper_alo.py steps) show
            # alo.bg returns a genuine HTTP 404 for the first page past its
            # real last page, rather than a page that loads with zero
            # listings (the "stop on empty page" signal every sibling
            # scraper's own crawl loop relies on - see this module's own
            # docstring). Before this fix, that 404 was caught inside
            # fetch_listings_page() and returned as a plain None, identical
            # to a genuinely transient failure (a timeout or 5xx) - so
            # reaching the real end of the site always cost
            # MAX_CONSECUTIVE_PAGE_FAILURES (5) wasted page requests before
            # stopping, AND - the real risk - a genuine mid-crawl outage
            # would print the exact same "looks like a real outage" message
            # as an ordinary, harmless end-of-day completion, making the two
            # impossible to tell apart from a run's own logs.
            #
            # A 404 here is still trusted faster than a generic transient
            # failure (both real runs confirmed several consecutive 404s in
            # a row at the true end, not one flaky one among successes), but
            # NOT on the very first occurrence: alo.bg sits behind
            # CONNECT_FAILURE_MAX_RETRIES-acknowledged anti-bot/rate-limit
            # behavior against GitHub Actions IP ranges, and a WAF serving a
            # single spurious 404 instead of a 429/503 mid-crawl is a known
            # technique - requiring 2 in a row before treating this as the
            # real end avoids silently truncating a day's crawl on one
            # spurious response, while still stopping promptly (not after
            # MAX_CONSECUTIVE_PAGE_FAILURES=5 wasted requests) once it's
            # confirmed. Does not count toward MAX_CONSECUTIVE_PAGE_FAILURES,
            # which stays reserved for actual transient failures (timeouts,
            # connection errors, 5xx) where a retry might have succeeded.
            consecutive_permanently_gone += 1
            print(f"DEBUG: page {page_num} returned 404 "
                  f"({consecutive_permanently_gone}/2 consecutive) - "
                  f"(t={elapsed:.0f}s, {len(seen)} listings so far)")
            if consecutive_permanently_gone >= 2:
                print(f"DEBUG: {consecutive_permanently_gone} consecutive 404s - "
                      f"reached the real end of pagination")
                break
            continue

        if link_count is None:
            consecutive_failures += 1
            print(f"DEBUG: page {page_num} fetch failed after retries "
                  f"({consecutive_failures}/{MAX_CONSECUTIVE_PAGE_FAILURES} consecutive) - "
                  f"skipping this page (t={elapsed:.0f}s, {len(seen)} listings so far)")
            if consecutive_failures >= MAX_CONSECUTIVE_PAGE_FAILURES:
                print(f"DEBUG: {consecutive_failures} consecutive page failures - "
                      f"stopping (looks like a real outage, not just one-off flakiness)")
                break
            continue

        consecutive_failures = 0
        consecutive_permanently_gone = 0
        print(f"DEBUG: page {page_num} links matching listing URL pattern = {link_count} "
              f"(t={elapsed:.0f}s, {len(seen)} listings so far)")
        if not link_count:
            break
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


# Fields fetch_update_dates() (run separately via backfill_detail_alo.py -
# main() never calls it inline, see fetch_listings()'s own comment) adds
# on top of what the grid crawl (fetch_listings_page()) itself produces -
# never present on a fresh grid-only record. update_history() below must
# merge these in from the previous "latest" rather than let a fresh grid
# re-touch wipe them off an already-detail-checked, still-active listing
# every ~6 hours - docs/backlog.md item 9a. Same field list
# merge_history_conflict.py's own module docstring already names for this
# exact gap.
#
# "sqm" is a special case added 2026-09-24: it's normally a GRID field
# (fetch_listings_page()'s own SQM_RE sets it directly when a card happens
# to show "Квадратура: ..." text), which is why it isn't detail-only in
# the sense the other fields below are - a fresh grid crawl that DOES find
# a real sqm value must still be able to overwrite an old one (real edits
# happen). But now that fetch_update_dates() can ALSO fill sqm in (via
# extract_specs_alo(), for the ~99% of listings whose card never showed
# it), the same merge-not-replace protection every other detail-only field
# already gets is needed here too - otherwise a later grid-only re-touch
# that (as usual) finds no "Квадратура:" text on the card would silently
# wipe a real sqm value this backfill had already filled in, the exact
# shape of bug this field list exists to prevent. Being in this list still
# means "prefer the fresh value when the fresh value is non-empty" (see
# update_history() below) - so a genuine grid-parsed sqm change still
# wins, this only stops a grid MISS from clobbering a real detail-page
# value.
_DETAIL_ONLY_FIELDS = (
    "description", "photos", "site_updated_at", "lat", "lng",
    "_detail_fetched", "_photos_checked", "_gallery_specs_rechecked",
    "_description_title_echo_rechecked", "sqm",
    "property_type_raw", "construction_type", "built_year", "completion_status",
    "floor_number", "floor_qualifier", "features", "has_elevator", "furnished",
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
# scrape cycle due to pagination timing/site load. This scraper runs on its
# OWN 24h schedule (scrape-large.yml, not the 6-hourly scrape.yml the other
# portals use - see that workflow's own comment for why), unlike the 20h
# threshold detect_relistings.py uses for the 6-hourly portals - a 20h cutoff
# here would flag every single listing "removed" the moment a day passes
# without a scrape, confirmed live against imoti.net's own committed
# history.json (100% of listings came back "removed" at 20h; same scraper
# structure applies here). 48h gives a full extra cycle of slack for an
# occasionally slow/delayed run before concluding a listing is genuinely
# gone. Once removed, days_on_market/score freeze at the day it was last
# confirmed live instead of continuing to climb forever against an ad
# that's no longer there.
GONE_AFTER = timedelta(hours=48)


def compute_leads(history):
    leads = []
    for lid, rec in history.items():
        prices = [s["price_eur"] for s in rec["snapshots"] if s["price_eur"]]
        if not prices:
            continue
        first_price = prices[0]
        last_price = prices[-1]
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
        reference_date = (
            datetime.fromisoformat(site_updated_at) if site_updated_at else datetime.fromisoformat(rec["first_seen"])
        )
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
    area_avg = {}
    for key, v in area_totals.items():
        area_avg[key] = sum(v) / len(v)

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
    print("Found " + str(len(listings)) + " listings, " + str(len(leads)) + " tracked leads")


if __name__ == "__main__":
    main()
