/* Repo Change Dashboard — one commit feed across providers, repos, branches, services. */

/* Build marker — check this in the console to be sure you are not looking at a
 * cached script from a previous version. */
const APP_BUILD = "2026-08-26.tabs";

/* The ?v= fingerprint this very file was fetched with. Compared against the
 * server's current fingerprint in init(), so a stale cached script reports
 * itself instead of failing in confusing ways. */
const LOADED_VERSION = (() => {
  // Read straight off the src string: `new URL()` would make this line depend on
  // a global that need not exist wherever this file is evaluated.
  const src = document.currentScript?.src || "";
  return (src.match(/[?&]v=([^&]*)/) || [])[1] || "";
})();

const $ = (id) => {
  const node = document.getElementById(id);
  if (!node) console.warn(`[dashboard] no element with id "${id}" — a control will be inert`);
  return node;
};

/* Attach a listener without letting one missing element take down the rest of
 * the page. Before this, a single stale id threw during wiring and every
 * listener declared after it — including the tab bar — was never attached. */
function on(target, event, handler) {
  if (!target) return false;
  target.addEventListener(event, handler);
  return true;
}

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
  tag:
    '<svg class="ic" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round" aria-hidden="true"><path d="M7.6 2H3.2A1.2 1.2 0 0 0 2 3.2v4.4c0 .32.13.62.35.85l5.6 5.6a1.2 1.2 0 0 0 1.7 0l4.3-4.3a1.2 1.2 0 0 0 0-1.7l-5.6-5.6A1.2 1.2 0 0 0 7.6 2Z"/><circle cx="5.2" cy="5.2" r="1" fill="currentColor" stroke="none"/></svg>',
  copy:
    '<svg class="ic" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round" aria-hidden="true"><rect x="5.5" y="5.5" width="8" height="8" rx="1.4"/><path d="M10.5 5.5v-1a1.4 1.4 0 0 0-1.4-1.4H3.9A1.4 1.4 0 0 0 2.5 4.5v5.2a1.4 1.4 0 0 0 1.4 1.4h1"/></svg>',
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
  tipCard: $("tip-card"),
  layout: $("layout"),
  sidebar: $("sidebar"),
  panelActivity: $("panel-activity"),
  panelSummary: $("panel-summary"),
  panelCompare: $("panel-compare"),
  panelLookup: $("panel-lookup"),
  compareRepo: $("compare-repo"),
  compareScope: $("compare-scope"),
  sumRepo: $("sum-repo"),
  sumBranch: $("sum-branch"),
  sumFolder: $("sum-folder"),
  sumRun: $("sum-run"),
  sumCsv: $("sum-csv"),
  sumScope: $("sum-scope"),
  sumBody: $("sum-body"),
  lkSha: $("lk-sha"),
  lkRun: $("lk-run"),
  lkBody: $("lk-body"),
  panelTags: $("panel-tags"),
  panelOrder: $("panel-order"),
  ordInput: $("ord-input"),
  ordRepo: $("ord-repo"),
  ordBranch: $("ord-branch"),
  ordRun: $("ord-run"),
  ordClear: $("ord-clear"),
  ordScope: $("ord-scope"),
  ordProblems: $("ord-problems"),
  ordBody: $("ord-body"),
  tagsRepo: $("tags-repo"),
  tagsScope: $("tags-scope"),
  tagsRefresh: $("tags-refresh"),
  tagsBody: $("tags-body"),
  stagedStrip: $("staged-strip"),
  tagDialog: $("tag-dialog"),
  tagTarget: $("tag-target"),
  tagStepInput: $("tag-step-input"),
  tagStepConfirm: $("tag-step-confirm"),
  tagName: $("tag-name"),
  tagMessage: $("tag-message"),
  tagError: $("tag-error"),
  tagError2: $("tag-error-2"),
  tagSummary: $("tag-summary"),
  tagCancel: $("tag-cancel"),
  tagReview: $("tag-review"),
  tagBack: $("tag-back"),
  tagCreate: $("tag-create"),
  pushDialog: $("push-dialog"),
  pushSummary: $("push-summary"),
  pushError: $("push-error"),
  pushCancel: $("push-cancel"),
  pushConfirm: $("push-confirm"),
  compareBase: $("compare-base"),
  compareHead: $("compare-head"),
  compareRun: $("compare-run"),
  compareSwap: $("compare-swap"),
  compareView: $("compare-view"),
  compareHeading: $("compare-heading"),
  compareSub: $("compare-sub"),
  compareStatus: $("compare-status"),
  compareLink: $("compare-link"),
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
  tab: "activity",
  summary: null,
  lookup: null,
  tags: null,
  order: null,
  staged: [],
  // The row a tag is being created for, and the tag queued for pushing.
  tagTarget: null,
  pushTarget: null,
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

  // A tab opened before the feed landed will have empty pickers; fill them now.
  initSummaryTab();
  initCompareTab();

  // A refresh can retire options the user had selected.
  pruneSelections();
  buildFilters();
  render();
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
  state[dimension] = key;
  pruneSelections();
  buildFilters();
  render();
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
    // Counts distinct tag names on the visible commits, so a zero here answers
    // "why am I seeing no tags?" without hunting through the feed.
    ["Tags", new Set(commits.flatMap((c) => c.tags || [])).size],
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

  // Tags pointing at this exact commit — the release marker, if any.
  const tagChips = (c.tags || [])
    .map(
      (tagName) =>
        `<span class="chip tag" title="${escapeAttr(`Tag: ${tagName}`)}">${ICON.tag}<span class="txt">${escapeHtml(tagName)}</span></span>`
    )
    .join("");

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
      <div class="chips">${tagChips}${folderChips}${branchChips}</div>
      ${body}
    </div>
    <div class="commit-side">
      <time class="when" datetime="${escapeAttr(c.date || "")}" title="${escapeAttr(absoluteTime(c.date))}">${relativeTime(c.date)}</time>
      <span class="side-row">
        <a class="sha" href="${escapeAttr(c.url)}" target="_blank" rel="noopener" title="Open commit ${escapeAttr(c.sha || "")}">${escapeHtml((c.sha || "").slice(0, 7))}</a>
        <button type="button" class="copy-sha" data-sha="${escapeAttr(c.sha || "")}" title="Copy full hash ${escapeAttr(c.sha || "")}" aria-label="Copy full commit hash">${ICON.copy}</button>
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
  renderTipCard(commits);

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

/* Repo, base and head all live in this tab now, so the comparison no longer
 * depends on whatever the Activity sidebar happens to have selected. */
/* The configured repositories, from the feed when it has loaded and from
 * /api/config otherwise. The other tabs must not sit empty just because the
 * Activity feed is still in flight, or failed. */
function repoOptions() {
  if (state.data?.repos?.length) {
    return state.data.repos.map((r) => ({ key: r.key, name: r.full_name }));
  }
  const cfg = state.config;
  if (!cfg) return [];
  return [
    ...(cfg.repos || []).map((name) => ({ key: `github:${name}`, name })),
    ...(cfg.csr_repos || []).map((r) => ({ key: r.key, name: `${r.project}/${r.repo}` })),
  ];
}

