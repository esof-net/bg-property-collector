"""
Fills in lat/lng and description for bazar.bg listings that
scraper_bazar.py's nationwide grid crawl left unenriched.

scraper_bazar.py's nationwide conversion deliberately stopped visiting
every listing's own detail page during the main scrape (see its module
docstring) - nationwide scale (29 cities vs. the old Sofia-only) made that
no longer affordable in a single run. It now only does the fast grid
crawl; category is still set immediately there (a pure title-keyword
classifier, no network needed), but lat/lng - which live only on each
listing's own detail page as data-lat/data-long attributes - are left
unset, as is description.

This script is the other half: a separate, resumable pass that visits
each listing's own page (the same plain, non-JS HTTP fetch + Focus-backend
#see_on_map parsing the old inline code used) for whatever hasn't been
checked yet, prioritized newest-first (by first_seen) so freshly
discovered listings get real coordinates/description before older ones
queue behind them - same pattern backfill_detail_alo.py already
established. description is extracted from the page's own ld+json block
(geo_utils.extract_description_ldjson()) - live-verified to carry the
real agent/seller-written text, not just an auto-generated summary.

2026-09-24: also fills in the structured spec table (property type/sqm/
construction type/floor number) and agency contact (name + real website,
deliberately never phone - see its own comment) via geo_utils.
extract_specs_bazar()/extract_contact_bazar() - the same real-content gap
already closed for alo.bg (extract_specs_alo()/extract_contact_alo()),
now closed here too. Unlike description/photos (ld+json-backed, live-
verified), these are built from real user-supplied screenshots of a live
listing rather than a direct HTTP probe - see extract_specs_bazar()'s own
comment in geo_utils.py for the full evidentiary trail.

A listing is marked "coords_checked" once its detail page has actually
been visited, regardless of whether that page turned up real coordinates
or a description - some bazar.bg listings genuinely have neither on their
own page, and without an explicit marker those would get needlessly
re-visited by every future run instead of being treated as done.

2026-09-29: a second, separate marker - "specs_checked" - fixes the exact
"flag says checked, extractor didn't actually run" bug already fixed for
alo.bg (see backfill_detail_alo.py's own "_photos_checked"/
"_gallery_specs_rechecked" comments): extract_specs_bazar()/
extract_contact_bazar() were added 2026-09-24, but every listing already
marked "coords_checked": True BEFORE that date had already been visited
under the OLD version of this script, which never called them - being
"coords_checked": True never meant "specs/contact were actually
attempted", just that a visit happened, so it can't double as "checked
under an extractor that knows about specs/contact at all". Live-measured
against the real committed data/history_bazar.json.gz on 2026-09-29: 43,185
listings were already coords_checked before 2026-09-24, and only 21.5% of
those have any specs - almost all of the rest (91.7% of the no-specs
ones) already have a real description, proving their page WAS
successfully fetched and the gap is this stale-flag bug, not bazar.bg
"not publishing" the field. Listings visited on/after 2026-09-24 hit
99.2% specs coverage, confirming the extractor itself works fine once it
actually runs. Every listing missing "specs_checked" gets a one-time
re-visit (same two-tier, floor-protected shape as backfill_detail_alo.py's
select_batch(), see SPECS_RECHECK_FLOOR below) - set unconditionally on
every real visit (like "coords_checked") so a listing that genuinely has
no specs/contact on its own page under the current extractor still counts
as done and isn't retried forever.

Scheduled hourly (see backfill-detail-bazar.yml) - previously
workflow_dispatch-only since it only filled in map coordinates, a
lower-priority field; now that it also fills in description (real
listing content, not just a map pin), it needs to actually keep running
rather than wait to be re-dispatched by hand.

A live production run still timed out at 45 minutes despite the
1000-listing cap below being sized with margin for the assumed-typical
case - real per-listing timing varies enough (network conditions, a
patch of slow-to-respond pages) that a fixed count is not a reliable
guarantee, and nothing was saved until the very end, so that run's whole
batch was discarded and reported as a failed run for zero reason (no
error, just ordinary variance). Same fix as backfill_detail_imoti_net.py/
backfill_detail_alo.py's own timeout problems: an internal time budget
plus periodic checkpointing, so a run exits cleanly (and keeps what it
found) well before the workflow's external timeout would ever need to
step in, and a run that hits several consecutive failures in a row (the
site throttling or blocking, same as alo.bg's own fix) stops early
instead of grinding through the rest of a doomed batch.
"""

