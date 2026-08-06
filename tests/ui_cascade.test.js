/* Drill-down selection tests for app/static/app.js.
 *
 * Runs the real dashboard script in a stubbed DOM, then drives the selection
 * state directly. Two things are asserted:
 *   1. Picking a repository scopes the folder and branch lists to that repository.
 *   2. Two repositories that each contain a `backend/` folder are never conflated.
 *
 * Run with:  node tests/ui_cascade.test.js
 */
const fs = require("fs");
const vm = require("vm");
const path = require("path");

function makeEl(tag) {
  const el = {
    tag,
    type: "",
    value: "",
    disabled: false,
    title: "",
    href: "",
    offsetHeight: 57,
    children: [],
    // A real <select> exposes these; selectedText() reads them.
    options: [],
    selectedIndex: 0,
    // Assigning innerHTML replaces an element's content, so it must drop any
    // children the stub is tracking — `feed.innerHTML = ""` relies on that.
    _html: "",
    get innerHTML() {
      return this._html;
    },
    set innerHTML(v) {
      this._html = v == null ? "" : String(v);
      this.children = [];
    },
    // The app's escapeHtml() writes textContent and reads back innerHTML, so the
    // stub has to model that link or every escaped string comes back empty.
    _text: "",
    get textContent() {
      return this._text;
    },
    set textContent(v) {
      this._text = v == null ? "" : String(v);
      this.innerHTML = this._text
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;");
    },
    setAttribute() {},
    addEventListener(kind, fn) {
      if (kind === "click") this.onClick = fn;
    },
    appendChild(c) {
      this.children.push(c);
      return c;
    },
    replaceChildren(...c) {
      this.children = c;
    },
    append(...c) {
      this.children.push(...c);
    },
  };

  // A real classList, backed by a set that stays in sync with className —
  // the compare view toggles `hidden` through it and the tests read it back.
  const classes = new Set();
  Object.defineProperty(el, "className", {
    get: () => [...classes].join(" "),
    set: (v) => {
      classes.clear();
      String(v || "")
        .split(/\s+/)
        .filter(Boolean)
        .forEach((c) => classes.add(c));
    },
  });
  el.classList = {
    add: (...c) => c.forEach((x) => classes.add(x)),
    remove: (...c) => c.forEach((x) => classes.delete(x)),
    toggle: (c, on) => ((on === undefined ? !classes.has(c) : on) ? classes.add(c) : classes.delete(c)),
    has: (c) => classes.has(c),
    contains: (c) => classes.has(c),
  };
  return el;
}

const els = new Map();
// Records CSS custom properties the app sets on :root (e.g. --topbar-h).
const rootVars = new Map();

const context = {
  console,
  document: {
    getElementById: (id) => {
      if (!els.has(id)) els.set(id, makeEl(id));
      return els.get(id);
    },
    createElement: (tag) => makeEl(tag),
    documentElement: {
      style: { setProperty: (k, v) => rootVars.set(k, v) },
    },
  },
  window: { addEventListener() {} },
  fetch: () => new Promise(() => {}), // init IIFE hangs harmlessly
  URLSearchParams,
  Date,
  Math,
  Set,
  Map,
  JSON,
};
context.globalThis = context;
vm.createContext(context);
vm.runInContext(
  fs.readFileSync(path.join(__dirname, "..", "app", "static", "app.js"), "utf8"),
  context
);

// Top-level `const` lands in the realm's global lexical scope, not on the context
// object, so surface the bindings the test needs.
vm.runInContext(
  `globalThis.__api = { state, tally, folderKeysOf, splitFolderKey, repoNameOf,
     scopedTo, STAGE, FOLDER_SEP, visibleCommits, renderStats, folderLabel,
     buildFilters, foldersOf, select, pruneSelections, commitNode, render,
     escapeAttr, escapeHtml, applyPreset, isoDay, rangeLabel, currentCriteria,
     renderCompare, compareCriteria, exitCompare };`,
  context
);
const api = context.__api;

/* --- synthetic feed: TWO repos that both have a `backend` folder --- */
const REPOS = [
  { provider: "github", key: "github:acme/shop", full_name: "acme/shop", default_branch: "main" },
  { provider: "csr", key: "csr:gcp-proj/pay", full_name: "gcp-proj/pay", default_branch: "master" },
];