function fillRepoSelect(select, selected) {
  const repos = repoOptions();
  select.innerHTML = repos
    .map(
      (r) =>
        `<option value="${escapeAttr(r.key)}"${r.key === selected ? " selected" : ""}>${escapeHtml(r.name)}</option>`
    )
    .join("");
  return select.value || null;
}

/* Config can hide branches from every picker; report how many, so a short list
 * is never quietly short. */
function hiddenBranchNote(body) {
  return body?.hidden ? ` · ${body.hidden} hidden by branch_exclude` : "";
}

async function fetchBranches(repoKey) {
  const res = await fetch(`/api/branches?key=${encodeURIComponent(repoKey)}`);
  const body = await res.json();
  if (!res.ok) throw new Error(body.detail || res.statusText);
  return body;
}

async function loadBranchesForCompare() {
  const repoKey = el.compareRepo.value;
  if (!repoKey) return;

  el.compareScope.textContent = `Loading branches for ${repoNameOf(repoKey)}…`;
  el.compareRun.disabled = true;
  el.compareSwap.disabled = true;
  el.compareBase.innerHTML = "";
  el.compareHead.innerHTML = "";

  try {
    const body = await fetchBranches(repoKey);
    const names = body.branches || [];

    if (names.length < 2) {
      el.compareScope.textContent = `${repoNameOf(repoKey)} has ${
        names.length === 1 ? "only one branch" : "no branches"
      } — nothing to compare.${hiddenBranchNote(body)}`;
      return;
    }

    const options = names
      .map((n) => `<option value="${escapeAttr(n)}">${escapeHtml(n)}</option>`)
      .join("");
    el.compareBase.innerHTML = options;
    el.compareHead.innerHTML = options;

    // Default to "what would merging X into the default branch bring?".
    const fallback = names.includes(body.default_branch) ? body.default_branch : names[0];
    el.compareBase.value = fallback;
    el.compareHead.value = names.find((n) => n !== fallback) || names[0];
    el.compareScope.textContent =
      `${names.length} branch${names.length === 1 ? "" : "es"} in ${repoNameOf(repoKey)}` +
      hiddenBranchNote(body);
    el.compareRun.disabled = false;
    el.compareSwap.disabled = false;
  } catch (err) {
    el.compareScope.textContent = `Could not list branches: ${err.message}`;
  }
}

async function runCompare() {
  const repoKey = el.compareRepo.value;
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
  el.compareView?.classList.add("hidden");
}

