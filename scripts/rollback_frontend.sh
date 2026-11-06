#!/usr/bin/env bash
# Rolls the FRONTEND on a gh-pages checkout back to a known-good commit while KEEPING the current statistics.
# A plain `git reset` of gh-pages would also roll back newer data/home_stats.json + data/area_index.json and, on its
# own, does not change the live site (only a deploy-site run does). This script:
#   - restores every frontend file (index.html, _vite/, assets/, CNAME, .nojekyll, release.json) from <known-good-sha>
#   - removes frontend files added since then
#   - leaves data/home_stats.json, data/area_index.json and .github/ exactly as they are now
#   - validates the result and makes ONE local commit. It does NOT push and does NOT deploy.
# NOT a scraper.
#
# Usage (in a clone made with `git clone --branch gh-pages ...`): rollback_frontend.sh <known-good-sha> [checkout-dir=.]
set -euo pipefail

sha="${1:?usage: rollback_frontend.sh <known-good-sha> [checkout-dir]}"
dir="${2:-.}"
here="$(cd "$(dirname "$0")" && pwd)"
cd "$dir"

fail() { echo "ERROR: $*" >&2; exit 1; }

git rev-parse --verify -q "$sha^{commit}" >/dev/null || fail "$sha is not a commit in this clone"
[ "$(git rev-parse --abbrev-ref HEAD)" = "gh-pages" ] || fail "run this in a gh-pages checkout"
[ -z "$(git status --porcelain)" ] || fail "the working tree is not clean"
git fetch -q origin gh-pages
[ "$(git rev-parse HEAD)" = "$(git rev-parse origin/gh-pages)" ] || fail "local gh-pages differs from origin/gh-pages; pull first"
git cat-file -e "$sha:release.json" 2>/dev/null || fail "$sha has no release.json, so it is not a frontend release commit"

is_kept() { case "$1" in .github/*|data/home_stats.json|data/area_index.json) return 0;; *) return 1;; esac; }

# 1. remove frontend files that did not exist in the known-good commit
while IFS= read -r -d '' f; do
  is_kept "$f" && continue
  git cat-file -e "$sha:$f" 2>/dev/null || git rm -q -- "$f"
done < <(git ls-tree -r -z --name-only HEAD)

# 2. restore every frontend file from the known-good commit
restore=()
while IFS= read -r -d '' f; do
  is_kept "$f" && continue
  restore+=("$f")
done < <(git ls-tree -r -z --name-only "$sha")
[ "${#restore[@]}" -gt 0 ] || fail "$sha contains no frontend files"
git checkout "$sha" -- "${restore[@]}"

python3 "$here/validate_site_data.py" data || fail "the preserved data files are invalid; nothing committed"

commit="$(python3 -c 'import json;print(json.load(open("release.json"))["commit"])')"
git add -A
if git diff --cached --quiet; then
  echo "gh-pages already holds release $commit; nothing to roll back."
  exit 0
fi
git -c user.name="${GIT_AUTHOR_NAME:-property-bot}" -c user.email="${GIT_AUTHOR_EMAIL:-bot@users.noreply.github.com}" \
  commit -q -m "Rollback frontend to release $commit (from $(git rev-parse --short "$sha"))"
git show --stat --oneline HEAD | head -20
echo
echo "Committed locally. Review it, then push (git push origin gh-pages) and run the deploy-site workflow."
