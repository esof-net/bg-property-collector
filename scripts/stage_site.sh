#!/usr/bin/env bash
# Checks that a gh-pages checkout is a COMPLETE site and copies it to an output folder (without .git / .github).
# Used by deploy-site.yml before anything is uploaded to Pages. NOT a scraper.
#
# Usage: stage_site.sh <gh-pages-checkout> <out-dir>
set -euo pipefail

src="${1:?usage: stage_site.sh <gh-pages-checkout> <out-dir>}"
out="${2:?usage: stage_site.sh <gh-pages-checkout> <out-dir>}"
here="$(cd "$(dirname "$0")" && pwd)"

fail() { echo "::error::$*"; exit 1; }

[ -s "$src/index.html" ] || fail "index.html is missing: gh-pages holds no built site yet"
[ -f "$src/.nojekyll" ] || fail ".nojekyll is missing from gh-pages"
[ "$(tr -d '[:space:]' < "$src/CNAME" 2>/dev/null || true)" = "imotenradar.com" ] || fail "CNAME is missing or is not imotenradar.com"

python3 - "$src/release.json" <<'PY' || exit 1
import json, re, sys
try:
    r = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception as e:
    print(f"::error::release.json is missing or not valid JSON ({e})"); sys.exit(1)
if r.get("repo") != "esof-net/bg-property-tracker" or not re.fullmatch(r"[0-9a-f]{40}", str(r.get("commit", ""))) or not r.get("committedAt"):
    print("::error::release.json must hold repo esof-net/bg-property-tracker, a 40-hex commit and committedAt"); sys.exit(1)
print(f"release {r['commit']} (committed {r['committedAt']})")
PY

assets="$(grep -oE '/_vite/[^"'"'"' )>]+' "$src/index.html" | sort -u || true)"
[ -n "$assets" ] || fail "index.html references no /_vite/ assets"
for a in $assets; do
  [ -s "$src$a" ] || fail "index.html references $a but the file is missing from gh-pages"
done
echo "$(echo "$assets" | wc -l) /_vite/ assets present."

python3 "$here/validate_site_data.py" "$src/data" || fail "data/home_stats.json or data/area_index.json is invalid (see above)"

rm -rf "$out"
mkdir -p "$out"
rsync -a --exclude=/.git --exclude=/.github "$src"/ "$out"/
echo "Staged $(find "$out" -type f | wc -l) files in $out."
