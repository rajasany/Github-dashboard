# Repo Change Dashboard

One pane showing commit activity across **all branches** of the repositories you track —
on **GitHub** and **Google Cloud Source Repositories** — merged into a single feed.

FastAPI backend + a single static page. Credentials stay server-side; the browser only
ever talks to this app.

## Setup

```bash
cp .env.example .env                 # add your GITHUB_TOKEN
cp config.example.yaml config.yaml   # list the repos to track
./run.sh                             # → http://127.0.0.1:8000
```

### GitHub

The token needs read access to the repos you list — a classic PAT with the `repo`
scope, or a fine-grained PAT with **Contents: read-only** and **Metadata: read-only**.
Create one at https://github.com/settings/tokens.

The token is optional for **public** repos: without one the app runs unauthenticated
at 60 API requests/hour and shows a warning banner. Private repos and a 5000/hour
limit require a token.

### Google Cloud Source Repositories

```bash
gcloud auth login          # once; the dashboard reuses these credentials
```

Then list your repos under `gcloud:` in `config.yaml`. Nothing goes in `.env` — the
app calls `gcloud auth print-access-token` itself and caches the result for 45 minutes.
Set `GCLOUD_ACCOUNT` if several accounts are logged in, or `GCLOUD_ACCESS_TOKEN` to
supply a token directly (useful in CI with a service account).

**How it works, and why it's different from GitHub.** CSR has no REST API for
branches or commit history — `sourcerepo.googleapis.com` only lists, creates, and
deletes repositories. So the dashboard reads CSR history from git itself: a bare
`git clone --mirror` into `.cache/mirrors/`, refreshed with `git fetch --prune` on
each dashboard refresh, then queried with `for-each-ref` and `git log`.

Consequences worth knowing:

- **First load is slow** for a large repo — it's a full mirror clone. Later refreshes
  are incremental fetches.
- **Disk cost** — roughly the size of the repo's history, per repo, under `.cache/`.
  Safe to delete; it re-clones on next refresh.
- **Cheaper than GitHub per refresh** — one `git fetch` regardless of branch count,
  versus GitHub's one API call per branch.
- **Stale branches are skipped for free**, because local git exposes each branch's tip
  date. The GitHub REST API doesn't, so GitHub spends a call per branch either way.
- The git token is passed to git via `GIT_CONFIG_*` environment variables, not argv,
  so it doesn't show up in `ps` output.

