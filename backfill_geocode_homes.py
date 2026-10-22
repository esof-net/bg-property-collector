"""
Fills in lat/lng for homes.bg listings that scraper_homes.py left
uncoordinated.

scraper_homes.py's nationwide rewrite deliberately stopped doing live
Nominatim geocoding in-line during the scrape (see its module docstring
for the full story - a bounded ~80-listing trial took over 10 minutes and
was still running when cancelled, since nationwide locations mean almost
none hit the old Sofia-only cache). It now only does a cache-only lookup
and leaves everything else as lat=lng=None.

This script is the other half: a separate, decoupled pass that does the
real live geocoding (same rate-limited, cached Geocoder as before) against
whatever's still missing coordinates in the committed history file, so the
scrape itself stays fast and reliable while location data catches up over
however many runs it takes - the same "don't let unbounded outside-network
work block or crash the actual scrape" pattern backfill_wayback_prices.py
already established for price history.

Not scheduled automatically (see backfill-geocode-homes.yml) - re-run it
by hand (or on a cron, later) until the missing-coordinate count reaches
zero, same as any other backfill.
"""

import time

import scraper_homes as sh
from geo_utils import Geocoder, save_json_any

# Stop visiting new listings once a run has spent this much of the
# workflow's 45-minute timeout - a backstop against the same "fixed count
# alone isn't a reliable guarantee" issue found in the detail backfills
# (backfill_detail_imoti_net.py/backfill_detail_alo.py/
# backfill_detail_bazar.py): occasional Nominatim slowness could in
# principle still push a run past its budget even with a conservative
# per-run cap. Checkpointing periodically means that stays cheap insurance
# rather than a reason to lose a whole run's progress.
TIME_BUDGET_SECONDS = 35 * 60
CHECKPOINT_EVERY = 50

# Caps a single run's live-lookup count so this can't itself balloon into
# an unbounded, multi-hour job the way in-line geocoding did at nationwide
# scale (see the module docstring) - it's meant to be re-run repeatedly
# (now hourly, on a schedule) until the missing-coordinate count hits
# zero, each run chipping away at a bounded, predictable chunk instead of
# trying to clear everything in one go. At ~1.1-9s per uncached lookup,
# 1500 lookups could take up to 225 minutes worst case - doesn't fit the
# 45-minute timeout an hourly cron needs (a run that regularly overran its
# slot would just pile up in the shared scrape-property-listings
# concurrency group). 250 lookups is at most ~37.5 minutes worst case,
# comfortably inside that budget.
MAX_LOOKUPS_PER_RUN = 250


def main():
    history = sh.load_history()
    geocoder = Geocoder()

    missing = [
        (lid, rec) for lid, rec in history.items()
        if rec.get("latest", {}).get("lat") is None
    ]
    print(f"DEBUG: {len(missing)} / {len(history)} listings missing coordinates")

    def checkpoint():
        sh.save_history(history)
        geocoder.save()
        leads = sh.compute_leads(history)
        save_json_any(sh.LEADS_FILE, leads)

    deadline = time.monotonic() + TIME_BUDGET_SECONDS
    filled = 0
    attempted = 0
    for lid, rec in missing[:MAX_LOOKUPS_PER_RUN]:
        if time.monotonic() >= deadline:
            print(f"DEBUG: stopping at {attempted}/{MAX_LOOKUPS_PER_RUN} - approaching this run's time budget")
            break
        latest = rec["latest"]
        area = latest.get("area", "")
        city = latest.get("city")
        # Reconstructs the same "<area>, <city>" location string
        # scraper_homes.py's own in-line cache-only lookup queries at scrape
        # time (parse_offer()'s geo_query = f"{location}, България", where
        # location is homes.bg's own raw "area, city" text) - NOT what this
        # function used to build (bare `area` alone, despite this comment's
        # own claim that it matched). Dropping city here was a real bug
        # (backlog item 20-22-adjacent audit, 2026-09-22/23): any area name
        # that's ambiguous across more than one Bulgarian city/town - e.g.
        # "Широк център", "жк. Тракия", "жк. Христо Ботев", "к.к.Слънчев
        # Бряг" are all real names duplicated in multiple towns - got
        # geocoded to whichever city Nominatim guessed with no
        # disambiguating context, silently corrupting the oblast for
        # hundreds of listings whose own `city` field was already correct.
        # Including city here both fixes new lookups and reuses the exact
        # same cache key scraper_homes.py's own query would produce, so a
        # correct lookup here counts as a cache hit for future scrapes too.
        location = f"{area}, {city}" if area and city else area
        geo_query = f"{location}, България" if location else None
        if not geo_query:
            continue
        attempted += 1
        coords = geocoder.geocode(geo_query)
        if coords:
            latest["lat"] = coords["lat"]
            latest["lng"] = coords["lng"]
            filled += 1
        if attempted % CHECKPOINT_EVERY == 0:
            checkpoint()

    checkpoint()

    processed = min(len(missing), MAX_LOOKUPS_PER_RUN)
    print(f"DEBUG: filled {filled} / {processed} attempted this run ({len(missing) - processed} still queued for a future run)")


if __name__ == "__main__":
    main()
