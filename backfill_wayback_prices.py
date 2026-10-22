"""
One-time backfill: injects real pre-tracking price history for imot.bg and
bazar.bg listings, sourced from the Wayback Machine's archived captures of
each portal's Sofia search page.

Why this works when nothing else did: individual listing pages are almost
never archived (~2.5% coverage, see the removed wayback_probe.py), but
search/category pages are crawled far more often since they're linked-to
and indexed. probe_wayback_category.py confirmed this empirically: imot.bg's
search page has 18 real captures (Apr 2025 - Jul 2026), and a meaningful
fraction of the listing IDs on those old archived pages are STILL present
in our currently-tracked data - the most recent capture alone overlapped
35% of a 40-listing sample. That overlap is exactly what makes this useful:
for those specific listings, the Wayback Machine saw a real price before we
ever did.

This fetches every real capture of each search page (not a sample), parses
listing cards using the same technique as the live scrapers
(scraper_imot.py / scraper_bazar.py), and for every listing that's both
(a) currently in our tracked history and (b) was captured at a timestamp
earlier than our own first_seen for it, inserts that as a real historical
snapshot - never touching or reordering anything we observed ourselves.
Each injected snapshot is tagged "source": "wayback" so it stays
distinguishable from our own live observations (compute_leads() ignores
unknown keys, so this doesn't change how price_history gets derived - a
genuine price difference between the Wayback snapshot and whatever we
first recorded shows up as a real point on the chart, honestly sourced).

Only run once per portal's available captures - Wayback doesn't crawl more
often by re-running this, so there's nothing to gain from scheduling it.
"""

import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from geo_utils import load_json_any, save_json_any

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; PersonalDealTracker/1.0)"}
ARCHIVE_UA = "bg-property-tracker/1.0 (personal deal-tracking tool, non-commercial)"
CDX_URL = "https://web.archive.org/cdx/search/cdx"
REQUEST_DELAY_SECONDS = 2.0
MAX_RETRIES = 4
PORTAL_COOLDOWN_SECONDS = 30

DATA_DIR = Path(__file__).parent / "data"

# (portal, category url, history filename, id prefix, id regex, price line regex)
PORTALS = [
    (
        "imot.bg",
        "https://www.imot.bg/obiavi/prodazhbi/grad-sofiya",
        "history_imot.json.gz",
        "imot_",
        # Deliberately looser than scraper_imot.py's own LISTING_LINK_RE
        # (which requires a trailing "-" right after the ID): a first
        # backfill attempt using that exact live-site regex matched zero
        # links on every one of 18 archived snapshots, even a July-2026
        # one using an essentially current template - while this looser
        # pattern (no trailing-hyphen requirement) is the one that
        # actually found the real overlapping IDs in the first place
        # (probe_wayback_category.py). Archived hrefs apparently don't
        # reliably carry that trailing hyphen.
        re.compile(r"obiava-(\d[a-z0-9]{15,20})"),
        re.compile(r"^([\d\s]{3,10})\s?€$"),
    ),
    (
        "bazar.bg",
        "https://bazar.bg/obiavi/prodazhba-apartamenti/sofia",
        "history_bazar.json.gz",
        "bazar_",
        re.compile(r"obiava-(\d+)"),
        None,  # bazar.bg's price line is a bare "€" marker on the line AFTER the digits; handled specially
    ),
]


def cdx_all_captures(url):
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(
                CDX_URL,
                params={"url": url, "output": "json", "collapse": "digest", "limit": 500, "fl": "timestamp,statuscode"},
                headers={"User-Agent": ARCHIVE_UA},
                timeout=25,
            )
            resp.raise_for_status()
            rows = resp.json()
            if not rows or len(rows) < 2:
                return []
            header, *data_rows = rows
            return sorted(set(r[0] for r in data_rows if r[1] == "200"))
        except Exception as e:
            print(f"  CDX attempt {attempt}/{MAX_RETRIES} failed: {e}")
            if attempt < MAX_RETRIES:
                time.sleep(REQUEST_DELAY_SECONDS * attempt)
    return None


