"""
Fills in lat/lng for imot.bg listings that scraper_imot.py left
uncoordinated.

scraper_imot.py's nationwide rewrite deliberately stopped doing live
Nominatim geocoding in-line during the scrape (see its module docstring
for the full story - the same problem scraper_homes.py hit first at
nationwide scale). It now only does a cache-only lookup and leaves
everything else as lat=lng=None.

This script is the other half: a separate, decoupled pass that does the
real live geocoding (same rate-limited, cached Geocoder as before) against
whatever's still missing coordinates in the committed history file, so the
scrape itself stays fast and reliable while location data catches up over
however many runs it takes - the same pattern backfill_geocode_homes.py
already established.

Not scheduled automatically (see backfill-geocode-imot.yml) - re-run it
by hand (or on a cron, later) until the missing-coordinate count reaches
zero, same as any other backfill.
"""

import time

import scraper_imot as si
from geo_utils import Geocoder, save_json_any

# See backfill_geocode_homes.py's own comment for the full reasoning -
# same backstop against occasional Nominatim slowness pushing a run past
# its budget even with a conservative per-run cap.
TIME_BUDGET_SECONDS = 35 * 60
CHECKPOINT_EVERY = 50

# Caps a single run's live-lookup count so this can't itself balloon into
# an unbounded, multi-hour job the way in-line geocoding did at nationwide
# scale (see the module docstring) - meant to be re-run repeatedly (now
# hourly, on a schedule) until the missing-coordinate count hits zero.
# 250 (down from 1500) fits comfortably inside the 45-minute timeout an
# hourly cron needs - see backfill_geocode_homes.py's own comment for the
# full per-lookup timing reasoning.
MAX_LOOKUPS_PER_RUN = 250


def main():
    history = si.load_history()
    geocoder = Geocoder()

    missing = [
        (lid, rec) for lid, rec in history.items()
        if rec.get("latest", {}).get("lat") is None
    ]
    print(f"DEBUG: {len(missing)} / {len(history)} listings missing coordinates")

    def checkpoint():
        si.save_history(history)
        geocoder.save()
        leads = si.compute_leads(history)
        save_json_any(si.LEADS_FILE, leads)

    deadline = time.monotonic() + TIME_BUDGET_SECONDS
    filled = 0
    attempted = 0
    for lid, rec in missing[:MAX_LOOKUPS_PER_RUN]:
        if time.monotonic() >= deadline:
            print(f"DEBUG: stopping at {attempted}/{MAX_LOOKUPS_PER_RUN} - approaching this run's time budget")
            break
        latest = rec["latest"]
        area = latest.get("area", "")
        city = latest.get("city", "")
        # Same query shape scraper_imot.py builds at scrape time - the
        # cache is keyed by this exact string, so a live lookup here later
        # counts as a hit for any future scrape run too.
        geo_query = f"{area}, {city}, България" if area and city else None
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
