"""
Resolves a `git pull --rebase` content conflict on a `data/history*.json`
file by actually merging both sides instead of blindly discarding one -
see docs/decisions.md's 2026-09-22 entry ("alo.bg grid crawl was never
actually dead") for the full investigation this came out of.

The bug this replaces: every scrape/backfill workflow's git-commit step
falls back to

    git diff --name-only --diff-filter=U | xargs -r git checkout --ours --

on a rebase conflict, with a comment claiming this "keeps main's version
of the conflicted files". During an active `git rebase` (which is what
`git pull --rebase` runs), git's own meaning of "ours"/"theirs" is
swapped from a normal merge: "ours" (stage 2) is the commit being rebased
ONTO - i.e. upstream/origin/main - and "theirs" (stage 3) is the commit
being replayed - i.e. THIS run's own freshly-scraped data. So
`checkout --ours` does exactly what its comment says (keeps main's
version) - but for a workflow whose only job is to land freshly-scraped
data, "keep main's version and discard what this run just produced" is
backwards the moment main's version is the STALE one, which is exactly
what happens whenever this workflow's run genuinely has newer data than
whatever an interleaved concurrent workflow (an hourly backfill sharing
the same data/ files) most recently pushed.

Live-confirmed as the actual root cause of alo.bg's grid crawl "silently
dying" (docs/backlog.md item 3): scraper_alo.py's fetch_listings() found
77,625-89,324 real listings on every single run from 2026-09-16 through
at least 2026-09-19 (confirmed from GitHub Actions job logs - the crawl
was never broken), but data/history_alo.json hit a real content conflict
against backfill-detail-alo.yml's concurrent hourly commits on every one
of those runs, and `checkout --ours` discarded the freshly-merged file
every time - freezing the committed data/history_alo.json at whatever
backfill-detail-alo.yml had last pushed, which is why every listing's
last real snapshot capped at 2026-09-16T07:57:07Z regardless of how many
subsequent runs "succeeded".

data/history*.json's shape (see scraper_alo.py's update_history()/
save_history(), and the equivalent in every other scraper - same schema
everywhere) is a dict keyed by listing id:
    {"<id>": {"first_seen": "<iso>", "snapshots": [{"seen_at": "<iso>",
     "price_eur": <int>, ...}, ...], "latest": {<scraped fields>}}, ...}
That shape is safe to merge without guessing: each listing's id is an
independent record, so the merge is a per-id union, not a line-based text
merge. For an id present on both sides: "first_seen" takes the earlier of
the two (whichever side saw it first is correct regardless of which
commit happened to survive), "snapshots" takes the union of both sides'
entries (deduped on exact (seen_at, price_eur) - see below), and "latest"
is a shallow dict merge preferring whichever side's own snapshot history
is more recent (the side that actually re-scraped this listing most
recently) but keeping any keys unique to the OTHER side (this is a
deliberate, minimal mitigation for a separate, already-flagged issue -
scraper_alo.py's update_history() replaces "latest" wholesale each run
rather than merging into it, so a plain grid-crawl commit's "latest" has
none of backfill_detail_alo.py's enrichment fields like site_updated_at/
lat/lng/description/photos/_detail_fetched - see docs/decisions.md for
why that's being flagged as a separate follow-up rather than fixed here).

Not applied to data/leads*.json (the OTHER conflicted files in every run
that hit this) - deliberately left on the existing checkout --ours
fallback (now made loud, not silent - see the workflow files). leads*.json
is a fully-derived array that every scraper's own compute_leads()
recomputes from scratch off history*.json on its very next run, so as
long as history*.json - the actual source of truth - stops losing data,
leads*.json self-heals within one cycle with no separate merge logic
needed; writing a safe generic merger for an array keyed by nothing would
be real, unnecessary extra risk for zero lasting benefit.

**"latest" is now a per-field merge, not a per-record coin flip on
snapshot recency (issue #243 / docs/missy-findings/2026-09-23.md).**
The paragraph above ("'latest' is a shallow dict merge preferring
whichever side's own snapshot history is more recent") was true of the
first version of this script and is exactly what caused a real, confirmed
incident: PR #199 fixed imoti.net's "everything is 'apartment'"
classifier bug by correcting `latest.category` in place on `main`, with
no new snapshot appended (a deliberate data-only correction). Four hours
later, a `scrape-large.yml` run that had checked out `main` BEFORE that
fix landed - so was still running the old, buggy classifier - hit its
rebase conflict AFTER the fix was already on `main`. That run's `latest`
had a chronologically newer snapshot (it really did re-scrape), so the
old "whichever side's snapshots are more recent wins the whole `latest`
dict" rule picked the *stale, buggy* run's `category`, silently
reintroducing the bug for ~17,680 listings. Snapshot recency is a
legitimate signal for fields that genuinely change in the real world
between scrapes (price_eur, sqm, photo, detail-page fields, etc. - this
script's job there is unchanged and still works exactly as documented
above). It is NOT a legitimate signal for `category`/`category_confidence`
/`portal`/`city` (`STABLE_LATEST_FIELDS` below): these are
classifier/parser output *for a given listing's own already-stored
inputs*, so two sides genuinely disagreeing on one of them is much more
likely to mean one side ran different (typically older) code than that
the real world changed. And critically, this script can lean on a real
asymmetry rather than a guess: at the moment a conflict is being resolved,
`main`'s code can never be OLDER than `local`'s - `local`'s code was
whatever the run checked out whenever it started and is frozen for that
run's whole duration, while `main` only ever moves forward - so `local`
running newer code than what's on `main` right now isn't a real
possibility in this git-rebase-onto-main flow. For `category`/
`category_confidence` specifically, this script goes one step further
than trusting that asymmetry blindly: for the two portals whose scraper
calls `category_classifier.classify_listing()` with exactly the
title/url that end up stored in `latest` (imoti.net, alo.bg -
`RECOMPUTABLE_CLASSIFIER_PORTALS` below; imoti.bg also uses that
classifier but with extra inputs `latest` doesn't retain, so it's
deliberately excluded rather than recomputed inexactly), it actually
re-runs today's classifier against each side's own stored title/url and
prefers whichever side's stored value that recomputation still confirms
- catching cases the recency-asymmetry argument alone wouldn't (e.g. a
classifier keyword-list improvement that landed on `main` as a *code*
change, before any new scrape re-touched this particular listing, so
`main`'s own stored `latest.category` is itself still the stale value
until its own next scrape). See `_resolve_stable_field_conflicts()`'s own
docstring for the exact precedence. For every other field, and for the 6
portals without a safe recompute, behavior is unchanged from the
paragraph above.

Usage (from the workflow's conflict-fallback step, after `git pull
--rebase origin main` reports a conflict):
    python merge_history_conflict.py $(git diff --name-only --diff-filter=U)
Exits 0 and `git add`s each file it actually resolved. Any conflicted
path that doesn't match data/history*.json is left untouched (still
conflicted) for the caller's own existing fallback to handle - this
script only ever touches the specific files it knows how to merge safely.
"""

