"""
One-time correction pass: geocodes every listing that currently falls into
the site's "Others" province bucket (no known oblast) across the 6 portals
whose own scraped text already names a real settlement/area - just not one
this project happened to already recognize (see sync_to_supabase.py's
BG_MUNICIPALITY_TO_OBLAST docstring for the bulk of that story).

Deliberately reuses geo_utils.Geocoder unchanged - the same confidence-
gated bare-name cross-check built earlier this project specifically so a
genuinely ambiguous settlement name (a real, different place sharing the
same name elsewhere in Bulgaria - "Бяла" is the clearest known example,
a real municipality in both Varna and Ruse oblasts) gets correctly SKIPPED
here rather than guessed at, the same way it protects the live scrapers'
own geocoding. This script does not lower that bar - if a name can't be
resolved confidently, it stays in "Others" rather than being force-fit
into the wrong province, on the same principle that caused real damage
earlier in this project when it was violated (see
verify_geocode_qualifiers.py's own docstring).

Once a listing has real lat/lng, sync_to_supabase.py's own
oblast_key_from_latlng() (point-in-polygon against real oblast boundaries)
resolves its province correctly on the next sync - no new classification
code needed here, just getting coordinates onto these specific listings
now instead of waiting for each portal's own generic, non-targeted
geocode/detail backfill to eventually reach them by chance.

alo.bg is NOT covered here - its "Others" listings carry no usable text
at all (city=None, area="Bulgaria", a scraper-side parsing gap on cards
this shape doesn't match) - see backfill_others_alo_detail.py, which
visits each one's own detail page instead (the only real signal alo.bg
has for these).

Not scheduled - a one-time correction, run by hand once, its job done.
"""

import sync_to_supabase as sts
from geo_utils import Geocoder, save_json_any

import scraper_olx as s_olx
import scraper_bcpea as s_bcpea
import scraper_homes as s_homes
import scraper_bazar as s_bazar
import scraper_imot as s_imot
import scraper_imoti_bg as s_imoti_bg

# (module, portal name as it appears in leads_*.json's own "portal" field)
PORTALS = [
    (s_olx, "olx.bg"),
    (s_bcpea, "sales.bcpea.org"),
    (s_homes, "homes.bg"),
    (s_bazar, "bazar.bg"),
    (s_imot, "imot.bg"),
    (s_imoti_bg, "imoti.bg"),
]


def location_text(l):
    """Best available settlement/area text to geocode, portal conventions
    already established elsewhere in this project (see
    sync_to_supabase.py's listing_oblast_key() for the same field
    preference order)."""
    if l.get("portal") == "sales.bcpea.org":
        return sts.bcpea_settlement_from_title(l.get("title"))
    for field in ("area", "city"):
        value = l.get(field)
        if value and value.strip(" ,"):
            return value
    return None


def main():
    geocoder = Geocoder()
    total_resolved = 0
    total_checked = 0

    for module, portal_name in PORTALS:
        history = module.load_history()
        changed = False
        resolved_this_portal = 0
        checked_this_portal = 0

        for lid, rec in history.items():
            latest = rec["latest"]
            latest.setdefault("portal", portal_name)
            if latest.get("lat") is not None:
                continue
            city_key = sts.listing_city_key(latest)
            if sts.listing_oblast_key(latest, city_key) is not None:
                continue  # already resolves some other way - not "Others"

            text = location_text(latest)
            if not text:
                continue
            checked_this_portal += 1
            total_checked += 1

            query = f"{text}, България"
            coords = geocoder.geocode(query)
            if not coords:
                continue

            # Confirm the geocode actually lands inside a real oblast
            # before committing to it - belt-and-suspenders on top of the
            # Geocoder's own confidence gate (defends against, e.g., a
            # confident match just outside Bulgaria's own borders).
            oblast = sts.oblast_key_from_latlng(coords["lat"], coords["lng"])
            if not oblast:
                continue

            latest["lat"] = coords["lat"]
            latest["lng"] = coords["lng"]
            changed = True
            resolved_this_portal += 1
            total_resolved += 1
            if resolved_this_portal % 50 == 0:
                print(f"DEBUG: {portal_name}: resolved {resolved_this_portal}/{checked_this_portal} so far")
                # Checkpoint every 50 - a run over ~3,600 listings against a
                # live, rate-limited geocoder can run for hours; saving only
                # at the very end would lose everything found so far if the
                # job hits its timeout mid-portal. geocoder.save() is a
                # cheap no-op unless new entries were actually added
                # (_dirty-gated), so this costs nothing on cache hits.
                geocoder.save()
                module.save_history(history)
                leads = module.compute_leads(history)
                save_json_any(module.LEADS_FILE, leads)

        print(f"{portal_name}: resolved {resolved_this_portal} of {checked_this_portal} checked "
              f"({len(history)} total listings)")

        if changed:
            module.save_history(history)
            leads = module.compute_leads(history)
            save_json_any(module.LEADS_FILE, leads)

    geocoder.save()
    print(f"\nDone: {total_resolved} of {total_checked} checked listings resolved to a real province "
          f"across all 6 portals (the rest - genuinely ambiguous names like 'Бяла', or names Nominatim "
          f"itself can't confidently place - stay in Others rather than being guessed at)")


if __name__ == "__main__":
    main()