function renderCompare() {
  const c = state.compare;
  if (!c) return;

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

/* ---------- commit hash + tag readout ---------- */

async function copyText(text, button) {
  const done = (ok) => {
    button.classList.toggle("copied", ok);
    button.classList.toggle("failed", !ok);
    button.title = ok ? "Copied" : "Could not copy — select the hash and copy manually";
    setTimeout(() => {
      button.classList.remove("copied", "failed");
      button.title = `Copy full hash ${text}`;
    }, 1400);
  };
  try {
    await navigator.clipboard.writeText(text);
    done(true);
  } catch {
    // Clipboard access is refused on insecure origins and without a gesture;
    // fall back rather than failing silently.
    try {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.setAttribute("readonly", "");
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      const ok = document.execCommand("copy");
      ta.remove();
      done(ok);
    } catch {
      done(false);
    }
  }
}

/* One delegated listener rather than one per row — the feed is re-rendered on
 * every filter change, and per-row handlers would leak with it. */
function handleCopyClick(event) {
  const button = event.target.closest?.(".copy-sha");
  if (!button) return;
  event.preventDefault();
  copyText(button.dataset.sha || "", button);
}

/* The head of the current selection: which commit a branch + folder is sitting
 * on right now, its full hash, and the release it belongs to. */
function renderTipCard(commits) {
  if (!commits.length || state.compare) {
    el.tipCard.classList.add("hidden");
    return;
  }

  const tip = commits[0]; // visibleCommits() is newest-first
  const scope = [
    state.branch ? `branch ${state.branch}` : null,
    state.folder ? splitFolderKey(state.folder).folder : null,
    !state.branch && !state.folder && state.repo ? repoNameOf(state.repo) : null,
  ].filter(Boolean);

  // Nearest tag at or before the tip, scanning down the visible list.
  const tagIndex = commits.findIndex((c) => (c.tags || []).length);
  const tagged = tagIndex >= 0 ? commits[tagIndex] : null;

  const tagsHere = (tip.tags || []).length
    ? (tip.tags || [])
        .map((n) => `<span class="chip tag">${ICON.tag}<span class="txt">${escapeHtml(n)}</span></span>`)
        .join("")
    : `<span class="muted">no tag on this commit</span>`;

  let nearest = "";
  if (tagged && tagIndex > 0) {
    nearest = `<div class="tip-row">
        <span class="tip-label">Most recent tag</span>
        <span class="tip-value">
          <span class="chip tag">${ICON.tag}<span class="txt">${escapeHtml((tagged.tags || [])[0])}</span></span>
          <span class="muted">${tagIndex} commit${tagIndex === 1 ? "" : "s"} back, at ${escapeHtml(tagged.sha.slice(0, 7))}</span>
        </span>
      </div>`;
  } else if (!tagged) {
    // "None in this range" would be misleading if the repository has no tags at
    // all — it implies widening the date range would turn some up.
    const repoKeys = new Set(commits.map((c) => c.repo_key));
    const tagsInScope = [...repoKeys].reduce(
      (n, key) => n + Object.keys(state.data.tags?.[key] || {}).length,
      0
    );
    const names = [...repoKeys].map(repoNameOf).join(", ");
    nearest = `<div class="tip-row">
        <span class="tip-label">Most recent tag</span>
        <span class="tip-value muted">${
          tagsInScope
            ? "none in the loaded date range — widen it to reach further back"
            : `${escapeHtml(names)} has no tags`
        }</span>
      </div>`;
  }

  el.tipCard.classList.remove("hidden");
  el.tipCard.innerHTML = `
    <div class="tip-head">
      <h3>Latest commit${scope.length ? ` · ${escapeHtml(scope.join(" · "))}` : ""}</h3>
      <span class="muted">${escapeHtml(relativeTime(tip.date))}</span>
    </div>
    <div class="tip-row">
      <span class="tip-label">Commit hash</span>
      <span class="tip-value">
        <code class="full-sha">${escapeHtml(tip.sha)}</code>
        <button type="button" class="copy-sha" data-sha="${escapeAttr(tip.sha)}" title="Copy full hash ${escapeAttr(tip.sha)}" aria-label="Copy full commit hash">${ICON.copy}</button>
        <a class="btn tiny" href="${escapeAttr(tip.url)}" target="_blank" rel="noopener">Open</a>
      </span>
    </div>
    <div class="tip-row">
      <span class="tip-label">Tag here</span>
      <span class="tip-value">${tagsHere}</span>
    </div>
    ${nearest}
    <div class="tip-row">
      <span class="tip-label">Message</span>
      <span class="tip-value">${escapeHtml(tip.title)} <span class="muted">— ${escapeHtml(tip.author_name)}</span></span>
    </div>`;
}

/* ---------- tabs ---------- */

const TABS = {
  activity: { panel: () => el.panelActivity, sidebar: true },
  summary: { panel: () => el.panelSummary, sidebar: false, onShow: initSummaryTab },
  compare: { panel: () => el.panelCompare, sidebar: false, onShow: initCompareTab },
  lookup: { panel: () => el.panelLookup, sidebar: false, onShow: () => el.lkSha.focus?.() },
  tags: { panel: () => el.panelTags, sidebar: false, onShow: initTagsTab },
  order: { panel: () => el.panelOrder, sidebar: false, onShow: initOrderTab },
};

function showTab(name) {
  const tab = TABS[name] || TABS.activity;
  state.tab = name in TABS ? name : "activity";

  for (const [key, def] of Object.entries(TABS)) {
    def.panel().classList.toggle("hidden", key !== state.tab);
    const button = $(`tab-${key}`);
    if (button) {
      button.classList.toggle("is-active", key === state.tab);
      button.setAttribute("aria-selected", String(key === state.tab));
    }
  }
  // Only the activity feed is driven by the sidebar filters; the other tabs
  // carry their own pickers, so the sidebar would just be dead weight.
  el.sidebar.classList.toggle("hidden", !tab.sidebar);
  el.layout.classList.toggle("no-sidebar", !tab.sidebar);
  tab.onShow?.();
}

function initCompareTab() {
  if (el.compareRepo.options.length || !repoOptions().length) return;
  fillRepoSelect(el.compareRepo, state.repo);
  loadBranchesForCompare();
}

/* ---------- summary table ---------- */

function initSummaryTab() {
  if (el.sumRepo.options.length || !repoOptions().length) return;
  fillRepoSelect(el.sumRepo, state.repo);
  loadBranchesForSummary();
}

async function loadBranchesForSummary() {
  const repoKey = el.sumRepo.value;
  if (!repoKey) return;
  el.sumBranch.innerHTML = "<option>Loading…</option>";
  el.sumRun.disabled = true;
  try {
    const body = await fetchBranches(repoKey);
    const names = body.branches || [];
    el.sumBranch.innerHTML = names
      .map((n) => `<option value="${escapeAttr(n)}">${escapeHtml(n)}</option>`)
      .join("");
    if (names.includes(body.default_branch)) el.sumBranch.value = body.default_branch;
    el.sumRun.disabled = !names.length;
    state.summaryBranchNote = hiddenBranchNote(body);

    // Clear the folder scope BEFORE running, not after. Otherwise the previous
    // repository's selection is still in the box when runSummary() reads it, so
    // the request carries a folder that belongs to a different repo — and if the
    // request then fails, that other repo's folders stay on screen.
    el.sumFolder.innerHTML = '<option value="">All folders</option>';
    el.sumFolder.value = "";

    if (names.length) runSummary();
  } catch (err) {
    el.sumBranch.innerHTML = "";
    el.sumScope.textContent = `Could not list branches: ${err.message}`;
  }
}

async function runSummary() {
  const repoKey = el.sumRepo.value;
  const branch = el.sumBranch.value;
  if (!repoKey || !branch) return;

  el.sumRun.disabled = true;
  el.sumRun.textContent = "Loading…";
  try {
    const params = new URLSearchParams({ key: repoKey, branch });
    if (el.sumFolder.value) params.set("folder", el.sumFolder.value);
    if (el.dateFrom.value) params.set("since", el.dateFrom.value);
    if (el.dateTo.value) params.set("until", el.dateTo.value);
    if (!el.dateFrom.value) params.set("days", "14");

    const res = await fetch(`/api/summary?${params}`);
    const body = await res.json();
    if (!res.ok) {
      el.sumBody.replaceChildren(stateNode("Could not build the summary", body.detail || res.statusText));
      return;
    }
    state.summary = body;
    renderSummary();
  } catch (err) {
    el.sumBody.replaceChildren(stateNode("Could not build the summary", err.message));
  } finally {
    el.sumRun.disabled = false;
    el.sumRun.textContent = "Show summary";
  }
}

function renderSummary() {
  const s = state.summary;
  if (!s) return;

  // Repopulate the folder picker from what this branch actually contains,
  // keeping the current choice if it still exists.
  const chosen = el.sumFolder.value;
  el.sumFolder.innerHTML =
    '<option value="">All folders</option>' +
    (s.folders_available || [])
      .map((f) => `<option value="${escapeAttr(f)}">${escapeHtml(f)}</option>`)
      .join("");
  if ((s.folders_available || []).includes(chosen)) el.sumFolder.value = chosen;

  el.sumScope.textContent =
    `${s.repo} › ${s.branch}${s.folder ? ` › ${s.folder}` : ""} · ` +
    `${s.row_count} commit${s.row_count === 1 ? "" : "s"}, ${s.tagged_rows} tagged · ` +
    `${rangeLabel()}` +
    (s.capped ? ` · showing the newest ${s.limit} commits on this branch` : "") +
    (state.summaryBranchNote || "");

  if (!s.rows.length) {
    el.sumBody.replaceChildren(
      stateNode(
        "No commits match",
        s.folder
          ? `Nothing touched “${s.folder}” on ${s.branch} in this date range.`
          : `Nothing was committed on ${s.branch} in this date range. Widen the dates in the header.`
      )
    );
    return;
  }

  // One row per commit; a commit with two tags gets one line per tag so the
  // tag columns never have to hold a list.
  const rows = [];
  for (const r of s.rows) {
    const tags = r.tags.length ? r.tags : [null];
    tags.forEach((tag, i) => {
      rows.push(`
        <tr${i ? ' class="tag-continuation"' : ""}>
          <td class="mono">${
            i
              ? ""
              : `<a href="${escapeAttr(r.url)}" target="_blank" rel="noopener">${escapeHtml(r.sha.slice(0, 10))}</a>
                 <button type="button" class="copy-sha" data-sha="${escapeAttr(r.sha)}" title="Copy full hash ${escapeAttr(r.sha)}" aria-label="Copy full commit hash">${ICON.copy}</button>`
          }</td>
          <td>${i ? "" : escapeHtml(r.author_name)}</td>
          <td class="mono nowrap">${i ? "" : escapeHtml(fmtStamp(r.date))}</td>
          <td>${
            tag
              ? `<span class="chip tag">${ICON.tag}<span class="txt">${escapeHtml(tag.name)}</span></span>`
              : '<span class="muted">—</span>'
          }</td>
          <td>${
            tag
              ? tag.annotated
                ? escapeHtml(tag.tagger_name || "unknown")
                : '<span class="muted" title="A lightweight tag is only a ref: git stores no author or date for it">lightweight</span>'
              : '<span class="muted">—</span>'
          }</td>
          <td class="mono nowrap">${
            tag && tag.tagger_date ? escapeHtml(fmtStamp(tag.tagger_date)) : '<span class="muted">—</span>'
          }</td>
          <td>${i ? "" : escapeHtml(r.title)}</td>
          <td>${
            i
              ? ""
              : `<button type="button" class="btn tiny create-tag" data-sha="${escapeAttr(r.sha)}" data-title="${escapeAttr(r.title)}">Tag…</button>`
          }</td>
        </tr>`);
    });
  }

  const box = document.createElement("div");
  box.className = "table-scroll";
  box.innerHTML = `
    <table class="data-table">
      <thead>
        <tr>
          <th>Commit hash</th><th>Commit creator</th><th>Commit date</th>
          <th>Tag</th><th>Tag creator</th><th>Tag date</th><th>Message</th><th></th>
        </tr>
      </thead>
      <tbody>${rows.join("")}</tbody>
    </table>`;
  el.sumBody.replaceChildren(box);
}

/** Local, unambiguous, and sortable as text — good for a dense table. */
function fmtStamp(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso).slice(0, 19);
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function summaryCsv() {
  const s = state.summary;
  if (!s?.rows?.length) return;
  const cell = (v) => `"${String(v ?? "").replace(/"/g, '""')}"`;
  const lines = [
    ["commit_hash", "commit_creator", "commit_date", "tag", "tag_creator", "tag_date", "message"]
      .map(cell)
      .join(","),
  ];
  for (const r of s.rows) {
    for (const tag of r.tags.length ? r.tags : [null]) {
      lines.push(
        [
          r.sha,
          r.author_name,
          r.date,
          tag?.name || "",
          tag ? (tag.annotated ? tag.tagger_name || "" : "lightweight (no tagger)") : "",
          tag?.tagger_date || "",
          r.title,
        ]
          .map(cell)
          .join(",")
      );
    }
  }
  const name = `summary_${s.repo.replace(/\W+/g, "-")}_${s.branch.replace(/\W+/g, "-")}.csv`;
  downloadBlob(new Blob([lines.join("\n")], { type: "text/csv;charset=utf-8" }), name);
}

function downloadBlob(blob, name) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

/* ---------- creating tags ---------- */

/* Two deliberate steps. Staging writes only to this app's local store; pushing
 * is what reaches the remote, and is confirmed separately. */

async function refreshStaged() {
  try {
    const res = await fetch("/api/tags/staged");
    const body = await res.json();
    state.staged = body.staged || [];
  } catch {
    state.staged = [];
  }
  renderStagedStrip();
}

function renderStagedStrip() {
  const pending = state.staged.filter((s) => !s.pushed);
  const recent = state.staged.filter((s) => s.pushed).slice(0, 3);

  if (!pending.length && !recent.length) {
    el.stagedStrip.classList.add("hidden");
    el.stagedStrip.replaceChildren();
    return;
  }
  el.stagedStrip.classList.remove("hidden");

  const row = (s) => `
    <li class="staged-row${s.pushed ? " is-pushed" : ""}">
      <span class="chip tag">${ICON.tag}<span class="txt">${escapeHtml(s.name)}</span></span>
      <span class="staged-meta">
        ${escapeHtml(s.repo)} · <code>${escapeHtml(s.sha.slice(0, 10))}</code>
        ${s.commit_title ? `· ${escapeHtml(_clipText(s.commit_title, 44))}` : ""}
      </span>
      ${
        s.pushed
          ? '<span class="status-badge is-ahead">pushed</span>'
          : `<span class="status-badge is-behind">local only</span>
             <button type="button" class="btn tiny push-tag" data-id="${s.id}">Push…</button>
             <button type="button" class="btn tiny discard-tag" data-id="${s.id}">Discard</button>`
      }
      ${s.push_error ? `<span class="staged-error">${escapeHtml(s.push_error)}</span>` : ""}
    </li>`;

  el.stagedStrip.innerHTML = `
    <h3>${pending.length ? `${pending.length} tag${pending.length === 1 ? "" : "s"} staged locally` : "Recently pushed"}</h3>
    <ul class="staged-list">${[...pending, ...recent].map(row).join("")}</ul>`;
}

function _clipText(text, limit) {
  const s = String(text || "");
  return s.length <= limit ? s : `${s.slice(0, limit - 1)}…`;
}

function openTagDialog(rowSha, rowTitle) {
  const s = state.summary;
  if (!s) return;
  state.tagTarget = { repo_key: s.repo_key, repo: s.repo, sha: rowSha, title: rowTitle };

  el.tagTarget.innerHTML = `
    <div class="tip-row"><span class="tip-label">Repository</span><span class="tip-value">${escapeHtml(s.repo)}</span></div>
    <div class="tip-row"><span class="tip-label">Commit</span><span class="tip-value"><code class="full-sha">${escapeHtml(rowSha)}</code></span></div>
    <div class="tip-row"><span class="tip-label">Message</span><span class="tip-value">${escapeHtml(rowTitle)}</span></div>`;

  el.tagName.value = "";
  el.tagMessage.value = "";
  showTagStep("input");
  el.tagDialog.showModal?.();
  el.tagName.focus?.();
}

function showTagStep(step) {
  el.tagStepInput.classList.toggle("hidden", step !== "input");
  el.tagStepConfirm.classList.toggle("hidden", step !== "confirm");
  el.tagError.classList.add("hidden");
  el.tagError2.classList.add("hidden");
}

function reviewTag() {
  const name = el.tagName.value.trim();
  if (!name) {
    el.tagError.textContent = "Give the tag a name.";
    el.tagError.classList.remove("hidden");
    return;
  }
  const t = state.tagTarget;
  el.tagSummary.innerHTML = `
    <p>About to create an <strong>annotated</strong> tag:</p>
    <div class="tip-row"><span class="tip-label">Tag</span><span class="tip-value"><span class="chip tag">${ICON.tag}<span class="txt">${escapeHtml(name)}</span></span></span></div>
    <div class="tip-row"><span class="tip-label">Comment</span><span class="tip-value">${escapeHtml(el.tagMessage.value.trim()) || '<span class="muted">none</span>'}</span></div>
    <div class="tip-row"><span class="tip-label">On commit</span><span class="tip-value"><code class="full-sha">${escapeHtml(t.sha)}</code></span></div>
    <div class="tip-row"><span class="tip-label">In</span><span class="tip-value">${escapeHtml(t.repo)}</span></div>
    <p class="dialog-note">Created locally only — the remote is untouched until you push.</p>`;
  showTagStep("confirm");
}

async function createStagedTag() {
  const t = state.tagTarget;
  if (!t) return;
  el.tagCreate.disabled = true;
  el.tagCreate.textContent = "Creating…";
  try {
    const res = await fetch("/api/tags/stage", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        repo_key: t.repo_key,
        sha: t.sha,
        name: el.tagName.value.trim(),
        message: el.tagMessage.value.trim(),
      }),
    });
    const body = await res.json();
    if (!res.ok) {
      el.tagError2.textContent = body.detail || res.statusText;
      el.tagError2.classList.remove("hidden");
      return;
    }
    el.tagDialog.close?.();
    await refreshStaged();
    if (state.summary) runSummary();
  } catch (err) {
    el.tagError2.textContent = err.message;
    el.tagError2.classList.remove("hidden");
  } finally {
    el.tagCreate.disabled = false;
    el.tagCreate.textContent = "Create locally";
  }
}

