"""
One-time (and safely re-runnable) migration: evicts history_*.json/
leads_*.json records for listings that have been gone (not seen in any
scrape) for longer than geo_utils.STALE_RECORD_RETENTION (180 days by
default) - see that constant's own comment in geo_utils.py for the full
real-data reasoning (record count, not per-record payload or snapshot
bloat, is the dominant driver of these files' unbounded size; 180 days is
picked from real measured relisting-gap data, not a guessed round number).

This is the bridge/remediation half of the 2026-09-25 scrape.yml GH001
fix: evict_stale_records() is now also wired into every scraper's own
save_history() (geo_utils.py), so growth is bounded automatically on every
future run - this script exists to (a) let the same eviction be run
on-demand against whatever is currently committed, without waiting for the
next scheduled scrape, and (b) as the actual mechanism to shrink data
already sitting in a portal's history_*.json/leads_*.json today, if a
portal's committed files are already over (or close to) GitHub's 100MB
push limit.

Deliberately does NOT recompute leads_*.json's area_avg_price_per_sqm/
pct_vs_area_avg/score fields for the records it keeps (that would mean
importing each scraper module's own compute_leads(), several of which
pull in heavy/optional top-level deps like playwright that aren't
installed in every context this needs to run from - see
backfill_wayback_prices.py's own comment for the same reasoning applied
elsewhere in this repo). Evicted records are, by construction, listings
that have been gone for 180+ days - they are never "active" and were
already excluded from every area average and from the frontend besides.
Kept records' own price/photos/status fields are untouched; only their
area-average-derived fields go stale until the very next real scrape run,
which recomputes compute_leads() from scratch over the (now-evicted)
history and overwrites leads_*.json correctly regardless. That's an
acceptable, clearly-scoped simplification for a bridge migration - it is
not a substitute for the ongoing per-scraper mechanism, which does not
have this limitation (see save_history() in each scraper_*.py: it calls
evict_stale_records() on the exact `history` object that same run's own
compute_leads() call runs over right after).

Usage:
    python evict_stale_history.py                 # all 8 portals
    python evict_stale_history.py homes bazar      # just these portals
    python evict_stale_history.py --dry-run        # report only, no writes
"""
import sys
from pathlib import Path

from geo_utils import evict_stale_records, STALE_RECORD_RETENTION, load_json_any, save_json_any

DATA_DIR = Path(__file__).parent / "data"

# portal short name -> (history filename, leads filename). homes.bg's own
# two files are .json.gz, not plain .json - 2026-09-25 addendum to this
# same incident fix (see geo_utils.py's "Compressed on-disk JSON storage"
# comment): eviction alone doesn't unblock the very next scrape.yml run,
# since dd83178's tracking-ID collision fix means that run's own record
# count jumps ~1.90x independent of anything stale. migrate_portal() below
# reads/writes through load_json_any()/save_json_any(), which are
# extension-aware, so every other portal here is unaffected.
PORTAL_FILES = {
    "homes": ("history_homes.json.gz", "leads_homes.json.gz"),
    "imot": ("history_imot.json.gz", "leads_imot.json.gz"),
    "olx": ("history_olx.json.gz", "leads_olx.json.gz"),
    "bazar": ("history_bazar.json.gz", "leads_bazar.json.gz"),
    "imoti_bg": ("history_imoti_bg.json", "leads_imoti_bg.json"),
    "bcpea": ("history_bcpea.json", "leads_bcpea.json"),
    "alo": ("history_alo.json.gz", "leads_alo.json.gz"),
    "imoti_net": ("history.json", "leads.json"),
}


def migrate_portal(portal, dry_run=False):
    history_fn, leads_fn = PORTAL_FILES[portal]
    history_path = DATA_DIR / history_fn
    leads_path = DATA_DIR / leads_fn

    if not history_path.exists():
        print(f"{portal}: {history_fn} not found, skipping")
        return

    history_size_before = history_path.stat().st_size
    history = load_json_any(history_path)
    count_before = len(history)

    evicted_ids = set(history.keys())
    evicted = evict_stale_records(history)
    evicted_ids -= set(history.keys())  # now just the evicted ones

    leads_size_before = leads_path.stat().st_size if leads_path.exists() else 0
    leads_count_before = 0
    leads_count_after = 0

    print(
        f"{portal}: {count_before} history records, {evicted} evicted "
        f"(not seen in over {STALE_RECORD_RETENTION.days} days), "
        f"{count_before - evicted} kept"
    )

    if dry_run:
        print(f"  dry-run: not writing ({history_fn}: {history_size_before/1e6:.2f}MB before)")
        return

    if evicted:
        save_json_any(history_path, history)
    history_size_after = history_path.stat().st_size
    print(
        f"  {history_fn}: {history_size_before/1e6:.2f}MB -> {history_size_after/1e6:.2f}MB "
        f"({(history_size_before - history_size_after)/1e6:.2f}MB freed)"
    )

    if leads_path.exists():
        leads = load_json_any(leads_path)
        leads_count_before = len(leads)
        if evicted_ids:
            leads = [l for l in leads if l.get("id") not in evicted_ids]
            save_json_any(leads_path, leads)
        leads_count_after = len(leads)
        leads_size_after = leads_path.stat().st_size
        print(
            f"  {leads_fn}: {leads_size_before/1e6:.2f}MB -> {leads_size_after/1e6:.2f}MB "
            f"({(leads_size_before - leads_size_after)/1e6:.2f}MB freed), "
            f"{leads_count_before} -> {leads_count_after} leads"
        )


def main():
    args = sys.argv[1:]
    dry_run = "--dry-run" in args
    portals = [a for a in args if not a.startswith("--")]
    if not portals:
        portals = list(PORTAL_FILES.keys())
    unknown = [p for p in portals if p not in PORTAL_FILES]
    if unknown:
        print(f"Unknown portal(s): {unknown}. Known: {list(PORTAL_FILES.keys())}", file=sys.stderr)
        sys.exit(1)
    for portal in portals:
        migrate_portal(portal, dry_run=dry_run)


if __name__ == "__main__":
    main()