def fetch_snapshot(timestamp, original_url):
    # https, not http - "Connection refused" on port 80 for every bazar.bg
    # fetch in a prior run (even after a 30s cooldown) suggests plain HTTP
    # to archive.org gets blocked somewhere in this network path; archive.org
    # fully supports HTTPS.
    wayback_url = f"https://web.archive.org/web/{timestamp}id_/{original_url}"
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(wayback_url, headers=HEADERS, timeout=30)
            resp.raise_for_status()
            # probe_snapshot_structure.py found the real bug behind two
            # failed backfill attempts: archived imot.bg pages are
            # Windows-1251-encoded Cyrillic, but requests' encoding
            # detection (trusting a header on the archived response) was
            # decoding them as Latin-1 - garbling not just the Cyrillic
            # text but the multi-byte "€" symbol itself, so no price line
            # could ever match regardless of the regex used. The id regex
            # fix in the previous attempt was real and still needed (231
            # matched links confirmed it works), it just wasn't the whole
            # story. apparent_encoding sniffs the real encoding from the
            # actual byte content instead of trusting a wrong header.
            resp.encoding = resp.apparent_encoding
            return resp.text
        except Exception as e:
            print(f"    snapshot fetch attempt {attempt}/{MAX_RETRIES} failed: {e}")
            if attempt < MAX_RETRIES:
                # A first backfill run got "Connection refused" on every one
                # of bazar.bg's fetches, right after 18 straight successful
                # imot.bg fetches - looks like archive.org started
                # throttling this runner's IP after a sustained burst.
                # Exponential (not linear) backoff gives it real room to
                # cool down between retries.
                time.sleep(REQUEST_DELAY_SECONDS * (3 ** attempt))
    return None