> Note: Google [deprecated Cloud Source Repositories](https://cloud.google.com/source-repositories/docs/deprecations)
> — it's closed to new customers since June 2024. The provider here is plain git
> against a remote, so the same code path works for any git host if you migrate.

## What it shows

- **Unified commit feed** across every branch of every configured repo and both
  providers, newest first. A commit reachable from several branches appears **once**,
  tagged with every branch it was found on — so the default branch doesn't duplicate
  the feature branch it merged.
- **Folder / microservice attribution** — each commit is tagged with the folder(s)
  whose files it changed, so you can see per-service activity in a monorepo. See below.
- **Stat tiles** — commits, repos with activity, services touched, active branches,
  contributors, and how many commits are not yet on the default branch.
- **Drill-down picker** — select a source, then a repository, and the panels below show
  that repository's services and branches. One selection per level, no checkboxes; an
  "All …" row at the top of each list clears that level. Plus free-text search over
  message / author / SHA / folder, and a "Show" menu to isolate commits not yet on the
  default branch.
- **Grouping** — by day (default), by repository, or by service/folder.
- **Lookback window** — 24 hours to 90 days (API accepts up to 365).
- **Rate-limit readout** for GitHub in the header.
- Per-repo failures are reported in a banner without taking the rest of the feed down.

## Folder / microservice tracking

Configured under `folders:` in `config.yaml`. Each commit's changed paths are reduced
to owning folders:

| Layout | Config | `services/auth/main.py` becomes |
| --- | --- | --- |
| Services at top level (`auth/`, `billing/`) | `depth: 1` | `services` |
| Services under a prefix | `paths: ["services/*"]` | `services/auth` |
| Deeper nesting | `depth: 2` | `services/auth` |

- **Folders are scoped to their repository.** A folder only means something inside the
  repo that contains it, so identity is the pair `(repo, folder)` — a `backend/` in two
  repositories is two different services, counted and selected separately, never merged
  into one entry. With no repository selected the services list groups entries under a
  repo header so same-named folders stay visibly distinct.
- A commit touching several services is tagged with **all** of them, and appears under
  each when grouping by folder. Selecting one service narrows its chips to that service.
- Files at the repo root collapse to `(repo root)` rather than being attributed to a
  service.
- `exclude` globs drop non-service folders (`__pycache__`, `node_modules`, `.github`).
  They match a whole folder or any single segment, so `__pycache__` also drops
  `api/__pycache__`.
- Merge commits are attributed against their first parent, so both providers agree.

**Cost.** This is where the two providers differ most:

- **CSR is free** — `git log --name-only` returns file lists in the call already being
  made.
- **GitHub costs one extra API call per commit**, because the list-commits endpoint
  carries no file list. Those results are immutable, so they're cached permanently in
  `.cache/commit-files.sqlite3` — a one-time cost per commit, not per refresh. A cold
  load of 16 commits spends 16 calls; every refresh after that spends 0.

Raw file paths are cached rather than derived folder names, so changing `depth`,
`paths`, or `exclude` re-buckets everything instantly with **no** new API calls.
Set `folders.enabled: false` to skip file lookups entirely.

GitHub caps a commit's `files` list at 300 entries; commits above that are marked with
a `+` on the file count and may under-report folders.

## Configuration

`.env`

| Variable | Default | Purpose |
| --- | --- | --- |
| `GITHUB_TOKEN` | — | Personal access token. Optional for public repos. |
| `GITHUB_API_BASE` | `https://api.github.com` | Point at GitHub Enterprise if needed. |
| `GCLOUD_ACCOUNT` | — | Which gcloud account to use, if several are logged in. |
| `GCLOUD_ACCESS_TOKEN` | — | Use this OAuth token instead of calling gcloud. |
| `MIRROR_DIR` | `./.cache/mirrors` | Where CSR mirror clones live. |
| `GIT_TIMEOUT_SECONDS` | `240` | Timeout for any single git operation. |
| `CACHE_TTL_SECONDS` | `120` | Server-side GitHub response cache. **Refresh** clears it. |
| `MAX_CONCURRENCY` | `8` | Parallel in-flight requests. |
| `HOST` / `PORT` | `127.0.0.1` / `8000` | Bind address for `run.sh`. |

`config.yaml`

- `repos` — GitHub `owner/repo` entries (full URLs are also accepted).
- `gcloud.project` — default GCP project for bare repo names.
- `gcloud.repos` — bare names, `{project, repo}` dicts, or pasted clone URLs.
- `defaults.days` / `defaults.commits_per_branch` — initial window and per-branch cap.
- `branch_include` / `branch_exclude` — glob patterns applied to both providers.
- `folders.enabled` / `folders.depth` / `folders.paths` / `folders.exclude` — see
  [Folder / microservice tracking](#folder--microservice-tracking).

## API

| Endpoint | Description |
| --- | --- |
| `GET /api/config` | Tracked repos, providers, and defaults, for the UI to bootstrap. |
| `GET /api/feed?days=14&key=github:owner/repo&refresh=false` | The merged feed. `key` may repeat; values are `github:owner/repo` or `csr:project/repo`. |
| `GET /api/health` | Liveness + configured repo counts per provider. |

## Layout

| File | Role |
| --- | --- |
| [app/models.py](app/models.py) | Normalised commit/repo shapes both providers emit. |
| [app/github.py](app/github.py) | GitHub REST client + per-repo collection. |
| [app/csr.py](app/csr.py) | gcloud token handling, git mirror, CSR collection. |
| [app/paths.py](app/paths.py) | Changed paths → owning folder/service. |
| [app/store.py](app/store.py) | SQLite cache of commit → file paths. |
| [app/feed.py](app/feed.py) | Merges providers, dedupes by `(repo, sha)`, derives folders. |
| [app/main.py](app/main.py) | Routes. |
| [app/static/](app/static/) | The single-page UI. |
| [tests/](tests/) | Path-attribution and filter-cascade tests. |

## Selecting your way through the data

The sidebar is a drill-down, not a set of checkboxes. Each panel holds a single-select
list; the leading **All …** row clears that level.

```
Source        → All sources | GitHub | Cloud Source Repos
  Repository  → All repositories | acme/shop | gcp-proj/pay
    Service   → All services | backend | frontend        (of the selected repo)
    Branch    → All branches | main | feature/x          (of the selected repo)
      Author  → All authors | alice | bob
```

Each list is built only from commits matching the selections **above** it, so picking a
repository is what scopes its services and branches. Every row shows a commit count for
the current scope, and the active path is echoed next to the search box
(`16 commits · last 14d · acme/shop › backend › main`).

Selections that stop making sense are dropped automatically: switching from a repo where
you'd selected `backend` to one without it clears the service, while a branch like `main`
that exists in both is kept.

## Tests

```bash
.venv/bin/python tests/test_paths.py   # changed paths -> owning folder   (16 checks)
node tests/ui_cascade.test.js          # drill-down selection behaviour   (23 checks)
```

The second suite runs the real `app/static/app.js` in a stubbed DOM and asserts the cases
that matter: selecting a repository scopes the folder and branch lists to it; two
repositories each containing a `backend/` folder stay separate; stale downstream
selections are pruned on repo switch; and the lists render single-select buttons with no
checkbox inputs.

## Known limits of this version

- GitHub commits per branch are capped at one API page (max 100). A very busy branch
  over a long window will be truncated — the feed is a recent-activity view, not a
  full history export. CSR has no such cap beyond `commits_per_branch`.
- No ahead/behind counts vs the default branch, and no pull request state yet.
- CSR commits have no avatar or profile link — git only carries a name and email.
- Folder attribution is per-commit, not per-line: a commit that touches two services
  counts once for each, so per-service commit counts sum to more than the feed total.
- The GitHub HTTP cache and gcloud token cache are in-process; restarting clears them.
  Mirror clones and the commit-file cache persist on disk under `.cache/`.
- Repos are read from `config.yaml` only — there's no UI to add them, and `/api/feed`
  rejects any key not listed there.