function openPushDialog(id) {
  const tag = state.staged.find((s) => String(s.id) === String(id));
  if (!tag) return;
  state.pushTarget = tag;
  el.pushSummary.innerHTML = `
    <div class="tip-row"><span class="tip-label">Tag</span><span class="tip-value"><span class="chip tag">${ICON.tag}<span class="txt">${escapeHtml(tag.name)}</span></span></span></div>
    <div class="tip-row"><span class="tip-label">Repository</span><span class="tip-value"><strong>${escapeHtml(tag.repo)}</strong></span></div>
    <div class="tip-row"><span class="tip-label">Commit</span><span class="tip-value"><code class="full-sha">${escapeHtml(tag.sha)}</code></span></div>
    <div class="tip-row"><span class="tip-label">Comment</span><span class="tip-value">${escapeHtml(tag.message) || '<span class="muted">none</span>'}</span></div>`;
  el.pushError.classList.add("hidden");
  el.pushDialog.showModal?.();
}

async function confirmPush() {
  const tag = state.pushTarget;
  if (!tag) return;
  el.pushConfirm.disabled = true;
  el.pushConfirm.textContent = "Pushing…";
  try {
    const res = await fetch(`/api/tags/push/${tag.id}`, { method: "POST" });
    const body = await res.json();
    if (!res.ok) {
      el.pushError.textContent = body.detail || res.statusText;
      el.pushError.classList.remove("hidden");
      await refreshStaged();
      return;
    }
    el.pushDialog.close?.();
    await refreshStaged();
    if (state.summary) runSummary();
    if (state.tags) loadTagsOverview();
  } catch (err) {
    el.pushError.textContent = err.message;
    el.pushError.classList.remove("hidden");
  } finally {
    el.pushConfirm.disabled = false;
    el.pushConfirm.textContent = "Push tag";
  }
}