import gzip
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

HISTORY_NAME_RE = None  # set below, avoids importing re at module import time for a one-liner

# Imported defensively - this script's own rule (see main()'s "always exit
# 0" comment) is that a merge failure here should degrade, never crash the
# whole conflict-resolution pass. If category_classifier.py is ever moved/
# renamed/broken, _recompute_category() below just returns None for every
# record and the STABLE_LATEST_FIELDS fallback (prefer "main") still
# applies on its own.
try:
    from category_classifier import classify_listing
except ImportError:
    classify_listing = None

# scraper.py (portal "imoti.net") and scraper_alo.py (portal "alo.bg") both
# call classify_listing() with exactly title=<the same title stored in
# "latest">, url=<the same url stored in "latest"> - so recomputing from a
# record's own stored "latest" reproduces their exact original call.
# scraper_imoti_bg.py (portal "imoti.bg") also uses classify_listing(), but
# passes description= (imoti.bg's title-only scraped record doesn't always
# retain a matching description string in the same shape) and a url
# mutated with a category slug that's popped off before "latest" is ever
# saved - recomputing from "latest" alone would NOT reproduce that call, so
# imoti.bg is deliberately left out rather than guessed at with an inexact
# recompute. The other 5 portals (bazar.bg, bcpea.org, homes.bg, imot.bg,
# olx.bg) use a different classifier entirely (geo_utils.classify_category()
# or, for homes.bg, a non-text classification from which search category the
# listing was found under) - not recomputed here.
RECOMPUTABLE_CLASSIFIER_PORTALS = {"imoti.net", "alo.bg"}

