"""
Fails the workflow loudly when a scraper's just-committed output shows no
sign of a fresh crawl having actually landed - the "fail loud, never
silent" counterpart, for a different failure shape, to sync_to_supabase.
py's existing near-zero-results data-loss guard (see that file's own
"Data-loss safety guard" section): that guard catches a scraper that
returns almost nothing; this one catches a scraper that keeps returning a
healthy count every run while the file that's actually supposed to land
on disk silently stops updating - alo.bg's grid crawl did exactly this
for 6 days (docs/backlog.md item 3): scraper_alo.py's own step reported
success and "Found 77,000+ listings" in its logs every single day, but
none of it ever reached the committed data/history_alo.json (see
merge_history_conflict.py's docstring for the actual root cause and its
fix) - so every downstream signal that only looks at "did the step
exit 0" stayed green throughout, and it took a human audit to notice.

Two independent checks, either one failing fails the whole check:
1. Freshness: the single most recent snapshot across the whole history
   file must be recent - if it isn't, this run's crawl (or an earlier
   day's) never actually landed in the file now on disk, regardless of
   what the scraper step itself reported.
2. Active ratio: the computed leads file's source_status="active" share
   must clear a floor well under every portal's own healthy range (see
   MIN_ACTIVE_RATIO below) - catches the downstream symptom directly (an
   entire portal flipping "removed"/"sold" in one pass) as a second,
   independent signal in case the freshness check above is ever fooled by
   something this script's author didn't anticipate.

Run this AFTER the commit/push step succeeds (so it's checking the exact
state that actually landed on main, not this run's pre-conflict-resolution
local state) and WITHOUT continue-on-error, so a real staleness event
makes the workflow run itself report a failure - unlike scraper_alo.py's
own step, whose continue-on-error is deliberately about isolating one
scraper's crash from the rest of the run, not about hiding a bad result.
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

from geo_utils import load_json_any

DATA_DIR = Path(__file__).parent / "data"


# homes.bg's own history/leads files are .json.gz, not plain .json (2026-09-25
# addendum to the GH001 incident fix - see geo_utils.py's "Compressed
# on-disk JSON storage" comment). main() builds each portal's path
# generically from its suffix, so this falls back to the .gz sibling when
# the plain path doesn't exist, rather than hardcoding homes.bg as a
# special case here.
def _resolve_data_path(plain_path):
    if plain_path.exists():
        return plain_path
    gz_path = plain_path.with_name(plain_path.name + ".gz")
    return gz_path if gz_path.exists() else plain_path

# Generous slack above this repo's cron cadences (24h for scrape-large.yml,
# 6h for scrape.yml) so an occasionally slow/delayed run doesn't trip this -
# tight enough that a crawl silently dead for days (alo.bg's real incident)
# fails fast instead of taking 48h+ (GONE_AFTER's own threshold) to show up
# as a symptom.
MAX_FRESHEST_SNAPSHOT_AGE_HOURS = 30

# Corrected 2026-09-22 (Missy's PR #197 review): this threshold is only
# actually safe for the portals currently wired up below (alo.bg, imoti.net)
# - it is NOT comfortably under every portal's real active ratio. Measured
# directly against the real committed data: imoti.net 63.8%, imot.bg 68.8%,
# alo.bg 0.0% (the real incident this check exists to catch), olx.bg 43.3%,
# homes.bg 91.2%, bazar.bg 45.2%, imoti.bg 90.3%, bcpea 60.1%. olx.bg and
# bazar.bg sit only 3-5 points above this 40% floor - a normal day's
# fluctuation could trip a false portal-wide-failure alarm for either of
# them. Do NOT extend this check to olx.bg or bazar.bg with this same
# threshold without first re-deriving a real per-portal margin from their
# own healthy-day data, the way this was done for alo.bg/imoti.net.
DEFAULT_MIN_ACTIVE_RATIO = 0.40
MIN_LISTINGS_FOR_RATIO_CHECK = 50

# 2026-09-24 (docs/backlog.md's scrape.yml commit-failure incident): wires
# this check into scrape.yml for the 6 portals it owns (homes.bg, imot.bg,
# olx.bg, bazar.bg, imoti.bg, bcpea), which previously had no freshness/
# safety-net coverage at all - exactly the gap that let 3 consecutive
# failed commits (~15h+ of no fresh data landing on main) go completely
# undetected. Per the caveat above, each portal gets its OWN floor with
# real margin below ITS OWN measured healthy-day active ratio (the same
# 2026-09-22 measurements quoted above) rather than one shared threshold
# copy-pasted across all of them: homes.bg (91.2% healthy) and imoti.bg
# (90.3% healthy) can safely take a much higher floor than olx.bg (43.3%)
# and bazar.bg (45.2%), which stay at a LOWER floor than
# DEFAULT_MIN_ACTIVE_RATIO precisely because - per the same caveat - they
# don't have the margin to use a higher one without real false-alarm risk.
# A portal not listed here (alo.bg, imoti.net's "" suffix) keeps using
# DEFAULT_MIN_ACTIVE_RATIO unchanged, exactly as before this change.
#
# Recalibrated 2026-09-26 (homes.bg only) - the 0.55 floor set above was
# itself stale within 2 days and, undetected, would have failed EVERY
# scheduled scrape.yml run from #189 through #193 (~20h+, all on this one
# check) as a false alarm, not a real crawl failure. Root cause: dd83178
# (2026-09-23, "Fix homes.bg tracking-ID type collision") fixed
# build_tracking_id() to stop dropping homes.bg's hs/as/lp/la type prefix.
# Before that fix, two different-typed listings sharing a bare numeric id
# silently collided onto one tracking key, permanently hiding one "side"
# of the collision. Once fixed, every collision pair's previously-hidden
# other side surfaced as a genuinely new record on its next crawl - a
# real, wanted, one-time correction to the tracked population, not a
# live incident (see docs/backlog.md item 23 for the fix itself and item
# 37's addenda for the resulting ~74,012 -> ~140,337 record-count jump,
# and docs/decisions.md's 2026-09-26 entry for the full investigation).
# Confirmed directly against real committed data across the 5 runs this
# tripped (#189-193, 2026-09-25/26): active ratio held steady at
# 47.3%-48.0% (66,377-68,116 active out of 140,387-142,420 total) - the
# real-world active-listing count is capped by homes.bg's own actual
# nationwide inventory (~70,253, per scraper_homes.py's module docstring)
# regardless of crawl health, while the tracked-total denominator roughly
# doubled from the collision-fix backfill, so ~47-48% is the new
# mathematically-expected healthy baseline, not a degraded one. (Also
# confirmed mechanically: of the 74,304 "removed" records in the current
# data/leads_homes.json.gz, 66,882 (90%) share removed_at=2026-09-23 -
# the exact day the backfill ran - with the large majority of those at
# days_on_market=28, matching the 2026-08-25 nationwide-coverage
# expansion - a one-time mechanical artifact, not organic delisting.)
# New floor 0.25 keeps the same ~22-25pt-margin-below-observed-baseline
# discipline already used for this portal's similarly-tight peers
# (olx.bg: 43.3% healthy -> 0.20 floor, ~23pt margin; bazar.bg: 45.2%
# healthy -> 0.20 floor, ~25pt margin) rather than the much larger ~35pt
# margin affordable for the high-baseline portals (imoti.bg, homes.bg's
# own old figure) - homes.bg is now a tight portal like olx.bg/bazar.bg,
# not a high-baseline one, and its floor is calibrated the same way
# theirs was. Still comfortably clears a genuine incident: a real
# dead/stuck crawl (alo.bg's own 0.0% incident, or anything in the
# 5-10% pathological range) fails this floor by a wide margin.
PER_PORTAL_MIN_ACTIVE_RATIO = {
    "homes": 0.25,     # healthy ~47.3-48.0% post-2026-09-23 ID-collision-fix backfill (see addendum above) - ~22-23pt margin
    "imot": 0.45,      # healthy ~68.8% - ~24pt margin
    "olx": 0.20,       # healthy ~43.3% - tight portal, see caveat above; best safely-available margin
    "bazar": 0.20,     # healthy ~45.2% - tight portal, see caveat above; best safely-available margin
    "imoti_bg": 0.55,  # healthy ~90.3% - ~35pt margin
    "bcpea": 0.35,     # healthy ~60.1% - ~25pt margin
}


def check_history_freshness(history_path):
    if not history_path.exists():
        print(f"::error::check_scrape_freshness.py: {history_path} does not exist - nothing to check")
        return False
    history = load_json_any(history_path)
    if not history:
        print(f"check_scrape_freshness.py: {history_path} is empty - skipping (nothing tracked yet)")
        return True
    newest = None
    for rec in history.values():
        snaps = rec.get("snapshots") or []
        if not snaps:
            continue
        seen_at = snaps[-1].get("seen_at")
        if not seen_at:
            continue
        ts = datetime.fromisoformat(seen_at)
        if newest is None or ts > newest:
            newest = ts
    if newest is None:
        print(f"::error::check_scrape_freshness.py: {history_path}: no listing has a single snapshot with "
              f"a seen_at - can't tell when this portal was last actually crawled")
        return False
    age_hours = (datetime.now(timezone.utc) - newest).total_seconds() / 3600
    if age_hours > MAX_FRESHEST_SNAPSHOT_AGE_HOURS:
        print(
            f"::error::check_scrape_freshness.py: {history_path}: freshest snapshot across all "
            f"{len(history)} tracked listings is {age_hours:.1f}h old (limit "
            f"{MAX_FRESHEST_SNAPSHOT_AGE_HOURS}h) - this run's crawl doesn't look like it actually "
            f"landed on disk, even though the scraper step itself may have reported success. See "
            f"docs/backlog.md item 3 and merge_history_conflict.py's docstring for the known cause "
            f"of this exact failure shape."
        )
        return False
    print(f"OK: check_scrape_freshness.py: {history_path}: freshest snapshot is {age_hours:.1f}h old "
          f"(limit {MAX_FRESHEST_SNAPSHOT_AGE_HOURS}h)")
    return True


def check_leads_active_ratio(leads_path, min_ratio):
    if not leads_path.exists():
        print(f"::error::check_scrape_freshness.py: {leads_path} does not exist - nothing to check")
        return False
    leads = load_json_any(leads_path)
    total = len(leads)
    if total < MIN_LISTINGS_FOR_RATIO_CHECK:
        print(f"check_scrape_freshness.py: {leads_path} has only {total} listings - skipping active-ratio check")
        return True
    active = sum(1 for l in leads if l.get("source_status") == "active")
    ratio = active / total
    if ratio < min_ratio:
        print(
            f"::error::check_scrape_freshness.py: {leads_path}: only {active}/{total} listings "
            f"({ratio:.1%}) are source_status=active (floor {min_ratio:.0%}) - this looks like "
            f"a portal-wide false-removal event (a dead/stuck crawl aging every listing past its own "
            f"GONE_AFTER threshold), not a real mass delisting."
        )
        return False
    print(f"OK: check_scrape_freshness.py: {leads_path}: {active}/{total} ({ratio:.1%}) active (floor {min_ratio:.0%})")
    return True


def main():
    if len(sys.argv) < 2:
        print('Usage: check_scrape_freshness.py <portal_suffix> [<portal_suffix> ...]')
        print('  e.g. "alo" checks data/history_alo.json + data/leads_alo.json; '
              '"" checks data/history.json + data/leads.json')
        sys.exit(2)
    ok = True
    for suffix in sys.argv[1:]:
        suffix_part = f"_{suffix}" if suffix else ""
        history_path = _resolve_data_path(DATA_DIR / f"history{suffix_part}.json")
        leads_path = _resolve_data_path(DATA_DIR / f"leads{suffix_part}.json")
        min_ratio = PER_PORTAL_MIN_ACTIVE_RATIO.get(suffix, DEFAULT_MIN_ACTIVE_RATIO)
        ok = check_history_freshness(history_path) and ok
        ok = check_leads_active_ratio(leads_path, min_ratio) and ok
    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
