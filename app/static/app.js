/* Repo Change Dashboard — one commit feed across providers, repos, branches, services. */

const $ = (id) => document.getElementById(id);

const PROVIDER_LABEL = { github: "GitHub", csr: "Cloud Source Repos" };
const NO_FOLDER = "(no folder data)";
// Separates repo key from folder path in a scoped folder key. A control
// character, so it can't collide with a real path.
const FOLDER_SEP = "\u001f";

/* Tiny inline icons. Two or three strokes each so they stay legible at 11px, and
 * drawn with currentColor so they inherit each chip's state. */
const ICON = {
  folder:
    '<svg class="ic" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round" aria-hidden="true"><path d="M2 4.4a1.2 1.2 0 0 1 1.2-1.2h2.5l1.4 1.6h5.7A1.2 1.2 0 0 1 14 6v5.6a1.2 1.2 0 0 1-1.2 1.2H3.2A1.2 1.2 0 0 1 2 11.6V4.4Z"/></svg>',
  branch:
    '<svg class="ic" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" aria-hidden="true"><path d="M4.5 6v6M4.5 11c0-2.8 2.2-4.6 5-4.6"/><circle cx="4.5" cy="3.6" r="1.9" fill="currentColor" stroke="none"/><circle cx="11.4" cy="5.4" r="1.9" fill="currentColor" stroke="none"/></svg>',
  caret:
    '<svg class="ic caret" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><path d="M6 3.5 11 8l-5 4.5V3.5Z"/></svg>',
  search:
    '<svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" aria-hidden="true"><circle cx="11" cy="11" r="7"/><path d="m16.5 16.5 4 4"/></svg>',
};

// A folder touched by two commits within this window reads as active rework
// rather than two unrelated changes landing on the same area by chance.
const CHURN_WINDOW_HOURS = 24;

const el = {
  topbar: $("topbar"),
  preset: $("preset"),
  dateFrom: $("date-from"),
  dateTo: $("date-to"),
  dateClear: $("date-clear"),
  exportPdf: $("export-pdf"),
  exportPptx: $("export-pptx"),
  scopeLine: $("scope-line"),
  feedView: $("feed-view"),
  comparePanel: $("compare-panel"),
  compareScope: $("compare-scope"),
  compareBase: $("compare-base"),
  compareHead: $("compare-head"),
  compareRun: $("compare-run"),
  compareSwap: $("compare-swap"),
  compareView: $("compare-view"),
  compareHeading: $("compare-heading"),
  compareSub: $("compare-sub"),
  compareStatus: $("compare-status"),
  compareLink: $("compare-link"),
  compareExit: $("compare-exit"),
  compareExportPdf: $("compare-export-pdf"),
  compareExportPptx: $("compare-export-pptx"),
  compareStats: $("compare-stats"),
  compareBody: $("compare-body"),
  refresh: $("refresh"),
  rate: $("rate"),
  banner: $("banner"),
  providerPanel: $("provider-panel"),
  providerFilters: $("provider-filters"),
  repoFilters: $("repo-filters"),
  folderPanel: $("folder-panel"),
  folderFilters: $("folder-filters"),
  folderSearch: $("folder-search"),
  branchFilters: $("branch-filters"),
  branchSearch: $("branch-search"),
  authorFilters: $("author-filters"),
  folderScope: $("folder-scope"),
  branchScope: $("branch-scope"),
  scopeCommits: $("scope-commits"),
  groupBy: $("group-by"),
  reset: $("reset"),
  stats: $("stats"),
  search: $("search"),
  resultCount: $("result-count"),
  feed: $("feed"),
};

/* One active selection per dimension; null means "all". Selections drill down:
 * choosing a repository is what makes its folders and branches visible below. */
const state = {
  data: null,
  provider: null,
  repo: null,
  folder: null,
  branch: null,
  author: null,
  // Non-null puts the pane into branch-comparison mode instead of the feed.
  compare: null,
};

/** Folders a commit touched, with an explicit bucket when we have no data. */
function foldersOf(c) {
  return c.folders && c.folders.length ? c.folders : [NO_FOLDER];
}

/* A commit reads as a revert when GitHub's own revert button named it that way
 * ("Revert "..."" title, or a "This reverts commit <sha>" line in the body) or
 * the author used the conventional-commit "revert:" prefix by hand. */
function isRevert(c) {
  return /^revert\b/i.test(c.title) || /this reverts commit/i.test(c.body || "");
}

/* Flags commits whose folder was touched again shortly after (or before) —
 * a cheap proxy for an area under active rework rather than a one-off change.
 * Computed once over the full loaded window so it doesn't shift as sidebar
 * filters narrow the visible set. */
function annotateChurn(commits) {
  const byFolder = new Map();
  for (const c of commits) {
    c._churn = false;
    for (const f of foldersOf(c)) {
      if (f === NO_FOLDER) continue; // "no folder data" isn't a real, shared area
      const key = `${c.repo_key}${FOLDER_SEP}${f}`;
      if (!byFolder.has(key)) byFolder.set(key, []);
      byFolder.get(key).push(c);
    }
  }
  const windowMs = CHURN_WINDOW_HOURS * 3600 * 1000;
  for (const list of byFolder.values()) {
    const sorted = [...list].sort((a, b) => new Date(a.date) - new Date(b.date));
    for (let i = 1; i < sorted.length; i++) {
      if (new Date(sorted[i].date) - new Date(sorted[i - 1].date) <= windowMs) {
        sorted[i]._churn = true;
        sorted[i - 1]._churn = true;
      }
    }
  }
}

/* ---------- helpers ---------- */

