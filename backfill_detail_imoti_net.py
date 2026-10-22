"""
Fills in site_posted_at/lat,lng for imoti.net listings that scraper.py's
nationwide grid crawl left unenriched.

scraper.py's fetch_listings() deliberately stopped visiting every
listing's own page during the main scrape (see its module docstring) -
nationwide scale (~21,700 listings vs. the old Sofia-only estimate this
was originally sized for) made that no longer affordable in a single run:
a real production run timed out at the workflow's 300-minute step limit
with the detail pass only 3,600/21,719 listings in, discarding that
entire run's freshly-scraped grid data too, since nothing gets saved
until after both steps finish. It now only does the fast grid crawl
(category is already set there - a pure title-keyword classifier, no
network needed) and leaves site_posted_at/lat,lng unset.

This script is the other half: a separate, resumable pass that visits
each listing's own page (scraper.py's own fetch_listing_dates(), reused
unchanged) for whatever hasn't been checked yet, prioritized newest-first
(by first_seen) so freshly discovered listings get real dates/coordinates
before older ones queue behind them - same pattern backfill_detail_alo.py
already established.

A listing is marked "detail_checked" once its detail page has actually
been visited, regardless of whether that page turned up a real date or
coordinates - some listings genuinely have neither on their own page, and
without an explicit marker those would get needlessly re-visited by every
future run instead of being treated as done.

Scheduled hourly (see backfill-detail-imoti-net.yml) - this docstring
previously (incorrectly) said otherwise; the workflow file itself has had
an hourly cron for a while. In practice this portal still has the worst
lat/lng coverage of all 8 (9.6% live-checked, vs. 21-63% elsewhere).
scrape.yml/scrape-large.yml used to share this backfill's concurrency
group and could run for hours, starving this backfill's own hourly
triggers out of the queue before they ever ran - fixed since by giving
scrape.yml/scrape-large.yml their own dedicated groups (see those
workflows' own comments).

MAX_LOOKUPS_PER_RUN was 1,500 for a while and every single hourly run
still hit the workflow's 45-minute timeout partway through (confirmed
via job logs - killed with only ~150-300 processed), for two compounding
reasons: this portal's backlog (still 90%+ unchecked) has a much higher
rate of listings that have since gone 404/410 than a fresher backlog
would, and fetch_with_retries() used to retry those 3x anyway even
though a permanently-gone page can never succeed on retry - burning
~15s per gone listing for nothing. Fixed fetch_with_retries() to fail
fast on 404/410 instead, and lowered this to a real, tested-safe number;
on top of that, fetch_listing_dates() now takes a deadline and
checkpoints its own progress as it goes, so even an unusually slow run
saves what it found instead of the old all-or-nothing "only write at the
very end" behavior - and, just as important, actually exits on its own
before the workflow's hard timeout would kill it, so a run this slow now
reports success (with less processed that run) instead of failure.

Does NOT fill description: confirmed live via probe_descriptions.py that
imoti.net's own detail page carries no free-text description anywhere -
neither in its meta tags nor its ld+json block nor any labeled HTML
block, only structured price/sqm/floor/broker-contact info. Not a gap to
close, a genuine per-portal limitation - index.html already shows a
graceful "no description available from this portal" fallback for it.
"""

import json
import time

import scraper as si

# Matches homes.bg's/imot.bg's/olx.bg's own geocode-backfill cap - bounds a
# single run's detail-page-visit count so this can't itself balloon into
# an unbounded, multi-hour job at nationwide scale. Kept generous since
# the internal time budget below (not this count) is what actually
# decides when a run stops.
MAX_LOOKUPS_PER_RUN = 1500

# Stop visiting new listings once a run has spent this much of the
# workflow's 45-minute timeout - the rest of that budget is headroom for
# whatever page is in flight, the final checkpoint, computing leads, and
# the commit/push step, so a run that ends up needing every second of the
# time budget still exits on its own instead of getting hard-killed.
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

    batch = dict(missing[:MAX_LOOKUPS_PER_RUN])
    to_check = {lid: rec["latest"] for lid, rec in batch.items()}

    def checkpoint():
        si.save_history(history)
        leads = si.compute_leads(history)
        si.LEADS_FILE.write_text(json.dumps(leads, ensure_ascii=False, indent=2), encoding="utf-8")

    deadline = time.monotonic() + TIME_BUDGET_SECONDS
    si.fetch_listing_dates(to_check, on_checkpoint=checkpoint, checkpoint_every=CHECKPOINT_EVERY, deadline=deadline)

    checkpoint()

    checked = sum(1 for l in to_check.values() if l.get("detail_checked"))
    print(f"DEBUG: detail-checked {checked} listings this run "
          f"({len(missing) - checked} still queued for a future run)")


if __name__ == "__main__":
    main()
