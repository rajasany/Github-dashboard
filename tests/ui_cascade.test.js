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
    // The copy-hash button stores the full sha in a data attribute.
    dataset: {},
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
    // The copy-hash handler is delegated on document, and the clipboard
    // fallback appends a temporary textarea to body.
    addEventListener() {},
    body: { appendChild: (c) => c },
  },
  window: { addEventListener() {} },
  navigator: { clipboard: { writeText: async () => {} } },
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
     showTab, TABS, renderSummary, renderLookup, fmtStamp,
     escapeAttr, escapeHtml, applyPreset, isoDay, rangeLabel, currentCriteria,
     renderTipCard,
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

const SHOP_KEY = "github:acme/shop";
const PAY_KEY = "csr:gcp-proj/pay";

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

DATA.commits.forEach((c) => (c.tags = []));
DATA.commits[1].tags = ["v1.0.0", "release-aug"];
DATA.commits[3].tags = ["pay-v2"];
// The repo-level sha -> tags map the feed ships, used to tell "no tags at all"
// apart from "no tags inside this date range".
DATA.tags = {
  [SHOP_KEY]: { sha2: ["v1.0.0", "release-aug"] },
  [PAY_KEY]: { sha4: ["pay-v2"] },
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

// Hiding the other views is the tab bar's job now; renderCompare only reveals
// its own result block.
check("compare result revealed", !els.get("compare-view").classList.has("hidden"), true);
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
check("exiting clears the result and the state", [
  els.get("compare-view").classList.has("hidden"),
  S.compare,
], [true, null]);


console.log("\n=== 20. commit hash and tags on a row ===");
reset();
const tagRow = api.commitNode(DATA.commits[1]).innerHTML;
check("tag chips rendered", (tagRow.match(/class="chip tag"/g) || []).length, 2);
check("tag name shown", /class="txt">v1.0.0</.test(tagRow), true);
check("short hash still links out", /class="sha"[^>]*>sha2</.test(tagRow), true);
check("copy button carries the full hash", /class="copy-sha" data-sha="sha2"/.test(tagRow), true);

const plainRow = api.commitNode(DATA.commits[0]).innerHTML;
check("untagged commit shows no tag chip", /class="chip tag"/.test(plainRow), false);

console.log("\n=== 21. latest-commit card for a branch + folder ===");
reset();
S.repo = SHOP;
S.branch = "main";
S.folder = fkey(SHOP, "backend");
api.render();
const card = els.get("tip-card");
check("card is shown", card.classList.has("hidden"), false);
const tagHereRow = (k) =>
  (card.innerHTML.match(new RegExp(k + "<\\/span>\\s*<span class=\"tip-value[^>]*>([\\s\\S]*?)<\\/span>\\s*<\\/div>"))
    || [])[1] || "";
check("heading names the branch and the folder",
  /branch main · backend/.test(card.innerHTML), true);
check("full hash is present", /class="full-sha">sha1</.test(card.innerHTML), true);
check("card has its own copy button", /class="copy-sha" data-sha="sha1"/.test(card.innerHTML), true);
check("tip commit is untagged, and the card says so",
  /no tag on this commit/.test(tagHereRow("Tag here")), true);
check("nearest tag is reported with a distance",
  /v1\.0\.0[\s\S]*1 commit back/.test(tagHereRow("Most recent tag")), true);

// A selection whose newest commit IS tagged.
reset();
S.repo = PAY;
S.branch = "master";
api.render();
check("tagged tip lists its tag", /class="chip tag"/.test(tagHereRow("Tag here")), true);
check("no distance row when the tip itself is tagged",
  /Most recent tag/.test(els.get("tip-card").innerHTML), false);

// Nothing tagged anywhere in scope.
reset();
S.repo = SHOP;
S.folder = fkey(SHOP, "frontend");
S.branch = "feature/x";
api.render();
check("says so when the repo has tags but none in range",
  /none in the loaded date range/.test(els.get("tip-card").innerHTML), true);

// A repository with no tags at all must not be told to widen the date range.
const savedTags = DATA.tags;
DATA.tags = { [SHOP]: {}, [PAY]: {} };
DATA.commits.forEach((c) => (c.tags = []));
reset();
S.repo = SHOP;
api.render();
check("a repo with no tags says so, not 'widen the range'",
  /acme\/shop has no tags/.test(els.get("tip-card").innerHTML), true);
check("and does not suggest widening the range",
  /widen it to reach further back/.test(els.get("tip-card").innerHTML), false);
DATA.tags = savedTags;
DATA.commits[1].tags = ["v1.0.0", "release-aug"];
DATA.commits[3].tags = ["pay-v2"];

console.log("\n=== 21b. tags KPI tile ===");
reset();
api.renderStats(api.visibleCommits());
check("tile counts distinct tag names",
  /<div class="value">3<\/div><div class="label">Tags</.test(els.get("stats").innerHTML), true);
DATA.commits.forEach((c) => (c.tags = []));
api.renderStats(api.visibleCommits());
check("tile reads zero when nothing is tagged",
  /<div class="value">0<\/div><div class="label">Tags</.test(els.get("stats").innerHTML), true);
DATA.commits[1].tags = ["v1.0.0", "release-aug"];
DATA.commits[3].tags = ["pay-v2"];

console.log("\n=== 22. card hides when there is nothing to head ===");
reset();
els.get("search").value = "zzz-no-match";
api.render();
check("no commits means no card", els.get("tip-card").classList.has("hidden"), true);
els.get("search").value = "";


console.log("\n=== 23. tab bar ===");
for (const name of ["activity", "summary", "compare", "lookup"]) {
  api.showTab(name);
  const visible = ["activity", "summary", "compare", "lookup"].filter(
    (n) => !els.get(`panel-${n}`).classList.has("hidden")
  );
  check(`showTab(${name}) reveals exactly its own panel`, visible, [name]);
}
api.showTab("activity");
check("sidebar shown on Activity", els.get("sidebar").classList.has("hidden"), false);
api.showTab("summary");
check("sidebar folded away on other tabs", els.get("sidebar").classList.has("hidden"), true);
check("layout drops its sidebar column", els.get("layout").classList.has("no-sidebar"), true);
api.showTab("nonsense");
check("an unknown tab falls back to Activity", S.tab, "activity");

console.log("\n=== 24. summary table ===");
const SUM = {
  repo_key: SHOP, repo: "acme/shop", branch: "main", folder: null,
  since: "2026-08-01", until: null, limit: 100, capped: false,
  commits_scanned: 3, row_count: 3, tagged_rows: 2,
  folders_available: ["backend", "web"],
  rows: [
    { sha: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", url: "#", author_name: "alice",
      date: "2026-08-04T10:00:00Z", title: "annotated release", folders: ["backend"], files_changed: 2,
      tags: [{ name: "v2.0.0", annotated: true, tagger_name: "Release Bot",
               tagger_date: "2026-08-04T11:00:00Z", message: "ship it" }] },
    { sha: "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", url: "#", author_name: "bob",
      date: "2026-08-03T10:00:00Z", title: "lightweight tagged", folders: ["web"], files_changed: 1,
      tags: [{ name: "nightly", annotated: false, tagger_name: null, tagger_date: null, message: null }] },
    { sha: "cccccccccccccccccccccccccccccccccccccccc", url: "#", author_name: "carol",
      date: "2026-08-02T10:00:00Z", title: "no tag here", folders: ["backend"], files_changed: 4, tags: [] },
  ],
};
S.summary = SUM;
api.renderSummary();
const sumHtml = els.get("sum-body").children[0].innerHTML;
const headers = [...sumHtml.matchAll(/<th>([^<]+)<\/th>/g)].map((m) => m[1]);
check("the seven requested columns, in order", headers, [
  "Commit hash", "Commit creator", "Commit date", "Tag", "Tag creator", "Tag date", "Message",
]);
check("annotated tag shows its creator", /Release Bot/.test(sumHtml), true);
check("annotated tag shows its own date",
  sumHtml.includes(api.fmtStamp("2026-08-04T11:00:00Z")), true);
check("lightweight tag is labelled, not faked", /lightweight/.test(sumHtml), true);
check("lightweight tag has no invented creator", /nightly[\s\S]*?Release Bot/.test(sumHtml), false);
check("untagged commit renders a dash", (sumHtml.match(/—/g) || []).length >= 2, true);
check("hash is copyable at full length",
  /data-sha="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"/.test(sumHtml), true);
check("folder picker repopulated from the branch",
  [...els.get("sum-folder").innerHTML.matchAll(/<option value="([^"]*)"/g)].map((m) => m[1]),
  ["", "backend", "web"]);
check("scope line reports counts", /3 commits, 2 tagged/.test(els.get("sum-scope").textContent), true);

S.summary = { ...SUM, rows: [], row_count: 0, tagged_rows: 0, folder: "backend" };
api.renderSummary();
check("empty result names the folder that matched nothing",
  /Nothing touched .backend./.test(els.get("sum-body").children[0].innerHTML), true);

console.log("\n=== 25. commit lookup ===");
S.lookup = {
  query: "abc1234", sha: "abc1234", found: true, searched: 2, errors: [],
  matches: [{
    provider: "github", repo_key: SHOP, repo: "acme/shop",
    sha: "abc1234abc1234abc1234abc1234abc1234abc12", url: "#",
    title: "the commit", body: "", author_name: "alice", author_login: "alice",
    date: "2026-08-04T10:00:00Z", committer_name: "alice", committer_date: "2026-08-04T10:00:00Z",
    parents: ["dddddddd"], files_changed: 3, additions: 40, deletions: 5,
    branches: [
      { name: "main", is_default: true, distance_to_head: 7, is_head: false, total_commits: 20, position: 13 },
      { name: "dev", is_default: false, distance_to_head: 0, is_head: true, total_commits: 13, position: 13 },
    ],
    branches_probed: 2, branches_unprobed: 0, branches_failed: [],
    folders: ["backend", "web"],
    directories: ["backend/api", "web/src"],
    tags: [{ name: "v1.5", annotated: true, tagger_name: "Tagger Person",
             tagger_date: "2026-08-04T12:00:00Z", message: "milestone" }],
    nearest_tag: null, default_branch: "main",
  }],
};
api.renderLookup();
const lkCard = els.get("lk-body").children[0].innerHTML;
check("names the repository", /acme\/shop/.test(lkCard), true);
check("shows the full hash", /abc1234abc1234abc1234abc1234abc1234abc12/.test(lkCard), true);
check("lists both containing branches", /main/.test(lkCard) && /dev/.test(lkCard), true);
check("reports distance to head", /<td class="num">7<\/td>/.test(lkCard), true);
check("reports position within the branch", /13.*of 20/.test(lkCard), true);
check("marks the branch where it is the head", /at head/.test(lkCard), true);
check("shows tag creator and tag date",
  /Tagger Person/.test(lkCard) && lkCard.includes(api.fmtStamp("2026-08-04T12:00:00Z")), true);
check("draws a position track with a marker",
  /class="graph-bar"/.test(lkCard) && /graph-marker/.test(lkCard), true);
check("branch is surfaced at the top, not only in the table",
  /Branch<\/span>[\s\S]*?class="chip branch/.test(lkCard), true);
check("folders are surfaced", /Folders? changed<\/span>[\s\S]*?class="chip folder/.test(lkCard), true);
check("both folder names shown", /backend/.test(lkCard) && /web/.test(lkCard), true);
// The single-commit view must show where the files actually are, not the
// coarser service bucket used for grouping elsewhere.
check("shows the exact directories, not the service rollup",
  /backend\/api/.test(lkCard) && /web\/src/.test(lkCard), true);
check("the label pluralises for several folders", /Folders changed/.test(lkCard), true);

S.lookup = {
  ...S.lookup,
  matches: [{ ...S.lookup.matches[0], directories: ["app/static"], folders: ["app"] }],
};
api.renderLookup();
const oneDir = els.get("lk-body").children[0].innerHTML;
check("a single-folder commit names that folder", /app\/static/.test(oneDir), true);
check("and not its parent bucket alone", /class="txt">app<\/span>/.test(oneDir), false);
check("the label is singular for one folder", /Folder changed/.test(oneDir), true);

// Older payloads without `directories` must still render something.
S.lookup = {
  ...S.lookup,
  matches: [{ ...S.lookup.matches[0], directories: undefined, folders: ["legacy"] }],
};
api.renderLookup();
check("falls back to folders when directories are absent",
  /legacy/.test(els.get("lk-body").children[0].innerHTML), true);

// A failed probe must not be reported as "not on any branch".
S.lookup = {
  ...S.lookup,
  matches: [{ ...S.lookup.matches[0], branches: [], branches_failed: ["main", "dev"] }],
};
api.renderLookup();
const failCard = els.get("lk-body").children[0].innerHTML;
check("a failed probe says so", /2 branch probe\(s\) failed/.test(failCard), true);
check("and does not claim the commit is unreachable",
  /No branch in this repository contains/.test(failCard), false);

S.lookup = {
  ...S.lookup,
  matches: [{ ...S.lookup.matches[0], branches: [], branches_failed: [] }],
};
api.renderLookup();
check("a genuine miss still reads as unreachable",
  /No branch in this repository contains/.test(els.get("lk-body").children[0].innerHTML), true);

S.lookup = { query: "zzz", sha: "zzz", found: false, searched: 3, matches: [], errors: [] };
api.renderLookup();
check("a miss says how many repos were searched",
  /Searched 3 configured repositories/.test(els.get("lk-body").children[0].innerHTML), true);

console.log("\n=== 26. timestamp formatting ===");
check("pads to a sortable local stamp", /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$/.test(api.fmtStamp("2026-08-04T10:00:00Z")), true);
check("empty input yields empty output", api.fmtStamp(null), "");
check("unparseable input is not invented", api.fmtStamp("not-a-date"), "not-a-date");

console.log(allOk ? "\nALL PASS" : "\nSOME FAILURES");
process.exit(allOk ? 0 : 1);
