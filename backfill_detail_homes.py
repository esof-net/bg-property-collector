"""
Fills in description for homes.bg listings - docs/backlog.md item 9d.

homes.bg is this site's single largest portal by active listings and had a
flat, confirmed 0/67,935 (0.0%) real-description rate: PR #217 (2026-09-23)
correctly stopped scraper_homes.py's grid crawl from writing homes.bg's own
construction-material/furnishing tag line as if it were a real free-text
description (see scraper_homes.py's parse_offer() and its own module
docstring), but nothing was ever built to backfill a real one in its place -
every other large portal (alo.bg, imot.bg, olx.bg, bazar.bg, bcpea.org,
imoti.bg) already has its own backfill_detail_*.py doing exactly that.

This script is that missing backfill: a new, separate, resumable pass over
scraper_homes.py's own history, visiting each listing's own detail page
(offer["url"]) via scraper_homes.py's own fetch_listing_details() (a plain
requests.Session() fetch - homes.bg's grid crawl already succeeds this way,
with no anti-bot blocking observed, unlike imot.bg/olx.bg which need
Playwright), prioritized newest-first (by first_seen) so freshly discovered
listings get a real description before older ones queue behind them - same
pattern every other portal's backfill_detail_*.py already established
(backfill_detail_imot.py is the closest structural match: like homes.bg, its
grid crawl has never visited individual detail pages at all).

A listing is marked "detail_checked" once its detail page has actually been
visited, regardless of whether that page turned up a real description -
without an explicit marker those would get needlessly re-visited by every
future run instead of being treated as done. See geo_utils.
extract_description_homes()'s own module comment for the full 3-tier
selector search order this uses (heading text -> meta tags -> ld+json) and
why - this sandbox's egress to homes.bg is fully blocked (confirmed via curl
and WebFetch against several hosts), so none of it could be verified against
a real live page; every tier was chosen because it's a technique already
proven on at least one other portal in this codebase, and the whole chain
degrades to None (never a guess) on a markup mismatch.

Critical corequisite this backfill depends on (also shipped in the same
change, see scraper_homes.py's own _DETAIL_ONLY_FIELDS/update_history()
comment): scraper_homes.py's update_history() used to do an unconditional
`history[lid]["latest"] = l` full replace, with no merge-preservation for
detail-only fields at all. Without that fix, every description this script
writes would be silently wiped the next time scrape.yml's grid re-touches
the same still-active listing (every ~6 hours) - the exact bug items 9a/9b/
9c already fixed for the other seven scrapers.

Scheduled hourly (see backfill-detail-homes.yml) - a fresh full backlog
(~67,000 listings, per docs/backlog.md item 9d's own count) clears in
roughly a day and a half at this run's cap (MAX_LOOKUPS_PER_RUN * 24 runs/
day comfortably exceeds it), after which each run only has to keep up with
that day's newly-discovered listings, a much smaller number.
"""

import time

import scraper_homes as sh
from geo_utils import save_json_any

# Plain HTTP requests are far cheaper than imot.bg/olx.bg's Playwright-based
# detail fetch, but fetch_listing_details() rate-limits itself to 1 request/
# second (see scraper_homes.py's DETAIL_REQUEST_DELAY_SECONDS) to be polite
# to a site that has shown no anti-bot blocking so far - this cap is sized
# generously against the resulting real per-run throughput, not the other
# way around. Kept generous since the internal time budget below (not this
# count) is what actually decides when a run stops.
MAX_LOOKUPS_PER_RUN = 1800

# Stop visiting new listings once a run has spent this much of the
# workflow's 45-minute timeout - real headroom for whatever request is in
# flight, the final checkpoint, computing leads, and the commit/push step.
TIME_BUDGET_SECONDS = 35 * 60

CHECKPOINT_EVERY = 150


def main():
    history = sh.load_history()

    missing = [
        (lid, rec) for lid, rec in history.items()
        if not rec.get("latest", {}).get("detail_checked")
    ]
    missing.sort(key=lambda item: item[1].get("first_seen", ""), reverse=True)
    print(f"DEBUG: {len(missing)} / {len(history)} listings not yet detail-checked")

    batch = missing[:MAX_LOOKUPS_PER_RUN]
    to_check = [rec["latest"] for _, rec in batch]

    def checkpoint():
        sh.save_history(history)
        leads = sh.compute_leads(history)
        save_json_any(sh.LEADS_FILE, leads)

    deadline = time.monotonic() + TIME_BUDGET_SECONDS
    sh.fetch_listing_details(to_check, on_checkpoint=checkpoint, checkpoint_every=CHECKPOINT_EVERY, deadline=deadline)

    checkpoint()

    processed = sum(1 for l in to_check if l.get("detail_checked"))
    found = sum(1 for l in to_check if l.get("description"))
    print(f"DEBUG: detail-checked {processed} listings this run, {found} got a real description "
          f"({len(missing) - processed} still queued for a future run)")


if __name__ == "__main__":
    main()
