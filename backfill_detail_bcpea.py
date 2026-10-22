"""
Fills in district/photo/coordinates for sales.bcpea.org listings that
scraper_bcpea.py's grid crawl left un-detail-checked.

scraper_bcpea.py's fetch_listings() deliberately stopped visiting every
listing's own detail page during the main scrape (see its module
docstring) - at ~1,300 nationwide listings, each detail visit needing a
fresh browser context plus a live geocode call routinely pushed the
combined grid+detail work well past the scrape's 30-minute step timeout,
discarding that run's freshly-scraped grid data too. It now only does the
fast grid crawl; district/photo/coordinates are left unset.

This script is the other half: a separate, resumable pass that visits
each listing's own page (scraper_bcpea.py's own fetch_listing_details(),
unchanged) for whatever hasn't been checked yet, prioritized newest-first
(by first_seen) so freshly discovered listings get real detail before
older ones queue behind them - same pattern backfill_detail_alo.py/
backfill_detail_bazar.py/backfill_detail_imoti_net.py already established.

A listing is marked "detail_checked" once its detail page has actually
been visited, regardless of whether that page turned up real detail - same
reasoning those three backfills already use for their own markers.

Scheduled hourly (see backfill-detail-bcpea.yml) - at ~1,300 total
listings and this run's own conservative per-run cap, a fresh backlog
clears within a few hours, after which each run only has to keep up with
that day's newly-discovered listings, a much smaller number.
"""

import json
import time

import scraper_bcpea as sb

# Each detail visit here is slower than a plain HTTP fetch (a fresh
# Playwright browser context per request, plus a live Nominatim geocode
# call at ~1 req/sec) - a smaller per-run cap than the plain-requests
# backfills keeps a single run comfortably inside its own timeout. Kept
# generous since the internal time budget below (not this count) is what
# actually decides when a run stops - see fetch_listing_details()'s own
# comment for why a fixed count alone isn't a reliable guarantee.
MAX_LOOKUPS_PER_RUN = 400

# Stop visiting new listings once a run has spent this much of the
# workflow's 45-minute timeout - real headroom for whatever page is in
# flight, the final checkpoint, computing leads, and the commit/push step.
TIME_BUDGET_SECONDS = 35 * 60

CHECKPOINT_EVERY = 100


def main():
    history = sb.load_history()

    missing = [
        (lid, rec) for lid, rec in history.items()
        if not rec.get("latest", {}).get("detail_checked")
    ]
    missing.sort(key=lambda item: item[1].get("first_seen", ""), reverse=True)
    print(f"DEBUG: {len(missing)} / {len(history)} listings not yet detail-checked")

    batch = missing[:MAX_LOOKUPS_PER_RUN]
    to_check = [rec["latest"] for _, rec in batch]

    def checkpoint():
        sb.save_history(history)
        leads = sb.compute_leads(history)
        sb.LEADS_FILE.write_text(json.dumps(leads, ensure_ascii=False, indent=2), encoding="utf-8")

    deadline = time.monotonic() + TIME_BUDGET_SECONDS
    sb.fetch_listing_details(to_check, on_checkpoint=checkpoint, checkpoint_every=CHECKPOINT_EVERY, deadline=deadline)

    checkpoint()

    processed = sum(1 for l in to_check if l.get("detail_checked"))
    print(f"DEBUG: detail-checked {processed} listings this run "
          f"({len(missing) - processed} still queued for a future run)")


if __name__ == "__main__":
    main()
