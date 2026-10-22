"""
One-time diagnostic: how many existing merged_listings groups in Supabase
actually span more than one city?

group_listings() (sync_to_supabase.py) currently matches purely on
(price bucket, normalized area) with no city check at all - a live report
found a bazar.bg listing genuinely in Veliko Tarnovo merged with a
homes.bg/alo.bg pair genuinely in Dobrich, all sharing the generic area
name "Център" ("center" - identical text in every Bulgarian town) at a
coincidentally matching price. This script quantifies the damage before
the fix ships: listing_sources.city_key is already a stored column per
source row (computed once at sync time), so this is a pure read/aggregate
over existing data, no recomputation needed.

Groups things by merged_id, counts how many groups have more than one
distinct non-null city_key among their member sources. A group with only
one city_key represented (even if some members have city_key=None,
unclassified) is NOT counted as cross-city - only a group with two or
more DIFFERENT known cities is real, confirmed corruption.

Read-only, workflow_dispatch only.

Paginates by keyset (the table's own primary key, portal+source_id) rather
than OFFSET/LIMIT. Two live runs against this table both failed partway
through with a 500 from Supabase - the first at offset=166000, a later
one (after the table had grown further) at offset=221000 - after ~10+
minutes of otherwise-silent successful paging. That's the classic
deep-OFFSET failure mode: an OFFSET query still has to scan and discard
every row before the offset, so its cost grows with how deep the page is,
until it eventually exceeds the server's statement timeout - a genuine
retry of the same offset would very likely fail again the same way, not
just a one-off blip. Keyset pagination (order by the primary key, filter
for "greater than the last row seen") does the same amount of work no
matter how deep the page is, so it doesn't have this failure mode at all.

That first keyset attempt paginated across the WHOLE table in one pass,
using PostgREST's or=(portal.gt.X,and(portal.eq.X,source_id.gt.Y)) to
emulate a single composite-key cursor spanning all 8 portals - the same
row (('alo.bg', 'alo_11255428')) then failed identically across multiple
separate days with a genuine Postgres 57014 ("canceling statement due to
statement timeout"), not a data problem. An OR-of-two-conditions filter
isn't guaranteed to use the (portal, source_id) primary key index as
tightly as a real tuple comparison would, and alo.bg is this table's
largest portal by far - a bad plan landing mid-scan through it is exactly
where a timeout would show up. Paginating one portal at a time instead
(a fixed, known set of 8) turns every single page into `portal=eq.<X>
AND source_id>Y`, a plain single-column range scan bounded by an
equality filter - about as index-friendly as a query gets, and there's
no OR left for the planner to potentially get wrong.
"""

import json
import os
import sys
import time
from collections import defaultdict

import requests

MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 5

# Same 8 portals sync_to_supabase.py's PORTAL_FILES writes - listing_sources
# has no other values in this column, so this is an exhaustive partition,
# not a filter that could quietly skip real rows.
PORTALS = [
    "imoti.net", "alo.bg", "homes.bg", "imot.bg",
    "olx.bg", "bazar.bg", "imoti.bg", "sales.bcpea.org",
]


def fetch_page(base_url, headers, page_size, portal, cursor):
    """cursor is None for this portal's first page, else the last
    source_id seen for THIS portal - single-column range, no cross-portal
    OR needed since each portal is paginated to completion on its own."""
    params = {
        "select": "portal,source_id,merged_id,city_key",
        "portal": f"eq.{portal}",
        "order": "source_id.asc",
        "limit": page_size,
    }
    if cursor is not None:
        params["source_id"] = f"gt.{cursor}"
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(
                f"{base_url}/rest/v1/listing_sources",
                headers=headers,
                params=params,
                timeout=60,
            )
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            # str(e) on an HTTPError is just the status line ("500 Server
            # Error: ..."); the response body is where Postgres actually
            # puts its real error (message/details/hint/code) - this is
            # what surfaced the 57014 statement-timeout above.
            body = getattr(e, "response", None)
            body_text = body.text[:2000] if body is not None else "(no response body available)"
            print(
                f"DEBUG: page fetch failed for {portal!r} at cursor {cursor} (attempt {attempt}/{MAX_RETRIES}): {e}\n"
                f"  response body: {body_text}"
            )
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)
    print(f"ERROR: giving up on {portal!r} at cursor {cursor} after {MAX_RETRIES} attempts", file=sys.stderr)
    sys.exit(1)


def main():
    supabase_url = os.environ.get("SUPABASE_URL")
    secret_key = os.environ.get("SUPABASE_SECRET_KEY")
    if not supabase_url or not secret_key:
        print("SUPABASE_URL and SUPABASE_SECRET_KEY must be set", file=sys.stderr)
        sys.exit(1)

    headers = {
        "apikey": secret_key,
        "Authorization": f"Bearer {secret_key}",
    }
    base_url = supabase_url.rstrip("/")

    by_merged_id = defaultdict(list)
    page_size = 1000
    total_rows = 0
    for portal in PORTALS:
        portal_rows = 0
        cursor = None
        while True:
            rows = fetch_page(base_url, headers, page_size, portal, cursor)
            if not rows:
                break
            for r in rows:
                by_merged_id[r["merged_id"]].append(r)
            total_rows += len(rows)
            portal_rows += len(rows)
            cursor = rows[-1]["source_id"]
            if total_rows % 20000 == 0:
                print(f"DEBUG: loaded {total_rows} rows so far...")
            if len(rows) < page_size:
                break
        print(f"DEBUG: {portal}: {portal_rows} rows")

    print(f"Loaded {total_rows} listing_sources rows across {len(by_merged_id)} merged groups")

    cross_city_groups = []
    for merged_id, members in by_merged_id.items():
        if len(members) < 2:
            continue
        cities = {m["city_key"] for m in members if m.get("city_key")}
        if len(cities) > 1:
            cross_city_groups.append((merged_id, members, cities))

    print(f"\n{len(cross_city_groups)} merged groups span more than one city "
          f"(out of {sum(1 for m in by_merged_id.values() if len(m) >= 2)} groups with >=2 members)")

    print("\nSample of up to 20 cross-city groups:")
    for merged_id, members, cities in cross_city_groups[:20]:
        print(f"  {merged_id}  cities={sorted(cities)}")
        for m in members:
            print(f"    {m['portal']:16s} {m['source_id']:20s} city_key={m.get('city_key')}")

    with open("cross_city_merge_ids.json", "w", encoding="utf-8") as f:
        json.dump([mid for mid, _, _ in cross_city_groups], f)
    print(f"\nWrote {len(cross_city_groups)} affected merged_ids to cross_city_merge_ids.json")

    # Runs daily now (see the workflow) as an ongoing watchdog, not a
    # one-time diagnostic - the f63d126 fix stops new cross-city merges from
    # being CREATED, but says nothing about whether a stale merged_id from
    # before the fix could still be sitting in the live table (upserts don't
    # retroactively split an already-wrong group unless something changes
    # its membership). A non-zero count here is a real dedup regression, not
    # noise - exiting non-zero surfaces it as a failed run/notification the
    # same way every other scheduled workflow in this project already does,
    # instead of silently succeeding either way.
    if cross_city_groups:
        print(f"\nERROR: {len(cross_city_groups)} cross-city merged group(s) found in the live table - "
              f"dedup regression, see the sample above.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
