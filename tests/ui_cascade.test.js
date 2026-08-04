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
    title: "",
    className: "",
    offsetHeight: 57,
    children: [],
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
    classList: { toggle() {}, add() {}, remove() {} },
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
     escapeAttr, escapeHtml };`,
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

console.log(allOk ? "\nALL PASS" : "\nSOME FAILURES");
process.exit(allOk ? 0 : 1);
