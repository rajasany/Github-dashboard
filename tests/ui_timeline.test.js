/* Runs the real app/static/timeline.js in a stubbed DOM.
 *
 * What matters: commits bucket into the reader's own calendar days, the window
 * is sent as instants rather than wall-clock text, a filtered branch list is
 * disclosed, and commit titles cannot become markup.
 *
 * Run with:  node tests/ui_timeline.test.js
 */

const fs = require("fs");
const vm = require("vm");
const path = require("path");

function makeEl(tag) {
  const el = {
    tag, value: "", disabled: false, textContent: "", dataset: {},
    options: [], _html: "",
    get innerHTML() { return this._html; },
    set innerHTML(v) { this._html = v == null ? "" : String(v); },
    setAttribute() {},
    addEventListener(kind, fn) { (this._on ||= {})[kind] = fn; },
    insertAdjacentHTML(_where, html) { this._html += html; },
    querySelectorAll: () => [],
  };
  const classes = new Set();
  el.classList = {
    add: (...c) => c.forEach((x) => classes.add(x)),
    remove: (...c) => c.forEach((x) => classes.delete(x)),
    toggle: (c, on) => ((on === undefined ? !classes.has(c) : on) ? classes.add(c) : classes.delete(c)),
    contains: (c) => classes.has(c),
  };
  return el;
}

const els = new Map();
let RESPONSE = null;
const requests = [];

const context = {
  console,
  document: {
    getElementById: (id) => {
      if (!els.has(id)) els.set(id, makeEl(id));
      return els.get(id);
    },
    createElement: (tag) => makeEl(tag),
    addEventListener() {},
    body: makeEl("body"),
  },
  window: {
    addEventListener() {},
    __dash: {
      repoOptions: () => [{ key: "github:acme/shop", name: "acme/shop" }],
      fillRepoSelect: (select) => {
        select.innerHTML = '<option value="github:acme/shop">acme/shop</option>';
        select.value = "github:acme/shop";
        select.options = [{ value: "github:acme/shop" }];
      },
      config: () => ({ defaults: { days: 14 } }),
      // The real one from app.js, in miniature: what matters here is that the
      // timeline calls it rather than inventing its own weaker chip.
      cherryChip: (cp) => {
        if (!cp?.is_cherry_pick) return "";
        const recorded = cp.evidence === "recorded";
        return `<span class="chip cherry${recorded ? "" : " is-weak"}" title="${
          recorded ? `Cherry-picked from ${cp.source_sha}` : "mentions a cherry-pick"
        }"><span class="txt">cherry-pick${recorded ? ` ${cp.source_sha.slice(0, 7)}` : "?"}</span></span>`;
      },
    },
  },
  fetch: async (url) => {
    requests.push(url);
    return {
      ok: true, status: 200, statusText: "OK",
      text: async () => JSON.stringify(RESPONSE),
    };
  },
  URLSearchParams, Date, Math, Set, Map, JSON, Number, String, Object, Array, Promise, Error,
};
context.globalThis = context;
vm.createContext(context);
vm.runInContext(
  fs.readFileSync(path.join(__dirname, "..", "app", "static", "timeline.js"), "utf8"),
  context
);

