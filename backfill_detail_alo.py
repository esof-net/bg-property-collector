"""
Fills in site_updated_at/lat,lng/category for alo.bg listings that
scraper_alo.py's nationwide grid crawl left unenriched.

scraper_alo.py's nationwide conversion deliberately stopped visiting every
listing's own page during the main scrape (see its module docstring) -
nationwide scale (~156,000 listings vs. the old ~10,000 Sofia-only) made
that no longer affordable in a single run. It now only does the fast grid
crawl and leaves detail fields unset.

This script is the other half: a separate, resumable pass that visits
listing pages (scraper_alo.py's own fetch_update_dates()) for whatever's
still unenriched, prioritized newest-first (by first_seen) so freshly
discovered listings get their real posted date and coordinates before
older ones queue behind them - same "don't let unbounded outside-network
work block or crash the actual scrape" pattern backfill_geocode_homes.py
already established for homes.bg's geocoding.

Scheduled hourly (see backfill-detail-alo.yml) rather than left to manual
dispatch like the geocode backfills - at ~1,000 listings/run (see
MAX_LOOKUPS_PER_RUN below) and 24 runs/day, a fresh ~156,000-listing
backlog takes roughly a week to fully clear; after that, each run only
has to keep up with newly-discovered listings from that day's scrapes,
which is a much smaller number.

fetch_update_dates() now takes a deadline and on_checkpoint, and stops
early after several consecutive per-listing failures - added after a
live production run hit a burst of 429 Too Many Requests across many
different listings in a row (the site actively throttling this run, not
any one listing being bad) and, with nothing saved until the very end,
ground through the rest of its ~1,000-listing batch retrying doomed
requests until the workflow's 45-minute timeout hard-killed it, reporting
a failed run with zero progress kept - every single hourly run, for
hours. Same fix as backfill_detail_imoti_net.py's own timeout problem
(see that script's docstring), adapted for a throttling burst here
instead of a run of permanently-gone (404/410) listings there.
"""

import time

import scraper_alo as sa
from geo_utils import save_json_any

# ~1,200 listings/run at ~1.0-1.5s each (REQUEST_DELAY_SECONDS plus network
# round-trip) is roughly 20-30 minutes - comfortably inside the hourly
# cadence this runs on, with real margin before the next run would start.
# Kept generous since the internal time budget below (not this count) is
# what actually decides when a run stops. Raised from 1,000 on 2026-09-26
# to make room for the new DESCRIPTION_TITLE_ECHO_RECHECK_FLOOR below
# without shrinking never_fetched's own guaranteed share.
MAX_LOOKUPS_PER_RUN = 1200

# Guaranteed floor for the _photos_checked recheck tier (see below),
# placed FIRST in this run's processing order rather than just included
# somewhere in the candidate pool - live-confirmed the bug this fixes:
# with recheck items appended after the much larger never-fetched tier
# (currently ~69,000 vs ~18,000) and both drawn from the same
# MAX_LOOKUPS_PER_RUN-sized slice, a real run's entire 1,000-listing batch
# came from never-fetched alone - the recheck backlog got exactly zero
# listings touched, run after run, until never-fetched first drops below
# ~1,000. Giving recheck first claim on a fixed slice of every run's
# budget means it actually clears in a predictable number of days instead
# of waiting - potentially over a week, at real observed throughput - for
# an unrelated, far larger backlog to finish first.
PHOTOS_RECHECK_FLOOR = 250

# Guaranteed floor for the NEW "_gallery_specs_rechecked" tier (see below) -
# same starvation fix as PHOTOS_RECHECK_FLOOR, sized for this tier's much
# larger backlog (29,792 vs photos_recheck's ~6,000 at the time this was
# added) so it clears in days, not weeks, given a fixed, shared
# MAX_LOOKUPS_PER_RUN budget across three tiers now instead of two. Kept
# below PHOTOS_RECHECK_FLOOR + this in total budget so never_fetched still
# keeps a real, non-zero share every run (see remaining_cap below) -
# roughly: photos_recheck clears in ~1 day, this tier in ~3 days, and
# never_fetched (currently the largest backlog) continues at a reduced but
# still real throughput, in line with this file's own "roughly a week"
# estimate above.
GALLERY_SPECS_RECHECK_FLOOR = 400

# Guaranteed floor for the NEW "_description_title_echo_rechecked" tier (see
# below) - same starvation fix again, one level deeper: a 2026-09-26
# production sample found 77.7% of already-"rechecked" descriptions are
# still title echoes (see geo_utils.extract_description_alo()'s
# _looks_like_title_echo() comment). Its real backlog is currently much
# smaller than gallery_specs_recheck's own (1,777 vs 30,041, measured
# directly against data/history_alo.json on 2026-09-26 - gallery_specs_
# recheck itself hasn't cleared anywhere near as fast as originally
# estimated, since new listings keep entering it from never_fetched/
# photos_recheck faster than its own floor clears them), so this floor is
# kept modest rather than matched to GALLERY_SPECS_RECHECK_FLOOR - no need
# to compete as hard for budget against a tier with an 17x larger backlog.
DESCRIPTION_TITLE_ECHO_RECHECK_FLOOR = 150