# "latest" fields that are classifier/parser-derived output for a given
# listing's own already-stored inputs, not raw scraped real-world state -
# see the module docstring's "'latest' is now a per-field merge" section
# for the full reasoning. Deliberately does NOT include price_eur/sqm/
# photo/site_posted_at/lat/lng/description/detail_checked/etc. - those
# really do change over time and snapshot recency remains a legitimate,
# unchanged signal for them.
STABLE_LATEST_FIELDS = {"category", "category_confidence", "portal", "city"}


def is_history_file(path):
    name = Path(path).name
    # .json.gz, not just plain .json - 2026-09-25 addendum to the GH001
    # incident fix (see geo_utils.py's "Compressed on-disk JSON storage"
    # comment): homes.bg's own history_homes.json/leads_homes.json are now
    # gzip-compressed. Without this, a homes.bg rebase conflict would
    # silently fall through to "unresolved" below and hit the caller's own
    # checkout --ours fallback - reintroducing exactly the silent-data-loss
    # bug this whole script exists to prevent (see module docstring), for
    # the one portal under the most concurrent-writer pressure right now
    # (scrape.yml + the hourly backfill-geocode-homes.yml both write to
    # it).
    return (
        name in ("history.json", "history.json.gz")
        or (name.startswith("history_") and (name.endswith(".json") or name.endswith(".json.gz")))
    )


def git_show(rev, path):
    """Returns the conflicted file's content at the given git stage
    ('main' -> :2:, 'local' -> :3:), or None if that stage doesn't exist
    (e.g. the file was added fresh on only one side). Transparently
    gzip-decompresses when `path` ends in `.gz` - `git show` returns the
    raw committed blob bytes regardless of format, so this is the one
    place that needs to know about it; every caller still gets back a
    plain JSON text string exactly as before this change."""
    stage = {"main": ":2:", "local": ":3:"}[rev]
    result = subprocess.run(
        ["git", "show", f"{stage}{path}"], capture_output=True
    )
    if result.returncode != 0:
        return None
    raw = result.stdout
    if Path(path).name.endswith(".gz"):
        raw = gzip.decompress(raw)
    return raw.decode("utf-8")


def snapshot_key(s):
    # Two snapshots are "the same real observation" if they share a
    # timestamp and price - anything else (a genuinely different scrape
    # at a different moment, or the same moment with a different price,
    # which shouldn't happen but isn't this script's job to police) is
    # kept as a separate entry.
    return (s.get("seen_at"), s.get("price_eur"))


def freshest_seen_at(rec):
    snaps = rec.get("snapshots") or []
    if not snaps:
        return None
    return snaps[-1].get("seen_at")


def _recompute_category(latest):
    """Best-effort recompute of (category, category_confidence) straight
    from this record's own stored title/url, using whatever
    category_classifier.py is on disk right now (the same code a future
    scrape would use). Returns None when this record's portal isn't one
    of RECOMPUTABLE_CLASSIFIER_PORTALS, classify_listing isn't
    importable, or the record is missing the inputs it needs - callers
    treat None as "couldn't verify," never as a value to trust."""
    if classify_listing is None:
        return None
    if latest.get("portal") not in RECOMPUTABLE_CLASSIFIER_PORTALS:
        return None
    if not latest.get("title") or not latest.get("url"):
        return None
    category, confidence, _ = classify_listing(title=latest["title"], url=latest["url"])
    return category, confidence


