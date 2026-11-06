"""Tests for scripts/validate_site_data.py (the gate in front of gh-pages/data/*.json).

Fixtures in tests/fixtures/site_data/ are copies of the files committed on main (home_stats.json whole, area_index.json
trimmed to 20 areas). When the full real files are available (data/ in a full checkout, or SITE_DATA_DIR) they are
validated too. Run:  python -m unittest tests.test_validate_site_data
"""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import validate_site_data as v  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "site_data"
NOW = datetime(2026, 11, 3, 12, 0, 0, tzinfo=timezone.utc)  # fixture generatedAt is 2026-11-03T09:06:47Z


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class SiteDataCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def site(self, name="site", stats=None, areas=None, raw_stats=None, raw_areas=None, skip=()):
        d = self.tmp / name
        d.mkdir(parents=True, exist_ok=True)
        if "home_stats.json" not in skip:
            (d / "home_stats.json").write_text(raw_stats if raw_stats is not None else json.dumps(stats or load("home_stats.json")), encoding="utf-8")
        if "area_index.json" not in skip:
            (d / "area_index.json").write_text(raw_areas if raw_areas is not None else json.dumps(areas or load("area_index.json")), encoding="utf-8")
        return d

    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = v.main([str(a) for a in argv], now=NOW)
        return code, out.getvalue()

    def stats_with(self, **changes):
        s = load("home_stats.json")
        for k, val in changes.items():
            if val is KeyError:
                s.pop(k, None)
            else:
                s[k] = val
        return s


class RealSchemas(SiteDataCase):
    def test_committed_fixtures_are_valid(self):
        code, out = self.run_cli(FIXTURES)
        self.assertEqual(code, v.EXIT_OK, out)
        self.assertIn("generatedAt=2026-11-03T09:06:47Z", out)

    def test_area_index_has_no_generated_at_and_needs_none(self):
        areas = load("area_index.json")
        self.assertNotIn("generatedAt", areas)
        self.assertEqual(v.validate_area_index(areas), [])
        areas["generatedAt"] = "garbage"  # an extra field is ignored, never required or compared
        self.assertEqual(v.validate_area_index(areas), [])

    def test_full_real_files_when_available(self):
        real = Path(os.environ.get("SITE_DATA_DIR") or ROOT / "data")
        if not (real / "home_stats.json").exists() or not (real / "area_index.json").exists():
            self.skipTest("full data files not present (set SITE_DATA_DIR or run in a full checkout)")
        problems, stats, _, areas = v.validate_dir(real)
        self.assertEqual(problems, [], "\n".join(problems))
        self.assertGreater(areas, 0)


class MalformedHomeStats(SiteDataCase):
    def assertInvalid(self, stats=None, raw=None, contains=""):
        d = self.site(stats=stats, raw_stats=raw)
        code, out = self.run_cli(d)
        self.assertEqual(code, v.EXIT_INVALID, out)
        self.assertIn(contains, out)

    def test_not_json(self):
        self.assertInvalid(raw="{", contains="home_stats.json: not valid JSON")

    def test_empty_file(self):
        self.assertInvalid(raw="", contains="not valid JSON")

    def test_not_an_object(self):
        self.assertInvalid(raw="[]", contains="top level must be an object")

    def test_wrong_schema_versions(self):
        for bad in (2, 0, "1", True, None):
            with self.subTest(schemaVersion=bad):
                self.assertInvalid(stats=self.stats_with(schemaVersion=bad), contains="schemaVersion must be 1")

    def test_generated_at_missing_malformed_or_future(self):
        self.assertInvalid(stats=self.stats_with(generatedAt=KeyError), contains="generatedAt is missing")
        self.assertInvalid(stats=self.stats_with(generatedAt="2026-11-03"), contains="generatedAt must look like")
        self.assertInvalid(stats=self.stats_with(generatedAt="2026-11-03T09:06:47+00:00"), contains="generatedAt must look like")
        self.assertInvalid(stats=self.stats_with(generatedAt="2026-11-04T09:06:47Z"), contains="in the future")

    def test_counts(self):
        self.assertInvalid(stats=self.stats_with(uniqueCount=0), contains="uniqueCount")
        self.assertInvalid(stats=self.stats_with(uniqueCount=True), contains="uniqueCount")
        self.assertInvalid(stats=self.stats_with(uniqueCount=10, sourceCount=5), contains="sourceCount must not be smaller")

    def test_count_maps(self):
        for name in v.COUNT_MAPS:
            with self.subTest(name=name):
                self.assertInvalid(stats=self.stats_with(**{name: {}}), contains=f"{name} must not be empty")
                self.assertInvalid(stats=self.stats_with(**{name: KeyError}), contains=f"{name} must be an object")
        self.assertInvalid(stats=self.stats_with(cityCounts={"sofia": -1}), contains="non-negative integer")
        self.assertInvalid(stats=self.stats_with(cityCounts={"sofia": "5"}), contains="non-negative integer")
        self.assertInvalid(stats=self.stats_with(cityCounts={"sofia": True}), contains="non-negative integer")
        self.assertInvalid(stats=self.stats_with(notSoldScoreHistogram=KeyError), contains="notSoldScoreHistogram must be an object")