# Stop visiting new listings once a run has spent this much of the
# workflow's 45-minute timeout - real headroom for whatever page is in
# flight, the final checkpoint, computing leads, and the commit/push step,
# so a run that needs every second of its time budget still exits on its
# own instead of getting hard-killed.
TIME_BUDGET_SECONDS = 35 * 60

CHECKPOINT_EVERY = 150


def select_batch(history):
    """Picks this run's batch of listing ids to visit, in three mutually-
    exclusive, newest-first-sorted tiers (never_fetched / photos_recheck /
    gallery_specs_recheck - see their own comments below), each tier's
    guaranteed floor claimed first, and returns the ordered
    `[(listing_id, history_record), ...]` list fetch_update_dates() should
    process this run. Pulled out of main() so this selection/budgeting
    logic - a real three-way split with two independent floors - can be
    unit-tested directly against a synthetic history dict, without needing
    to mock sa.load_history()/sa.fetch_update_dates()/network access."""
    # "_detail_fetched" is set unconditionally once a listing's detail page
    # has actually been visited (scraper_alo.py's fetch_update_dates()) -
    # unlike site_updated_at/lat,lng/category, which can genuinely stay
    # unset even after a real visit (the site doesn't always show them, and
    # category is now classified at grid-crawl time from title/url alone,
    # with no detail-page dependency at all - see fetch_listings_page()),
    # so this explicit marker is the only reliable "not yet enriched"
    # signal; a bare field-presence check would stop finding any work once
    # every field it could check is populated some other way.
    #
    # "_photos_checked" is a second, separate marker for the same reason:
    # extract_photos_alo() was added to fetch_update_dates() well after it
    # had already been visiting pages for weeks, so every listing marked
    # _detail_fetched before that point permanently looked "done" and would
    # never get its gallery backfilled at all - live-confirmed, 18,010 such
    # listings with 0 photos among them. Listings missing _photos_checked
    # get a one-time re-visit for that; already-never-visited listings still
    # come first (tier 0) since they're missing everything, not just photos
    # (tier 1).
    #
    # "_gallery_specs_rechecked" is a THIRD, separate marker added
    # 2026-09-24 for the same reason again, one level deeper: every listing
    # already marked _photos_checked: True had still only ever been visited
    # under the OLD extract_photos_alo() - a real HTML-attribute-order bug
    # that made it match 0/29,792 real galleries (see geo_utils.py's own
    # comment on the fix, and extract_specs_alo()/extract_contact_alo()'s
    # own comments for the narrower issues fixed in those at the same time).
    # Being "_photos_checked: True" never meant "photos were actually
    # found" - just that a check was attempted - so it can't double as "was
    # checked under a working extractor" the way it's tempting to assume.
    # Listings missing _gallery_specs_rechecked get their own one-time
    # re-visit (tier 2).
    #
    # "_description_title_echo_rechecked" is a FOURTH, separate marker added
    # 2026-09-26 for the same reason yet again: every listing already marked
    # _gallery_specs_rechecked: True had still only ever run
    # extract_description_alo() without its new title-echo guard - a
    # 2026-09-26 production sample found 233/300 (77.7%) of its non-empty
    # descriptions are still exact title echoes (see geo_utils.py's own
    # comment on the fix). Listings missing
    # _description_title_echo_rechecked get their own one-time re-visit
    # (tier 3) - lowest priority of the four, sorted newest-first within its
    # own tier same as the others, since these listings at least already
    # have a description/photos/coordinates/site_updated_at from their
    # prior visit, unlike tier 0's.
    never_fetched = [
        (lid, rec) for lid, rec in history.items()
        if not rec.get("latest", {}).get("_detail_fetched")
    ]
    photos_recheck = [
        (lid, rec) for lid, rec in history.items()
        if rec.get("latest", {}).get("_detail_fetched") and not rec.get("latest", {}).get("_photos_checked")
    ]
    gallery_specs_recheck = [
        (lid, rec) for lid, rec in history.items()
        if rec.get("latest", {}).get("_photos_checked")
        and not rec.get("latest", {}).get("_gallery_specs_rechecked")
    ]
    description_title_echo_recheck = [
        (lid, rec) for lid, rec in history.items()
        if rec.get("latest", {}).get("_gallery_specs_rechecked")
        and not rec.get("latest", {}).get("_description_title_echo_rechecked")
    ]
    never_fetched.sort(key=lambda item: item[1].get("first_seen", ""), reverse=True)
    photos_recheck.sort(key=lambda item: item[1].get("first_seen", ""), reverse=True)
    gallery_specs_recheck.sort(key=lambda item: item[1].get("first_seen", ""), reverse=True)
    description_title_echo_recheck.sort(key=lambda item: item[1].get("first_seen", ""), reverse=True)
    print(f"DEBUG: {len(never_fetched)} never detail-fetched, "
          f"{len(photos_recheck)} detail-fetched but not yet photo-checked, "
          f"{len(gallery_specs_recheck)} photo-checked but not yet rechecked under "
          f"the fixed gallery/specs/contact extractors, "
          f"{len(description_title_echo_recheck)} gallery-rechecked but not yet "
          f"rechecked under the title-echo-aware description extractor, {len(history)} total")

    # All three recheck tiers' guaranteed floors go FIRST in processing
    # order (not just included somewhere in the pool) so they get first
    # claim on this run's actual time budget too, not only a slot in the
    # candidate list - see PHOTOS_RECHECK_FLOOR's/GALLERY_SPECS_RECHECK_
    # FLOOR's/DESCRIPTION_TITLE_ECHO_RECHECK_FLOOR's own comments for the
    # starvation bug this avoids.
    photos_recheck_slice = photos_recheck[:PHOTOS_RECHECK_FLOOR]
    gallery_specs_recheck_slice = gallery_specs_recheck[:GALLERY_SPECS_RECHECK_FLOOR]
    description_title_echo_recheck_slice = description_title_echo_recheck[:DESCRIPTION_TITLE_ECHO_RECHECK_FLOOR]
    remaining_cap = (
        MAX_LOOKUPS_PER_RUN - len(photos_recheck_slice) - len(gallery_specs_recheck_slice)
        - len(description_title_echo_recheck_slice)
    )
    never_fetched_slice = never_fetched[:remaining_cap]
    # If never-fetched itself is smaller than its share of the cap (a
    # near-empty backlog), spend the leftover room on more of the recheck
    # tiers instead of leaving it unused - photos_recheck first (it's the
    # smallest, oldest backlog, closest to fully clearing), then
    # gallery_specs_recheck, then description_title_echo_recheck.
    leftover_cap = remaining_cap - len(never_fetched_slice)
    extra_photos_recheck_slice = (
        photos_recheck[PHOTOS_RECHECK_FLOOR:PHOTOS_RECHECK_FLOOR + leftover_cap] if leftover_cap > 0 else []
    )
    leftover_cap -= len(extra_photos_recheck_slice)
    extra_gallery_specs_recheck_slice = (
        gallery_specs_recheck[GALLERY_SPECS_RECHECK_FLOOR:GALLERY_SPECS_RECHECK_FLOOR + leftover_cap]
        if leftover_cap > 0 else []
    )
    leftover_cap -= len(extra_gallery_specs_recheck_slice)
    extra_description_title_echo_recheck_slice = (
        description_title_echo_recheck[
            DESCRIPTION_TITLE_ECHO_RECHECK_FLOOR:DESCRIPTION_TITLE_ECHO_RECHECK_FLOOR + leftover_cap
        ] if leftover_cap > 0 else []
    )
    return (
        photos_recheck_slice + gallery_specs_recheck_slice + description_title_echo_recheck_slice
        + never_fetched_slice + extra_photos_recheck_slice + extra_gallery_specs_recheck_slice
        + extra_description_title_echo_recheck_slice
    )