async function discardStaged(id) {
  const tag = state.staged.find((s) => String(s.id) === String(id));
  if (tag && !window.confirm(`Discard the local tag “${tag.name}”? It has not been pushed.`)) return;
  await fetch(`/api/tags/staged/${id}`, { method: "DELETE" });
  await refreshStaged();
}

/* Delegated so the buttons survive every re-render of the strip. */
function handleTagClicks(event) {
  const push = event.target.closest?.(".push-tag");
  if (push) return openPushDialog(push.dataset.id);
  const discard = event.target.closest?.(".discard-tag");
  if (discard) return discardStaged(discard.dataset.id);
  const create = event.target.closest?.(".create-tag");
  if (create) return openTagDialog(create.dataset.sha, create.dataset.title || "");
}

/* ---------- tags overview ---------- */

function initTagsTab() {
  if (!el.tagsRepo.options.length && repoOptions().length) {
    fillRepoSelect(el.tagsRepo, state.repo);
    loadTagsOverview();
  }
}

async function loadTagsOverview() {
  const repoKey = el.tagsRepo.value;
  if (!repoKey) return;

  el.tagsRefresh.disabled = true;
  el.tagsRefresh.textContent = "Loading…";
  el.tagsBody.replaceChildren(
    stateNode("Reading tags…", `Listing tags in ${repoNameOf(repoKey)} and working out which branches hold them.`)
  );
  try {
    const res = await fetch(`/api/tags/overview?key=${encodeURIComponent(repoKey)}`);
    const body = await res.json();
    if (!res.ok) {
      el.tagsBody.replaceChildren(stateNode("Could not load tags", body.detail || res.statusText));
      return;
    }
    state.tags = body;
    state.staged = body.staged || [];
    renderStagedStrip();
    renderTagsOverview();
  } catch (err) {
    el.tagsBody.replaceChildren(stateNode("Could not load tags", err.message));
  } finally {
    el.tagsRefresh.disabled = false;
    el.tagsRefresh.textContent = "Load tags";
  }
}

const NO_FOLDER_TAG = "(no folder data)";

function renderTagsOverview() {
  const d = state.tags;
  if (!d) return;

  if (d.error) {
    el.tagsScope.textContent = "";
    el.tagsBody.replaceChildren(stateNode(`Could not read ${d.repo}`, d.error));
    return;
  }

  const capNote = [
    d.capped?.tags ? `${d.capped.tags} older tags not branch-checked` : "",
    d.capped?.branches ? `${d.capped.branches} branches not checked` : "",
  ].filter(Boolean);

  el.tagsScope.textContent =
    `${d.repo} · ${d.tags.length} tag${d.tags.length === 1 ? "" : "s"} · ` +
    `${(d.branches_known || []).length} branch${(d.branches_known || []).length === 1 ? "" : "es"}` +
    (capNote.length ? ` · ${capNote.join(", ")}` : "");

  if (!d.tags.length) {
    el.tagsBody.replaceChildren(
      stateNode(
        `No tags in ${d.repo}`,
        "Create one from the Summary table: pick a commit, press Tag…, then push it."
      )
    );
    return;
  }

  /* A tag's commit can touch several folders, so it is listed under each — the
   * per-folder counts therefore sum to more than the repository's tag total. */
  const byFolder = new Map();
  for (const tag of d.tags) {
    for (const folder of tag.folders?.length ? tag.folders : [NO_FOLDER_TAG]) {
      if (!byFolder.has(folder)) byFolder.set(folder, []);
      byFolder.get(folder).push(tag);
    }
  }
  const folders = [...byFolder.entries()].sort(
    (a, b) => b[1].length - a[1].length || a[0].localeCompare(b[0])
  );

  const branchCell = (tag) => {
    // null means "we did not probe", which is not the same as "no branch".
    if (tag.branches === null || tag.branches === undefined) {
      return '<span class="muted" title="Not checked — see the cap noted above">not checked</span>';
    }
    if (!tag.branches.length) {
      return '<span class="muted" title="The tagged commit is not reachable from any branch — it may be on a deleted branch">no branch</span>';
    }
    return tag.branches
      .map(
        (b) =>
          `<span class="chip branch${b === d.default_branch ? " is-default" : ""}">${ICON.branch}<span class="txt">${escapeHtml(b)}</span></span>`
      )
      .join(" ");
  };

  const groups = folders
    .map(
      ([folder, tags]) => `
      <div class="tag-folder">
        <h4>
          <span class="chip folder${folder === NO_FOLDER_TAG ? " unknown" : ""}">${
            folder === NO_FOLDER_TAG ? "" : ICON.folder
          }<span class="txt">${escapeHtml(folder)}</span></span>
          <span class="muted">${tags.length} tag${tags.length === 1 ? "" : "s"}</span>
        </h4>
        <div class="table-scroll">
          <table class="data-table">
            <thead>
              <tr><th>Tag</th><th>Branch</th><th>Commit</th><th>Tag creator</th><th>Tag date</th><th>Comment</th></tr>
            </thead>
            <tbody>
              ${tags
                .map(
                  (tag) => `
                <tr>
                  <td><span class="chip tag">${ICON.tag}<span class="txt">${escapeHtml(tag.name)}</span></span></td>
                  <td>${branchCell(tag)}</td>
                  <td class="mono">${escapeHtml(tag.commit_sha.slice(0, 10))}
                      <button type="button" class="copy-sha" data-sha="${escapeAttr(tag.commit_sha)}" title="Copy full hash ${escapeAttr(tag.commit_sha)}" aria-label="Copy full commit hash">${ICON.copy}</button></td>
                  <td>${
                    tag.annotated
                      ? escapeHtml(tag.tagger_name || "unknown")
                      : '<span class="muted" title="A lightweight tag has no tag object, so git stores no creator or date">lightweight</span>'
                  }</td>
                  <td class="mono nowrap">${
                    tag.tagger_date ? escapeHtml(fmtStamp(tag.tagger_date)) : '<span class="muted">—</span>'
                  }</td>
                  <td>${escapeHtml(tag.message || "") || '<span class="muted">—</span>'}</td>
                </tr>`
                )
                .join("")}
            </tbody>
          </table>
        </div>
      </div>`
    )
    .join("");

  const box = document.createElement("div");
  box.className = "tag-repo";
  box.innerHTML = groups;
  el.tagsBody.replaceChildren(box);
}