function relativeTime(iso) {
  if (!iso) return "";
  const secs = (Date.now() - new Date(iso).getTime()) / 1000;
  const steps = [
    [60, "s"],
    [3600, "m", 60],
    [86400, "h", 3600],
    [604800, "d", 86400],
  ];
  if (secs < 60) return `${Math.max(0, Math.floor(secs))}s ago`;
  for (const [limit, unit, divisor] of steps.slice(1)) {
    if (secs < limit) return `${Math.floor(secs / divisor)}${unit} ago`;
  }
  return new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function dayLabel(iso) {
  const d = new Date(iso);
  const today = new Date();
  const yesterday = new Date(today.getTime() - 86400000);
  const same = (a, b) => a.toDateString() === b.toDateString();
  if (same(d, today)) return "Today";
  if (same(d, yesterday)) return "Yesterday";
  // Assembled by hand: passing weekday + month + day + year to toLocaleDateString
  // yields awkward output in some locales ("Mon, 15 Jun, 2026").
  const weekday = d.toLocaleDateString(undefined, { weekday: "short" });
  const dayMonth = d.toLocaleDateString(undefined, { day: "numeric", month: "short" });
  const year = d.getFullYear() === today.getFullYear() ? "" : ` ${d.getFullYear()}`;
  return `${weekday} · ${dayMonth}${year}`;
}

function showBanner(html, isError) {
  el.banner.className = `banner${isError ? " error" : ""}`;
  el.banner.innerHTML = html;
}

function hideBanner() {
  el.banner.className = "banner hidden";
  el.banner.innerHTML = "";
}

/* ---------- date range ---------- */

/** YYYY-MM-DD in the viewer's own timezone, which is what a date input expects. */
function isoDay(d) {
  const local = new Date(d.getTime() - d.getTimezoneOffset() * 60000);
  return local.toISOString().slice(0, 10);
}

/** Fill the From/To inputs from a preset. "custom" leaves them alone. */
function applyPreset(preset) {
  if (preset === "custom") return;
  const today = new Date();

  if (preset === "mtd") {
    el.dateFrom.value = isoDay(new Date(today.getFullYear(), today.getMonth(), 1));
    el.dateTo.value = "";
    return;
  }

  const days = Number(preset) || 14;
  const from = new Date(today);
  // "Last 7 days" should include today, so step back 6 whole days, not 7.
  from.setDate(from.getDate() - (days - 1));
  el.dateFrom.value = isoDay(from);
  // Left blank on purpose: the range runs through to now, and stays correct
  // tomorrow without the user having to touch it.
  el.dateTo.value = "";
}

function rangeLabel() {
  const from = el.dateFrom.value;
  const to = el.dateTo.value;
  const pretty = (iso) =>
    new Date(`${iso}T00:00:00`).toLocaleDateString(undefined, {
      day: "numeric",
      month: "short",
      year: "numeric",
    });
  if (!from) return "";
  return to ? `${pretty(from)} – ${pretty(to)}` : `${pretty(from)} – today`;
}

/* ---------- data ---------- */

async function load({ refresh = false } = {}) {
  el.refresh.disabled = true;
  el.refresh.textContent = "Loading…";
  try {
    const params = new URLSearchParams();
    // `from` is required; an empty `to` deliberately means "through to now",
    // which is what "everything after this date" needs.
    if (el.dateFrom.value) params.set("since", el.dateFrom.value);
    if (el.dateTo.value) params.set("until", el.dateTo.value);
    if (!el.dateFrom.value) params.set("days", "14");
    if (refresh) params.set("refresh", "true");
    const res = await fetch(`/api/feed?${params}`);
    const body = await res.json();
    if (!res.ok) {
      showBanner(`<strong>Could not load data.</strong> ${body.detail || res.statusText}`, true);
      el.feed.replaceChildren(
        stateNode("Could not load commits", body.detail || res.statusText || "The request failed.")
      );
      return;
    }
    state.data = body;
    onDataLoaded();
  } catch (err) {
    showBanner(`<strong>Request failed.</strong> ${err.message}`, true);
  } finally {
    el.refresh.disabled = false;
    el.refresh.textContent = "Refresh";
  }
}

/* Session-scoped so a failed/cancelled sign-in doesn't bounce the tab back and
 * forth forever — after one automatic trip to /auth/gcloud, later loads just
 * show the banner with a manual retry link. */
const GCLOUD_AUTH_ATTEMPTED_KEY = "gcloudAuthAttempted";

function onDataLoaded() {
  const { errors, rate_limit: rl } = state.data;
  const authError = errors.find((e) => e.code === "gcloud_auth_required");

  if (authError && !sessionStorage.getItem(GCLOUD_AUTH_ATTEMPTED_KEY)) {
    sessionStorage.setItem(GCLOUD_AUTH_ATTEMPTED_KEY, "1");
    window.location.href = "/auth/gcloud";
    return;
  }
  if (!authError) sessionStorage.removeItem(GCLOUD_AUTH_ATTEMPTED_KEY);

  if (authError) {
    showBanner(
      `<strong>Google Cloud sign-in needed.</strong> ${escapeHtml(authError.error)}` +
        `<a class="btn primary gcloud-signin" href="/auth/gcloud">Sign in with Google Cloud</a>`,
      true
    );
  } else if (errors.length) {
    const items = errors.map((e) => `<li><code>${e.repo}</code> — ${e.error}</li>`).join("");
    showBanner(`<strong>${errors.length} source(s) could not be read:</strong><ul>${items}</ul>`, true);
  } else if (state.noToken) {
    showBanner("<strong>No <code>GITHUB_TOKEN</code> set.</strong> Running unauthenticated — public repos only, 60 API requests/hour. Copy <code>.env.example</code> to <code>.env</code> and restart for private repos and a 5000/hour limit.", false);
  } else {
    hideBanner();
  }

  el.rate.textContent = rl.remaining != null ? `GitHub API ${rl.remaining}/${rl.limit}` : "";

  for (const c of state.data.commits) c.is_revert = isRevert(c);
  annotateChurn(state.data.commits);

  // A refresh can retire options the user had selected.
  pruneSelections();
  buildFilters();
  render();
  loadBranchesForCompare();
}

/* ---------- filters ---------- */

function tally(keyFn, commits) {
  const counts = new Map();
  for (const c of commits) {
    for (const key of keyFn(c)) counts.set(key, (counts.get(key) || 0) + 1);
  }
  return counts;
}

/* A single-select list. One row is active at a time; the leading "All …" row
 * clears the selection. No checkboxes — picking a row narrows everything below. */
function optlist(
  container,
  counts,
  {
    active,
    onSelect,
    allLabel,
    allCount = null,
    search = "",
    label = (k) => k,
    rowLabel = null,
    group = null,
  } = {}
) {
  // `label` drives search/sort/tooltip; `rowLabel` is the visible text, which can
  // be shorter when a group header already supplies the context.
  const shownLabel = rowLabel || label;
  container.innerHTML = "";

  const entries = [...counts.entries()]
    .filter(([key]) => !search || label(key).toLowerCase().includes(search.toLowerCase()))
    .sort((a, b) => b[1] - a[1] || label(a[0]).localeCompare(label(b[0])));

  // Summing the per-key counts over-counts whenever one commit owns several keys
  // (a commit touching three services). Callers pass the real commit total.
  const total = allCount ?? [...counts.values()].reduce((a, b) => a + b, 0);

  const addRow = (key, text, count, { isAll = false, title = text } = {}) => {
    const row = document.createElement("button");
    row.type = "button";
    row.className = `opt${key === active ? " active" : ""}${isAll ? " all" : ""}`;
    row.setAttribute("role", "option");
    row.setAttribute("aria-selected", String(key === active));
    row.title = title;

    const name = document.createElement("span");
    name.className = "label";
    name.textContent = text;

    const num = document.createElement("span");
    num.className = "count";
    num.textContent = count;

    row.append(name, num);
    row.addEventListener("click", () => onSelect(key === active ? null : key));
    container.appendChild(row);
  };

  if (allLabel) addRow(null, allLabel, total, { isAll: true });

  if (!entries.length) {
    const empty = document.createElement("p");
    empty.className = "muted empty";
    empty.textContent = search ? "No match." : "Nothing to show.";
    container.appendChild(empty);
    return;
  }

  // When grouped, keep each group's rows together and order groups by weight.
  let ordered = entries;
  if (group) {
    const weight = new Map();
    for (const [key, count] of entries) {
      const name = group(key) || "";
      weight.set(name, (weight.get(name) || 0) + count);
    }
    ordered = [...entries].sort((a, b) => {
      const ga = group(a[0]) || "";
      const gb = group(b[0]) || "";
      if (ga !== gb) return weight.get(gb) - weight.get(ga) || ga.localeCompare(gb);
      return b[1] - a[1] || label(a[0]).localeCompare(label(b[0]));
    });
  }

  let currentGroup = null;
  for (const [key, count] of ordered) {
    if (group) {
      const name = group(key);
      if (name && name !== currentGroup) {
        currentGroup = name;
        const head = document.createElement("div");
        head.className = "opt-group";
        head.textContent = name;
        container.appendChild(head);
      }
    }
    addRow(key, shownLabel(key), count, { title: label(key) });
  }
}

function repoNameOf(key) {
  return state.data.repos.find((r) => r.key === key)?.full_name || key;
}

function repoShortName(key) {
  const name = repoNameOf(key);
  return name.includes("/") ? name.slice(name.indexOf("/") + 1) : name;
}

/* A folder only means something inside its repository — two repos can each have
 * a `backend/`, and they are different services. So folder identity is the pair
 * (repo, folder), and the filter list is scoped accordingly. */
function folderKeysOf(c) {
  return foldersOf(c).map((f) => `${c.repo_key}${FOLDER_SEP}${f}`);
}

function splitFolderKey(key) {
  const at = key.indexOf(FOLDER_SEP);
  return at < 0
    ? { repoKey: "", folder: key }
    : { repoKey: key.slice(0, at), folder: key.slice(at + FOLDER_SEP.length) };
}

function folderLabel(key) {
  const { repoKey, folder } = splitFolderKey(key);
  // Prefix with the repo only when more than one is in play, to keep it readable.
  return state.repo ? folder : `${repoShortName(repoKey)} / ${folder}`;
}

/* Selections drill down in this order. A list's options are computed from the
 * commits matching every selection *above* it, so choosing a repository is what
 * scopes the branch list, and choosing a branch further scopes the folder list. */
const STAGE = { PROVIDER: 1, REPO: 2, BRANCH: 3, FOLDER: 4, AUTHOR: 5 };

function passesUpTo(c, stage) {
  if (stage > STAGE.PROVIDER && state.provider && c.provider !== state.provider) return false;
  if (stage > STAGE.REPO && state.repo && c.repo_key !== state.repo) return false;
  if (stage > STAGE.BRANCH && state.branch && !c.branches.includes(state.branch)) return false;
  if (stage > STAGE.FOLDER && state.folder && !folderKeysOf(c).includes(state.folder)) return false;
  if (stage > STAGE.AUTHOR && state.author && c.author_name !== state.author) return false;
  return true;
}

function scopedTo(stage) {
  return state.data.commits.filter((c) => passesUpTo(c, stage));
}

/* After a selection changes, drop any downstream selection that no longer exists
 * in the narrowed scope — e.g. switching repository invalidates its folder. */
function pruneSelections() {
  if (state.provider && !state.data.commits.some((c) => c.provider === state.provider))
    state.provider = null;

  const repoOpts = tally((c) => [c.repo_key], scopedTo(STAGE.REPO));
  if (state.repo && !repoOpts.has(state.repo)) state.repo = null;

  const branchOpts = tally((c) => c.branches, scopedTo(STAGE.BRANCH));
  if (state.branch && !branchOpts.has(state.branch)) state.branch = null;

  const folderOpts = tally(folderKeysOf, scopedTo(STAGE.FOLDER));
  if (state.folder && !folderOpts.has(state.folder)) state.folder = null;

  const authorOpts = tally((c) => [c.author_name], scopedTo(STAGE.AUTHOR));
  if (state.author && !authorOpts.has(state.author)) state.author = null;
}

function select(dimension, key) {
  const repoBefore = state.repo;
  state[dimension] = key;
  pruneSelections();
  buildFilters();
  render();
  // The compare pickers list every branch of the selected repo, so they have to
  // be refetched when that selection moves.
  if (state.repo !== repoBefore) {
    exitCompare();
    loadBranchesForCompare();
  }
}

function buildFilters() {
  // Only worth showing the source picker when more than one provider is configured.
  el.providerPanel.classList.toggle("hidden", state.data.providers.length < 2);
  const providerScope = scopedTo(STAGE.PROVIDER);
  optlist(el.providerFilters, tally((c) => [c.provider], providerScope), {
    active: state.provider,
    onSelect: (k) => select("provider", k),
    allLabel: "All sources",
    allCount: providerScope.length,
    label: (p) => PROVIDER_LABEL[p] || p,
  });

  const repoScope = scopedTo(STAGE.REPO);
  optlist(el.repoFilters, tally((c) => [c.repo_key], repoScope), {
    active: state.repo,
    onSelect: (k) => select("repo", k),
    allLabel: "All repositories",
    allCount: repoScope.length,
    label: repoNameOf,
    // The owner/org prefix is redundant in a list scoped to one dashboard's repos.
    rowLabel: repoShortName,
  });

  // Branches are shown for whichever repository is selected.
  el.branchScope.textContent = state.repo
    ? `in ${repoShortName(state.repo)}`
    : "across all repositories — select one above to narrow";
  const branchScope = scopedTo(STAGE.BRANCH);
  optlist(el.branchFilters, tally((c) => c.branches, branchScope), {
    active: state.branch,
    onSelect: (k) => select("branch", k),
    allLabel: "All branches",
    allCount: branchScope.length,
    search: el.branchSearch.value,
  });

  // Folders/services are scoped by repository and, once picked, by branch too.
  el.folderScope.textContent = state.repo
    ? state.branch
      ? `in ${repoShortName(state.repo)} on ${state.branch}`
      : `in ${repoShortName(state.repo)}`
    : "across all repositories — select one above to narrow";
  el.folderPanel.classList.toggle("hidden", !state.data.folders?.enabled);
  const folderScope = scopedTo(STAGE.FOLDER);
  optlist(el.folderFilters, tally(folderKeysOf, folderScope), {
    active: state.folder,
    onSelect: (k) => select("folder", k),
    allLabel: "All services",
    allCount: folderScope.length,
    search: el.folderSearch.value,
    label: folderLabel,
    // With a repo selected the header is redundant; otherwise group by repo so
    // same-named folders in different repos stay visibly distinct.
    rowLabel: (key) => splitFolderKey(key).folder,
    group: state.repo ? null : (key) => repoShortName(splitFolderKey(key).repoKey),
  });

  const authorScope = scopedTo(STAGE.AUTHOR);
  optlist(el.authorFilters, tally((c) => [c.author_name], authorScope), {
    active: state.author,
    onSelect: (k) => select("author", k),
    allCount: authorScope.length,
    allLabel: "All authors",
  });
}

function visibleCommits() {
  const query = el.search.value.trim().toLowerCase();
  const scopeMode = el.scopeCommits.value; // "all" | "off-default" | "reverts" | "churn"

  return state.data.commits.filter((c) => {
    if (state.provider && c.provider !== state.provider) return false;
    if (state.repo && c.repo_key !== state.repo) return false;
    if (state.author && c.author_name !== state.author) return false;
    if (scopeMode === "off-default" && c.on_default) return false;
    if (scopeMode === "reverts" && !c.is_revert) return false;
    if (scopeMode === "churn" && !c._churn) return false;

    // A selected branch narrows the chips to that branch; otherwise show them all.
    const branches = state.branch
      ? c.branches.filter((b) => b === state.branch)
      : c.branches;
    if (!branches.length) return false;
    c._visibleBranches = branches;

    // Folder selection is keyed by (repo, folder), so picking `backend` in one
    // repo never matches a `backend` in another.
    const folderKeys = state.folder
      ? folderKeysOf(c).filter((k) => k === state.folder)
      : folderKeysOf(c);
    if (!folderKeys.length) return false;
    c._visibleFolderKeys = folderKeys;
    const folders = folderKeys.map((k) => splitFolderKey(k).folder);
    c._visibleFolders = folders;

    if (query) {
      const haystack = `${c.title} ${c.body} ${c.author_name} ${c.author_login || ""} ${c.sha} ${c.repo} ${folders.join(" ")}`;
      if (!haystack.toLowerCase().includes(query)) return false;
    }
    return true;
  });
}

/* ---------- render ---------- */

function renderStats(commits) {
  const authors = new Set(commits.map((c) => c.author_name));
  const branches = new Set(commits.flatMap((c) => c._visibleBranches.map((b) => `${c.repo_key}#${b}`)));
  const repos = new Set(commits.map((c) => c.repo_key));
  const offDefault = commits.filter((c) => !c.on_default).length;

  const filesChanged = commits.reduce((sum, c) => sum + (c.files_changed || 0), 0);

  const tiles = [
    ["Commits", commits.length],
    ["Repositories", `${repos.size} / ${state.data.repos.length}`],
    ["Active branches", branches.size],
    ["Contributors", authors.size],
    ["Files changed", filesChanged.toLocaleString()],
    ["Not on default branch", offDefault],
    ["Reverts", commits.filter((c) => c.is_revert).length],
  ];

  if (state.data.folders?.enabled) {
    // Count (repo, folder) pairs — a `backend` in two repos is two services.
    const services = new Set(
      commits.flatMap((c) => c._visibleFolderKeys).filter((k) => splitFolderKey(k).folder !== NO_FOLDER)
    );
    tiles.splice(3, 0, ["Services touched", services.size]);
    tiles.push([`Rapid re-touches (${CHURN_WINDOW_HOURS}h)`, commits.filter((c) => c._churn).length]);
  }

  // The first tile is the pane's hero figure — exactly one per view.
  el.stats.innerHTML = tiles
    .map(
      ([label, value], i) =>
        `<div class="stat${i === 0 ? " hero" : ""}"><div class="value">${escapeHtml(String(value))}</div><div class="label">${escapeHtml(label)}</div></div>`
    )
    .join("");
}

function absoluteTime(iso) {
  if (!iso) return "";
  return new Date(iso).toLocaleString(undefined, {
    dateStyle: "full",
    timeStyle: "short",
  });
}

function commitNode(c) {
  const node = document.createElement("article");
  node.className = "commit";

  const initials = (c.author_name || "?").trim().slice(0, 2).toUpperCase();
  const avatar = c.avatar_url
    ? `<img class="avatar" src="${escapeAttr(c.avatar_url)}" alt="" loading="lazy" />`
    : `<div class="avatar avatar-fallback" aria-hidden="true">${escapeHtml(initials)}</div>`;

  const author = c.author_login
    ? `<a class="author" href="https://github.com/${encodeURIComponent(c.author_login)}" target="_blank" rel="noopener">${escapeHtml(c.author_name)}</a>`
    : `<span class="author">${escapeHtml(c.author_name)}</span>`;

  // Only name the provider when more than one is in play; otherwise it is noise.
  const sourceBadge =
    state.data.providers.length > 1
      ? `<span class="chip source ${c.provider}">${escapeHtml(PROVIDER_LABEL[c.provider] || c.provider)}</span>`
      : "";

  const revertBadge = c.is_revert
    ? `<span class="chip revert" title="Commit message indicates this reverts a prior change">Revert</span>`
    : "";
  const churnBadge = c._churn
    ? `<span class="chip churn" title="Another commit touched the same folder within ${CHURN_WINDOW_HOURS}h">Rework</span>`
    : "";

  const defaultBranch = defaultBranchOf(c.repo_key);
  const branchChips = c._visibleBranches
    .map((b) => {
      const isDefault = b === defaultBranch;
      const title = isDefault ? `Default branch: ${b}` : `Branch: ${b}`;
      return `<span class="chip branch${isDefault ? " is-default" : ""}" title="${escapeAttr(title)}">${ICON.branch}<span class="txt">${escapeHtml(b)}</span></span>`;
    })
    .join("");

  const folderChips = state.data.folders?.enabled
    ? c._visibleFolders
        .map((f) => {
          const unknown = f === NO_FOLDER;
          const title = unknown ? "No folder data for this commit" : `Service / folder: ${f}`;
          return `<span class="chip folder${unknown ? " unknown" : ""}" title="${escapeAttr(title)}">${unknown ? "" : ICON.folder}<span class="txt">${escapeHtml(f)}</span></span>`;
        })
        .join("")
    : "";

  const fileCount = c.files_changed
    ? `<span class="files" title="${c.files_truncated ? "At least " : ""}${c.files_changed} file${c.files_changed === 1 ? "" : "s"} changed">${c.files_changed} file${c.files_changed === 1 ? "" : "s"}${c.files_truncated ? "+" : ""}</span>`
    : "";

  // Full message is opt-in; native <details> gives keyboard and screen-reader
  // behaviour for free.
  const body = c.body
    ? `<details class="commit-body">
         <summary>${ICON.caret}<span>Full message</span></summary>
         <pre>${escapeHtml(c.body)}</pre>
       </details>`
    : "";

  node.innerHTML = `
    ${avatar}
    <div class="commit-main">
      <h3 class="commit-title"><a href="${escapeAttr(c.url)}" target="_blank" rel="noopener">${escapeHtml(c.title)}</a></h3>
      <div class="commit-meta">
        ${author}
        <span class="sep">·</span>
        <span class="repo-name">${escapeHtml(c.repo)}</span>
        ${sourceBadge}
        ${revertBadge}
        ${churnBadge}
      </div>
      <div class="chips">${folderChips}${branchChips}</div>
      ${body}
    </div>
    <div class="commit-side">
      <time class="when" datetime="${escapeAttr(c.date || "")}" title="${escapeAttr(absoluteTime(c.date))}">${relativeTime(c.date)}</time>
      <span class="side-row">
        <a class="sha" href="${escapeAttr(c.url)}" target="_blank" rel="noopener" title="View commit ${escapeAttr(c.sha || "")}">${escapeHtml((c.sha || "").slice(0, 7))}</a>
        ${fileCount}
      </span>
    </div>`;
  return node;
}

function defaultBranchOf(repoKey) {
  return state.data.repos.find((r) => r.key === repoKey)?.default_branch;
}

function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = text ?? "";
  return div.innerHTML;
}