def main():
    history = sa.load_history()
    missing = select_batch(history)

    batch = dict(missing)
    to_enrich = {lid: rec["latest"] for lid, rec in batch.items()}

    def checkpoint():
        sa.save_history(history)
        leads = sa.compute_leads(history)
        save_json_any(sa.LEADS_FILE, leads)

    deadline = time.monotonic() + TIME_BUDGET_SECONDS
    sa.fetch_update_dates(to_enrich, on_checkpoint=checkpoint, checkpoint_every=CHECKPOINT_EVERY, deadline=deadline)

    checkpoint()

    # "_description_title_echo_rechecked" (not any of its three older
    # siblings) is the right signal for "actually visited this run" - a
    # recheck-tier listing already had some subset of these flags True
    # before this run started (that's the whole reason it's in this batch),
    # so checking an earlier one here would count every recheck listing as
    # "processed" whether or not fetch_update_dates() actually got to it
    # before the time budget ran out. All four flags are set together on
    # every real visit now, so _description_title_echo_rechecked (the last
    # one set) is accurate for freshly-visited listings across all four
    # tiers.
    processed = sum(1 for l in to_enrich.values() if l.get("_description_title_echo_rechecked"))
    print(f"DEBUG: detail-enriched {processed} listings this run "
          f"({len(missing) - processed} still queued for a future run)")


if __name__ == "__main__":
    main()
