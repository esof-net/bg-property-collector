# Working in this repo (esof-net/bg-property-collector)

Read this before touching GitHub Actions or the shared working directory - both have caused real, user-visible problems this session.

## Never iterate by repeatedly dispatching a live GitHub Actions workflow

Every failed `workflow_dispatch` run sends the repo owner a failure email. A "dispatch it, watch it fail, patch one line, dispatch again" debugging loop is genuinely disruptive to them even when each individual run is harmless - this happened for real (5 failed runs of one diagnostic script in ~35 minutes, all from iterating live instead of validating first) and the owner had to tell us directly to stop.

Before any live dispatch of a new or changed script:
- Read it end to end for the failure modes you can already see. The specific bugs that caused real failures this session were all things a careful read would have caught: an unhandled HTTP error with no response body logged (so the first failure taught nothing), a slow aggregate query (`count=exact`) with no statement-timeout fallback, a column referenced that doesn't exist in production yet.
- Dry-run or syntax-check whatever you can locally first.
- Get it right before it touches production Actions - one live dispatch that works beats five that iterate you there.

If a live dispatch still fails despite that care, fix the *specific* confirmed cause from its actual logs before dispatching again - don't treat redispatch itself as a debugging step.

This applies to every agent working here, not just one role - general-purpose builders have caused this as often as named agents.

## This working directory is shared with other concurrently-running agents

More than one agent session can be operating in `/home/user/bg-property-collector` (or wherever this repo is checked out) at the same time, each able to see the others' in-progress, uncommitted changes. Before committing or discarding anything:
- Run `git status`/`git log --oneline -5` first. If you see uncommitted changes you didn't make, leave them alone unless they're clearly unrelated and you're only committing your own specific files.
- For anything beyond a trivial docs edit, prefer building in an isolated `git worktree` off `origin/main` (or the relevant branch) rather than working directly in the shared checkout - this has already prevented real collisions this session.
- Never `git add -A` in a shared checkout.

## Nothing ships without Missy's review

Every code change - however small, however confident you are - goes through Missy's review before merging, per each agent's own standing rules. Self-merging is a real mistake that's happened this session (caught and corrected each time, but avoid it in the first place).

## The website is published from `gh-pages`, not from `main` (prepared, not active until the owner finishes the checklist)

`imotenradar.com` is served by GitHub Pages (Source: GitHub Actions) from the **built** single-page app, which the private repo `esof-net/bg-property-tracker` builds with Vite and pushes to this repo's orphan **`gh-pages`** branch. The old raw `index.html` on `main` is the legacy site and stays only as a rollback target.

- **`main`** = scrapers and data. **`gh-pages`** = built site (`index.html`, `_vite/`, `assets/`, `CNAME`, `.nojekyll`, `release.json`) + `data/home_stats.json` + `data/area_index.json` + `.github/workflows/deploy-on-push.yml`. Never edit `gh-pages` by hand and never put scraper files on it.
- **Who writes what on `gh-pages`:** the private repo's deploy key writes the frontend; `publish-site-data.yml` writes only the two data files (copied from `main` after a successful scrape, validated by `scripts/validate_site_data.py`); nobody's sync touches `.github/`.
- **`deploy-site.yml`** deploys the current `gh-pages` tip to Pages. **`publish-site-data.yml`**, **`deploy-site.yml`** and the `gh-pages` stub (`site/deploy-on-push.yml` on `main` is its reviewed source) are **not scrapers**: they never change scraper behaviour, schedules or `scripts/commit_and_push.sh`, and a failure there is a site-deploy finding, not scraper health.
- Both stay inert until the repository variable `PUBLIC_SITE_LIVE` is `true` (set only at cutover). The same rules as above apply to them: do not dispatch them in a loop, read them end to end before a live run, and ask the owner before any push to `gh-pages`, dispatch or Pages setting change.
- Full procedure, acceptance tests and rollback (`scripts/rollback_frontend.sh`): `docs/public-site-deploy.md` in the private repo.
