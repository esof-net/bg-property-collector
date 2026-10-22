"""
Ongoing relisting detector: some sellers "reduce" a property's price not by
editing the existing ad but by deleting it and posting a brand new listing
(new portal ID, often at a lower price) - invisible to per-ID price
tracking since, from the scraper's point of view, it's just one listing
disappearing and an unrelated one appearing. This chains those pairs back
together using a signal that's already in our own data and needs no
network access: several portals host each listing's photos at a URL that
embeds a stable per-image identifier, which stays the same when a seller
reuses their existing photos on a new post even though the listing ID
changes.

Validated directly against real data before building this: bazar.bg's
photo URLs embed a stable Focus-backend image ID
(imotstatic*.focus.bg/.../<id>_<size>.jpg) - a real pair was found this
way, bazar_55673363 (delisted) -> bazar_55775706 (a new listing that
appeared the next day using the literal same photo file). imot.bg shares
the same Focus backend. homes.bg and imoti.bg host photos under a unique
per-image filename too. imoti.net's photo path embeds the CURRENT
listing's own ID (self-referential - a relisting would get a new path
regardless of whether it's the same photo file, so URL matching can't
work there) and alo.bg's stored "photo" field turned out to often be the
listing agent's avatar image, not a property photo, shared across many
unrelated listings from the same agent - both excluded here since URL
matching against either would produce false positives. Real photo-hash
comparison could extend coverage to those two later, but that needs to
download and compare actual image bytes, which needs network access this
runs without.

For each portal it supports: finds listings that stopped appearing in
scrapes (were removed) and a different, currently-active listing on the
same portal that started appearing afterward using the identical photo
file. Injects the removed listing's final known price as a real
historical snapshot on the new listing, tagged "source": "relisted_from"
with the old listing's ID for provenance - never fabricated, always the
old listing's own last real observed price at its own last real observed
time. Safe to run after every scrape: already-recorded pairs are skipped
on subsequent runs.
"""

import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from geo_utils import (
    compute_motivation_score, listing_city_key, relisting_chain_guard_tripped,
    load_json_any, save_json_any,
)

DATA_DIR = Path(__file__).parent / "data"

# Listings not seen in a scrape for at least this long are treated as
# removed - long enough to not misfire on a listing simply skipped by one
# scrape cycle due to pagination timing/site load, short enough to catch
# real removals promptly.
GONE_AFTER = timedelta(hours=20)

FOCUS_RE = re.compile(r"(\d[a-z0-9]{15,20})_[A-Za-z0-9]{2}\.[a-z]+", re.IGNORECASE)
HOMES_RE = re.compile(r"/(\d+[a-z]?)\.[a-z]+(?:$|[?;])", re.IGNORECASE)
GENERIC_HASH_RE = re.compile(r"/([a-z0-9_-]{10,})/image(?:;|$)", re.IGNORECASE)
IMOTI_BG_RE = re.compile(r"/(r_[0-9a-f]+_[0-9a-f]+)\.[a-z]+$", re.IGNORECASE)

# portal -> (history filename, photo-key extractor). Portals whose photo
# scheme can't support URL-based matching (see module docstring) are
# simply absent from this dict, not included with a no-op extractor.
# homes.bg's own filename is .json.gz, not plain .json - 2026-09-25
# addendum to the GH001 incident fix (see geo_utils.py's "Compressed
# on-disk JSON storage" comment). detect_portal()/main() below read and
# write it through load_json_any()/save_json_any(), which are
# extension-aware, so this is the only line that needed to change here.
PORTALS = {
    "bazar.bg": ("history_bazar.json.gz", lambda url: _match(FOCUS_RE, url)),
    "imot.bg": ("history_imot.json.gz", lambda url: _match(FOCUS_RE, url)),
    "homes.bg": ("history_homes.json.gz", lambda url: _match(HOMES_RE, url)),
    "olx.bg": ("history_olx.json.gz", lambda url: _match(GENERIC_HASH_RE, url)),
    "imoti.bg": ("history_imoti_bg.json", lambda url: _match(IMOTI_BG_RE, url)),
}


def _match(pattern, url):
    if not url:
        return None
    m = pattern.search(url)
    return m.group(1) if m else None


