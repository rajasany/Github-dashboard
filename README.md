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

### The Migrations tab

Off until `config.yaml` has a `migrations:` section, and it needs an `auth:` section
too — it is the one part of this app that cares who you are. See
[Migration requests](#migration-requests), and read *Identity and roles* there before
putting it anywhere other people can reach.

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
- **Branch comparison** — pick a base and a head and see what one has that the other
  does not, measured from their merge base.
- **Date range** — pick a period preset or type explicit From / To dates. Leaving
  **To** empty means "everything after this date, through to now".
- **Commit hashes and tags** — full copyable hash per commit, tag chips, and a
  "latest commit" card for the selected branch and folder.
- **Reports** — export exactly what is on screen as a PDF or as PowerPoint slides.
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
| `TAGGER_NAME` / `TAGGER_EMAIL` | — | Credited as the tagger on tags you create. |
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
| `GET /api/feed?since=2026-07-01&until=2026-08-05&key=…&refresh=false` | The merged feed. `since`/`until` are UTC calendar dates; `until` is inclusive and may be omitted for "through to now". `days=N` still works as a lookback when `since` is absent. `key` may repeat; values are `github:owner/repo` or `csr:project/repo`. |
| `GET /api/summary?key=…&branch=…&folder=…&since=…&limit=100` | One row per commit on a branch, with full tag metadata. |
| `POST /api/commits/order` | `{commits, repo_key?, branch?}` → the list validated and ordered newest-first. |
| `GET /api/bulk/template` | A starter `.xlsx` with the expected columns, so they need not be guessed. |
| `GET /api/bulk/placeholders` | The placeholders a tag convention may use, with one line of help each. |
| `POST /api/bulk/process` | Multipart: `file` (.xlsx/.csv, 5 MB cap), `convention`, optional `days`/`since`/`branch`. One summary + proposed tag per sheet row. |
| `GET /api/lookup?sha=…` | Which repos and branches hold a commit, and its position in each. |
| `GET /api/tags/overview?key=…` | One repo's tags with the folders and branches of each. Omit `key` for a cross-repo listing without branch data. |
| `POST /api/tags/stage` | `{repo_key, sha, name, message}` → stage a tag locally. Writes nothing to the remote. |
| `POST /api/tags/push/{id}` | Publish a staged tag. **This is the step that reaches the remote.** |
| `DELETE /api/tags/staged/{id}` | Discard a staged tag that has not been pushed. |
| `GET /api/branches?key=github:owner/repo` | Every branch of one repository, for the compare pickers. |
| `GET /api/compare?key=…&base=main&head=feature/x` | Three-dot comparison: commits, changed files, ahead/behind, merge base. |
| `POST /api/report` | `{format: "pdf"\|"pptx", criteria: {…}, commits: [...]}` → the document as a file download. |
| `GET /api/migrations/meta` | Who you are, your roles, the dropdown lists, the field rules, the active freeze. |
| `GET /api/migrations/records?release=…&microservice=…&status=…&mine=true&created_from=…&created_to=…&sort=date_created&dir=asc` | The register, each row carrying the fields *you* may edit. |
| `POST /api/migrations/records` | Raise a request. Developer role; request-stage fields only. |
| `PATCH /api/migrations/records/{sl}` | Change fields. 403 if the role is wrong, 409 if the workflow is not there yet, 423 if frozen. |
| `GET /api/migrations/records/{sl}/audit` | Every change to one record: when, who, from, to. |
| `GET /api/migrations/template` | The .xlsx template, with the permitted values as real dropdowns. |
| `POST /api/migrations/upload` | Multipart: `file`, `commit`. Validates every row; writes only when `commit` is true. |
| `GET /api/migrations/freezes` | Freeze windows. `POST` to add and `DELETE /{id}` to lift — approvers only. |
| `GET /api/migrations/export.csv` | The whole register as CSV, every field. Takes the same filter and sort parameters, so it matches the screen. |
| `GET /api/health` | Liveness + configured repo counts per provider. |

## Layout

| File | Role |
| --- | --- |
| [app/models.py](app/models.py) | Normalised commit/repo shapes both providers emit. |
| [app/github.py](app/github.py) | GitHub REST client + per-repo collection. |
| [app/csr.py](app/csr.py) | gcloud token handling, git mirror, CSR collection. |
| [app/paths.py](app/paths.py) | Changed paths → owning folder/service. |
| [app/store.py](app/store.py) | SQLite cache of commit → file paths. |
| [app/report.py](app/report.py) | Rollup + PDF and PowerPoint generation. |
| [app/summary.py](app/summary.py) | Repo + branch + folder → the commit/tag table. |
| [app/lookup.py](app/lookup.py) | Hash → repo, branches, graph position. |
| [app/tagging.py](app/tagging.py) | Tag staging, pushing, and the tag overview. |
| [app/ordering.py](app/ordering.py) | Parse a commit list, validate it, order by ancestry. |
| [app/compare.py](app/compare.py) | Branch comparison for both providers. |
| [app/spreadsheet.py](app/spreadsheet.py) | Reads an uploaded .xlsx/.csv; finds the header row, maps columns. |
| [app/naming.py](app/naming.py) | The tag convention: placeholders, validation, next `{n}` in a series. |
| [app/bulk.py](app/bulk.py) | Resolves each sheet row to a repo/folder and summarises it. |
| [app/identity.py](app/identity.py) | Who a tag is attributed to: config, then the signed-in provider account. |
| [app/auth.py](app/auth.py) | Identity from the proxy header, the trusted-proxy guard, role matching. |
| [app/migrations.py](app/migrations.py) | The 28 fields, the workflow gates, the store, the audit trail, freezes. |
| [app/migration_sheet.py](app/migration_sheet.py) | The request template and the upload reader. |
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

## Date range

The top bar carries a **Period** preset plus explicit **From** and **To** dates.

- Presets (last 24 hours / 7 / 14 / 30 / 90 days / 12 months, this month) fill the two
  date boxes. Typing in either box switches the preset to *Custom* on its own.
- **Leaving `To` empty means "through to now"** — that is the "everything after a
  certain date" case, and it keeps working tomorrow without being edited.
  The **To today** button clears it again.
- `To` is **inclusive of the whole day named**. Picking `To = 5 Aug` includes commits
  made at 23:00 on 5 August, which the naive midnight reading would silently drop.
- Both dates are UTC calendar dates. Filtering happens at the source — GitHub's API
  `since`/`until` params and `git log --since/--until` — so commits outside the range
  are never fetched, and a narrow range is genuinely cheaper.

`days=N` still works on the API as a lookback when `since` is absent, and is counted
back from `until` when that is given.

## Comparing two branches

Select a repository, then use **Compare branches** in the sidebar: pick a **Base**
(what you already have) and a **Head** (what you might bring in) and press **Compare**.
The pane switches from the feed to a comparison; **Back to feed** returns.

**The comparison is three-dot** — the diff is measured from the two branches' **merge
base**, not from the tip of `base`. This matters: if `base` has moved on independently,
a two-dot diff would report *its* commits as reversed changes on `head`, which is the
classic way branch comparisons mislead. The merge base is shown in the subtitle so you
can see what it was measured against.

What you get:

- A status word — **ahead / behind / diverged / identical** — plus commits ahead and
  behind, so a branch that is both ahead *and* stale is not mistaken for simply ahead.
- Files changed, lines added and removed, and services touched.
- The changed-file table, sorted added → modified → removed, with per-file line counts.
- The commits that `head` has and `base` does not, rendered as normal feed rows with
  their service and branch chips.
- **⇅ Swap direction** re-runs the comparison the other way, which is what you want when
  a branch turns out to be behind rather than ahead.

Both providers answer it natively:

| | GitHub | Cloud Source Repositories |
| --- | --- | --- |
| Counts | `compare` API `ahead_by` / `behind_by` | `git rev-list --left-right --count base...head` |
| Commits | `compare` API (capped at 250) | `git log base..head` (uncapped) |
| Diff | `compare` API `files` (capped at 300) | `git diff --numstat base...head` |

Caps are reported in the UI rather than silently truncating. Folder attribution for
GitHub comparisons reuses the feed's permanent commit-file cache, so commits already
seen in the feed cost no extra API calls.

## Reports

**Report → PDF / Slides**, next to the result count. The document contains exactly the
rows on screen: the browser posts the commits it is displaying along with the criteria
that produced them, so there is no second copy of the filter logic on the server that
could drift out of step with the UI.

Both formats carry the same material:

| Section | Contents |
| --- | --- |
| Cover | Title, date range, and every filter that is set (source, repository, service, branch, author, "showing", search text, grouping) |
| Summary | Commits, repositories, services, branches, contributors, not-on-default, files changed |
| By repository | Commits, services touched, contributors, files |
| By service / folder | Commits, contributors, files — scoped `repository / folder`, so a `backend` in two repos stays two rows |
| By contributor | Commits, repositories, services, files |
| By day *(PDF)* | Commit counts per day |
| Commit detail | Date, repository, service, branch, author, SHA, files, message |

Every figure is derived from the posted commit list inside `app/report.py`, so the
report and the dashboard cannot disagree. Downloads are named
`repo-changes_<scope>_<from>_to_<to>.pdf`.

Above 400 commits the detail table is cut and the report **says so** on the page —
the totals and breakdowns still cover every commit.

## Tabs

| Tab | What it answers |
| --- | --- |
| **Activity** | What changed recently, across every repo and branch — the filtered feed. |
| **Summary table** | For one repo + branch + folder: every commit with its tag, in a flat table. |
| **Compare branches** | What one branch has that another does not, from their merge base. |
| **Find a commit** | Given a hash: which repo and branches hold it, and how far each has moved on. |
| **Tags** | Every tag in every repo, grouped by the service / folder its commit touched. |
| **Order commits** | Paste a list of commits; it validates and orders them newest-first. |
| **Release plan** | Upload a spreadsheet of repo/folder rows; get each one's latest commit and a proposed tag name. |
| **Migrations** | The SIT → QA → PROD request register: raise, approve, record and freeze. |

The sidebar filters drive the Activity feed only; the other tabs carry their own
pickers, so the sidebar folds away on them. The date range in the header applies to
Activity and to the Summary table.

## Summary table

Pick a **repository**, a **branch** and optionally a **service / folder**. The table has
one row per commit:

| Commit hash | Commit creator | Commit date | Tag | Tag creator | Tag date | Message |
| --- | --- | --- | --- | --- | --- | --- |
| `f6b73f0f41` ⧉ | rajasany | 2026-08-07 18:58 | `v0.9` | lightweight | — | first commit |

- The hash is a link to the commit plus a copy button for the full 40 characters.
- A commit carrying two tags gets one line per tag, so the tag columns never hold a list.
- **CSV** exports exactly the rows on screen.
- Only the chosen branch is queried, so this costs one page of commits — not one call per
  branch the way the feed does.

**On "Tag creator" and "Tag date":** only *annotated* tags (`git tag -a`) have them. A
lightweight tag (`git tag v1`) is just a ref pointing at a commit — git stores no author
and no timestamp for it anywhere. Those rows read **lightweight** rather than borrowing
the commit's own author and date, which would look like an answer to a question nobody
asked. `psf/requests`, for instance, uses lightweight tags throughout; `git/git` uses
annotated ones and shows a real tagger for each.

## Cherry-picks

Every tab that lists commits — Activity, Compare, Summary table, Find a commit, Order
commits — marks a commit that says it is a cherry-pick, with a 🍒 chip.

Two confidence levels, kept apart deliberately:

| Chip | Meaning |
| --- | --- |
| `cherry-pick 9fceb02` (solid) | `git cherry-pick -x` recorded the source commit in the message. Reliable, and the source hash is shown. |
| `cherry-pick?` (dashed) | The message merely *mentions* a cherry-pick. Flagged, but no source was recorded, so none is invented. |

**The important caveat: a plain `git cherry-pick` records nothing at all.** The trailer
only exists when `-x` was used. So the absence of a chip is *not* evidence that a commit
is original — it means git has nothing to say either way. Detecting those would need
patch-ID comparison across branches, which the GitHub API cannot do at all and which is
expensive even locally.

Detection is on the commit message, so it works identically for GitHub and CSR.
"None of your current commits are cherry-picks" is a real answer, not a broken feature —
verified against a fixture containing a genuine `cherry-pick -x` commit.

## Order commits

Paste a list of commit hashes — one per line, comma separated, bulleted, short hashes,
or full commit URLs. The screen validates the list and lays it out newest-first on a
timeline, with the newest marked **Latest**.

```
●  Latest  branch restriction                            ← newest
   e224c189f7f8892e4b604f0c10b829c3e9e02088
   rajasany · 2026-08-26 21:49 · position 10 of 13 · 3 since head
●  #2      Commit hash and tag changes
   659b1095b5cd46b8062117e9abb436c20115aff8
   rajasany · 2026-08-26 20:50 · position 9 of 13 · 4 since head
│          4 other commits in between
●  #3      Dashboard addition
   …
```

Each entry carries the full hash (copyable), the author, the creation time, any tags on
that commit, and its position in the branch. Gaps in the chain are shown, so you can see
how far apart two commits in the list actually are.

**Validation.** The repository is settled by the first commit that resolves; every other
commit is then checked against it. A commit from a *different* repository is excluded and
says where it actually lives (`not in rajasany/Github-dashboard — it is in
rajasany/Insurance`). A commit that exists but is not on the chosen branch is excluded
and says so. Unreadable input and duplicates are listed separately as ignored. Nothing is
dropped silently.

You can pin the repository and branch with the two dropdowns; left alone, the repository
is detected from the commits and the default branch is used.

**Ordering is by position in history, not by timestamp.** Author dates can be rewritten
by a rebase or a cherry-pick, so sorting by date can put commits in an order they were
never applied in. Where the two disagree the ancestry order wins and a note says the
dates disagree — rather than quietly presenting one as the other.

## Release plan

Upload a spreadsheet listing the repositories and folders going out in a release; each
row comes back with what actually changed there and a tag name proposed from your own
convention. Nothing is written to any remote — the proposals are text until you press
**Tag…** on a row, which hands it to the ordinary staging flow described below.

**The sheet.** Use **Download template** for a starter workbook, or upload what you
already have. Only a **repository** column is required. The header row is found by
scanning the first 15 rows, so a title line or a blank row above the headers is fine, and
these column names are all recognised (case and spacing are ignored):

| Meaning | Accepted headers |
| --- | --- |
| Repository | `Repository Name`, `Repo`, `Project`, `Git Repo`, … |
| Folder | `Service / Folder`, `Folder Path`, `Module`, `Component`, `Directory`, … |
| Branch | `Branch Name`, `Branch`, `Ref`, `Target Branch` |
| Commit | `Commit Hash`, `Commit ID`, `SHA`, `Revision`, … |
| Tag | `Proposed Tag`, `Tag Name`, `Tag` |
| Note | `Notes`, `Comment`, `Remarks`, `Description` |

The repository cell is matched leniently: `demo/payments`, bare `payments`, the
`csr:demo/payments` key, and a pasted clone URL all resolve, ignoring case. A blank
folder means the whole repository; a blank branch means that repo's default branch, or
whatever you set in **Branch override**. Rows that cannot be resolved are reported with
a reason rather than dropped, so the output always has one row per input row. The limits
are 500 sheet rows read and 100 processed, and a 5 MB upload.

**The convention.** The tag pattern is yours to write, from these placeholders:

| | |
| --- | --- |
| `{repo}` `{repo_name}` `{owner}` | full name, name alone, owner alone |
| `{folder}` `{folder_slug}` | folder path, and a slug safe for a ref name |
| `{branch}` `{branch_slug}` | branch name, and its slug |
| `{sha}` `{sha7}` | the folder's latest commit, full and short |
| `{date}` `{yyyy}` `{mm}` `{dd}` `{today}` | that commit's date |
| `{n}` `{n:03}` | the next number in the series, optionally zero-padded |

The default is `release/{repo_name}/{folder_slug}/{yyyy}.{mm}.{n:02}`. An unknown or
mistyped placeholder is rejected outright rather than passed through, so a literal
`{sha7}` can never end up inside a real tag name.

`{n}` counts only within its own series. The pattern is turned into a regex with a
capture group where `{n}` sits, and only existing tags matching *that* shape are
considered — so a stray `v9.9.9` or a tag from another folder cannot inflate the number.
Numbers claimed by earlier rows of the same run are counted too, under a lock, so two
rows can never propose the same tag.

**What each row reports:** the resolved repository and branch, the folder's latest commit
(hash, title, author, date), how many commits and files changed in the window, the exact
directories touched, how many were cherry-picked, any tag already on that commit, and the
proposed name — flagged if a tag of that name already exists. **Download CSV** exports
the table as shown.

Note that the latest commit is the latest *for that folder*, not the repository's tip.
Two rows naming different folders of one repository will usually report different
commits, which is the point of the tab.

## Migration requests

A register for moving a change along SIT → QA → PROD. A developer raises a request, an
approver approves it and plans a QA date, DevOps records the QA migration, the approver
confirms it is ready for production after testing, and DevOps records the production
migration. Each of those steps is owned by a role, and the fields belonging to a step
open only when the preceding step is done.

**This tab needs to know who you are, which the rest of the dashboard does not.** See
*Identity and roles* below before enabling it.

### The workflow

| Stage | Who | What they set | Opens when |
| --- | --- | --- | --- |
| Request | developer | Rel#, path, microservice, repo, track lead, requestor, reason, description, the four risk flags, commit hash, DB script path | always — and locks once approved |
| Approval | approver | Approved By, QA Date Planned | always |
| QA migration | devops | Executed in QA, QA Migration Remarks | once approved |
| Ready for production | approver | Ready for Prod | once the QA migration is recorded |
| Production migration | devops | Executed in PROD, Prod Migration Remarks | once marked ready for prod |

Editing a request after it has been approved would invalidate what was approved, so the
request fields lock at that point. An approver can clear **Approved By** to withdraw the
approval, which reopens them and clears the approval date.

The status shown against each row is derived from the record's own fields rather than
stored, so it cannot drift out of step with them.

### The fields

Eight are filled in by the app and cannot be typed by anyone: **SL#** (the sequence),
**Created By**, **Date Created**, **Date Approved**, **QA Migrated By**, **Prod Migration
Date**, **Prod Migrated By** and the derived status. The rest are role-gated as above.

*Prod Migrated By is not in the original field list — it is there for symmetry with QA
Migrated By, so a production migration records who performed it and not only when.*

**Repo Name** and **Track Lead Name** are narrowed to the microservice selected, and the
server rejects a pairing that does not match, so a sheet naming another microservice's
repo is caught rather than stored.

**Amber flags.** Four yes/no fields mark a request as carrying risk, and show amber
wherever they appear — in the form and as chips in the register:

| Flag | Also requires |
| --- | --- |
| Code & Image Change? | **Commit Hash**, when Yes |
| Environment Change | — |
| Env Secret Details | — |
| DDL/DML | **DB Script Path**, when Yes |

Those two conditional requirements are enforced on creation *and* on edit — turning a
flag on later demands the field it implies.

### Reading the register

Each row shows its **Created** date as `dd-mon-yy` — `05-Sep-26` — with the full
timestamp on hover. The month is a fixed English abbreviation rather than a locale-driven
one, so the column reads the same everywhere.

Click any column header to sort by it; clicking again reverses. Sorting happens on the
server and the CSV export takes the same parameters, so an export always matches what is
on screen. Two orderings are not alphabetical, deliberately:

- **Created** sorts on the underlying timestamp, so rows without one sort as oldest
  rather than jumping to the top of a newest-first list;
- **Status** sorts along the workflow — awaiting approval, approved, in QA, ready, in
  prod — because alphabetically "Approved" would precede "Awaiting approval".

**Created from / Created to** filter by date, inclusive at both ends. The range is sent
as instants computed from your own midnight, so a record filters into the same calendar
day that is printed beside it, whatever timezone you are in. Calling the API directly you
may pass plain `YYYY-MM-DD` dates instead, which are read as UTC; a bare end date covers
the whole of that day rather than its first instant.

### Retiring and deleting records

Approvers and admins get two ways to take a record out of the register:

| | What it does | Reversible |
| --- | --- | --- |
| **Deactivate** | Marks it inactive. It leaves the default listing, becomes read-only, and the change is recorded in its history. | Yes — **Reactivate** brings it back. |
| **Delete…** | Removes the record and its whole audit trail from the database. | No. |

Both are in the record's own dialog, on the left of the footer away from **Save**.
Delete asks for confirmation in place, and the confirmation names Deactivate as the
reversible alternative. Deleting is refused during a freeze, on the grounds that a freeze
stops record entry and erasing one is the most final entry there is.

Tick **Show inactive** to bring retired records back into view; they carry an *Inactive*
chip and a muted row. When they are hidden the count line says so, so a missing record is
never a silent omission. The CSV export gains an **Active** column and takes the same
toggle.

An inactive record cannot be edited by anyone, whatever their role — reactivate it first.
That way what it said when it was retired is what it still says.

### Freeze windows

An approver can close record entry for a time span, with a reason. While a freeze is
active **nobody can create or edit a record — not developers, not DevOps, and not
approvers either**; every write is refused with 423 and the reason is shown as a banner.
Managing freezes is exempt, so an approver can always lift one. The window is stored as
an instant, and the browser converts your local wall-clock entry, so a freeze means the
same moment for everyone regardless of timezone.

Note that this stops DevOps recording a migration *that has already happened* during the
window. That is what "no one can edit/enter records during that time" asks for; if you
would rather a freeze blocked only new requests and approvals, that is a one-line change
to `_guard_freeze`.

### Uploading a spreadsheet

**Download the template** gives an .xlsx whose columns carry the permitted values as real
Excel dropdowns, plus a Lists sheet spelling out which repo and track lead belong to each
microservice. Only the fields a developer owns are in it — approvals and migration results
are role-gated events with an audit trail, and a spreadsheet is not a way to assert that
an approver approved something.

Upload is a two-step: the sheet is validated and shown back to you row by row, and
nothing is written until you press **Import valid rows**. Rows with problems are reported
with the reason and skipped; the good ones still import. Headers are matched by name with
the same tolerance as the Release plan tab — a title line above the header is fine, and
several spellings of each column are accepted. The caps are 200 rows and 5 MB.

### Identity and roles

The app has no login of its own. In normal use it reads the caller's address from a
header set by an SSO proxy in front of it (for trying it out without one, see *Testing
without SSO* below) — `X-Forwarded-Email` by default — and maps that address to
roles in `config.yaml`:

```yaml
auth:
  header: X-Forwarded-Email
  trusted_proxies: [127.0.0.1, 10.0.0.0/8]
  roles:
    developer: ["*@example.com"]
    approver:  [lead@example.com]
    devops:    [devops@example.com]
```

**The header is only trustworthy if the app cannot be reached except through the proxy.**
A header is just bytes the client sends: anyone who can open a socket to uvicorn directly
can claim to be anybody. `trusted_proxies` is the guard — the header is honoured only
from those addresses, and the peer address is used for that check, never
`X-Forwarded-For`, which is itself client-supplied. Leave the list unset and the app
still runs, but every page carries a standing warning that it is unprotected. Roles may
be exact addresses or globs, matching ignores case, and one person may hold several.

### The admin role

`admin` is a fourth role, and a superset: every check any other role satisfies, it
satisfies too. It exists so that clearing up the register can be separated from approving
things — give someone `admin` and they can raise, approve, execute, retire, delete and
freeze. The roles actually held are still reported as configured, so an admin shows as
"admin" rather than as all four.

Retiring and deleting need `approver` **or** `admin`, so neither is out of reach if you
never configure an admin.

### Testing without SSO

You do not need a proxy to try this out. `auth.dev_mode` lets the caller declare who they
are and puts a role switcher at the top of the tab, so one person can walk a request
through the whole workflow — raise it as a developer, approve it, record the QA
migration as DevOps — without restarting anything:

```yaml
auth:
  dev_mode: true
  dev_user: dev@example.com   # who you are before picking someone else
  roles:
    developer: ["*@example.com"]
    approver:  [lead@example.com]
    devops:    [ops@example.com]
```

The switcher lists every exact address in `roles` with the roles it would hold — globs
cannot be enumerated, so only exact entries appear, plus **Other…** for anything else.
The choice is kept in a `dev_user` cookie, which is why file downloads are attributed
correctly too. Scripts and `curl` can send an `X-Dev-User` header instead, which wins
over the cookie:

```bash
curl -H 'X-Dev-User: lead@example.com' localhost:8000/api/migrations/meta
```

Roles always come from the `roles:` mapping, even in dev mode — so what you are testing
is the mapping you will actually deploy, not a separate set of rules.

**`dev_mode` and `trusted_proxies` are mutually exclusive.** Setting an allowlist turns
dev mode off, because a proxy on the same host also presents as loopback and would
otherwise let dev mode be reached straight through it. Configure one or the other, not
both — `config.example.yaml` lays them out as Option A and Option B for that reason.

**Restart after editing config.yaml.** `--reload` watches Python files, not YAML, so a
change to `auth:` or `migrations:` does not take effect until the server is restarted.

To see the role checks actually bite, give the roles to different addresses rather than
one — otherwise every step simply succeeds and nothing is being tested:

```yaml
  roles:
    developer: [dev@tcs.com]
    approver:  [lead@tcs.com]
    devops:    [ops@tcs.com]
```

They need not be real mailboxes; in dev mode they are only labels to switch between.

**This is a complete authentication bypass**, so three things hold it shut:

| Guard | Effect |
| --- | --- |
| `dev_mode` defaults to false | It is never on unless you ask for it. |
| Ignored whenever `trusted_proxies` is set | If you have configured a real proxy you are not testing. This matters because a proxy on the same host is *also* loopback, and would otherwise pass the check below while forwarding anyone's request. |
| Loopback only, unless `dev_allow_remote: true` | A flag left on by accident is not reachable from the network. |

While it is on, the server prints a warning on every start and the tab carries a standing
amber banner naming the risk. If `dev_mode` is set but a guard refused it, the tab says
which one — rather than silently behaving as though the setting were absent.

Everything above is enforced server-side, in `apply_changes`. The UI asks the API which
fields the current user may edit and renders only those, but that is a courtesy to stop
people being offered controls that would be refused — it is not the control itself.

### What is recorded

Every change is written to an audit trail: when, who, the field, the old value and the
new one. It is on the **History** panel of each record, and survives edits and
withdrawals. **CSV** exports the whole register, every field, honouring the filters.

## Creating tags

A **Tag…** button sits on every commit you can see: each row of the **Summary table**,
each result in **Find a commit**, and each entry in **Order commits**. The button carries
its own repository, so it works the same from any tab. The flow is two steps on purpose,
because a tag on a shared remote is awkward to retract:

1. **Create locally.** Name the tag, add a comment, review a confirmation showing the
   exact commit and repository, then create. This writes **only** to this app's local
   store (`.cache/staged-tags.sqlite3`). Nothing leaves the machine.
2. **Push.** Staged tags appear in a strip at the top of the page — visible from every
   tab, since a tag staged in one place must be pushable from anywhere — marked
   *local only*, with
   **Push…** and **Discard**. Push asks for a second confirmation naming the remote,
   then creates the tag there.

Tags are created **annotated**, so they carry a tagger and a date — a lightweight tag
would leave those columns permanently blank.

**Who the tag is attributed to**, in order of precedence:

| Source | Where it comes from |
| --- | --- |
| `TAGGER_NAME` + `TAGGER_EMAIL` | `.env`, for a deliberate bot identity |
| **The signed-in person** | CSR: `gcloud config get-value account`. GitHub: the token's owner via `GET /user` |
| Fallback | `Repo Change Dashboard`, only when nothing else can be established |

So a CSR tag is credited to whoever is signed in to gcloud, and a GitHub tag to whoever
owns the token — no configuration required. The gcloud account gives only an email
address, so the name is its local part verbatim (`person@example.com` → `person`); it is
not prettified, because inventing "Person" would assert a human name the account never
states. Where GitHub hides a profile email, its documented
`id+login@users.noreply.github.com` form is used, which still routes to the account.

Refused before anything happens: invalid git tag names (spaces, `~ ^ : ? *`, `..`,
leading/trailing `.` `/` `-`, `.lock`), a name already staged, a name that already
exists on the remote, and a commit that is not in the repository. Pushing twice is
refused; discarding is only possible before a push.

Pushing to GitHub needs a token with **write** access to the repository. Pushing to CSR
uses the mirror. Note that `clone --mirror` sets `remote.origin.mirror`, under which a
plain push would synchronise *every* ref including deletions — so the push disables that
for the invocation and names a single explicit refspec. Only the one tag can travel.

## Tags tab

Pick a **repository** from the dropdown; its tags are grouped by the service / folder
their commit touched, with the branches that contain each one:

```
rajasany/Github-dashboard · 1 tag · 2 branches
  🗂 app     1 tag
     Tag     Branch   Commit       Tag creator   Tag date           Comment
     v1.20   ⑂ main   34755374a4   Release Bot   2026-08-26 22:51   tag 1.2.0
  🗂 tests   1 tag
     v1.20   ⑂ main   34755374a4   Release Bot   2026-08-26 22:51   tag 1.2.0
```

**On the Branch column.** A tag names a *commit*, not a branch — git records no branch
on a tag at all. So this column reports *the branches whose history contains that
commit*, which is the closest true answer, and it is often more than one: a tag on a
commit both `main` and a feature branch descend from lists both. The default branch is
listed first. Where a commit is on no branch (deleted branch, PR-only), the column reads
**no branch** rather than blank.

Working that out costs one comparison per (tag, branch) pair on GitHub, which is why the
tab is scoped to one repository. Above 30 tags or 10 branches the probing stops and the
scope line says how much was skipped — those rows read **not checked**, never a
misleading "no branch". CSR repositories get it free from `git for-each-ref --contains`.

A tag whose commit touched several folders is listed under each, so the per-folder counts
sum to more than the repository's tag total. Lightweight tags show **lightweight** in
place of a creator and date, for the reason described above.

## Find a commit

Paste a commit hash — full, abbreviated to as few as 4 characters, or a whole commit URL —
and every configured repository is searched in parallel.

```
acme/shop                                                    GitHub
Commit hash  abc1234abc1234abc1234abc1234abc1234abc12  [copy] [Open]
Authored     alice on 2026-08-04 15:30
Changes      3 files · +40 −5 · parent dddddddd
v1.5         tagged by Tagger Person on 2026-08-04 17:30 · milestone

Position in the graph
  Branch            Commits since   Position    Progress along branch
  main (default)                7   13 of 20    ▓▓▓▓▓▓▓░░░
  dev  at head                  0   13 of 13    ▓▓▓▓▓▓▓▓▓▓
```

- **Folders changed** are the directories the files actually sit in. This is
  deliberately *more specific* than the service/folder used elsewhere: a commit
  confined to `app/static/app.js` reports **`app/static`**, not the `app` bucket the
  Activity feed groups by. Inspecting one commit and grouping activity are different
  questions, so they get different answers. A commit spanning directories lists each
  one, and `(repo root)` sorts last as the least specific.
- **Position within each folder** is counted over *that directory's own history*,
  not the repository's. A commit can be 9 of 15 on `main` but 5 of 11 in `tests/`,
  because only 11 commits ever touched `tests/`. The two tables are labelled
  separately — *Position in the branch* and *Position within each folder* — so the
  numbers can never be mistaken for each other. Root-level files are not a
  directory, so they report why rather than a number.
- **Commits since** is how far that branch has moved on past this commit.
- **Position** is the commit's ordinal from the root of that branch, so it reads as
  "13 of 20". A commit sits at a *different* depth on each branch that contains it,
  which is why this is per-branch rather than a single number.
- GitHub has no "which branches contain this commit" endpoint, so containment is
  established with one three-dot comparison per branch: `behind_by == 0` means the commit
  is an ancestor. Past 25 branches the probe stops and the card says how many were skipped.
- Total commits on a branch come from the page count of the commits endpoint; the CSR
  provider gets exact counts from `git rev-list` instead.
- **Nearest earlier tag** is shown for CSR repositories, where `git describe` makes it
  free. The GitHub REST API has no equivalent, so that line is omitted rather than
  guessed at.

## Commit hashes and tags

Every commit row carries its short hash as a link plus a **copy button** that puts the
**full 40-character hash** on the clipboard. Tags pointing at a commit appear as amber
chips beside the service and branch chips.

Above the feed, a **Latest commit** card answers "where is this branch and folder right
now?" for whatever is selected:

```
Latest commit · branch main · services/auth          3h ago
Commit hash    dd125701459136e6baa94f887e4c18ebc8c8774a  [copy] [Open]
Tag here       no tag on this commit
Most recent tag  v0.1.0   1 commit back, at dfd03c5
Message        auth: token refresh — Dev One
```

- **Tag here** lists tags on that exact commit; **Most recent tag** appears only when the
  tip itself is untagged, and states how far back the last tagged commit is.
- "N commits back" is counted **within the loaded date range**, not over all history —
  widen the range if you need to reach further.
- Annotated tags are dereferenced to the commit they point at. Without that they would
  resolve to the tag object's own SHA and never match a commit; both providers handle it
  (`/tags` does it server-side, `%(*objectname)` does it in git).
- Tags are read per repository — GitHub `/repos/{o}/{r}/tags`, CSR
  `git for-each-ref refs/tags` — so the full set is available even when the tagged commit
  falls outside the current date window.

Reports carry this too: a **Tagged commits** table with full hashes, a `Tag` column and a
12-character `Commit` column in the detail table, and a `Tags` figure in the summary.

## Reading the commit feed

Each row is one commit, deduped across branches:

```
[avatar]  Booth Addition                                    15 Jun
          Raja Sanyal · rajasany/meetingapp        a31370a  215 files
          🗂 backend  🗂 data  🗂 deploy  🗂 frontend   ⑂ main
          ▸ Full message
```

- **Services** are square-cornered, accent-tinted, folder-icon chips; **branches** are
  neutral pills with a branch icon. Shape *and* colour differ, so the two are never
  told apart by hue alone. The default branch is filled and inked rather than accented.
- **Full message** appears only when the commit has a body beyond its subject line; it
  is a native `<details>`, so it works by keyboard and screen reader.
- The relative time carries the exact timestamp as a tooltip, and sits in a `<time>`
  element with a machine-readable `datetime`.
- Day headings stick below the top bar while you scroll. The offset is measured from
  the real top bar at runtime, so it stays correct when the header wraps.
- Group headings show a per-group commit count; the row hover cue is also applied on
  keyboard focus.

### Theme

The UI is white — one light theme, regardless of the operating system's appearance
setting. There is no `prefers-color-scheme` switch: page and cards are both pure white,
and structure comes from a hairline border (`--border`, 1.34:1 against white) plus a
single faint band tone (`--surface-2`, 1.12:1) used for row hover, group headings, chips
and code blocks. `color-scheme: light` and a matching `<meta>` keep native selects and
scrollbars light on a dark-themed OS, with no dark flash on first paint.

Colours were measured rather than eyeballed: every text/background pair in the feed and
sidebar clears WCAG AA (4.5:1), the lowest being 4.59:1. The accent is teal (`--accent`,
5.59:1 on white); the chip and active-row ink is a dedicated `--accent-ink` token that
gives extra margin (6.7:1) over the plain accent (4.95:1) on the tinted `--accent-soft`
background.

To reintroduce a dark theme later, add a `@media (prefers-color-scheme: dark)` block
overriding the `:root` custom properties and drop the `color-scheme: light` line — no
other rule hard-codes a colour.

## Tests

```bash
.venv/bin/python tests/test_paths.py    # folder rollup + exact dirs         (26 checks)
.venv/bin/python tests/test_report.py   # date window + report rollup        (57 checks)
.venv/bin/python tests/test_lookup.py   # tag metadata, summary, lookup      (51 checks)
.venv/bin/python tests/test_tagging.py  # staging, pushing, tag branches     (68 checks)
.venv/bin/python tests/test_ordering.py # parsing and ordering a list        (30 checks)
.venv/bin/python tests/test_cherrypick.py # cherry-pick detection            (24 checks)
.venv/bin/python tests/test_compare.py  # branch comparison, real git repo   (33 checks)
.venv/bin/python tests/test_bulk.py     # sheet parsing, conventions, concurrency (55 checks)
.venv/bin/python tests/test_migrations.py # roles, workflow, freeze, retire    (270 checks)
node tests/ui_cascade.test.js          # selection, rendering, escaping      (186 checks)
node tests/ui_migrations.test.js       # rendering, dates, sorting, escaping  (72 checks)
```

The second suite runs the real `app/static/app.js` in a stubbed DOM and asserts the cases
that matter: selecting a repository scopes the folder and branch lists to it; two
repositories each containing a `backend/` folder stay separate; stale downstream
selections are pruned on repo switch; the lists render single-select buttons with no
checkbox inputs; commit rows mark the default branch and expose the full message only
when there is one; and a commit title containing `"`, `&`, or `<tag>` is escaped rather
than injected into the markup.

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
- The Migrations tab trusts an identity header, so it is exactly as trustworthy as the
  proxy in front of it. With no `auth.trusted_proxies` set it warns, but it still runs —
  it cannot tell on its own whether it is reachable directly.
- `auth.dev_mode` bypasses authentication entirely and is for testing. It is guarded
  three ways and announces itself loudly, but it is still a bypass: do not deploy with
  it on.
- Everything but the Migrations tab is unauthenticated. Anyone who can reach the app can
  read the commit feed, compare branches and stage tags; roles gate the migration
  register only.
- Deleting a migration request also deletes its audit trail. Deactivating is the
  reversible option and keeps the history; prefer it unless the record should genuinely
  never have existed.