import time

import scraper_bazar as sb
from geo_utils import (
    extract_coords_bazar, extract_contact_bazar, extract_description_ldjson,
    extract_photos_ldjson, extract_specs_bazar, save_json_any,
)

REQUEST_DELAY_SECONDS = 1.0
# Caps a single run's detail-page-visit count so this can't itself balloon
# into an unbounded, multi-hour job at nationwide scale - meant to be
# re-run repeatedly (by hand, or on a schedule) until the backlog clears.
# Kept generous since the internal time budget below (not this count) is
# what actually decides when a run stops.
MAX_LOOKUPS_PER_RUN = 1000

# Guaranteed floor for the "specs_checked" recheck tier (see the module
# docstring's 2026-09-29 note) - same starvation fix
# backfill_detail_alo.py's PHOTOS_RECHECK_FLOOR/GALLERY_SPECS_RECHECK_FLOOR
# exist for: with recheck items simply appended after (or mixed into) the
# never-checked tier and both drawn from the same MAX_LOOKUPS_PER_RUN-sized
# slice, a real run's entire batch could come from whichever tier sorts
# first by first_seen alone, starving the other. Sized well below
# MAX_LOOKUPS_PER_RUN so never-checked listings still get a real,
# guaranteed share of every run (see remaining_cap in select_batch()) even
# while this tier's backlog (43,185 pre-2026-09-24 listings, measured
# against real data on 2026-09-29) is far larger than never-checked's own
# (~9,100 at the same measurement).
SPECS_RECHECK_FLOOR = 400

# Stop visiting new listings once a run has spent this much of the
# workflow's 45-minute timeout - real headroom for whatever page is in
# flight, the final checkpoint, computing leads, and the commit/push step.
TIME_BUDGET_SECONDS = 35 * 60

CHECKPOINT_EVERY = 150

# Same reasoning as scraper_alo.py's MAX_CONSECUTIVE_DETAIL_FAILURES: a
# run of consecutive failures across many different listings means the
# site is currently blocking/throttling this run, not that particular
# listings are bad - further retries in the same run are equally doomed.
MAX_CONSECUTIVE_FAILURES = 5


def select_batch(history):
    """Picks this run's batch of listing ids to visit, in two mutually-
    exclusive, newest-first-sorted tiers (never_checked / specs_recheck -
    see the module docstring's 2026-09-29 note), the recheck tier's
    guaranteed floor claimed first, and returns the ordered
    `[(listing_id, history_record), ...]` list main() should process this
    run. Pulled out of main() (mirrors backfill_detail_alo.py's own
    select_batch()) so this selection/budgeting logic can be unit-tested
    directly against a synthetic history dict."""
    never_checked = [
        (lid, rec) for lid, rec in history.items()
        if not rec.get("latest", {}).get("coords_checked")
    ]
    specs_recheck = [
        (lid, rec) for lid, rec in history.items()
        if rec.get("latest", {}).get("coords_checked")
        and not rec.get("latest", {}).get("specs_checked")
    ]
    never_checked.sort(key=lambda item: item[1].get("first_seen", ""), reverse=True)
    specs_recheck.sort(key=lambda item: item[1].get("first_seen", ""), reverse=True)
    print(f"DEBUG: {len(never_checked)} never detail-checked, "
          f"{len(specs_recheck)} detail-checked but not yet rechecked under the "
          f"specs/contact extractors, {len(history)} total")

    # The recheck tier's guaranteed floor goes FIRST in processing order
    # (not just included somewhere in the pool) so it gets first claim on
    # this run's actual time budget too - see SPECS_RECHECK_FLOOR's own
    # comment for the starvation bug this avoids.
    specs_recheck_slice = specs_recheck[:SPECS_RECHECK_FLOOR]
    remaining_cap = MAX_LOOKUPS_PER_RUN - len(specs_recheck_slice)
    never_checked_slice = never_checked[:remaining_cap]
    # If never-checked itself is smaller than its share of the cap, spend
    # the leftover room on more of the recheck tier instead of leaving it
    # unused.
    leftover_cap = remaining_cap - len(never_checked_slice)
    extra_specs_recheck_slice = (
        specs_recheck[SPECS_RECHECK_FLOOR:SPECS_RECHECK_FLOOR + leftover_cap] if leftover_cap > 0 else []
    )
    return specs_recheck_slice + never_checked_slice + extra_specs_recheck_slice