/* ---------- find a commit ---------- */

async function runLookup() {
  const raw = el.lkSha.value.trim();
  if (!raw) return;

  el.lkRun.disabled = true;
  el.lkRun.textContent = "Searching…";
  el.lkBody.replaceChildren(stateNode("Searching…", "Checking every configured repository."));
  try {
    const res = await fetch(`/api/lookup?sha=${encodeURIComponent(raw)}`);
    const body = await res.json();
    if (!res.ok) {
      el.lkBody.replaceChildren(stateNode("Could not look that up", body.detail || res.statusText));
      return;
    }
    state.lookup = body;
    renderLookup();
  } catch (err) {
    el.lkBody.replaceChildren(stateNode("Could not look that up", err.message));
  } finally {
    el.lkRun.disabled = false;
    el.lkRun.textContent = "Find commit";
  }
}

function renderLookup() {
  const d = state.lookup;
  if (!d) return;

  if (!d.found) {
    el.lkBody.replaceChildren(
      stateNode(
        "No repository holds that commit",
        `Searched ${d.searched} configured repositor${d.searched === 1 ? "y" : "ies"} for ${d.sha}. ` +
          "Check the hash, or add the repository to config.yaml."
      )
    );
    return;
  }

  el.lkBody.replaceChildren(...d.matches.map(lookupCard));
}

/* Where the commit sits among the commits that touched *that folder* — counted
 * over that directory's history alone, not the repository's. The two can differ
 * sharply: a folder touched rarely will place the same commit much later in its
 * own history than in the branch as a whole. */
function folderPositionSection(m) {
  const entries = (m.folder_positions || []).filter((f) => f.position !== null || f.note);
  if (!entries.length) return "";

  const rows = entries
    .map((f) => {
      if (f.position === null) {
        return `<tr>
          <td><span class="chip folder${f.folder === "(repo root)" ? " unknown" : ""}">${
            f.folder === "(repo root)" ? "" : ICON.folder
          }<span class="txt">${escapeHtml(f.folder)}</span></span></td>
          <td colspan="2" class="muted">${escapeHtml(f.note || "not counted")}</td>
        </tr>`;
      }
      const pct = f.total ? Math.min(100, Math.max(0, (f.position / f.total) * 100)) : null;
      return `<tr>
        <td><span class="chip folder">${ICON.folder}<span class="txt">${escapeHtml(f.folder)}</span></span></td>
        <td class="num">${f.position} <span class="muted">of ${f.total}${f.capped ? "+" : ""}</span></td>
        <td class="graph-cell">${
          pct === null
            ? '<span class="muted">—</span>'
            : `<div class="graph-bar" title="${f.position} of ${f.total} commits that touched ${escapeAttr(f.folder)}">
                 <span class="graph-fill" style="width:${pct.toFixed(1)}%"></span>
                 <span class="graph-marker" style="left:clamp(6px, ${pct.toFixed(1)}%, calc(100% - 6px))"></span>
               </div>`
        }</td>
      </tr>`;
    })
    .join("");

  const branch = entries.find((f) => f.branch)?.branch;
  return `
    <h4 class="lookup-sub">Position within each folder${branch ? ` <span class="muted">on ${escapeHtml(branch)}</span>` : ""}</h4>
    <div class="table-scroll">
      <table class="data-table">
        <thead>
          <tr>
            <th>Folder</th>
            <th class="num" title="Counted only over commits that touched this folder">Position in folder</th>
            <th>Progress through the folder's history</th>
          </tr>
        </thead>
        <tbody>${rows}</tbody>
      </table>
    </div>
    ${
      m.folders_unpositioned
        ? `<p class="muted note">${m.folders_unpositioned} further folder(s) were not counted.</p>`
        : ""
    }
    ${
      entries.some((f) => f.capped)
        ? '<p class="muted note">A “+” means the folder\u2019s history was longer than the range searched, so the total is a lower bound.</p>'
        : ""
    }`;
}