def detect_portal(portal, history_filename, photo_key_fn):
    path = DATA_DIR / history_filename
    if not path.exists():
        return 0, False
    history = load_json_any(path)

    last_seens = {lid: rec["snapshots"][-1]["seen_at"] for lid, rec in history.items() if rec.get("snapshots")}
    if not last_seens:
        return 0, False
    latest_overall = max(last_seens.values())
    cutoff = datetime.fromisoformat(latest_overall) - GONE_AFTER

    gone_ids = [lid for lid, ls in last_seens.items() if datetime.fromisoformat(ls) < cutoff]
    active_ids = [lid for lid in history if lid not in gone_ids]

    active_by_photo = {}
    for lid in active_ids:
        pk = photo_key_fn(history[lid]["latest"].get("photo"))
        if pk:
            active_by_photo.setdefault(pk, []).append(lid)

    # First pass: find every candidate (gid, aid) pair WITHOUT mutating
    # history yet, so the mass-relisting-storm guard below can check the
    # real matched count before anything is injected - see
    # relisting_chain_guard_tripped's own docstring/geo_utils.py's
    # RELISTING_GUARD_ABS comment for why this exists and how its
    # thresholds were calibrated. Equivalent to the old single-pass
    # behavior when the guard doesn't trip, for the case that actually
    # matters here: each match's own "already tagged" check only looks at
    # snapshots persisted by a PRIOR run, and a match's target aid is
    # never itself a gid also being matched this pass (an active listing
    # can't also be one of this portal's gone_ids). One narrow edge case
    # Missy's review flagged, not fixed here since it's pre-existing and
    # orthogonal to the storm guard: if two different gids share the same
    # photo key and could both match the same aid in one run, the old
    # single-pass code mutated active_rec["first_seen"] on the first
    # match, which could change whether the second gid's own match
    # condition evaluates true - this two-pass version always evaluates
    # every candidate against the pre-mutation first_seen instead, so it
    # could (rarely) chain a pair the old code would have skipped or vice
    # versa. No data-loss risk either way; flagged for a future targeted
    # look, not blocking this fix.
    matches = []
    for gid in gone_ids:
        pk = photo_key_fn(history[gid]["latest"].get("photo"))
        if not pk:
            continue
        gone_rec = history[gid]
        gone_last_snap = gone_rec["snapshots"][-1]
        for aid in active_by_photo.get(pk, []):
            if aid == gid:
                continue
            active_rec = history[aid]
            if gone_last_snap["seen_at"] >= active_rec["first_seen"]:
                continue  # the "new" listing has to genuinely start after the old one stopped
            already = any(
                s.get("source") == "relisted_from" and s.get("relisted_from") == gid
                for s in active_rec["snapshots"]
            )
            if already:
                continue
            matches.append((gid, aid))

    if relisting_chain_guard_tripped(portal, len(matches), len(history)):
        return 0, True

    injected = 0
    for gid, aid in matches:
        gone_rec = history[gid]
        gone_last_snap = gone_rec["snapshots"][-1]
        active_rec = history[aid]
        # Captured now, before inserting - this is the new listing's own
        # earliest REAL (non-injected) snapshot, i.e. the actual moment
        # it first appeared and the price it actually appeared at. Not
        # necessarily active_rec["snapshots"][0]: a listing chained
        # through more than one prior relisting already has an earlier
        # injected marker sitting before its own real first entry.
        # Recorded on the tag itself (came_back_at/came_back_price) so
        # index.html can show the exact off-market gap and price instead
        # of approximating from whenever price_history's next real price
        # CHANGE happens to land, which can be well after the actual
        # relist date if the price held steady for a while.
        first_real_snap = next(
            (s for s in active_rec["snapshots"] if s.get("source") != "relisted_from"), None
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
        active_rec["snapshots"].insert(0, injected_snap)
        active_rec["snapshots"].sort(key=lambda s: s["seen_at"])
        if gone_last_snap["seen_at"] < active_rec["first_seen"]:
            active_rec["first_seen"] = gone_last_snap["seen_at"]
        injected += 1

    if injected:
        save_json_any(path, history)
    return injected, False


def compute_leads(history):
    # Duplicated from scraper_imot.py / scraper_bazar.py (identical in both)
    # rather than imported - see backfill_wayback_prices.py for why:
    # importing scraper_imot.py pulls in an unrelated top-level playwright
    # import that isn't installed in every context this runs from.
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
            # This function's own relisted_from injection (detect_portal(),
            # above) has to survive into the price_history it computes here,
            # in the very same run - otherwise the tag detect_portal() just
            # wrote is stripped again the moment this regenerates leads_*.json,
            # before it was ever committed. See scraper.py's identical
            # comment (every portal's compute_leads() needs this same fix)
            # for why the tag matters and why it's kept even at an unchanged
            # price.
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

    # Keyed by (city_key, area), not area alone - this diagnostic script's
    # own compute_leads() had never received the city-blind-area-average
    # fix the real scrapers got (task #49: "Център" and other generic area
    # names collide across different real cities otherwise) until this was
    # noticed while wiring in the motivation-score rework below, which
    # depends on a correct pct_vs_area_avg. See sync_to_supabase.py's
    # group_listings() for the full story.
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
        l["score"] = compute_motivation_score(
            l["drop_pct"], l["price_drop_count"], l["days_on_market"], l["pct_vs_area_avg"], l["price_history"]
        )

    leads.sort(key=lambda x: x["score"], reverse=True)
    return leads


def main():
    total = 0
    any_guard_tripped = False
    for portal, (history_filename, photo_key_fn) in PORTALS.items():
        injected, tripped = detect_portal(portal, history_filename, photo_key_fn)
        any_guard_tripped = any_guard_tripped or tripped
        if tripped:
            print(f"{portal}: chain-storm guard tripped - 0 relisting(s) chained this run (see ::error:: above)")
        else:
            print(f"{portal}: {injected} relisting(s) detected and chained")
        total += injected
        if injected:
            leads_filename = history_filename.replace("history_", "leads_", 1)
            history = load_json_any(DATA_DIR / history_filename)
            leads = compute_leads(history)
            save_json_any(DATA_DIR / leads_filename, leads)
            print(f"  regenerated {leads_filename} ({len(leads)} leads)")
    print(f"\nTotal relistings chained this run: {total}")
    if any_guard_tripped:
        # Non-zero exit so this step's own outcome (scrape.yml keeps
        # continue-on-error: true here, matching item 30's precedent) can
        # be surfaced as a real, visible workflow failure by the check
        # near the end of the job, instead of the guard trip staying
        # buried in this step's own log output.
        sys.exit(1)


if __name__ == "__main__":
    main()
