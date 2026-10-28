"""
Precomputes the small aggregate files the Home page renders from, so the
browser no longer has to download every merged_listings row just to count them.

Writes (next to the scraped data, so they are committed with it):
  data/home_stats.json  - totals, per-portal / city / oblast / type counts and a
                          score histogram (index.html's renderHome() and the
                          Browse by city / council / type tiles)
  data/area_index.json  - one {key, label} per normalized area (index.html's
                          areaKeyGroups(), used for the area dropdown)

Reads only the committed data/leads_*.json(.gz) files via sync_to_supabase's own
load_all_listings() / build_rows(), so every number comes from the exact rows
that get upserted into merged_listings - no Supabase access or secrets needed,
which also makes `python build_home_stats.py` a safe local dry run.

Run order in the scrape workflows: after the scrapers, BEFORE the step that
commits data/ (this must not run inside sync_to_supabase.py - that step runs
after the data commit, so a file written there would never be committed).

Usage: python build_home_stats.py [--data-dir DIR] [--dry-run]
"""
import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import sync_to_supabase as sync

# Stats-file shape version, so index.html can refuse a shape it doesn't know.
SCHEMA_VERSION = 1


def _score_key(score):
    # Same coercion index.html's hot-deal check uses: a missing score never
    # reaches the threshold (a null >= 40 is false in JS).
    return None if score is None else str(int(score))


def compute_stats(merged_rows):
    """Aggregates a build_rows() merged_rows list into the home_stats.json payload."""
    portal_counts = Counter()
    city_counts = Counter()
    oblast_counts = Counter()
    type_counts = Counter()
    histogram = Counter()
    source_count = 0

    for m in merged_rows:
        source_count += m.get("member_count") or 1
        for p in m.get("member_portals") or []:
            portal_counts[p] += 1
        # NULL key -> "others", exactly the tile index.html's renderCityTabs()
        # / renderOblastTabs() put key-less rows into.
        city_counts[m.get("city_key") or "others"] += 1
        oblast_counts[m.get("oblast_key") or "others"] += 1
        # renderTypeFilters(): bcpea rows count toward "auction" AND toward
        # their own type bucket; type_bucket is sync's type_filter_bucket().
        if m.get("portal") == "sales.bcpea.org":
            type_counts["auction"] += 1
        type_counts[m.get("type_bucket") or "other"] += 1
        if m.get("status") != "sold":
            key = _score_key(m.get("score"))
            if key is not None:
                histogram[key] += 1

    return {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "uniqueCount": len(merged_rows),
        "sourceCount": source_count,
        "portalCounts": dict(portal_counts),
        "cityCounts": dict(city_counts),
        "oblastCounts": dict(oblast_counts),
        "typeCounts": dict(type_counts),
        "notSoldScoreHistogram": dict(histogram),
    }


def compute_area_index(merged_rows):
    """Port of index.html's areaKeyGroups(): one representative raw label per
    normalized area key - the most frequent raw label, ties broken by the
    smaller string. Left unsorted: the browser sorts with localeCompare('bg'),
    which Python can't reproduce exactly."""
    counts = {}
    for m in merged_rows:
        area = m.get("area")
        if not area:
            continue
        key = m.get("area_key") or sync.normalize_area(area)
        if not key:
            continue
        counts.setdefault(key, Counter())[area] += 1
    groups = []
    for key, labels in counts.items():
        label = min(labels, key=lambda lab: (-labels[lab], lab))
        groups.append({"key": key, "label": label})
    return {"schemaVersion": SCHEMA_VERSION, "areas": groups}


def write_json(path, payload):
    # Compact, and written to a temp file first so a crash never leaves a
    # half-written file that the next commit would publish.
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--data-dir", type=Path, default=sync.DATA_DIR)
    parser.add_argument("--dry-run", action="store_true", help="compute and print a summary, write nothing")
    args = parser.parse_args(argv)

    sync.DATA_DIR = args.data_dir
    all_listings = sync.load_all_listings()
    if not all_listings:
        print("No listings loaded - refusing to write empty stats", file=sys.stderr)
        return 1
    _, merged_rows = sync.build_rows(all_listings)

    stats = compute_stats(merged_rows)
    area_index = compute_area_index(merged_rows)
    print(f"{stats['uniqueCount']} unique listings from {stats['sourceCount']} sources; "
          f"{len(stats['cityCounts'])} city buckets, {len(stats['oblastCounts'])} oblast buckets, "
          f"{len(area_index['areas'])} areas")

    if args.dry_run:
        print(json.dumps(stats, ensure_ascii=False)[:600])
        return 0
    write_json(args.data_dir / "home_stats.json", stats)
    write_json(args.data_dir / "area_index.json", area_index)
    print(f"Wrote home_stats.json and area_index.json to {args.data_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