def _resolve_category_conflict(main_latest, local_latest):
    """Only called when main_latest/local_latest both have a "category"
    and/or "category_confidence" and they disagree. Returns the
    (category, category_confidence) pair to use, in this order of trust:

    1. Recompute BOTH sides' own (title, url) through today's classifier.
       If exactly one side's stored category still matches its own fresh
       recompute and the other doesn't, that side is verified correct
       right now - use its (category, category_confidence) together,
       never mixed from different sides (confidence only makes sense
       paired with the category it was computed for). This is what
       correctly picks PR #199's already-merged fix over a stale, pre-fix
       run's fresher-but-wrong snapshot (issue #243): the stale run's OWN
       stored category no longer matches what today's classifier produces
       from that same run's own title/url, because the run's title/url
       were never wrong - only the code path that turned them into a
       category was.
    2. If both sides' own recomputes agree with each other - whether or
       not either matches what was actually stored (e.g. the classifier
       gained a keyword since both these runs happened) - that shared
       fresh answer is the most current truth available; use it directly
       rather than trusting either side's possibly-stale stored value.
    3. Otherwise recompute couldn't settle it (not a recomputable portal,
       missing title/url, or neither/both sides' stored value survives
       its own recompute without converging) - fall back to main's stored
       value. main is always at least as new, code-wise, as local (see
       the module docstring) even when this script can't independently
       verify the specific field, so this is a reasoned default, not a
       coin flip.
    """
    main_pair = (main_latest.get("category"), main_latest.get("category_confidence"))
    local_pair = (local_latest.get("category"), local_latest.get("category_confidence"))

    main_recomputed = _recompute_category(main_latest)
    local_recomputed = _recompute_category(local_latest)

    main_verified = main_recomputed is not None and main_recomputed == main_pair
    local_verified = local_recomputed is not None and local_recomputed == local_pair

    if main_verified and not local_verified:
        return main_pair
    if local_verified and not main_verified:
        return local_pair
    if main_recomputed is not None and main_recomputed == local_recomputed:
        return main_recomputed
    return main_pair


def _resolve_stable_field_conflicts(main_latest, local_latest):
    """Returns a dict of overrides for STABLE_LATEST_FIELDS keys present
    (with a differing value) on both sides - these override whatever the
    recency-based shallow union in merge_record() picked for just these
    specific keys; every other key (including a STABLE_LATEST_FIELDS key
    only one side has) is untouched."""
    overrides = {}
    if (
        main_latest.get("category") != local_latest.get("category")
        or main_latest.get("category_confidence") != local_latest.get("category_confidence")
    ) and "category" in main_latest and "category" in local_latest:
        category, confidence = _resolve_category_conflict(main_latest, local_latest)
        overrides["category"] = category
        overrides["category_confidence"] = confidence
    for field in STABLE_LATEST_FIELDS - {"category", "category_confidence"}:
        if field not in main_latest or field not in local_latest:
            continue
        if main_latest[field] != local_latest[field]:
            overrides[field] = main_latest[field]
    return overrides


