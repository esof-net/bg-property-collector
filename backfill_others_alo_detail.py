"""
One-time correction pass: visits the detail page of every alo.bg listing
currently in the site's "Others" province bucket, to get its real
coordinates.

alo.bg's own listing-grid cards for these specific listings never matched
LOCATION_RE (see scraper_alo.py's own docstring for that regex's shape) -
confirmed live via a full scan of the real committed data: every one of
them has city=None, area="Bulgaria" (the generic fallback), and no other
location signal anywhere in its own title text either. Unlike
homes.bg/olx.bg/imot.bg/imoti.bg (whose OWN pages carry no coordinates at
all anywhere, confirmed by direct investigation - see geo_utils.py's
module docstring), alo.bg's OWN detail page embeds a real, listing-exact
coordinate as a plain Google Maps share link - this is the one real
signal these specific listings have, and it's precise per-listing data,
not a geocoded approximation.

Reuses scraper_alo.py's own fetch_update_dates() unchanged (the same
function backfill_detail_alo.py's ordinary hourly pass calls) rather than
duplicating its extraction logic - just scoped to this one specific
subset instead of the generic newest-first queue, since these listings
have already been sitting in the "Others" bucket with zero other signal
and would otherwise wait an unpredictable number of hourly runs to be
reached by chance.

Not scheduled - a one-time correction, run by hand once, its job done.
"""

import sync_to_supabase as sts
import scraper_alo as sa
from geo_utils import save_json_any


def main():
    history = sa.load_history()

    targets = {}
    for lid, rec in history.items():
        latest = rec["latest"]
        if latest.get("lat") is not None:
            continue
        city_key = sts.listing_city_key(latest)
        if sts.listing_oblast_key(latest, city_key) is not None:
            continue  # already resolves some other way - not "Others"
        targets[lid] = latest

    print(f"DEBUG: {len(targets)} alo.bg 'Others' listings to visit")

    # Processed and saved in chunks, not one call over all of them: a full
    # run at ~1-2s/listing over 6,479 listings can take hours, and
    # fetch_update_dates() itself only mutates its in-memory dict - saving
    # only once at the very end would lose every real network fetch made
    # so far if the job hits its timeout partway through.
    CHUNK_SIZE = 200
    items = list(targets.items())
    resolved = 0
    for i in range(0, len(items), CHUNK_SIZE):
        chunk = dict(items[i:i + CHUNK_SIZE])
        sa.fetch_update_dates(chunk)
        resolved += sum(
            1 for latest in chunk.values()
            if latest.get("lat") is not None and sts.oblast_key_from_latlng(latest["lat"], latest["lng"])
        )

        sa.save_history(history)
        leads = sa.compute_leads(history)
        save_json_any(sa.LEADS_FILE, leads)
        print(f"DEBUG: checked {min(i + CHUNK_SIZE, len(items))}/{len(items)}, "
              f"resolved {resolved} so far")

    print(f"Done: {resolved} of {len(targets)} visited listings resolved to a real province "
          f"(the rest either had no coordinates on their own detail page either, or the page "
          f"itself failed to load)")


if __name__ == "__main__":
    main()
