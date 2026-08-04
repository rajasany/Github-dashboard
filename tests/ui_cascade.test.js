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
  return {
    tag,
    type: "",
    value: "",
    disabled: false,
    innerHTML: "",
    textContent: "",
    title: "",
    className: "",
    children: [],
    classList: { toggle() {}, add() {}, remove() {} },
    setAttribute() {},
    addEventListener(kind, fn) {
      if (kind === "click") this.onClick = fn;
    },
    appendChild(c) {
      this.children.push(c);
      return c;
    },
    append(...c) {
      this.children.push(...c);
    },
  };
}

const els = new Map();
const context = {
  console,
  document: {
    getElementById: (id) => {
      if (!els.has(id)) els.set(id, makeEl(id));
      return els.get(id);
    },
    createElement: (tag) => makeEl(tag),
  },
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
     buildFilters, foldersOf, select, pruneSelections };`,
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

function reset() {
  S.provider = S.repo = S.folder = S.branch = S.author = null;
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

console.log(allOk ? "\nALL PASS" : "\nSOME FAILURES");
process.exit(allOk ? 0 : 1);