/* escapeHtml is not enough inside a quoted attribute — it leaves `"` intact. */
function escapeAttr(text) {
  return escapeHtml(text).replace(/"/g, "&quot;");
}

function render() {
  if (!state.data) return;
  const commits = visibleCommits();

  renderStats(commits);

  el.resultCount.textContent = `${commits.length} commit${commits.length === 1 ? "" : "s"}`;

  // Spell out the full scope in one line, so what you are looking at — and what
  // an exported report will contain — is never ambiguous.
  const crumbs = [
    rangeLabel(),
    state.provider && (PROVIDER_LABEL[state.provider] || state.provider),
    state.repo && repoNameOf(state.repo),
    state.folder && splitFolderKey(state.folder).folder,
    state.branch && `branch ${state.branch}`,
    state.author,
    el.scopeCommits.value !== "all" && selectedText(el.scopeCommits),
    el.search.value.trim() && `“${el.search.value.trim()}”`,
  ].filter(Boolean);
  el.scopeLine.textContent = crumbs.join("  ›  ");

  el.feed.innerHTML = "";
  if (!commits.length) {
    const anyData = state.data.commits.length > 0;
    el.feed.appendChild(
      stateNode(
        anyData ? "No commits match" : "No commits in this window",
        anyData
          ? "Widen the selection on the left, clear the search box, or reset the selection."
          : "Nothing was committed in the selected period. Try a longer window from the menu above."
      )
    );
    return;
  }

  // Group headers carry a name and a count in separate spans so the count can sit
  // flush right and the name can ellipsis without eating it.
  const addHead = (name, count) => {
    const head = document.createElement("div");
    head.className = "group-head";
    head.title = name;

    const label = document.createElement("span");
    label.className = "group-name";
    label.textContent = name;

    const num = document.createElement("span");
    num.className = "group-count";
    num.textContent = `${count} commit${count === 1 ? "" : "s"}`;

    head.append(label, num);
    el.feed.appendChild(head);
  };

  // Folder grouping is one-to-many: a commit touching three services is listed
  // under each of them. The other modes are one-to-one, so they stream in order.
  if (el.groupBy.value === "folder") {
    // Grouped by (repo, folder) so same-named services in different repos stay apart.
    const byFolder = new Map();
    for (const c of commits) {
      for (const key of c._visibleFolderKeys) {
        if (!byFolder.has(key)) byFolder.set(key, []);
        byFolder.get(key).push(c);
      }
    }
    const ordered = [...byFolder.entries()].sort(
      (a, b) => b[1].length - a[1].length || folderLabel(a[0]).localeCompare(folderLabel(b[0]))
    );
    for (const [key, group] of ordered) {
      const { repoKey, folder } = splitFolderKey(key);
      addHead(state.repo ? folder : `${repoNameOf(repoKey)} / ${folder}`, group.length);
      for (const c of group) el.feed.appendChild(commitNode(c));
    }
    return;
  }

  const groupKey =
    el.groupBy.value === "repo"
      ? (c) => c.repo
      : (c) => dayLabel(c.date);

  // Count each group up front so its header can show a total.
  const counts = new Map();
  for (const c of commits) counts.set(groupKey(c), (counts.get(groupKey(c)) || 0) + 1);

  let current = null;
  for (const c of commits) {
    const key = groupKey(c);
    if (key !== current) {
      current = key;
      addHead(key, counts.get(key));
    }
    el.feed.appendChild(commitNode(c));
  }
}

function stateNode(title, hint) {
  const box = document.createElement("div");
  box.className = "state";
  box.innerHTML = `
    ${ICON.search}
    <p class="state-title">${escapeHtml(title)}</p>
    <p class="state-hint">${escapeHtml(hint)}</p>`;
  return box;
}

/* ---------- branch comparison ---------- */

/* A comparison is always within one repository, and always three-dot: what
 * `head` has that `base` does not, measured from their merge base. */

const FILE_STATUS_ORDER = { added: 0, modified: 1, renamed: 2, copied: 3, changed: 4, removed: 5 };

async function loadBranchesForCompare() {
  const repoKey = state.repo;
  el.comparePanel.classList.toggle("dimmed", !repoKey);

  if (!repoKey) {
    el.compareScope.textContent = "Select a single repository above to compare its branches.";
    el.compareBase.innerHTML = "";
    el.compareHead.innerHTML = "";
    el.compareRun.disabled = true;
    el.compareSwap.disabled = true;
    return;
  }

  el.compareScope.textContent = `in ${repoNameOf(repoKey)}`;
  el.compareRun.disabled = true;
  el.compareBase.innerHTML = '<option>Loading…</option>';
  el.compareHead.innerHTML = "";

  try {
    const res = await fetch(`/api/branches?key=${encodeURIComponent(repoKey)}`);
    const body = await res.json();
    if (!res.ok) throw new Error(body.detail || res.statusText);

    const names = body.branches || [];
    if (names.length < 2) {
      el.compareScope.textContent = `${repoNameOf(repoKey)} has only one branch — nothing to compare.`;
      el.compareBase.innerHTML = "";
      el.compareRun.disabled = true;
      el.compareSwap.disabled = true;
      return;
    }

    const options = names
      .map((n) => `<option value="${escapeAttr(n)}">${escapeHtml(n)}</option>`)
      .join("");
    el.compareBase.innerHTML = options;
    el.compareHead.innerHTML = options;

    // Default to "what would merging X into the default branch bring?", which is
    // the question people almost always mean.
    const fallback = body.default_branch && names.includes(body.default_branch) ? body.default_branch : names[0];
    el.compareBase.value = fallback;
    el.compareHead.value = names.find((n) => n !== fallback) || names[0];
    el.compareRun.disabled = false;
    el.compareSwap.disabled = false;
  } catch (err) {
    el.compareScope.textContent = `Could not list branches: ${err.message}`;
    el.compareBase.innerHTML = "";
    el.compareHead.innerHTML = "";
    el.compareRun.disabled = true;
    el.compareSwap.disabled = true;
  }
}

async function runCompare() {
  const repoKey = state.repo;
  const base = el.compareBase.value;
  const head = el.compareHead.value;
  if (!repoKey || !base || !head) return;

  if (base === head) {
    showBanner("<strong>Pick two different branches.</strong>", true);
    return;
  }

  el.compareRun.disabled = true;
  el.compareRun.textContent = "Comparing…";
  try {
    const params = new URLSearchParams({ key: repoKey, base, head });
    const res = await fetch(`/api/compare?${params}`);
    const body = await res.json();
    if (!res.ok) {
      showBanner(`<strong>Could not compare.</strong> ${body.detail || res.statusText}`, true);
      return;
    }
    state.compare = body;
    hideBanner();
    renderCompare();
  } catch (err) {
    showBanner(`<strong>Could not compare.</strong> ${err.message}`, true);
  } finally {
    el.compareRun.disabled = false;
    el.compareRun.textContent = "Compare";
  }
}

function exitCompare() {
  state.compare = null;
  el.compareView.classList.add("hidden");
  el.feedView.classList.remove("hidden");
}

function renderCompare() {
  const c = state.compare;
  if (!c) return;

  el.feedView.classList.add("hidden");
  el.compareView.classList.remove("hidden");

  el.compareHeading.textContent = `${c.base}  ←  ${c.head}`;
  el.compareSub.textContent =
    `${c.repo} · what “${c.head}” has that “${c.base}” does not, measured from their merge base` +
    (c.merge_base ? ` (${c.merge_base.slice(0, 7)})` : "");

  el.compareStatus.textContent = c.status;
  el.compareStatus.className = `status-badge is-${c.status}`;
  el.compareLink.href = c.html_url || "#";
  el.compareLink.classList.toggle("hidden", !c.html_url);

  const tiles = [
    ["Commits ahead", c.ahead_by],
    ["Commits behind", c.behind_by],
    ["Files changed", c.files_changed],
    ["Lines added", `+${(c.additions || 0).toLocaleString()}`],
    ["Lines removed", `−${(c.deletions || 0).toLocaleString()}`],
    ["Services touched", (c.folders || []).filter((f) => f !== "(repo root)").length],
  ];
  el.compareStats.innerHTML = tiles
    .map(
      ([label, value], i) =>
        `<div class="stat${i === 0 ? " hero" : ""}"><div class="value">${escapeHtml(String(value))}</div><div class="label">${escapeHtml(label)}</div></div>`
    )
    .join("");

  el.compareBody.replaceChildren();

  if (c.status === "identical") {
    el.compareBody.appendChild(
      stateNode(
        "These branches are identical",
        `“${c.head}” has nothing that “${c.base}” does not, and vice versa.`
      )
    );
    return;
  }

  if (!c.ahead_by) {
    el.compareBody.appendChild(
      stateNode(
        `“${c.head}” is ${c.behind_by} commit${c.behind_by === 1 ? "" : "s"} behind`,
        `Nothing on “${c.head}” is missing from “${c.base}”. Swap the direction to see what “${c.base}” added.`
      )
    );
    return;
  }

  // --- services touched -------------------------------------------------
  if ((c.folders || []).length) {
    const box = document.createElement("div");
    box.className = "compare-section";
    box.innerHTML =
      `<h3>Services touched</h3><div class="chips">` +
      c.folders
        .map(
          (f) =>
            `<span class="chip folder${f === "(repo root)" ? " unknown" : ""}">${f === "(repo root)" ? "" : ICON.folder}<span class="txt">${escapeHtml(f)}</span></span>`
        )
        .join("") +
      `</div>`;
    el.compareBody.appendChild(box);
  }

  // --- changed files ----------------------------------------------------
  const files = [...(c.files || [])].sort(
    (a, b) =>
      (FILE_STATUS_ORDER[a.status] ?? 9) - (FILE_STATUS_ORDER[b.status] ?? 9) ||
      a.path.localeCompare(b.path)
  );
  if (files.length) {
    const box = document.createElement("div");
    box.className = "compare-section";
    const rows = files
      .map(
        (f) => `
        <tr>
          <td><span class="file-status is-${escapeAttr(f.status)}">${escapeHtml(f.status)}</span></td>
          <td class="file-path">${escapeHtml(f.path)}${
            f.previous_path ? `<span class="muted"> ← ${escapeHtml(f.previous_path)}</span>` : ""
          }</td>
          <td class="num add">${f.binary ? "—" : `+${f.additions}`}</td>
          <td class="num del">${f.binary ? "—" : `−${f.deletions}`}</td>
        </tr>`
      )
      .join("");
    box.innerHTML = `
      <h3>Changed files <span class="muted">(${files.length})</span></h3>
      <div class="table-scroll">
        <table class="file-table">
          <thead><tr><th>Status</th><th>File</th><th class="num">Added</th><th class="num">Removed</th></tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
      ${c.files_truncated ? `<p class="muted note">${c.files_truncated} further files are not listed.</p>` : ""}`;
    el.compareBody.appendChild(box);
  }

  // --- commits ----------------------------------------------------------
  const box = document.createElement("div");
  box.className = "compare-section";
  const heading = document.createElement("h3");
  heading.innerHTML = `Commits on “${escapeHtml(c.head)}” <span class="muted">(${c.commits.length})</span>`;
  box.appendChild(heading);

  const feed = document.createElement("div");
  feed.className = "feed";
  for (const commit of c.commits) {
    // The feed's row renderer expects these derived fields.
    commit._visibleBranches = commit.branches;
    commit._visibleFolders = commit.folders || [];
    commit._visibleFolderKeys = (commit.folders || []).map((f) => `${commit.repo_key}${FOLDER_SEP}${f}`);
    feed.appendChild(commitNode(commit));
  }
  box.appendChild(feed);

  if (c.commits_truncated) {
    const note = document.createElement("p");
    note.className = "muted note";
    note.textContent = `${c.commits_truncated} older commits are counted above but not listed — GitHub caps a comparison at 250 commits.`;
    box.appendChild(note);
  }
  el.compareBody.appendChild(box);
}

/* ---------- report export ---------- */

function selectedText(select) {
  return select.options[select.selectedIndex]?.text || "";
}

/** A comparison's cover page: the branches, not the date range, define its scope. */
function compareCriteria() {
  const c = state.compare;
  return {
    since: (c.commits[c.commits.length - 1]?.date || "").slice(0, 10) || null,
    until: (c.commits[0]?.date || "").slice(0, 10) || null,
    range: `Branch comparison — not a date range`,
    comparison: `${c.base}  ←  ${c.head}  ·  ${c.ahead_by} ahead, ${c.behind_by} behind`,
    repository: c.repo,
    source: PROVIDER_LABEL[c.provider] || c.provider,
    branch: c.head,
    show: `Commits on “${c.head}” absent from “${c.base}”`,
    merge_base: c.merge_base ? c.merge_base.slice(0, 7) : null,
    files: `${c.files_changed} changed, +${c.additions} / −${c.deletions}`,
    grouped_by: "Comparison order",
    generated_at: new Date().toISOString(),
  };
}

/** The filters in force, in words — this becomes the report's cover page. */
function currentCriteria() {
  return {
    since: el.dateFrom.value || (state.data?.since || "").slice(0, 10),
    until: el.dateTo.value || null,
    range: rangeLabel(),
    source: state.provider ? PROVIDER_LABEL[state.provider] || state.provider : null,
    repository: state.repo ? repoNameOf(state.repo) : null,
    service: state.folder ? folderLabel(state.folder) : null,
    branch: state.branch || null,
    author: state.author || null,
    show: selectedText(el.scopeCommits),
    search: el.search.value.trim() || null,
    grouped_by: selectedText(el.groupBy),
    generated_at: new Date().toISOString(),
  };
}

/* The rows on screen are posted with the request, so the document is exactly
 * what you are looking at — the server never re-runs the filters. */
async function exportReport(format) {
  const comparingNow = Boolean(state.compare);
  const button = comparingNow
    ? format === "pdf" ? el.compareExportPdf : el.compareExportPptx
    : format === "pdf" ? el.exportPdf : el.exportPptx;
  const original = button.textContent;
  const comparing = comparingNow;
  const commits = comparing ? state.compare.commits : visibleCommits();

  if (!commits.length) {
    showBanner("<strong>Nothing to export.</strong> The current filter matches no commits.", true);
    return;
  }

  button.disabled = true;
  button.textContent = "Building…";
  try {
    const res = await fetch("/api/report", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        format,
        criteria: comparing ? compareCriteria() : currentCriteria(),
        commits: commits.map((c) => ({
          date: c.date,
          repo: c.repo,
          provider: c.provider,
          sha: c.sha,
          title: c.title,
          author_name: c.author_name,
          // Send what is *visible*, so a narrowed service or branch selection is
          // reflected in the document rather than the commit's full membership.
          branches: c._visibleBranches || c.branches,
          folders: c._visibleFolders || c.folders,
          files_changed: c.files_changed,
          on_default: c.on_default,
          url: c.url,
        })),
      }),
    });

    if (!res.ok) {
      let detail = res.statusText;
      try {
        detail = (await res.json()).detail || detail;
      } catch {
        /* non-JSON error body */
      }
      showBanner(`<strong>Report failed.</strong> ${detail}`, true);
      return;
    }

    const blob = await res.blob();
    const name =
      res.headers.get("Content-Disposition")?.match(/filename="([^"]+)"/)?.[1] ||
      `repo-changes.${format}`;

    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = name;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
  } catch (err) {
    showBanner(`<strong>Report failed.</strong> ${err.message}`, true);
  } finally {
    button.disabled = false;
    button.textContent = original;
  }
}