const FAILURES = [];
function check(name, got, want) {
  const ok = got === want;
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}`);
  if (!ok) {
    console.log(`        got  ${JSON.stringify(got)}\n        want ${JSON.stringify(want)}`);
    FAILURES.push(name);
  }
}

/* A fixed local day, so the buckets are predictable wherever this runs. */
const localAt = (y, m, d, hh, mm) => new Date(y, m - 1, d, hh, mm).toISOString();

const commit = (over = {}) => ({
  sha: "a".repeat(40), short: "a".repeat(10), url: "https://x/c",
  title: "did a thing", author: "Jane Doe", author_login: null,
  date: localAt(2026, 9, 10, 14, 30), folders: ["app"], branches: ["main"],
  tags: [], cherry_pick: { is_cherry_pick: false }, files_changed: 2, ...over,
});

const response = (over = {}) => ({
  repo_key: "github:acme/shop", repo: "acme/shop", branch: "", folder: "",
  since: localAt(2026, 9, 1, 9, 0), until: null,
  commits: [commit()], count: 1, folders_available: ["app", "tests"],
  authors: [{ name: "Jane Doe", commits: 1 }], files_changed: 2, tagged: 0,
  cherry_picks: 0, all_branches: true,
  branch_filter: { include: [], exclude: [], filtered: false },
  capped: false, limit: 30, errors: [], ...over,
});

const el = (id) => context.document.getElementById(id);
const body = () => el("tl-body").innerHTML;
const scope = () => el("tl-scope").innerHTML;

/* The tab only draws once repository, branch and folder are all chosen, so the
 * helper picks a branch and folder unless a test is checking the gate itself. */
async function show(data, { branch = "main", folder = "app" } = {}) {
  RESPONSE = data;
  requests.length = 0;
  el("tl-branch").value = branch;
  await context.window.initTimelineTab();
  if (el("tl-run")._on) await el("tl-run")._on.click();
  el("tl-folder").value = folder;
  if (el("tl-folder")._on) el("tl-folder")._on.change();
}

(async () => {
  console.log("=== nothing is shown until all three are chosen ===");
  RESPONSE = response();
  el("tl-branch").value = "";
  await context.window.initTimelineTab();
  check("with no branch, it asks for one", body().includes("Choose a branch"), true);
  check("and fetches nothing", requests.some((u) => u.startsWith("/api/timeline")), false);
  check("the folder picker is disabled", el("tl-folder").disabled, true);

  el("tl-branch").value = "main";
  await el("tl-branch")._on.change();
  check("with a branch but no folder, it asks for the folder",
        body().includes("Choose a folder to see its timeline"), true);
  check("having fetched the window once",
        requests.filter((u) => u.startsWith("/api/timeline")).length, 1);
  check("the folder picker is now usable", el("tl-folder").disabled, false);
  check("offering what the window contains",
        el("tl-folder").innerHTML.includes(">app<"), true);

  el("tl-folder").value = "app";
  el("tl-folder")._on.change();
  check("choosing a folder draws the table", body().includes("tl-table"), true);
  check("without asking the server again",
        requests.filter((u) => u.startsWith("/api/timeline")).length, 1);

  console.log("\n=== the default window ===");
  await show(response());
  check("the repo picker is filled", el("tl-repo").value, "github:acme/shop");
  const fromValue = el("tl-from").value;
  check("a from-time is set", /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(fromValue), true);
  check("and the to-time left open, meaning now", el("tl-to").value, "");
  const days = Math.round((Date.now() - Date.parse(fromValue)) / 86400000);
  check("defaulting to the configured 14 days", days, 14);

  const asked = requests.find((u) => u.startsWith("/api/timeline"));
  check("the window is sent as an instant, not wall-clock text",
        /since_epoch=\d+/.test(asked), true);
  check("and no wall-clock string is sent", /since=\d{4}-/.test(asked), false);

  console.log("\n=== the table ===");
  await show(response({
    commits: [
      commit({ sha: "1".repeat(40), short: "1111111111", title: "late in the day",
               date: localAt(2026, 9, 10, 23, 45) }),
      commit({ sha: "2".repeat(40), short: "2222222222", title: "earlier that day",
               date: localAt(2026, 9, 10, 9, 5) }),
      commit({ sha: "3".repeat(40), short: "3333333333", title: "the day before",
               date: localAt(2026, 9, 9, 16, 20) }),
    ],
    count: 3,
  }));
  for (const heading of ["Time", "Comment", "Hash", "Created by", "Files", "Branch", "Folders"]) {
    check(`a ${heading} column`, body().includes(`<th>${heading}</th>`) ||
          body().includes(`<th class="num">${heading}</th>`), true);
  }
  check("one row per commit", (body().match(/<td class="tl-when">/g) || []).length, 3);
  check("grouped under a day heading", (body().match(/tl-dayrow/g) || []).length, 2);
  check("each day counting its own", body().includes('class="tl-daycount">2<'), true);
  check("times to the minute", body().includes(">23:45<"), true);
  check("in the reader's own clock", body().includes(">09:05<"), true);
  check("newest day first",
        body().indexOf("late in the day") < body().indexOf("the day before"), true);

  console.log("\n=== what a commit shows ===");
  await show(response({
    commits: [commit({
      tags: [{ name: "v1.0" }], branches: ["main", "dev"],
      cherry_pick: { is_cherry_pick: true, source_sha: "b".repeat(40) },
      files_changed: 5,
    })],
  }));
  check("the short sha in its own column",
        body().includes('<td class="mono">aaaaaaaaaa</td>'), true);
  check("the author", body().includes("Jane Doe"), true);
  check("the file count", body().includes('<td class="num">5</td>'), true);
  check("the folder", body().includes("app"), true);
  check("a tag chip", body().includes(">v1.0<"), true);
  check("a chip per branch", (body().match(/chip branch/g) || []).length, 2);
  check("and a cherry-pick chip when it really is one",
        body().includes("cherry-pick"), true);

  await show(response({ commits: [commit({ cherry_pick: { is_cherry_pick: false } })] }));
  check("but not when it is not", body().includes("cherry-pick"), false);
  check("and the row carries no mark", body().includes('class="is-cherry"'), false);

  console.log("\n=== marking a cherry-pick ===");
  await show(response({
    commits: [commit({
      cherry_pick: { is_cherry_pick: true, evidence: "recorded", source_sha: "b".repeat(40) },
    })],
  }));
  check("the row is marked, not just the cell", body().includes('<tr class="is-cherry">'), true);
  check("the chip names the source commit", body().includes("cherry-pick bbbbbbb"), true);
  check("and is not weakened", body().includes("is-weak"), false);
  check("with the full source in the title", body().includes("b".repeat(40)), true);

  await show(response({
    commits: [commit({
      cherry_pick: { is_cherry_pick: true, evidence: "mentioned", source_sha: null },
    })],
  }));
  check("a bare mention is still marked", body().includes('<tr class="is-cherry">'), true);
  check("but shown as weaker", body().includes("is-weak"), true);
  check("with a question mark, not a sha", body().includes("cherry-pick?"), true);

  await show(response({
    commits: [
      commit({ sha: "1".repeat(40), short: "1111111111",
               cherry_pick: { is_cherry_pick: true, evidence: "recorded", source_sha: "c".repeat(40) } }),
      commit({ sha: "2".repeat(40), short: "2222222222" }),
    ],
    count: 2,
  }));
  check("only the cherry-picked row is marked",
        (body().match(/class="is-cherry"/g) || []).length, 1);
  check("and the summary counts it", scope().includes("1 cherry-picked"), true);

  console.log("\n=== what the summary line says ===");
  await show(response({
    branch: "main",
    commits: [
      commit({ author: "A", tags: [{ name: "v1" }], files_changed: 3 }),
      commit({ sha: "2".repeat(40), short: "2222222222", author: "A", files_changed: 1 }),
      commit({ sha: "3".repeat(40), short: "3333333333", author: "B", files_changed: 2 }),
    ],
    count: 3,
  }));
  check("the count of what is shown", scope().includes("3 commits"), true);
  check("the folder", scope().includes("app"), true);
  check("the branch", scope().includes("on main"), true);
  check("the authors in view", scope().includes("2 authors"), true);
  check("the files they touched", scope().includes("6 files touched"), true);
  check("and what is tagged", scope().includes("1 tagged"), true);

  // The counts follow the folder, not the whole window.
  await show(response({
    branch: "main",
    commits: [
      commit({ folders: ["app"] }),
      commit({ sha: "9".repeat(40), short: "9999999999", folders: ["tests"] }),
    ],
    count: 2, folders_available: ["app", "tests"],
  }));
  check("only the chosen folder is counted", scope().includes("1 commit ·"), true);
  check("and only its rows drawn", (body().match(/<td class="tl-when">/g) || []).length, 1);

  await show(response({ capped: true }));
  check("a filled page is admitted", scope().includes("older commits in this window may be missing"), true);

  console.log("\n=== empty and hostile input ===");
  await show(response({ commits: [], count: 0, folders_available: ["app"] }));
  check("an empty folder says so", body().includes("No commits in that folder"), true);
  check("and suggests what to change", body().includes("Try a longer period"), true);

  // The folder must stay in the row, or the helper's filter drops it and the
  // escaping is never exercised at all.
  await show(response({
    commits: [commit({ title: "<script>alert(1)</script>", author: "a&b",
                       folders: ["app", 'x"onmouseover="y'] })],
  }));
  check("a script tag cannot become markup", body().includes("<script>"), false);
  check("it appears as text", body().includes("&lt;script&gt;"), true);
  check("an ampersand is encoded", body().includes("a&amp;b"), true);
  check("a quote cannot break an attribute", body().includes('"onmouseover="y'), false);

  console.log(`\n${FAILURES.length ? `${FAILURES.length} FAILURES: ${FAILURES.join(", ")}` : "ALL PASS"}`);
  process.exit(FAILURES.length ? 1 : 0);
})();
