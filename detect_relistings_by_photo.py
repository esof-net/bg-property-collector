"""
Extends detect_relistings.py's relisting-chain mechanism (see that file's
docstring for the full rationale) to imoti.net and alo.bg - the two
portals excluded there because their stored photo URLs can't be matched
by string comparison alone: imoti.net's photo path embeds the listing's
own ID (so a relist gets a different URL regardless of whether it's the
same photo file) and alo.bg's captured "photo" field is often the agent's
avatar rather than the property photo. Together these two portals cover
~15,800 of our ~17,000 tracked listings - the two most consequential
portals we haven't been able to check for relistings at all.

This does real photo comparison instead: downloads the actual image
bytes and computes a simple perceptual hash (an 8x8 grayscale average
hash - resize, greyscale, compare each pixel to the mean, encode as a 64
bit fingerprint), then compares "gone" listings' photos against active
candidates via Hamming distance. Two genuinely-the-same source image
produce near-identical hashes even after a CDN re-compresses or resizes
them; two different photos essentially never do (random 64-bit hashes
differ in ~32 bits on average).

Narrows candidates locally first (free, no network) before downloading
anything: same portal, same sqm, and for imoti.net matching lat/lng when
both listings have it (alo.bg rarely captures coordinates, so area name
is used there instead). Only the resulting narrowed candidate set's
photos get downloaded and hashed - not the full active-listing pool,
which would mean tens of thousands of image downloads for what is
currently a small number of delisted candidates.
"""

import io
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests
from PIL import Image

from geo_utils import load_json_any, save_json_any

DATA_DIR = Path(__file__).parent / "data"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; PersonalDealTracker/1.0)"}
REQUEST_DELAY_SECONDS = 0.5
GONE_AFTER = timedelta(hours=20)
HASH_MATCH_THRESHOLD = 6  # out of 64 bits; same source image should differ by ~0
LATLNG_TOLERANCE = 0.003  # roughly 300m


def average_hash(image_bytes):
    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("L").resize((8, 8))
    except Exception:
        return None
    pixels = list(img.getdata())
    avg = sum(pixels) / len(pixels)
    bits = "".join("1" if p > avg else "0" for p in pixels)
    return int(bits, 2)


def hamming(a, b):
    return bin(a ^ b).count("1")


def fetch_hash(url, cache):
    if url in cache:
        return cache[url]
    time.sleep(REQUEST_DELAY_SECONDS)
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        h = average_hash(resp.content)
    except Exception as e:
        print(f"    photo fetch failed for {url}: {e}")
        h = None
    cache[url] = h
    return h


def find_candidates_imoti_net(history, gone_id, active_ids):
    gone = history[gone_id]["latest"]
    candidates = []
    for aid in active_ids:
        a = history[aid]["latest"]
        if a.get("sqm") != gone.get("sqm"):
            continue
        if gone.get("lat") and a.get("lat"):
            if abs(a["lat"] - gone["lat"]) > LATLNG_TOLERANCE or abs(a["lng"] - gone["lng"]) > LATLNG_TOLERANCE:
                continue
        elif gone.get("category") != a.get("category"):
            continue
        candidates.append(aid)
    return candidates


def find_candidates_alo(history, gone_id, active_ids):
    gone = history[gone_id]["latest"]
    candidates = []
    for aid in active_ids:
        a = history[aid]["latest"]
        if a.get("sqm") != gone.get("sqm") or a.get("area") != gone.get("area"):
            continue
        candidates.append(aid)
    return candidates


PORTALS = {
    "imoti.net": ("history.json", find_candidates_imoti_net),
    # .json.gz, not plain .json - 2026-09-26 GH001 incident (alo.bg's own
    # leads/history files hit GitHub's 100MB hard limit). See
    # geo_utils.py's "Compressed on-disk JSON storage" comment.
    # detect_portal()/main() below read/write through load_json_any()/
    # save_json_any(), which are extension-aware, so imoti.net's own
    # plain history.json is unaffected.
    "alo.bg": ("history_alo.json.gz", find_candidates_alo),
}