function lookupCard(m) {
  const box = document.createElement("section");
  box.className = "lookup-card";

  const tagRows = m.tags.length
    ? m.tags
        .map(
          (t) => `
        <div class="tip-row">
          <span class="tip-label">${escapeHtml(t.name)}</span>
          <span class="tip-value">
            ${
              t.annotated
                ? `tagged by <strong>${escapeHtml(t.tagger_name || "unknown")}</strong> on ${escapeHtml(fmtStamp(t.tagger_date))}`
                : '<span class="muted">lightweight tag — git records no creator or date for it</span>'
            }
            ${t.message ? `<span class="muted">· ${escapeHtml(t.message)}</span>` : ""}
          </span>
        </div>`
        )
        .join("")
    : `<div class="tip-row"><span class="tip-label">Tags</span><span class="tip-value muted">none on this commit${
        m.nearest_tag
          ? ` · nearest earlier tag <strong>${escapeHtml(m.nearest_tag.name)}</strong>, ${m.nearest_tag.commits_after} commit${m.nearest_tag.commits_after === 1 ? "" : "s"} before it`
          : ""
      }</span></div>`;

  // A bar per branch showing how far along that branch the commit sits.
  const branchRows = m.branches.length
    ? m.branches
        .map((b) => {
          // A filled bar reads as magnitude; this is a *position*, so the track
          // is drawn full width with a marker where the commit sits.
          const pct =
            b.total_commits && b.position
              ? Math.min(100, Math.max(0, (b.position / b.total_commits) * 100))
              : null;
          return `
        <tr>
          <td>
            <span class="chip branch${b.is_default ? " is-default" : ""}">${ICON.branch}<span class="txt">${escapeHtml(b.name)}</span></span>
            ${b.is_head ? '<span class="status-badge is-ahead">at head</span>' : ""}
          </td>
          <td class="num">${b.distance_to_head}</td>
          <td class="num">${b.position ?? "—"}${b.total_commits ? ` <span class="muted">of ${b.total_commits}</span>` : ""}</td>
          <td class="graph-cell">
            ${
              pct === null
                ? '<span class="muted">unknown</span>'
                : `<div class="graph-bar" title="${b.position} of ${b.total_commits} commits on ${escapeHtml(b.name)} — ${b.distance_to_head} since">
                     <span class="graph-fill" style="width:${pct.toFixed(1)}%"></span>
                     <span class="graph-marker" style="left:clamp(6px, ${pct.toFixed(1)}%, calc(100% - 6px))"></span>
                   </div>
                   <span class="graph-legend">root${b.is_head ? " → HEAD (here)" : " → HEAD"}</span>`
            }
          </td>
        </tr>`;
        })
        .join("")
    : `<tr><td colspan="4" class="muted">${
        m.branches_failed?.length
          ? `Could not determine containment — ${m.branches_failed.length} branch probe(s) failed. Try again; if it persists the API may be rate limited.`
          : "No branch in this repository contains the commit — it may be unreachable (on a deleted branch, or only inside a pull request)."
      }</td></tr>`;

  // The two questions people open this tab for — which branch, which service —
  // answered at the top rather than only inside the position table.
  const branchChips = m.branches.length
    ? m.branches
        .map(
          (b) =>
            `<span class="chip branch${b.is_default ? " is-default" : ""}" title="${escapeAttr(
              b.is_head ? `${b.name} — this commit is the tip` : `${b.name} — ${b.distance_to_head} commits since`
            )}">${ICON.branch}<span class="txt">${escapeHtml(b.name)}</span></span>`
        )
        .join("")
    : `<span class="muted">${
        m.branches_failed?.length
          ? "could not be determined"
          : "not reachable from any branch"
      }</span>`;

  /* The directories the files actually sit in — `app/static`, not the `app`
   * service bucket. Inspecting one commit is a different question from grouping
   * activity, so it gets the more specific answer. */
  const dirs = m.directories?.length ? m.directories : m.folders || [];
  const folderChips = dirs.length
    ? dirs
        .map(
          (f) =>
            `<span class="chip folder${f === "(repo root)" ? " unknown" : ""}" title="${escapeAttr(
              f === "(repo root)" ? "Files at the top level of the repository" : `Files changed in ${f}/`
            )}">${f === "(repo root)" ? "" : ICON.folder}<span class="txt">${escapeHtml(f)}</span></span>`
        )
        .join("")
    : '<span class="muted">no folder data for this commit</span>';

  box.innerHTML = `
    <div class="lookup-head">
      <div>
        <h3>${escapeHtml(m.repo)}</h3>
        <p class="muted">${escapeHtml(m.title)}</p>
      </div>
      <span class="chip source ${escapeAttr(m.provider)}">${escapeHtml(PROVIDER_LABEL[m.provider] || m.provider)}</span>
    </div>

    <div class="tip-row">
      <span class="tip-label">Branch</span>
      <span class="tip-value">${branchChips}</span>
    </div>
    <div class="tip-row">
      <span class="tip-label">Folder${dirs.length > 1 ? "s" : ""} changed</span>
      <span class="tip-value">${folderChips}</span>
    </div>

    <div class="tip-row">
      <span class="tip-label">Commit hash</span>
      <span class="tip-value">
        <code class="full-sha">${escapeHtml(m.sha)}</code>
        <button type="button" class="copy-sha" data-sha="${escapeAttr(m.sha)}" title="Copy full hash ${escapeAttr(m.sha)}" aria-label="Copy full commit hash">${ICON.copy}</button>
        <a class="btn tiny" href="${escapeAttr(m.url)}" target="_blank" rel="noopener">Open</a>
      </span>
    </div>
    <div class="tip-row">
      <span class="tip-label">Authored</span>
      <span class="tip-value">${escapeHtml(m.author_name)} on ${escapeHtml(fmtStamp(m.date))}</span>
    </div>
    <div class="tip-row">
      <span class="tip-label">Changes</span>
      <span class="tip-value">${m.files_changed} file${m.files_changed === 1 ? "" : "s"}${
        m.additions != null ? ` · +${m.additions} −${m.deletions}` : ""
      }${m.parents.length ? ` · parent ${escapeHtml(m.parents.map((p) => p.slice(0, 7)).join(", "))}` : " · root commit"}</span>
    </div>
    ${tagRows}

    <h4 class="lookup-sub">Position in the branch</h4>
    <div class="table-scroll">
      <table class="data-table">
        <thead>
          <tr>
            <th>Branch</th>
            <th class="num" title="Commits added on this branch since this one">Commits since</th>
            <th class="num" title="This commit's ordinal from the root of the branch">Position</th>
            <th>Progress along branch</th>
          </tr>
        </thead>
        <tbody>${branchRows}</tbody>
      </table>
    </div>
    ${
      m.branches_unprobed
        ? `<p class="muted note">${m.branches_unprobed} further branches were not checked — only the first ${m.branches_probed} were probed.</p>`
        : ""
    }
    ${folderPositionSection(m)}`;
  return box;
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
  // The tab bar sticks below the topbar, so everything else that sticks has to
  // clear both. Measured rather than assumed, since the topbar wraps.
  const tabs = document.querySelector?.(".tabs");
  if (tabs?.offsetHeight) {
    document.documentElement.style.setProperty("--tabs-h", `${tabs.offsetHeight}px`);
  }
}
window.addEventListener("resize", syncTopbarHeight);
syncTopbarHeight();

/* Tabs are wired FIRST and defensively: navigation between views must survive
 * any one control below being absent. */
for (const name of Object.keys(TABS)) {
  on($(`tab-${name}`), "click", () => showTab(name));
}

on(el.refresh, "click", () => load({ refresh: true }));
on(el.preset, "change", () => {
  applyPreset(el.preset.value);
  load();
});
// Editing either date by hand is what "custom" means — no need to pick it first.
for (const input of [el.dateFrom, el.dateTo]) {
  on(input, "change", () => {
    el.preset.value = "custom";
    load();
  });
}
on(el.dateClear, "click", () => {
  el.dateTo.value = "";
  el.preset.value = "custom";
  load();
});

on(el.compareRepo, "change", () => {
  exitCompare();
  loadBranchesForCompare();
});
on(el.compareRun, "click", runCompare);
on(el.compareSwap, "click", () => {
  const base = el.compareBase.value;
  el.compareBase.value = el.compareHead.value;
  el.compareHead.value = base;
  if (state.compare) runCompare();
});
on(el.compareExportPdf, "click", () => exportReport("pdf"));
on(el.compareExportPptx, "click", () => exportReport("pptx"));

on(el.sumRepo, "change", loadBranchesForSummary);
on(el.sumRun, "click", runSummary);
on(el.sumCsv, "click", summaryCsv);
// Changing branch or folder re-runs immediately once a table is on screen.
for (const control of [el.sumBranch, el.sumFolder]) {
  on(control, "change", runSummary);
}

on(el.tagsRefresh, "click", loadTagsOverview);
on(el.tagsRepo, "change", loadTagsOverview);
on(el.tagReview, "click", reviewTag);
on(el.tagBack, "click", () => showTagStep("input"));
on(el.tagCreate, "click", createStagedTag);
on(el.tagCancel, "click", () => el.tagDialog.close?.());
on(el.pushConfirm, "click", confirmPush);
on(el.pushCancel, "click", () => el.pushDialog.close?.());
on(document, "click", handleTagClicks);
// Enter in the name field advances to the confirmation step rather than
// submitting — creating a tag should never be one keystroke away.
on(el.tagName, "keydown", (event) => {
  if (event.key === "Enter") {
    event.preventDefault();
    reviewTag();
  }
});

on(el.lkRun, "click", runLookup);
on(el.lkSha, "keydown", (event) => {
  if (event.key === "Enter") runLookup();
});

on(el.exportPdf, "click", () => exportReport("pdf"));
on(el.exportPptx, "click", () => exportReport("pptx"));
on(document, "click", handleCopyClick);

