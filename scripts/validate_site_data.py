#!/usr/bin/env python3
"""Validates the two Home data files that the site reads: home_stats.json and area_index.json.

NOT a scraper. Used by the site-publishing workflows (publish-site-data.yml, deploy-site.yml via
scripts/stage_site.sh) so a broken, empty or older aggregate is never published to gh-pages.

Each file is validated on its own, against the shapes build_home_stats.py writes (SCHEMA_VERSION 1):
  home_stats.json  schemaVersion, generatedAt (ISO UTC), uniqueCount, sourceCount, portalCounts, cityCounts,
                   oblastCounts, typeCounts, notSoldScoreHistogram
  area_index.json  schemaVersion, areas[{key, label}]        (no generatedAt, and none is required)
Freshness is judged by home_stats.json's generatedAt only.

Usage:
  validate_site_data.py DIR [--not-older-than PUBLISHED_DIR] [--generated-after ISO_UTC]

Exit codes:
  0  valid (and fresh enough)
  1  invalid or missing file
  3  valid, but OLDER than the copy already published in PUBLISHED_DIR (never go backwards)
  4  valid, but generatedAt is before --generated-after (the scrape that triggered us did not regenerate it)
"""
import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

EXIT_OK = 0
EXIT_INVALID = 1
EXIT_OLDER_THAN_PUBLISHED = 3
EXIT_NOT_REGENERATED = 4

TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
COUNT_MAPS = ("portalCounts", "cityCounts", "oblastCounts", "typeCounts")
FUTURE_SKEW = timedelta(minutes=5)


def parse_ts(value):
    """Strict 'YYYY-MM-DDTHH:MM:SSZ' -> aware UTC datetime; raises ValueError otherwise."""
    if not isinstance(value, str):
        raise ValueError("not a string")
    return datetime.strptime(value, TS_FORMAT).replace(tzinfo=timezone.utc)


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _check_count_map(name, value, errors, allow_empty):
    if not isinstance(value, dict):
        errors.append(f"{name} must be an object")
        return
    if not value and not allow_empty:
        errors.append(f"{name} must not be empty")
    for k, v in value.items():
        if not isinstance(k, str):
            errors.append(f"{name} has a non-string key")
            break
        if not _is_int(v) or v < 0:
            errors.append(f"{name}[{k!r}] must be a non-negative integer")
            break


def validate_home_stats(obj, now=None):
    """Returns (errors, generated_at). generated_at is None when it could not be read."""
    now = now or datetime.now(timezone.utc)
    errors = []
    generated_at = None
    if not isinstance(obj, dict):
        return ["top level must be an object"], None
    if not _is_int(obj.get("schemaVersion")) or obj["schemaVersion"] != 1:
        errors.append(f"schemaVersion must be 1 (got {obj.get('schemaVersion')!r})")
    if "generatedAt" not in obj:
        errors.append("generatedAt is missing")
    else:
        try:
            generated_at = parse_ts(obj["generatedAt"])
            if generated_at > now + FUTURE_SKEW:
                errors.append(f"generatedAt {obj['generatedAt']} is in the future")
        except ValueError:
            errors.append(f"generatedAt must look like 2026-11-03T09:06:47Z (got {obj['generatedAt']!r})")
    unique, source = obj.get("uniqueCount"), obj.get("sourceCount")
    if not _is_int(unique) or unique <= 0:
        errors.append("uniqueCount must be a positive integer")
    if not _is_int(source) or source < 0:
        errors.append("sourceCount must be a non-negative integer")
    elif _is_int(unique) and source < unique:
        errors.append("sourceCount must not be smaller than uniqueCount")
    for name in COUNT_MAPS:
        _check_count_map(name, obj.get(name), errors, allow_empty=False)
    _check_count_map("notSoldScoreHistogram", obj.get("notSoldScoreHistogram"), errors, allow_empty=True)
    return errors, generated_at


def validate_area_index(obj):
    errors = []
    if not isinstance(obj, dict):
        return ["top level must be an object"]
    if not _is_int(obj.get("schemaVersion")) or obj["schemaVersion"] != 1:
        errors.append(f"schemaVersion must be 1 (got {obj.get('schemaVersion')!r})")
    areas = obj.get("areas")
    if not isinstance(areas, list) or not areas:
        errors.append("areas must be a non-empty list")
        return errors
    for i, a in enumerate(areas):
        if (not isinstance(a, dict) or not isinstance(a.get("key"), str) or not a["key"]
                or not isinstance(a.get("label"), str) or not a["label"]):
            errors.append(f"areas[{i}] must be an object with non-empty string key and label")
            break
    return errors


def load_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        raise ValueError("file is missing")
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise ValueError(f"not valid JSON ({e})")


def validate_dir(directory, now=None):
    """Validates both files in `directory`. Returns (problems, home_stats_dict_or_None, generated_at, area_count)."""
    directory = Path(directory)
    problems = []
    stats = None
    generated_at = None
    area_count = 0
    try:
        stats = load_json(directory / "home_stats.json")
        errs, generated_at = validate_home_stats(stats, now)
        problems += [f"home_stats.json: {e}" for e in errs]
    except ValueError as e:
        problems.append(f"home_stats.json: {e}")
    try:
        areas = load_json(directory / "area_index.json")
        errs = validate_area_index(areas)
        problems += [f"area_index.json: {e}" for e in errs]
        if not errs:
            area_count = len(areas["areas"])
    except ValueError as e:
        problems.append(f"area_index.json: {e}")
    return problems, stats, generated_at, area_count


def published_generated_at(directory):
    """generatedAt of the already-published home_stats.json, or None when absent/unreadable (then nothing to protect)."""
    try:
        errs, ts = validate_home_stats(load_json(Path(directory) / "home_stats.json"), now=datetime.max.replace(tzinfo=timezone.utc) - timedelta(days=1))
    except ValueError:
        return None
    return ts if not errs else None


def main(argv=None, now=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("directory", type=Path)
    p.add_argument("--not-older-than", type=Path, metavar="PUBLISHED_DIR")
    p.add_argument("--generated-after", metavar="ISO_UTC")
    args = p.parse_args(argv)

    problems, stats, generated_at, area_count = validate_dir(args.directory, now)
    if problems:
        for pr in problems:
            print(f"INVALID {pr}")
        return EXIT_INVALID
    print(f"OK home_stats generatedAt={stats['generatedAt']} uniqueCount={stats['uniqueCount']} "
          f"sourceCount={stats['sourceCount']} areas={area_count}")

    if args.generated_after:
        try:
            floor = parse_ts(args.generated_after)
        except ValueError:
            print(f"INVALID --generated-after {args.generated_after!r} is not YYYY-MM-DDTHH:MM:SSZ")
            return EXIT_INVALID
        if generated_at < floor:
            print(f"STALE generatedAt {stats['generatedAt']} is before {args.generated_after}: "
                  "the scrape did not regenerate the stats (build_home_stats.py is continue-on-error)")
            return EXIT_NOT_REGENERATED

    if args.not_older_than:
        published = published_generated_at(args.not_older_than)
        if published is None:
            print(f"NOTE no valid published home_stats.json in {args.not_older_than}; nothing to compare against")
        elif generated_at < published:
            print(f"OLDER generatedAt {stats['generatedAt']} is older than the published {published.strftime(TS_FORMAT)}")
            return EXIT_OLDER_THAN_PUBLISHED
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