let n = 0;
function C(repo_key, folders, branches, author, on_default = true) {
  return {
    provider: repo_key.split(":")[0],
    repo_key,
    repo: REPOS.find((r) => r.key === repo_key).full_name,
    sha: `sha${++n}`,
    title: `commit ${n}`,
    body: "",
    author_name: author,
    author_login: null,
    date: `2026-08-0${(n % 3) + 1}T10:00:00Z`,
    url: "#",
    branches,
    folders,
    files_changed: folders.length,
    files_truncated: false,
    on_default,
  };
}

const DATA = {
  repos: REPOS,
  providers: ["csr", "github"],
  folders: { enabled: true, depth: 1, patterns: [], exclude: [] },
  rate_limit: { limit: 5000, remaining: 4000 },
  errors: [],
  window_days: 14,
  commits: [
    C("github:acme/shop", ["backend"], ["main"], "alice"),
    C("github:acme/shop", ["backend", "frontend"], ["main"], "bob"),
    C("github:acme/shop", ["frontend"], ["feature/x"], "alice", false),
    C("csr:gcp-proj/pay", ["backend"], ["master"], "carol"),
    C("csr:gcp-proj/pay", ["ledger"], ["master"], "carol"),
    C("csr:gcp-proj/pay", ["backend", "ledger"], ["release/1"], "dave", false),
  ],
};

// One commit gets a body, and one gets hostile characters in its title and URL,
// so the rendering tests below exercise the expand affordance and escaping.
DATA.commits[0].body = "Longer explanation\nacross two lines.";
DATA.commits[1].title = 'Fix "quoted" title & <tag>';
DATA.commits[1].url = 'https://example.test/c?a="1"';

const S = api.state;
S.data = DATA;

const SHOP = "github:acme/shop";
const PAY = "csr:gcp-proj/pay";
const fkey = (repo, folder) => `${repo}${api.FOLDER_SEP}${folder}`;

const label = (k) => {
  const { repoKey, folder } = api.splitFolderKey(k);
  return `${api.repoNameOf(repoKey)} / ${folder}`;
};
const folderOpts = () =>
  [...api.tally(api.folderKeysOf, api.scopedTo(api.STAGE.FOLDER)).keys()].map(label).sort();
const branchOpts = () =>
  [...api.tally((c) => c.branches, api.scopedTo(api.STAGE.BRANCH)).keys()].sort();
const authorOpts = () =>
  [...api.tally((c) => [c.author_name], api.scopedTo(api.STAGE.AUTHOR)).keys()].sort();
const shas = () => api.visibleCommits().map((c) => c.sha);

// Give the two <select>s the options the real markup declares, so the criteria
// carried into a report read as words rather than as raw values.
els.get("scope-commits").options = [
  { value: "all", text: "All commits" },
  { value: "off-default", text: "Only commits not on the default branch" },
];
els.get("group-by").options = [
  { value: "day", text: "Day" },
  { value: "repo", text: "Repository" },
  { value: "folder", text: "Service / folder" },
];

function reset() {
  S.provider = S.repo = S.folder = S.branch = S.author = null;
  els.get("scope-commits").selectedIndex = 0;
  els.get("group-by").selectedIndex = 0;
  els.get("search").value = "";
  els.get("scope-commits").value = "all";
  els.get("folder-search").value = "";
  els.get("branch-search").value = "";
}