def merge_record(main_rec, local_rec):
    if main_rec is None:
        return local_rec
    if local_rec is None:
        return main_rec

    first_seen = main_rec.get("first_seen") or local_rec.get("first_seen")
    if main_rec.get("first_seen") and local_rec.get("first_seen"):
        first_seen = min(main_rec["first_seen"], local_rec["first_seen"])

    seen = set()
    merged_snapshots = []
    for s in (main_rec.get("snapshots") or []) + (local_rec.get("snapshots") or []):
        k = snapshot_key(s)
        if k in seen:
            continue
        seen.add(k)
        merged_snapshots.append(s)
    # seen_at is an ISO timestamp string - safe to sort lexicographically
    # (same format/timezone convention every scraper uses via
    # datetime.now(timezone.utc).isoformat()).
    merged_snapshots.sort(key=lambda s: s.get("seen_at") or "")

    main_fresh = freshest_seen_at(main_rec)
    local_fresh = freshest_seen_at(local_rec)
    if local_fresh and (not main_fresh or local_fresh > main_fresh):
        newer_latest, older_latest = local_rec.get("latest", {}), main_rec.get("latest", {})
    else:
        newer_latest, older_latest = main_rec.get("latest", {}), local_rec.get("latest", {})
    # Shallow union: the newer side's own fields win on overlap, but a
    # field only the older side has (typically backfill-only enrichment
    # fields the newer side's plain grid-crawl "latest" never carried -
    # see module docstring) survives instead of being silently dropped.
    merged_latest = {**older_latest, **newer_latest}
    # ...except STABLE_LATEST_FIELDS, where the newer-snapshot side isn't
    # a trustworthy winner (see module docstring / _resolve_stable_field_
    # conflicts()'s own docstring) - only actually overrides anything when
    # main_rec and local_rec both have the field and disagree.
    merged_latest.update(_resolve_stable_field_conflicts(main_rec.get("latest", {}), local_rec.get("latest", {})))

    merged = dict(main_rec if main_fresh and (not local_fresh or main_fresh >= local_fresh) else local_rec)
    merged["first_seen"] = first_seen
    merged["snapshots"] = merged_snapshots
    merged["latest"] = merged_latest
    return merged


def merge_history(main_json, local_json):
    main_data = json.loads(main_json) if main_json is not None else {}
    local_data = json.loads(local_json) if local_json is not None else {}
    merged = {}
    for lid in set(main_data) | set(local_data):
        merged[lid] = merge_record(main_data.get(lid), local_data.get(lid))
    return merged


def resolve(path):
    main_json = git_show("main", path)
    local_json = git_show("local", path)
    if main_json is None and local_json is None:
        print(f"WARNING: merge_history_conflict.py: {path} has neither :2: nor :3: stage - leaving unresolved")
        return False
    try:
        merged = merge_history(main_json, local_json)
    except (json.JSONDecodeError, TypeError) as e:
        print(f"::warning::merge_history_conflict.py: {path} failed to parse for a real merge ({e}) - leaving unresolved for the caller's existing fallback")
        return False
    if Path(path).name.endswith(".gz"):
        # Compact separators, not indent=2 - see geo_utils.py's
        # save_json_any() for why (indent costs real bytes post-gzip for
        # no benefit on a file that's never hand-read either way).
        with gzip.open(path, "wt", encoding="utf-8", compresslevel=9) as f:
            json.dump(merged, f, ensure_ascii=False, separators=(",", ":"))
    else:
        Path(path).write_text(
            json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    n_main = len(json.loads(main_json)) if main_json else 0
    n_local = len(json.loads(local_json)) if local_json else 0
    print(
        f"OK: merge_history_conflict.py: merged {path} - "
        f"main had {n_main} listings, this run had {n_local}, merged has {len(merged)} "
        f"(no listing or snapshot from either side was discarded)"
    )
    subprocess.run(["git", "add", path], check=True)
    return True


def main():
    paths = sys.argv[1:]
    if not paths:
        print("Usage: merge_history_conflict.py <conflicted-path> [<conflicted-path> ...]")
        sys.exit(0)
    unresolved = []
    for path in paths:
        if not is_history_file(path):
            unresolved.append(path)
            continue
        if not resolve(path):
            unresolved.append(path)
    if unresolved:
        # Not an error - the caller (the workflow's own bash fallback)
        # still needs to resolve these itself. Printed so it's visible in
        # the run's log which files got a real merge vs. the old
        # last-resort behavior.
        print("merge_history_conflict.py: left unresolved (caller's fallback applies): " + ", ".join(unresolved))
    # Always exit 0 - this script's job is best-effort resolution, not
    # deciding whether the overall push attempt succeeds.
    sys.exit(0)


if __name__ == "__main__":
    main()
