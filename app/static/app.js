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

const el = {
  days: $("days"),
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
};

/** Folders a commit touched, with an explicit bucket when we have no data. */
function foldersOf(c) {
  return c.folders && c.folders.length ? c.folders : [NO_FOLDER];
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
  return d.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric", year: "numeric" });
}

function showBanner(html, isError) {
  el.banner.className = `banner${isError ? " error" : ""}`;
  el.banner.innerHTML = html;
}

function hideBanner() {
  el.banner.className = "banner hidden";
  el.banner.innerHTML = "";
}

/* ---------- data ---------- */

async function load({ refresh = false } = {}) {
  el.refresh.disabled = true;
  el.refresh.textContent = "Loading…";
  try {
    const params = new URLSearchParams({ days: el.days.value });
    if (refresh) params.set("refresh", "true");
    const res = await fetch(`/api/feed?${params}`);
    const body = await res.json();
    if (!res.ok) {
      showBanner(`<strong>Could not load data.</strong> ${body.detail || res.statusText}`, true);
      el.feed.innerHTML = '<p class="muted pad">No data.</p>';
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

function onDataLoaded() {
  const { errors, rate_limit: rl } = state.data;

  if (errors.length) {
    const items = errors.map((e) => `<li><code>${e.repo}</code> — ${e.error}</li>`).join("");
    showBanner(`<strong>${errors.length} source(s) could not be read:</strong><ul>${items}</ul>`, true);
  } else if (state.noToken) {
    showBanner("<strong>No <code>GITHUB_TOKEN</code> set.</strong> Running unauthenticated — public repos only, 60 API requests/hour. Copy <code>.env.example</code> to <code>.env</code> and restart for private repos and a 5000/hour limit.", false);
  } else {
    hideBanner();
  }

  el.rate.textContent = rl.remaining != null ? `GitHub API ${rl.remaining}/${rl.limit}` : "";

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
  { active, onSelect, allLabel, search = "", label = (k) => k, rowLabel = null, group = null } = {}
) {
  // `label` drives search/sort/tooltip; `rowLabel` is the visible text, which can
  // be shorter when a group header already supplies the context.
  const shownLabel = rowLabel || label;
  container.innerHTML = "";

  const entries = [...counts.entries()]
    .filter(([key]) => !search || label(key).toLowerCase().includes(search.toLowerCase()))
    .sort((a, b) => b[1] - a[1] || label(a[0]).localeCompare(label(b[0])));

  const total = [...counts.values()].reduce((a, b) => a + b, 0);

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
 * scopes the folder and branch lists to that repository. */
const STAGE = { PROVIDER: 1, REPO: 2, FOLDER: 3, BRANCH: 4, AUTHOR: 5 };

function passesUpTo(c, stage) {
  if (stage > STAGE.PROVIDER && state.provider && c.provider !== state.provider) return false;
  if (stage > STAGE.REPO && state.repo && c.repo_key !== state.repo) return false;
  if (stage > STAGE.FOLDER && state.folder && !folderKeysOf(c).includes(state.folder)) return false;
  if (stage > STAGE.BRANCH && state.branch && !c.branches.includes(state.branch)) return false;
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

  const folderOpts = tally(folderKeysOf, scopedTo(STAGE.FOLDER));
  if (state.folder && !folderOpts.has(state.folder)) state.folder = null;

  const branchOpts = tally((c) => c.branches, scopedTo(STAGE.BRANCH));
  if (state.branch && !branchOpts.has(state.branch)) state.branch = null;

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
  optlist(el.providerFilters, tally((c) => [c.provider], scopedTo(STAGE.PROVIDER)), {
    active: state.provider,
    onSelect: (k) => select("provider", k),
    allLabel: "All sources",
    label: (p) => PROVIDER_LABEL[p] || p,
  });

  optlist(el.repoFilters, tally((c) => [c.repo_key], scopedTo(STAGE.REPO)), {
    active: state.repo,
    onSelect: (k) => select("repo", k),
    allLabel: "All repositories",
    label: repoNameOf,
  });

  // Folders and branches are shown for whichever repository is selected.
  const scopeNote = state.repo
    ? `in ${repoNameOf(state.repo)}`
    : "across all repositories — select one above to narrow";
  el.folderScope.textContent = scopeNote;
  el.branchScope.textContent = scopeNote;

  el.folderPanel.classList.toggle("hidden", !state.data.folders?.enabled);
  optlist(el.folderFilters, tally(folderKeysOf, scopedTo(STAGE.FOLDER)), {
    active: state.folder,
    onSelect: (k) => select("folder", k),
    allLabel: "All services",
    search: el.folderSearch.value,
    label: folderLabel,
    // With a repo selected the header is redundant; otherwise group by repo so
    // same-named folders in different repos stay visibly distinct.
    rowLabel: (key) => splitFolderKey(key).folder,
    group: state.repo ? null : (key) => repoNameOf(splitFolderKey(key).repoKey),
  });

  optlist(el.branchFilters, tally((c) => c.branches, scopedTo(STAGE.BRANCH)), {
    active: state.branch,
    onSelect: (k) => select("branch", k),
    allLabel: "All branches",
    search: el.branchSearch.value,
  });

  optlist(el.authorFilters, tally((c) => [c.author_name], scopedTo(STAGE.AUTHOR)), {
    active: state.author,
    onSelect: (k) => select("author", k),
    allLabel: "All authors",
  });
}

function visibleCommits() {
  const query = el.search.value.trim().toLowerCase();
  const offDefaultOnly = el.scopeCommits.value === "off-default";

  return state.data.commits.filter((c) => {
    if (state.provider && c.provider !== state.provider) return false;
    if (state.repo && c.repo_key !== state.repo) return false;
    if (state.author && c.author_name !== state.author) return false;
    if (offDefaultOnly && c.on_default) return false;

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

  const tiles = [
    ["Commits", commits.length],
    ["Repositories", `${repos.size} / ${state.data.repos.length}`],
    ["Active branches", branches.size],
    ["Contributors", authors.size],
    ["Not on default branch", offDefault],
  ];

  if (state.data.folders?.enabled) {
    // Count (repo, folder) pairs — a `backend` in two repos is two services.
    const services = new Set(
      commits.flatMap((c) => c._visibleFolderKeys).filter((k) => splitFolderKey(k).folder !== NO_FOLDER)
    );
    tiles.splice(3, 0, ["Services touched", services.size]);
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
    ? `<span class="files" title="${c.files_truncated ? "At least " : ""}${c.files_changed} file(s) changed">${c.files_changed} file${c.files_changed === 1 ? "" : "s"}${c.files_truncated ? "+" : ""}</span>`
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

  // Spell out the active drill-down so the current scope is never ambiguous.
  const crumbs = [
    state.provider && (PROVIDER_LABEL[state.provider] || state.provider),
    state.repo && repoNameOf(state.repo),
    state.folder && splitFolderKey(state.folder).folder,
    state.branch,
    state.author,
  ].filter(Boolean);
  el.resultCount.textContent =
    `${commits.length} commit${commits.length === 1 ? "" : "s"} · last ${state.data.window_days}d` +
    (crumbs.length ? ` · ${crumbs.join(" › ")}` : "");

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

/* ---------- wiring ---------- */

el.refresh.addEventListener("click", () => load({ refresh: true }));
el.days.addEventListener("change", () => load());
el.search.addEventListener("input", render);
el.branchSearch.addEventListener("input", buildFilters);
el.folderSearch.addEventListener("input", buildFilters);
el.scopeCommits.addEventListener("change", render);
el.groupBy.addEventListener("change", render);
el.reset.addEventListener("click", () => {
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
  el.days.value = String(cfg.defaults.days);

  if (!cfg.configured) {
    showBanner("<strong>Setup needed.</strong> Copy <code>config.example.yaml</code> to <code>config.yaml</code> and list your repositories, then restart.", true);
    el.feed.innerHTML = '<p class="muted pad">No repositories configured.</p>';
    return;
  }
  // Only warn about a missing GitHub token if GitHub repos are actually tracked.
  state.noToken = cfg.repos.length > 0 && !cfg.has_token;
  load();
})();