function report(name, got, want) {
  const ok = JSON.stringify(got) === JSON.stringify(want);
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}`);
  if (!ok) console.log(`        got  ${JSON.stringify(got)}\n        want ${JSON.stringify(want)}`);
  return ok;
}

let allOk = true;
const check = (...a) => {
  allOk = report(...a) && allOk;
};

console.log("=== 1. nothing selected: everything offered, nothing filtered ===");
reset();
check("all folders across both repos", folderOpts(), [
  "acme/shop / backend",
  "acme/shop / frontend",
  "gcp-proj/pay / backend",
  "gcp-proj/pay / ledger",
]);
check("all 6 commits visible", shas().length, 6);

console.log("\n=== 2. selecting a repository scopes folders and branches to it ===");
reset();
S.repo = SHOP;
check("only shop's folders offered", folderOpts(), ["acme/shop / backend", "acme/shop / frontend"]);
check("only shop's branches offered", branchOpts(), ["feature/x", "main"]);
check("only shop's authors offered", authorOpts(), ["alice", "bob"]);
check("only shop's commits in the feed", shas(), ["sha1", "sha2", "sha3"]);

reset();
S.repo = PAY;
check("only pay's folders offered", folderOpts(), [
  "gcp-proj/pay / backend",
  "gcp-proj/pay / ledger",
]);
check("only pay's branches offered", branchOpts(), ["master", "release/1"]);
check("only pay's commits in the feed", shas(), ["sha4", "sha5", "sha6"]);

console.log("\n=== 3. same-named folders in different repos stay distinct ===");
reset();
S.repo = SHOP;
S.folder = fkey(SHOP, "backend");
check("shop/backend selects only shop commits", shas(), ["sha1", "sha2"]);
reset();
S.repo = PAY;
S.folder = fkey(PAY, "backend");
check("pay/backend selects only pay commits", shas(), ["sha4", "sha6"]);

console.log("\n=== 4. switching repository invalidates the stale folder selection ===");
reset();
S.repo = SHOP;
S.folder = fkey(SHOP, "backend");
S.repo = PAY; // user picks a different repo
api.pruneSelections();
check("stale folder cleared, not carried over", S.folder, null);
check("feed falls back to the whole new repo", shas(), ["sha4", "sha5", "sha6"]);

console.log("\n=== 5. branch selection survives only if valid in the new repo ===");
reset();
S.repo = SHOP;
S.branch = "feature/x";
S.repo = PAY;
api.pruneSelections();
check("branch cleared when absent in new repo", S.branch, null);

reset();
S.branch = "main";
S.repo = SHOP;
api.pruneSelections();
check("branch kept when present in new repo", S.branch, "main");

console.log("\n=== 6. selecting a source cascades to repositories ===");
reset();
S.provider = "csr";
check(
  "only csr repos offered",
  [...api.tally((c) => [c.repo_key], api.scopedTo(api.STAGE.REPO)).keys()],
  [PAY]
);
S.repo = SHOP; // now inconsistent with the provider
api.pruneSelections();
check("mismatched repo cleared", S.repo, null);

console.log("\n=== 7. a selected branch narrows the chips on each commit ===");
reset();
S.repo = SHOP;
S.branch = "main";
const withBranch = api.visibleCommits();
check(
  "only the selected branch is chipped",
  [...new Set(withBranch.flatMap((c) => c._visibleBranches))],
  ["main"]
);

console.log("\n=== 8. multi-service commit narrows to the selected folder ===");
reset();
S.repo = PAY;
S.folder = fkey(PAY, "ledger");
const multi = api.visibleCommits().find((c) => c.sha === "sha6");
check("sha6 shown under ledger only", multi ? multi._visibleFolders : null, ["ledger"]);

console.log("\n=== 9. services-touched counts (repo, folder) pairs ===");
reset();
api.renderStats(api.visibleCommits());
const tiles = els.get("stats").innerHTML;
const svc = tiles.match(/>(\d+)<\/div><div class="label">Services touched/)?.[1];
check("4 distinct services across 2 repos", svc, "4");

console.log("\n=== 10. lists are single-select buttons, never checkboxes ===");
reset();
api.buildFilters();
const panels = [
  "provider-filters",
  "repo-filters",
  "folder-filters",
  "branch-filters",
  "author-filters",
];
const rows = panels.flatMap((id) => els.get(id).children).filter((c) => c.tag === "button");
check("rows were rendered", rows.length > 0, true);
check("no checkbox inputs anywhere", panels.flatMap((id) => els.get(id).children).some((c) => c.type === "checkbox"), false);
check("exactly one 'All …' row per populated panel", panels.filter((id) => els.get(id).children.some((c) => /^All /.test(c.children?.[0]?.textContent || ""))).length, 5);

console.log("\n=== 11. an 'All' row clears its dimension ===");
reset();
S.repo = SHOP;
api.select("repo", null);
check("repo cleared, full feed back", shas().length, 6);

console.log("\n=== 12. commit row rendering ===");
reset();
const html1 = api.commitNode(DATA.commits[0]).innerHTML;
const html2 = api.commitNode(DATA.commits[1]).innerHTML;
const html3 = api.commitNode(DATA.commits[2]).innerHTML;

check("service chip carries the folder class", /class="chip folder"/.test(html1), true);
check("branch chip carries the branch class", /class="chip branch/.test(html1), true);
check("default branch is marked", /class="chip branch is-default"/.test(html1), true);
check("non-default branch is not marked", /is-default/.test(html3), false);
check("chips carry an icon", /class="ic"/.test(html1), true);
check(
  "full message shown only when a body exists",
  [/commit-body/.test(html1), /commit-body/.test(html2)],
  [true, false]
);
check("time uses <time> with a machine-readable datetime", /<time class="when" datetime="2026-08-02T10:00:00Z"/.test(html1), true);
check("sha link present", /class="sha"/.test(html1), true);
check("avatar falls back to initials when no image", /avatar-fallback/.test(html1), true);

console.log("\n=== 13. escaping ===");
check("escapeAttr escapes quotes", api.escapeAttr('a"b'), "a&quot;b");
check("markup in a title is escaped, not injected", /&lt;tag&gt;/.test(html2) && !/<tag>/.test(html2), true);
check("ampersand escaped", /&amp;/.test(html2), true);
check("quoted URL cannot break out of href", html2.includes('href="https://example.test/c?a=&quot;1&quot;"'), true);

console.log("\n=== 14. KPI row leads with exactly one hero ===");
reset();
api.renderStats(api.visibleCommits());
const statsHtml = els.get("stats").innerHTML;
check("exactly one hero tile", (statsHtml.match(/class="stat hero"/g) || []).length, 1);
check("hero is the commits tile", statsHtml.startsWith('<div class="stat hero"><div class="value">6</div><div class="label">Commits</div>'), true);
check("labels read as sentence case", /Not on default branch/.test(statsHtml), true);

console.log("\n=== 15. empty result renders a state node, not bare text ===");
reset();
els.get("search").value = "zzz-no-such-commit";
api.render();
const feedKids = els.get("feed").children;
check("feed holds a single state node", feedKids.length, 1);
check(
  "state node has both a title and a hint",
  /state-title/.test(feedKids[0].innerHTML) && /state-hint/.test(feedKids[0].innerHTML),
  true
);

console.log("\n=== 16. group headers carry a name and a count ===");
reset();
api.render();
const heads = els.get("feed").children.filter((c) => c.className === "group-head");
check("headers were rendered", heads.length > 0, true);
check("each header holds a name span and a count span", heads.every((h) => h.children.length === 2), true);
check("the count reads as commits", /commit/.test(heads[0].children[1].textContent), true);

console.log("\n=== 17. date range presets ===");
const from = els.get("date-from");
const to = els.get("date-to");
const todayIso = api.isoDay(new Date());

api.applyPreset("7");
check("a 7-day preset leaves the end open (stays correct tomorrow)", to.value, "");
const days7 = Math.round((new Date(todayIso) - new Date(from.value)) / 86400000);
check("'last 7 days' spans 7 days inclusive of today", days7, 6);

api.applyPreset("1");
check("'last 24 hours' starts today", from.value, todayIso);

api.applyPreset("30");
check("'last 30 days' steps back 29 whole days", Math.round((new Date(todayIso) - new Date(from.value)) / 86400000), 29);

api.applyPreset("mtd");
check("'this month' starts on the 1st", from.value.slice(-2), "01");
check("'this month' is in the current month", from.value.slice(0, 7), todayIso.slice(0, 7));

from.value = "2026-03-05";
to.value = "2026-04-06";
api.applyPreset("custom");
check("'custom' does not overwrite hand-typed dates", [from.value, to.value], ["2026-03-05", "2026-04-06"]);

check("isoDay is timezone-stable (no UTC off-by-one)", api.isoDay(new Date(2026, 0, 1, 0, 30)), "2026-01-01");
check("isoDay handles late-evening local times", api.isoDay(new Date(2026, 0, 1, 23, 30)), "2026-01-01");

console.log("\n=== 18. range label ===");
from.value = "2026-03-05";
to.value = "";
check("open-ended range reads 'to today'", /today/.test(api.rangeLabel()), true);
to.value = "2026-04-06";
check("closed range shows both ends", /–/.test(api.rangeLabel()), true);
from.value = "";
check("no start date yields no label", api.rangeLabel(), "");

console.log("\n=== 19. export criteria mirror the active filters ===");
reset();
from.value = "2026-05-01";
to.value = "2026-06-01";
S.repo = SHOP;
S.folder = fkey(SHOP, "backend");
S.branch = "main";
S.author = "alice";
els.get("search").value = "fix";
const crit = api.currentCriteria();
check("since / until carried through", [crit.since, crit.until], ["2026-05-01", "2026-06-01"]);
check("repository named in full", crit.repository, "acme/shop");
check("service is the folder name", crit.service, "backend");
check("branch and author carried", [crit.branch, crit.author], ["main", "alice"]);
check("search text carried", crit.search, "fix");
check("generated_at is an ISO timestamp", /^\d{4}-\d{2}-\d{2}T/.test(crit.generated_at), true);

reset();
const bare = api.currentCriteria();
check("unset filters are null, not empty strings", [bare.repository, bare.service, bare.branch, bare.author, bare.search], [null, null, null, null, null]);

console.log("\n=== 20. branch comparison view ===");
reset();

function walk(node, out = []) {
  for (const c of node.children || []) {
    out.push(c);
    walk(c, out);
  }
  return out;
}

const CMP = {
  provider: "github",
  repo_key: SHOP,
  repo: "acme/shop",
  base: "main",
  head: "feature/x",
  status: "diverged",
  ahead_by: 2,
  behind_by: 1,
  total_commits: 2,
  merge_base: "abcdef1234567890",
  html_url: "https://example.test/compare",
  folders: ["(repo root)", "backend", "web"],
  files_changed: 3,
  additions: 40,
  deletions: 5,
  commits_truncated: 0,
  files_truncated: 0,
  files: [
    { path: "z-last.txt", status: "modified", additions: 1, deletions: 1 },
    { path: "web/new.ts", status: "added", additions: 30, deletions: 0 },
    { path: "old.md", status: "removed", additions: 0, deletions: 4 },
  ],
  commits: [
    { ...DATA.commits[0], repo_key: SHOP, branches: ["feature/x"], folders: ["backend"] },
    { ...DATA.commits[2], repo_key: SHOP, branches: ["feature/x"], folders: ["web"] },
  ],
};

S.compare = CMP;
api.renderCompare();

check("compare view shown, feed hidden", [
  !els.get("compare-view").classList.has("hidden"),
  els.get("feed-view").classList.has("hidden"),
], [true, true]);
check("heading names both branches", els.get("compare-heading").textContent, "main  ←  feature/x");
check("subtitle explains the direction", /what “feature\/x” has that “main” does not/.test(els.get("compare-sub").textContent), true);
check("subtitle names the merge base", /abcdef1/.test(els.get("compare-sub").textContent), true);
check("status badge carries a word, not just colour", els.get("compare-status").textContent, "diverged");
check("status badge is class-tagged", els.get("compare-status").className, "status-badge is-diverged");

const kpi = els.get("compare-stats").innerHTML;
check("ahead is the hero figure", /class="stat hero"><div class="value">2<\/div><div class="label">Commits ahead/.test(kpi), true);
check("behind reported too", /<div class="value">1<\/div><div class="label">Commits behind/.test(kpi), true);
check("line counts signed", /\+40/.test(kpi) && /−5/.test(kpi), true);
check("(repo root) excluded from services touched", /<div class="value">2<\/div><div class="label">Services touched/.test(kpi), true);

const nodes = walk(els.get("compare-body"));
check("commit rows rendered", nodes.filter((n) => n.className === "commit").length, 2);
// The walk already includes compare-body's direct children — concatenating both
// would count every table twice.
const bodyHtml = nodes.map((n) => n.innerHTML).join("");
check("changed-file table present", /file-table/.test(bodyHtml), true);
const order = [...bodyHtml.matchAll(/<td class="file-path">([^<]+)/g)].map((m) => m[1]);
check("files sort added → modified → removed", order, ["web/new.ts", "z-last.txt", "old.md"]);

console.log("\n=== 21. comparison edge states ===");
S.compare = { ...CMP, status: "identical", ahead_by: 0, behind_by: 0, commits: [], files: [], folders: [] };
api.renderCompare();
check("identical branches explained, no tables", walk(els.get("compare-body")).some((n) => n.className === "state"), true);

S.compare = { ...CMP, status: "behind", ahead_by: 0, behind_by: 3, commits: [], files: [] };
api.renderCompare();
const behindText = walk(els.get("compare-body")).map((n) => n.innerHTML).join("");
check("behind-only state suggests swapping direction", /Swap the direction/.test(behindText), true);
check("behind-only state names the count", /3 commits behind/.test(behindText), true);

console.log("\n=== 22. export describes the comparison, not the date filter ===");
S.compare = CMP;
const cc = api.compareCriteria();
check("comparison line is the headline criterion", cc.comparison, "main  ←  feature/x  ·  2 ahead, 1 behind");
check("repository carried", cc.repository, "acme/shop");
check("head branch carried", cc.branch, "feature/x");
check("diff totals carried", cc.files, "3 changed, +40 / −5");
check("merge base carried", cc.merge_base, "abcdef1");
check("range says it is not a date range", /not a date range/.test(cc.range), true);

api.exitCompare();
check("exiting restores the feed", [
  els.get("compare-view").classList.has("hidden"),
  !els.get("feed-view").classList.has("hidden"),
  S.compare,
], [true, true, null]);

console.log(allOk ? "\nALL PASS" : "\nSOME FAILURES");
process.exit(allOk ? 0 : 1);