def detect_portal(portal, history_filename, find_candidates_fn):
    path = DATA_DIR / history_filename
    if not path.exists():
        return 0
    history = load_json_any(path)

    last_seens = {lid: rec["snapshots"][-1]["seen_at"] for lid, rec in history.items() if rec.get("snapshots")}
    if not last_seens:
        return 0
    latest_overall = max(last_seens.values())
    cutoff = datetime.fromisoformat(latest_overall) - GONE_AFTER

    gone_ids = [lid for lid, ls in last_seens.items() if datetime.fromisoformat(ls) < cutoff]
    active_ids = [lid for lid in history if lid not in gone_ids]

    print(f"\n=== {portal}: {len(gone_ids)} gone, {len(active_ids)} active ===")

    hash_cache = {}
    injected = 0
    total_candidates_checked = 0
    for gid in gone_ids:
        gone_rec = history[gid]
        candidates = find_candidates_fn(history, gid, active_ids)
        candidates = [
            aid for aid in candidates
            if gone_rec["snapshots"][-1]["seen_at"] < history[aid]["first_seen"]
        ]
        if not candidates:
            continue
        total_candidates_checked += len(candidates)

        gone_photo = gone_rec["latest"].get("photo")
        if not gone_photo:
            continue
        gone_hash = fetch_hash(gone_photo, hash_cache)
        if gone_hash is None:
            continue

        for aid in candidates:
            already = any(
                s.get("source") == "relisted_from" and s.get("relisted_from") == gid
                for s in history[aid]["snapshots"]
            )
            if already:
                continue
            active_photo = history[aid]["latest"].get("photo")
            if not active_photo:
                continue
            active_hash = fetch_hash(active_photo, hash_cache)
            if active_hash is None:
                continue
            dist = hamming(gone_hash, active_hash)
            if dist <= HASH_MATCH_THRESHOLD:
                gone_last_snap = gone_rec["snapshots"][-1]
                # See detect_relistings.py's identical comment - this is the
                # new listing's own earliest REAL (non-injected) snapshot,
                # captured before inserting, so index.html can show the
                # exact off-market gap/price instead of approximating from
                # price_history's next recorded price CHANGE (which can
                # postdate the actual relist by however long the price then
                # held steady).
                first_real_snap = next(
                    (s for s in history[aid]["snapshots"] if s.get("source") != "relisted_from"), None
                )
                injected_snap = {
                    "seen_at": gone_last_snap["seen_at"],
                    "price_eur": gone_last_snap["price_eur"],
                    "source": "relisted_from",
                    "relisted_from": gid,
                }
                if first_real_snap:
                    injected_snap["came_back_at"] = first_real_snap["seen_at"]
                    injected_snap["came_back_price"] = first_real_snap["price_eur"]
                history[aid]["snapshots"].insert(0, injected_snap)
                history[aid]["snapshots"].sort(key=lambda s: s["seen_at"])
                if gone_last_snap["seen_at"] < history[aid]["first_seen"]:
                    history[aid]["first_seen"] = gone_last_snap["seen_at"]
                injected += 1
                print(f"  MATCH (hash distance {dist}): {gid} -> {aid}")

    print(f"  {total_candidates_checked} candidate pairs locally narrowed and checked, {injected} confirmed relistings")

    if injected:
        save_json_any(path, history)
    return injected


def main():
    # imoti.net's and alo.bg's own compute_leads() differ from the other
    # portals' (imoti.net derives "area" from the title and prefers
    # site_posted_at over first_seen for days_on_market) - imported here
    # rather than duplicated, unlike backfill_wayback_prices.py /
    # detect_relistings.py, since neither scraper.py nor scraper_alo.py
    # import anything (like playwright) that isn't already installed
    # wherever this runs.
    import sys
    sys.path.insert(0, str(Path(__file__).parent))

    total = 0
    injected_imoti = detect_portal("imoti.net", "history.json", find_candidates_imoti_net)
    total += injected_imoti
    if injected_imoti:
        import scraper
        history = load_json_any(DATA_DIR / "history.json")
        leads = scraper.compute_leads(history)
        save_json_any(DATA_DIR / "leads.json", leads)
        print(f"regenerated leads.json ({len(leads)} leads)")

    injected_alo = detect_portal("alo.bg", "history_alo.json.gz", find_candidates_alo)
    total += injected_alo
    if injected_alo:
        import scraper_alo
        history = load_json_any(DATA_DIR / "history_alo.json.gz")
        leads = scraper_alo.compute_leads(history)
        save_json_any(DATA_DIR / "leads_alo.json.gz", leads)
        print(f"regenerated leads_alo.json.gz ({len(leads)} leads)")

    print(f"\nTotal relistings chained this run: {total}")


if __name__ == "__main__":
    main()