console.info(`[dashboard] build ${APP_BUILD}`);

(async function init() {
  const cfg = await fetch("/api/config").then((r) => r.json());
  state.config = cfg;
  refreshStaged();

  // A cached script paired with a fresh page is the one failure that makes every
  // control inert at once; say so plainly rather than letting it look like a bug.
  if (cfg.app_version && LOADED_VERSION && cfg.app_version !== LOADED_VERSION) {
    showBanner(
      "<strong>You are running a cached copy of this page.</strong> " +
        "Reload to pick up the current version " +
        `(loaded <code>${escapeHtml(LOADED_VERSION)}</code>, server has <code>${escapeHtml(cfg.app_version)}</code>).`,
      true
    );
  }
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

/* ---------- order a pasted commit list ---------- */

function initOrderTab() {
  if (el.ordRepo.options.length <= 1 && repoOptions().length) {
    el.ordRepo.innerHTML =
      '<option value="">Detect from the commits</option>' +
      repoOptions()
        .map((r) => `<option value="${escapeAttr(r.key)}">${escapeHtml(r.name)}</option>`)
        .join("");
  }
  el.ordInput.focus?.();
}

async function loadBranchesForOrder() {
  const repoKey = el.ordRepo.value;
  // With no repository chosen the branch is settled server-side, once the
  // commits reveal which repository they belong to.
  if (!repoKey) {
    el.ordBranch.innerHTML = '<option value="">Default branch</option>';
    return;
  }
  try {
    const body = await fetchBranches(repoKey);
    el.ordBranch.innerHTML =
      '<option value="">Default branch</option>' +
      (body.branches || [])
        .map((n) => `<option value="${escapeAttr(n)}">${escapeHtml(n)}</option>`)
        .join("");
  } catch {
    el.ordBranch.innerHTML = '<option value="">Default branch</option>';
  }
}

async function runOrder() {
  const text = el.ordInput.value.trim();
  if (!text) {
    el.ordScope.textContent = "Paste some commit hashes first.";
    return;
  }

  el.ordRun.disabled = true;
  el.ordRun.textContent = "Checking…";
  el.ordBody.replaceChildren(stateNode("Checking each commit…", "Resolving the repository, then placing each commit on the branch."));
  el.ordProblems.replaceChildren();

  try {
    const res = await fetch("/api/commits/order", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        commits: text,
        repo_key: el.ordRepo.value || null,
        branch: el.ordBranch.value || null,
      }),
    });
    const body = await res.json();
    if (!res.ok) {
      el.ordScope.textContent = "";
      el.ordBody.replaceChildren(stateNode("Could not order that list", body.detail || res.statusText));
      return;
    }
    state.order = body;
    renderOrder();
  } catch (err) {
    el.ordBody.replaceChildren(stateNode("Could not order that list", err.message));
  } finally {
    el.ordRun.disabled = false;
    el.ordRun.textContent = "Validate & order";
  }
}

function renderOrder() {
  const d = state.order;
  if (!d) return;

  el.ordScope.textContent =
    `${d.repo} › ${d.branch || "(no branch)"} · ${d.count} commit${d.count === 1 ? "" : "s"} placed` +
    (d.total_commits ? ` on a branch of ${d.total_commits}` : "") +
    ` · ordered by ${d.ordered_by === "ancestry" ? "position in history" : "date"}`;

  // Anything that did not make it into the list is stated, never dropped quietly.
  const notes = [];
  if (d.problems.length) {
    notes.push(`
      <div class="order-problems">
        <h4>${d.problems.length} commit${d.problems.length === 1 ? "" : "s"} excluded</h4>
        <ul>${d.problems.map((p) => `<li><code>${escapeHtml(p.sha)}</code> — ${escapeHtml(p.reason)}</li>`).join("")}</ul>
      </div>`);
  }
  if (d.rejected.length) {
    notes.push(`
      <div class="order-problems muted-box">
        <h4>${d.rejected.length} input${d.rejected.length === 1 ? "" : "s"} ignored</h4>
        <ul>${d.rejected.map((r) => `<li><code>${escapeHtml(r.input)}</code> — ${escapeHtml(r.reason)}</li>`).join("")}</ul>
      </div>`);
  }
  if (d.date_order_differs) {
    notes.push(`
      <div class="order-problems warn-box">
        <h4>Commit dates disagree with history order</h4>
        <p>These are listed by their position in the branch, which is the real order
        they were applied. A rebase or cherry-pick can leave the author dates out of
        step, so sorting by date here would mislead.</p>
      </div>`);
  }
  el.ordProblems.innerHTML = notes.join("");

  if (!d.commits.length) {
    el.ordBody.replaceChildren(
      stateNode("Nothing left to show", "Every commit in the list was excluded — see the reasons above.")
    );
    return;
  }

  const rows = d.commits
    .map((c, i) => {
      const gap =
        i > 0 && c.position !== null && d.commits[i - 1].position !== null
          ? d.commits[i - 1].position - c.position - 1
          : 0;
      const gapRow =
        gap > 0
          ? `<li class="order-gap"><span class="order-rail"></span><span class="muted">${gap} other commit${gap === 1 ? "" : "s"} in between</span></li>`
          : "";

      const tags = (c.tags || [])
        .map((t) => `<span class="chip tag">${ICON.tag}<span class="txt">${escapeHtml(t.name)}</span></span>`)
        .join("");

      return `${gapRow}
        <li class="order-item${c.is_latest ? " is-latest" : ""}">
          <span class="order-rail"><span class="order-dot"></span></span>
          <div class="order-card">
            <div class="order-head">
              ${c.is_latest ? '<span class="status-badge is-ahead">Latest</span>' : `<span class="order-rank">#${c.rank}</span>`}
              <a class="order-title" href="${escapeAttr(c.url)}" target="_blank" rel="noopener">${escapeHtml(c.title)}</a>
              ${tags}
            </div>
            <div class="order-meta">
              <code class="full-sha">${escapeHtml(c.sha)}</code>
              <button type="button" class="copy-sha" data-sha="${escapeAttr(c.sha)}" title="Copy full hash ${escapeAttr(c.sha)}" aria-label="Copy full commit hash">${ICON.copy}</button>
              <span>${escapeHtml(c.author_name)}</span>
              <span class="sep">·</span>
              <span>${escapeHtml(fmtStamp(c.date))}</span>
              <span class="sep">·</span>
              <span>${
                c.position !== null
                  ? `position ${c.position}${d.total_commits ? ` of ${d.total_commits}` : ""} · ${c.distance_to_head} since head`
                  : '<span class="muted">position unknown</span>'
              }</span>
            </div>
          </div>
        </li>`;
    })
    .join("");

  const box = document.createElement("div");
  box.className = "order-timeline";
  box.innerHTML = `<ul class="order-list">${rows}</ul>`;
  el.ordBody.replaceChildren(box);
}

on(el.ordRun, "click", runOrder);
on(el.ordRepo, "change", loadBranchesForOrder);
on(el.ordClear, "click", () => {
  el.ordInput.value = "";
  el.ordScope.textContent = "";
  el.ordProblems.replaceChildren();
  el.ordBody.replaceChildren();
  state.order = null;
});
