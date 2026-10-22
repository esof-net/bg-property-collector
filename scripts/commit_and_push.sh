#!/bin/bash
# Reusable commit+push helper with the same conflict-retry logic scrape.yml
# and the other automated workflows in this repo use (see docs/decisions.md,
# 2026-09-20 entry: "git-push retry loop: rebase can't even start if tree
# still dirty after commit") - handles concurrent pushes from the many
# other automated jobs that write to this repo, including the residual-
# commit fix for when the tree is unexpectedly still dirty right after the
# main commit (which used to make this retry loop fail before a real
# rebase attempt even started).
#
# Usage: commit_and_push.sh "<commit message>" <path> [<path> ...]
set -e

if [ $# -lt 2 ]; then
  echo "Usage: $0 \"<commit message>\" <path> [<path> ...]" >&2
  exit 1
fi

MSG="$1"
shift

git config user.name "property-bot"
git config user.email "bot@users.noreply.github.com"

pushed=false
for attempt in 1 2 3 4 5; do
  git add "$@"
  git diff --cached --quiet || git commit -m "$MSG"
  # See docs/decisions.md 2026-09-20: rebase can refuse to even start if
  # the tree is unexpectedly still dirty after the commit above - commit
  # whatever's left rather than let that block the retry loop entirely.
  if [ -n "$(git status --porcelain)" ]; then
    echo "Tree still dirty after the commit above - committing the remainder before rebase"
    git add -A
    git commit -m "$MSG (residual)"
  fi
  if ! git pull --rebase origin main; then
    echo "Rebase hit a content conflict - resolving each file"
    # For the paths THIS call is committing, keep our new content (--theirs
    # during a rebase means "the commit being replayed", i.e. ours) - a
    # blanket --ours here would silently keep main's older version of the
    # very file we're trying to add/update and still report success, e.g.
    # if this script fires twice for the same day's findings file (see
    # docs/decisions.md 2026-09-21: exactly the "reports success but did
    # nothing" bug class Missy's audit looks for). For every other
    # conflicted file (unrelated to this call, just present in a dirty
    # tree), keep main's version, since that's always the safe default.
    for f in $(git diff --name-only --diff-filter=U); do
      is_target=false
      for target in "$@"; do
        [ "$f" = "$target" ] && is_target=true && break
      done
      if [ "$is_target" = "true" ]; then
        echo "  keeping OUR new content for $f (one of this call's own target paths)"
        git checkout --theirs -- "$f"
      else
        echo "  keeping main's version of $f (unrelated to this call)"
        git checkout --ours -- "$f"
      fi
      git add "$f"
    done
    if ! GIT_EDITOR=true git rebase --continue; then
      echo "rebase --continue still failed after auto-resolving conflicts - aborting this attempt and retrying from a clean state"
      git rebase --abort || true
      sleep $((attempt * 5))
      continue
    fi
  fi
  if git push; then
    pushed=true
    break
  fi
  echo "Push rejected (attempt $attempt/5) - re-pulling and retrying"
  sleep $((attempt * 5))
done

if [ "$pushed" != "true" ]; then
  echo "::error::Failed to push after 5 attempts - giving up"
  exit 1
fi
echo "Pushed successfully."