def timestamp_to_iso(ts):
    dt = datetime.strptime(ts, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    return dt.isoformat()


def parse_imot_prices(html, id_re, price_re):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    all_links = soup.find_all("a", href=True)
    matching_links = [a for a in all_links if id_re.search(a["href"])]

    results = {}
    for a in matching_links:
        m = id_re.search(a["href"])
        listing_id = m.group(1)
        if listing_id in results:
            continue
        node = a
        found_price = None
        for _ in range(8):
            if node.parent is None:
                break
            node = node.parent
            text = node.get_text(" ", strip=True)
            if len(text) > 800:
                break
            lines = [l.strip() for l in node.get_text("\n", strip=True).split("\n") if l.strip()]
            prices = []
            for l in lines:
                pm = price_re.match(l)
                if pm:
                    prices.append(int(re.sub(r"\D", "", pm.group(1))))
            if len(prices) == 1:
                found_price = prices[0]
                break
            if len(prices) > 1:
                break

        if found_price is None:
            # Fallback for archived templates where the price isn't its own
            # exact text line (e.g. inline with other text): search the
            # smallest reasonably-sized ancestor for a single "NNN NNN €"
            # substring anywhere in its text, same technique as the live
            # scraper's smallest_container_with_price, just applied second
            # rather than first since it's more prone to false matches.
            node = a
            loose_price_re = re.compile(r"[\d\s]{3,10}\s?€")
            for _ in range(8):
                if node.parent is None:
                    break
                node = node.parent
                text = node.get_text(" ", strip=True)
                if len(text) > 800:
                    break
                matches = loose_price_re.findall(text)
                if len(matches) == 1:
                    found_price = int(re.sub(r"\D", "", matches[0]))
                    break
                if len(matches) > 1:
                    break

        if found_price and found_price >= 1000:
            results[listing_id] = found_price
    return results


def parse_bazar_prices(html, id_re):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    all_links = soup.find_all("a", href=True)
    matching_links = [a for a in all_links if id_re.search(a["href"])]

    results = {}
    for a in matching_links:
        m = id_re.search(a["href"])
        listing_id = m.group(1)
        if listing_id in results:
            continue
        node = a
        found_price = None
        for _ in range(9):
            if node.parent is None:
                break
            node = node.parent
            text = node.get_text(" ", strip=True)
            if len(text) > 1500:
                break
            lines = [l.strip() for l in node.get_text("\n", strip=True).split("\n") if l.strip()]
            for i, l in enumerate(lines):
                if l == "€" and i > 0:
                    prev = lines[i - 1]
                    if re.fullmatch(r"[\d\s]{3,10}", prev):
                        found_price = int(re.sub(r"\s", "", prev))
                    break
            if found_price:
                break
        if found_price and 1000 <= found_price <= 10_000_000:
            results[listing_id] = found_price
    return results


def backfill_portal(portal, url, history_filename, id_prefix, id_re, price_re):
    print(f"\n\n===== {portal} =====")
    history_path = DATA_DIR / history_filename
    if not history_path.exists():
        print(f"  {history_filename} not found, skipping")
        return 0
    history = load_json_any(history_path)

    print("  fetching capture list...")
    timestamps = cdx_all_captures(url)
    if not timestamps:
        print("  no captures available, skipping")
        return 0
    print(f"  {len(timestamps)} real captures found")

    injected = 0
    for ts in timestamps:
        time.sleep(REQUEST_DELAY_SECONDS)
        html = fetch_snapshot(ts, url)
        if html is None:
            print(f"  snapshot {ts}: FAILED to fetch, skipping")
            continue

        if portal == "imot.bg":
            prices = parse_imot_prices(html, id_re, price_re)
        else:
            prices = parse_bazar_prices(html, id_re)

        archived_iso = timestamp_to_iso(ts)
        matched = 0
        for listing_id, price_eur in prices.items():
            lid = id_prefix + listing_id
            rec = history.get(lid)
            if rec is None:
                continue
            first_seen = rec.get("first_seen")
            if not first_seen or archived_iso >= first_seen:
                continue
            already_injected = any(
                s.get("source") == "wayback" and s.get("seen_at") == archived_iso
                for s in rec["snapshots"]
            )
            if already_injected:
                continue
            rec["snapshots"].insert(0, {"seen_at": archived_iso, "price_eur": price_eur, "source": "wayback"})
            rec["snapshots"].sort(key=lambda s: s["seen_at"])
            if archived_iso < rec["first_seen"]:
                rec["first_seen"] = archived_iso
            matched += 1
            injected += 1
        print(f"  snapshot {ts}: {len(prices)} listings parsed, {matched} injected as real pre-tracking history")

    if injected:
        save_json_any(history_path, history)
        print(f"  wrote {injected} injected snapshots to {history_filename}")
    else:
        print("  nothing to inject, history file unchanged")
    return injected


def compute_leads(history):
    # Duplicated verbatim from scraper_imot.py / scraper_bazar.py (both are
    # byte-for-byte identical) rather than imported: a prior run crashed
    # here with "ModuleNotFoundError: No module named 'playwright'" because
    # scraper_imot.py imports playwright at module level for its own
    # scraping (unrelated to compute_leads), and this workflow's pip
    # install never included it - after the encoding fix had already
    # computed and written 34 real injected snapshots to the history
    # files, but before the git commit step, so that work was lost when
    # the runner was torn down. Avoiding the import entirely is simpler
    # and faster than adding playwright+chromium here just to reach one
    # pure-Python function.
    # Same threshold detect_relistings.py uses for "no longer actually
    # posted on this portal" - kept in sync with the other 9 copies of this
    # function across the codebase (see module docstring above for why this
    # one is duplicated rather than imported).
    GONE_AFTER = timedelta(hours=20)

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
        score = round(min(max(drop_pct, 0) / 20, 1) * 50 + min(days_on_market / 180, 1) * 50)

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

        latest = rec["latest"]
        price_per_sqm = round(last_price / latest["sqm"]) if latest.get("sqm") else None

        entry = dict(latest)
        entry["price_eur"] = last_price
        entry["price_per_sqm"] = price_per_sqm
        entry["price_history"] = price_history
        entry["price_drop_count"] = price_drop_count
        entry["drop_pct"] = drop_pct
        entry["days_on_market"] = days_on_market
        entry["score"] = score
        entry["source_status"] = source_status
        entry["removed_at"] = last_seen.isoformat() if source_status == "removed" else None
        leads.append(entry)

    area_totals = {}
    for l in leads:
        if l["price_per_sqm"]:
            area_totals.setdefault(l["area"], []).append(l["price_per_sqm"])
    area_avg = {area: sum(v) / len(v) for area, v in area_totals.items()}

    for l in leads:
        if l["price_per_sqm"] and l["area"] in area_avg:
            avg = area_avg[l["area"]]
            l["area_avg_price_per_sqm"] = round(avg)
            l["pct_vs_area_avg"] = round((l["price_per_sqm"] - avg) / avg * 100, 1)
        else:
            l["area_avg_price_per_sqm"] = None
            l["pct_vs_area_avg"] = None

    leads.sort(key=lambda x: x["score"], reverse=True)
    return leads


def main():
    total_injected = {}
    for i, (portal, url, history_filename, id_prefix, id_re, price_re) in enumerate(PORTALS):
        if i > 0:
            print(f"\ncooling down {PORTAL_COOLDOWN_SECONDS}s before starting {portal}...")
            time.sleep(PORTAL_COOLDOWN_SECONDS)
        total_injected[portal] = backfill_portal(portal, url, history_filename, id_prefix, id_re, price_re)

    print("\n\n===== SUMMARY =====")
    for portal, count in total_injected.items():
        print(f"{portal}: {count} snapshots injected")

    if sum(total_injected.values()) == 0:
        print("Nothing injected - not regenerating leads files")
        return

    if total_injected.get("imot.bg"):
        history = load_json_any(DATA_DIR / "history_imot.json.gz")
        leads = compute_leads(history)
        save_json_any(DATA_DIR / "leads_imot.json.gz", leads)
        print(f"regenerated leads_imot.json.gz ({len(leads)} leads)")

    if total_injected.get("bazar.bg"):
        history = load_json_any(DATA_DIR / "history_bazar.json.gz")
        leads = compute_leads(history)
        save_json_any(DATA_DIR / "leads_bazar.json.gz", leads)
        print(f"regenerated leads_bazar.json.gz ({len(leads)} leads)")


if __name__ == "__main__":
    main()