/* ---------- wiring ---------- */

/* The sticky group headers must sit exactly below the topbar. The topbar's height
 * changes when its controls wrap, so measure it rather than hard-coding a value. */
function syncTopbarHeight() {
  const h = el.topbar?.offsetHeight;
  if (h) document.documentElement.style.setProperty("--topbar-h", `${h}px`);
}
window.addEventListener("resize", syncTopbarHeight);
syncTopbarHeight();

el.refresh.addEventListener("click", () => load({ refresh: true }));
el.preset.addEventListener("change", () => {
  applyPreset(el.preset.value);
  load();
});
// Editing either date by hand is what "custom" means — no need to pick it first.
for (const input of [el.dateFrom, el.dateTo]) {
  input.addEventListener("change", () => {
    el.preset.value = "custom";
    load();
  });
}
el.dateClear.addEventListener("click", () => {
  el.dateTo.value = "";
  el.preset.value = "custom";
  load();
});
el.compareRun.addEventListener("click", runCompare);
el.compareExit.addEventListener("click", exitCompare);
el.compareSwap.addEventListener("click", () => {
  const base = el.compareBase.value;
  el.compareBase.value = el.compareHead.value;
  el.compareHead.value = base;
  if (state.compare) runCompare();
});
el.compareExportPdf.addEventListener("click", () => exportReport("pdf"));
el.compareExportPptx.addEventListener("click", () => exportReport("pptx"));
el.exportPdf.addEventListener("click", () => exportReport("pdf"));
el.exportPptx.addEventListener("click", () => exportReport("pptx"));
el.search.addEventListener("input", render);
el.branchSearch.addEventListener("input", buildFilters);
el.folderSearch.addEventListener("input", buildFilters);
el.scopeCommits.addEventListener("change", render);
el.groupBy.addEventListener("change", render);
el.reset.addEventListener("click", () => {
  exitCompare();
  state.provider = null;
  state.repo = null;
  state.folder = null;
  state.branch = null;
  state.author = null;
  el.search.value = "";
  el.branchSearch.value = "";
  el.folderSearch.value = "";
  el.scopeCommits.value = "all";
  el.groupBy.value = "day";
  buildFilters();
  render();
});

(async function init() {
  const cfg = await fetch("/api/config").then((r) => r.json());
  // Seed the range from the configured default lookback.
  const seed = String(cfg.defaults.days);
  el.preset.value = [...el.preset.options].some((o) => o.value === seed) ? seed : "custom";
  applyPreset(el.preset.value === "custom" ? cfg.defaults.days : el.preset.value);

  if (!cfg.configured) {
    showBanner("<strong>Setup needed.</strong> Copy <code>config.example.yaml</code> to <code>config.yaml</code> and list your repositories, then restart.", true);
    el.feed.replaceChildren(
      stateNode("No repositories configured", "Copy config.example.yaml to config.yaml, list your repositories, then restart.")
    );
    return;
  }
  // Only warn about a missing GitHub token if GitHub repos are actually tracked.
  state.noToken = cfg.repos.length > 0 && !cfg.has_token;
  load();
})();
