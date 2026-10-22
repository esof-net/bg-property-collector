"""
Fills in description for imot.bg listings that scraper_imot.py's grid
crawl never visits the detail page for.

imot.bg's own search-result cards carry no free-text description at all
(only price/area/sqm/floor) - the real description sits on each listing's
own detail page, in a <div class="moreInfo"> prefixed with the fixed
label "Описание на имота:". Confirmed live via probe_descriptions.py
(a Playwright fetch - imot.bg blocks plain requests-based fetching, same
Cloudflare/Akamai-style check scraper_imot.py's own grid crawl already
works around).

This script is a new, separate, resumable pass - imot.bg's grid crawl has
never visited individual detail pages at all (unlike alo.bg/bazar.bg,
which used to do this inline before their own nationwide conversions and
already had a decoupled backfill to reuse) - visiting each listing's own
page (scraper_imot.py's fetch_listing_detail(), reusing the same
goto_with_retries() the grid crawl already uses) for whatever hasn't been
checked yet, prioritized newest-first (by first_seen) so freshly
discovered listings get a real description before older ones queue behind
them - same pattern backfill_detail_alo.py/backfill_detail_bazar.py/
backfill_detail_bcpea.py already established.

A listing is marked "detail_checked" once its detail page has actually
been visited, regardless of whether that page turned up a real
description - without an explicit marker those would get needlessly
re-visited by every future run instead of being treated as done.

Scheduled hourly (see backfill-detail-imot.yml) - a fresh full backlog
(~21,700 listings) clears over about two days at this run's cap, after
which each run only has to keep up with that day's newly-discovered
listings, a much smaller number.
"""

import time

import scraper_imot as si
from geo_utils import save_json_any

# One shared Playwright page reused across the whole batch (see
# fetch_listing_details()'s own comment) keeps the per-listing cost close
# to a plain page navigation, letting this run a larger batch than
# scraper_bcpea.py's own detail backfill (which needs a fresh browser
# context per listing) within the same time budget. Kept generous since
# the internal time budget below (not this count) is what actually
# decides when a run stops.
MAX_LOOKUPS_PER_RUN = 500

# Stop visiting new listings once a run has spent this much of the
# workflow's 45-minute timeout - real headroom for whatever page is in
# flight, the final checkpoint, computing leads, and the commit/push step.
TIME_BUDGET_SECONDS = 35 * 60

CHECKPOINT_EVERY = 150


def main():
    history = si.load_history()

    missing = [
        (lid, rec) for lid, rec in history.items()
        if not rec.get("latest", {}).get("detail_checked")
    ]
    missing.sort(key=lambda item: item[1].get("first_seen", ""), reverse=True)
    print(f"DEBUG: {len(missing)} / {len(history)} listings not yet detail-checked")

    batch = missing[:MAX_LOOKUPS_PER_RUN]
    to_check = [rec["latest"] for _, rec in batch]

    def checkpoint():
        si.save_history(history)
        leads = si.compute_leads(history)
        save_json_any(si.LEADS_FILE, leads)

    deadline = time.monotonic() + TIME_BUDGET_SECONDS
    si.fetch_listing_details(to_check, on_checkpoint=checkpoint, checkpoint_every=CHECKPOINT_EVERY, deadline=deadline)

    checkpoint()

    processed = sum(1 for l in to_check if l.get("detail_checked"))
    print(f"DEBUG: detail-checked {processed} listings this run "
          f"({len(missing) - processed} still queued for a future run)")


if __name__ == "__main__":
    main()