class MalformedAreaIndex(SiteDataCase):
    def assertInvalid(self, areas=None, raw=None, skip=(), contains=""):
        d = self.site(areas=areas, raw_areas=raw, skip=skip)
        code, out = self.run_cli(d)
        self.assertEqual(code, v.EXIT_INVALID, out)
        self.assertIn(contains, out)

    def test_missing_and_malformed(self):
        self.assertInvalid(skip=("area_index.json",), contains="area_index.json: file is missing")
        self.assertInvalid(raw="{", contains="area_index.json: not valid JSON")

    def test_schema_and_areas(self):
        self.assertInvalid(areas={"schemaVersion": 2, "areas": [{"key": "a", "label": "A"}]}, contains="schemaVersion must be 1")
        self.assertInvalid(areas={"schemaVersion": 1, "areas": []}, contains="areas must be a non-empty list")
        self.assertInvalid(areas={"schemaVersion": 1}, contains="areas must be a non-empty list")
        self.assertInvalid(areas={"schemaVersion": 1, "areas": [{"key": "a"}]}, contains="areas[0]")
        self.assertInvalid(areas={"schemaVersion": 1, "areas": [{"key": "", "label": "A"}]}, contains="areas[0]")
        self.assertInvalid(areas={"schemaVersion": 1, "areas": [{"key": "a", "label": 5}]}, contains="areas[0]")

    def test_each_file_is_judged_on_its_own(self):
        d = self.site(raw_stats="{", areas={"schemaVersion": 1, "areas": []})
        code, out = self.run_cli(d)
        self.assertEqual(code, v.EXIT_INVALID)
        self.assertIn("home_stats.json", out)
        self.assertIn("area_index.json", out)

    def test_missing_home_stats(self):
        d = self.site(skip=("home_stats.json",))
        code, out = self.run_cli(d)
        self.assertEqual(code, v.EXIT_INVALID)
        self.assertIn("home_stats.json: file is missing", out)


class Freshness(SiteDataCase):
    def at(self, ts):
        return self.stats_with(generatedAt=ts)

    def test_older_than_published_is_refused(self):
        new = self.site("new", stats=self.at("2026-11-03T06:00:00Z"))
        published = self.site("pub", stats=self.at("2026-11-03T09:06:47Z"))
        code, out = self.run_cli(new, "--not-older-than", published)
        self.assertEqual(code, v.EXIT_OLDER_THAN_PUBLISHED, out)

    def test_newer_or_equal_is_accepted(self):
        published = self.site("pub", stats=self.at("2026-11-03T06:00:00Z"))
        self.assertEqual(self.run_cli(self.site("new", stats=self.at("2026-11-03T09:06:47Z")), "--not-older-than", published)[0], v.EXIT_OK)
        self.assertEqual(self.run_cli(self.site("same", stats=self.at("2026-11-03T06:00:00Z")), "--not-older-than", published)[0], v.EXIT_OK)

    def test_nothing_published_yet_is_fine(self):
        empty = self.tmp / "empty"
        empty.mkdir()
        code, out = self.run_cli(self.site("new"), "--not-older-than", empty)
        self.assertEqual(code, v.EXIT_OK, out)
        self.assertIn("nothing to compare", out)

    def test_corrupt_published_copy_can_be_replaced(self):
        published = self.site("pub", raw_stats="{")
        self.assertEqual(self.run_cli(self.site("new"), "--not-older-than", published)[0], v.EXIT_OK)

    def test_invalid_data_is_rejected_before_freshness(self):
        published = self.site("pub", stats=self.at("2026-11-03T06:00:00Z"))
        bad = self.site("bad", raw_stats="{")
        self.assertEqual(self.run_cli(bad, "--not-older-than", published)[0], v.EXIT_INVALID)

    def test_not_regenerated_by_the_triggering_scrape(self):
        d = self.site()  # generatedAt 09:06:47Z
        self.assertEqual(self.run_cli(d, "--generated-after", "2026-11-03T06:00:00Z")[0], v.EXIT_OK)
        self.assertEqual(self.run_cli(d, "--generated-after", "2026-11-03T10:00:00Z")[0], v.EXIT_NOT_REGENERATED)
        self.assertEqual(self.run_cli(d, "--generated-after", "yesterday")[0], v.EXIT_INVALID)

    def test_area_index_age_is_never_compared(self):
        published = self.site("pub", areas={"schemaVersion": 1, "areas": [{"key": "z", "label": "Z"}]})
        self.assertEqual(self.run_cli(self.site("new"), "--not-older-than", published)[0], v.EXIT_OK)


if __name__ == "__main__":
    unittest.main()