def main():
    history = sb.load_history()

    missing = select_batch(history)

    batch = missing

    def checkpoint():
        sb.save_history(history)
        leads = sb.compute_leads(history)
        save_json_any(sb.LEADS_FILE, leads)

    deadline = time.monotonic() + TIME_BUDGET_SECONDS
    filled = 0
    checked = 0
    consecutive_failures = 0
    for i, (lid, rec) in enumerate(batch, 1):
        if time.monotonic() >= deadline:
            print(f"DEBUG: stopping at {i - 1}/{len(batch)} - approaching this run's time budget")
            break
        latest = rec["latest"]
        time.sleep(REQUEST_DELAY_SECONDS)
        latest["coords_checked"] = True
        # Set unconditionally alongside coords_checked (same point, same
        # semantics - see the module docstring's 2026-09-29 note) so a
        # listing whose page turns out to be gone (PermanentlyGone below)
        # or genuinely has no specs/contact still counts as done under the
        # current extractors, rather than being retried forever.
        latest["specs_checked"] = True
        checked += 1
        try:
            html = sb.fetch_html(latest["url"])
        except sb.PermanentlyGone:
            # A clean 404/410 proves the server is responding normally -
            # it's not the site-health signal this early-stop exists for,
            # so it resets the counter instead of feeding it (same fix as
            # scraper_alo.py's own consecutive-failure regression).
            consecutive_failures = 0
            continue
        if html is None:
            consecutive_failures += 1
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                print(f"DEBUG: {consecutive_failures} consecutive detail-page failures at "
                      f"{i}/{len(batch)} - looks like the site is throttling this run, stopping here")
                break
        else:
            consecutive_failures = 0
            coords = extract_coords_bazar(html)
            if coords:
                latest["lat"] = coords["lat"]
                latest["lng"] = coords["lng"]
                filled += 1
            description = extract_description_ldjson(html)
            if description:
                latest["description"] = description
            photos = extract_photos_ldjson(html)
            if photos:
                latest["photos"] = photos
            # Structured specs table - see geo_utils.extract_specs_bazar()'s
            # own comment. sqm feeds straight into the existing sqm field
            # (and, downstream, price_per_sqm in compute_leads()) - the
            # grid crawl never sets a real sqm value at all (see
            # scraper_bazar.py's own module docstring), so this is
            # unconditionally the only source of it, unlike alo.bg where a
            # grid-parsed sqm can occasionally beat the detail pass to it.
            specs = extract_specs_bazar(html)
            if specs:
                if specs.get("sqm") and not latest.get("sqm"):
                    latest["sqm"] = specs["sqm"]
                for field in ("property_type_raw", "construction_type", "floor_number"):
                    if field in specs:
                        latest[field] = specs[field]
            # Agency name + real (or plausibly imot.bg-hosted) agency
            # website - deliberately no phone number, see geo_utils.
            # extract_contact_bazar()'s own comment for why.
            contact = extract_contact_bazar(html)
            if contact:
                for field in ("agency_name", "agency_website"):
                    if field in contact:
                        latest[field] = contact[field]
        if i % 200 == 0:
            print(f"DEBUG: checked {i}/{len(batch)} listings")
        if i % CHECKPOINT_EVERY == 0:
            checkpoint()

    checkpoint()

    print(f"DEBUG: filled coords for {filled} / {checked} checked this run "
          f"({len(missing) - checked} still queued for a future run)")


if __name__ == "__main__":
    main()
