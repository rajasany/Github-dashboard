/* Runs the real app/static/migrations.js in a stubbed DOM.
 *
 * What matters here: the register renders what the server sent, the amber flags
 * appear on exactly the flagged fields, controls people are not entitled to use
 * are not offered, and — since every value goes into innerHTML — free text a
 * developer typed cannot become markup.
 *
 * Run with:  node tests/ui_migrations.test.js
 */

const fs = require("fs");
const vm = require("vm");
const path = require("path");

/* ------------------------------------------------------------------ stub --- */

function makeEl(tag) {
  const el = {
    tag,
    type: "",
    value: "",
    checked: false,
    disabled: false,
    title: "",
    textContent: "",
    dataset: {},
    files: [],
    children: [],
    _html: "",
    get innerHTML() {
      return this._html;
    },
    set innerHTML(v) {
      this._html = v == null ? "" : String(v);
    },
    setAttribute() {},
    removeAttribute() {},
    addEventListener(kind, fn) {
      (this._on ||= {})[kind] = fn;
    },
    removeEventListener() {},
    appendChild(c) {
      this.children.push(c);
      return c;
    },
    remove() {
      removed.push(this);
    },
    showModal() {
      this.shown = true;
    },
    close() {
      this._on?.close?.();
    },
    // Returns nothing: the tests assert on rendered HTML, not on wiring.
    querySelectorAll: () => [],
    querySelector: () => makeEl("div"),
  };
  const classes = new Set(String(tag || "").split(/\s+/).filter(Boolean));
  Object.defineProperty(el, "className", {
    get: () => [...classes].join(" "),
    set: (v) => {
      classes.clear();
      String(v || "").split(/\s+/).filter(Boolean).forEach((c) => classes.add(c));
    },
  });
  el.classList = {
    add: (...c) => c.forEach((x) => classes.add(x)),
    remove: (...c) => c.forEach((x) => classes.delete(x)),
    toggle: (c, on) => ((on === undefined ? !classes.has(c) : on) ? classes.add(c) : classes.delete(c)),
    contains: (c) => classes.has(c),
  };
  return el;
}

const els = new Map();
const appended = [];
const removed = [];
let promptAnswer = null;
const cookies = [];

/* What the stubbed server answers with. Tests reassign parts of this. */
let META = null;
let RECORDS = null;

function jsonResponse(body) {
  return {
    ok: true,
    status: 200,
    statusText: "OK",
    text: async () => JSON.stringify(body),
  };
}

const context = {
  console,
  document: {
    getElementById: (id) => {
      if (!els.has(id)) els.set(id, makeEl(id));
      return els.get(id);
    },
    createElement: (tag) => makeEl(tag),
    querySelector: () => makeEl("div"),
    addEventListener() {},
    removeEventListener() {},
    get cookie() {
      return cookies.join("; ");
    },
    set cookie(v) {
      cookies.push(String(v));
    },
    body: {
      appendChild: (c) => {
        appended.push(c);
        // The script finds the dialog again by id, so register it.
        if (c.id) els.set(c.id, c);
        return c;
      },
    },
  },
  window: {
    addEventListener() {},
    location: { href: "" },
    prompt: (_q, _d) => promptAnswer,
  },
  fetch: async (url) => {
    if (url.startsWith("/api/migrations/meta")) return jsonResponse(META);
    if (url.startsWith("/api/migrations/records?")) return jsonResponse(RECORDS);
    return jsonResponse({});
  },
  URLSearchParams,
  FormData: class {
    append() {}
  },
  Date,
  Math,
  Set,
  Map,
  JSON,
  Number,
  Boolean,
  Object,
  Array,
  String,
  Promise,
  Error,
};
context.globalThis = context;
vm.createContext(context);
vm.runInContext(
  fs.readFileSync(path.join(__dirname, "..", "app", "static", "migrations.js"), "utf8"),
  context
);

/* ------------------------------------------------------------------ data --- */

const FIELDS = [
  { key: "sl_no", label: "SL#", kind: "auto", stage: "request", roles: [], options: "", required: false, required_if: null, amber_when: "", depends_on: "", default: "", help: "" },
  { key: "release", label: "Rel#", kind: "enum", stage: "request", roles: ["developer"], options: "releases", required: true, required_if: null, amber_when: "", depends_on: "", default: "", help: "" },
  { key: "microservice", label: "Micro Service Name", kind: "enum", stage: "request", roles: ["developer"], options: "microservices", required: true, required_if: null, amber_when: "", depends_on: "", default: "", help: "" },
  { key: "repo_name", label: "Repo Name", kind: "enum", stage: "request", roles: ["developer"], options: "", required: true, required_if: null, amber_when: "", depends_on: "microservice", default: "", help: "" },
  { key: "track_lead", label: "Track Lead Name", kind: "enum", stage: "request", roles: ["developer"], options: "", required: true, required_if: null, amber_when: "", depends_on: "microservice", default: "", help: "" },
  { key: "reason", label: "Reason for Movement", kind: "longtext", stage: "request", roles: ["developer"], options: "", required: false, required_if: null, amber_when: "", depends_on: "", default: "", help: "" },
  { key: "code_image_change", label: "Code & Image Change?", kind: "yesno", stage: "request", roles: ["developer"], options: "", required: false, required_if: null, amber_when: "Yes", depends_on: "", default: "No", help: "" },
  { key: "commit_hash", label: "Commit Hash", kind: "text", stage: "request", roles: ["developer"], options: "", required: false, required_if: ["code_image_change", "Yes"], amber_when: "", depends_on: "", default: "", help: "Required when there is a code or image change." },
  { key: "ddl_dml", label: "DDL/DML", kind: "yesno", stage: "request", roles: ["developer"], options: "", required: false, required_if: null, amber_when: "Yes", depends_on: "", default: "No", help: "" },
  { key: "approved_by", label: "Approved By", kind: "enum", stage: "approval", roles: ["approver"], options: "approvers", required: false, required_if: null, amber_when: "", depends_on: "", default: "", help: "" },
  { key: "executed_in_qa", label: "Executed in QA", kind: "yesno", stage: "qa", roles: ["devops"], options: "", required: false, required_if: null, amber_when: "", depends_on: "", default: "No", help: "" },
];

const baseMeta = () => ({
  user: { email: "dev@example.com", roles: ["developer"], dev_mode: false, insecure: false, signed_in: true },
  auth_configured: true,
  fields: FIELDS,
  options: {
    releases: ["R2026.09"],
    migration_paths: ["SIT to QA"],
    microservices: ["payments", "cart"],
    change_requestors: ["Business Ops"],
    approvers: ["lead@example.com"],
    yesno: ["Yes", "No"],
  },
  services: [
    { name: "payments", repos: ["demo/payments"], track_leads: ["A. Kumar"] },
    { name: "cart", repos: ["demo/cart", "demo/cart-ui"], track_leads: ["R. Iyer"] },
  ],
  statuses: [
    { key: "submitted", label: "Awaiting approval" },
    { key: "in_prod", label: "Migrated to prod" },
  ],
  request_fields: ["release", "microservice", "repo_name", "track_lead", "reason", "code_image_change", "commit_hash", "ddl_dml"],
  freeze: null,
  frozen: false,
  dev: { enabled: false, configured: false, why_not: "", cookie: "dev_user", identities: [] },
});

const record = (over = {}) => ({
  sl_no: 1,
  release: "R2026.09",
  migration_path: "SIT to QA",
  microservice: "payments",
  repo_name: "demo/payments",
  track_lead: "A. Kumar",
  created_by: "dev@example.com",
  reason: "Defect fix",
  code_image_change: "No",
  commit_hash: "",
  ddl_dml: "No",
  approved_by: "",
  executed_in_qa: "No",
  date_created: "2026-09-05T10:30:00Z",
  date_created_epoch: 1788604200.0,
  active: true,
  status: "submitted",
  status_label: "Awaiting approval",
  amber: [],
  editable: ["release", "reason"],
  ...over,
});

/* ----------------------------------------------------------------- harness - */

const FAILURES = [];
function check(name, got, want) {
  const ok = got === want;
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}`);
  if (!ok) {
    console.log(`        got  ${JSON.stringify(got)}\n        want ${JSON.stringify(want)}`);
    FAILURES.push(name);
  }
}

const body = () => els.get("mig-body").innerHTML;
const notices = () => els.get("mig-notices").innerHTML;

async function load(meta, rows, sort) {
  META = meta;
  RECORDS = {
    rows,
    count: rows.length,
    user: meta.user,
    sort: sort || { field: "sl_no", dir: "desc" },
    sortable: ["sl_no", "release", "migration_path", "microservice", "repo_name",
               "track_lead", "created_by", "date_created", "status"],
    can_retire: META.user.roles.includes("approver") || META.user.roles.includes("admin"),
  };
  // Reset the module's memoised meta by clearing the filter selects it reads.
  for (const id of ["mig-f-release", "mig-f-service", "mig-f-path", "mig-f-status"]) {
    els.get(id) || els.set(id, makeEl(id));
    els.get(id).value = "";
  }
  for (const id of ["mig-f-mine", "mig-f-inactive"]) {
    els.get(id) || els.set(id, makeEl(id));
    els.get(id).checked = false;
  }
  for (const id of ["mig-f-from", "mig-f-to"]) {
    els.get(id) || els.set(id, makeEl(id));
    els.get(id).value = "";
  }
  await context.window.initMigrationsTab();
}

/* ------------------------------------------------------------------ tests - */

(async () => {
  console.log("=== the register ===");

  await load(baseMeta(), [record(), record({ sl_no: 2, microservice: "cart", repo_name: "demo/cart" })]);
  check("a row is rendered per record", (body().match(/<tr data-sl=/g) || []).length, 2);
  check("the SL# is shown", body().includes(">1</button>"), true);
  check("the repo is shown", body().includes("demo/payments"), true);
  check("the status label is shown", body().includes("Awaiting approval"), true);
  check("an editable row offers Open", body().includes(">Open<"), true);

  await load(baseMeta(), [record({ editable: [] })]);
  check("a row you cannot edit offers View", body().includes(">View<"), true);
  check("and does not offer Open", body().includes(">Open<"), false);

  console.log("\n=== the created date ===");

  await load(baseMeta(), [record()]);
  check("the date is shown as dd-mon-yy", body().includes(">05-Sep-26<"), true);
  check("the year is two digits, not four", body().includes("-2026<"), false);
  check("the full timestamp is on the title", body().includes('class="mig-date" title="'), true);
  check("there is a Created column header", body().includes(">Created<"), true);

  // Asserting the shape, not a literal day: the cell renders in the viewer's
  // timezone, so a fixed instant lands on different dates in different places.
  await load(baseMeta(), [record({ date_created: "2026-01-09T12:00:00Z" })]);
  check("single digits are zero-padded",
        /<td class="mig-date"[^>]*>\d{2}-[A-Z][a-z]{2}-\d{2}<\/td>/.test(body()), true);
  await load(baseMeta(), [record({ date_created: "2026-12-25T09:00:00Z" })]);
  check("December is Dec, not 12", body().includes("-Dec-26<"), true);
  await load(baseMeta(), [record({ date_created: "" })]);
  check("an undated row renders blank, not Invalid Date",
        body().includes("Invalid") || body().includes("NaN"), false);

  console.log("\n=== sorting ===");

  await load(baseMeta(), [record()]);
  check("sortable headers are buttons", body().includes('data-sort="date_created"'), true);
  check("the risk column is not sortable", body().includes('data-sort="amber"'), false);
  check("SL# leads descending by default", body().includes('data-sort="sl_no"><span'), false);
  check("the active header is marked", (body().match(/is-sorted/g) || []).length, 1);
  check("with a descending arrow", body().includes("▼"), true);

  await load(baseMeta(), [record()], { field: "date_created", dir: "asc" });
  check("the server's sort drives the header", body().includes("▲"), true);
  check("only one column is marked at a time", (body().match(/is-sorted/g) || []).length, 1);
  check("and it is the Created column",
        /aria-sort="ascending"[^>]*>\s*<button class="mig-sort" data-sort="date_created"/.test(body()), true);
  // 9 sortable columns, one of them active.
  check("the others report no sort", (body().match(/aria-sort="none"/g) || []).length, 8);

  console.log("\n=== the amber flags ===");

  await load(baseMeta(), [record({ code_image_change: "Yes", amber: ["code_image_change"] })]);
  check("a flagged field gets an amber chip", (body().match(/mig-amber/g) || []).length, 1);
  check("the chip names the field", body().includes("Code &amp; Image Change<"), true);

  await load(baseMeta(), [record({ code_image_change: "Yes", ddl_dml: "Yes", amber: ["code_image_change", "ddl_dml"] })]);
  check("two flags give two chips", (body().match(/mig-amber/g) || []).length, 2);

  await load(baseMeta(), [record()]);
  check("an unflagged row gets no chip", body().includes("mig-amber"), false);
  check("it shows a dash instead", body().includes("—"), true);

  console.log("\n=== escaping ===");

  // A developer types these into free text; they must never become markup.
  await load(baseMeta(), [
    record({
      reason: "<script>alert(1)</script>",
      repo_name: 'demo/"onmouseover="alert(1)',
      created_by: "a&b@example.com",
      track_lead: "<img src=x onerror=alert(1)>",
    }),
  ]);
  check("a script tag is escaped", body().includes("<script>"), false);
  check("and appears as text", body().includes("&lt;img src=x onerror=alert(1)&gt;"), true);
  check("a quote cannot break out of an attribute", body().includes('="alert(1)'), false);
  check("an ampersand is encoded", body().includes("a&amp;b@example.com"), true);

  console.log("\n=== notices ===");

  await load(baseMeta(), [record()]);
  check("the signed-in address is shown", notices().includes("dev@example.com"), true);
  check("so are the roles", notices().includes("developer"), true);
  check("no warning when all is well", notices().includes("mig-note warn"), false);

  const insecure = baseMeta();
  insecure.user.insecure = true;
  await load(insecure, [record()]);
  check("an unguarded identity header is called out", notices().includes("trusted_proxies"), true);


  const anon = baseMeta();
  anon.user = { email: "", roles: [], dev_mode: false, insecure: false, signed_in: false };
  await load(anon, [record()]);
  check("not signed in is stated plainly", notices().includes("Not signed in"), true);

  const noRole = baseMeta();
  noRole.user = { email: "x@y.com", roles: [], dev_mode: false, insecure: false, signed_in: true };
  await load(noRole, [record()]);
  check("an unmapped address is told it is read-only", notices().includes("read-only"), true);
  check("and cannot raise a request", els.get("mig-new").disabled, true);

  console.log("\n=== freeze ===");

  const frozen = baseMeta();
  frozen.frozen = true;
  frozen.freeze = {
    id: 1,
    starts_at: "2026-09-20T18:00:00Z",
    ends_at: "2026-09-21T06:00:00Z",
    reason: "release window",
    created_by: "lead@example.com",
    active: true,
    past: false,
  };
  await load(frozen, [record()]);
  check("the freeze is announced", notices().includes("Record entry is frozen"), true);
  check("with its reason", notices().includes("release window"), true);
  check("and who set it", notices().includes("lead@example.com"), true);
  check("raising a request is disabled", els.get("mig-new").disabled, true);
  check("uploading is disabled too", els.get("mig-upload").disabled, true);
  check("and the button says so", els.get("mig-new").title, "Record entry is frozen");
  check("the freeze button changes label", els.get("mig-freeze").textContent, "Frozen — manage…");

  await load(baseMeta(), [record()]);
  check("once lifted, raising is enabled again", els.get("mig-new").disabled, false);

  console.log("\n=== dev mode ===");

  const devMeta = baseMeta();
  devMeta.dev = {
    enabled: true,
    configured: true,
    why_not: "",
    cookie: "dev_user",
    identities: [
      { email: "dev@example.com", roles: ["developer"] },
      { email: "lead@example.com", roles: ["approver", "developer"] },
      { email: "ops@example.com", roles: ["devops"] },
    ],
  };
  await load(devMeta, [record()]);
  check("the dev bar appears", notices().includes("mig-devbar"), true);
  check("it is labelled unmistakably", notices().includes(">dev mode<"), true);
  check("every identity is offered", (notices().match(/<option value="[^"]+@/g) || []).length, 3);
  check("each shows the roles it carries", notices().includes("approver, developer"), true);
  check("the current one is selected", notices().includes('value="dev@example.com" selected'), true);
  check("and it warns there is no sign-in", notices().includes("There is no sign-in"), true);
  check("the proxy warning is not also shown", notices().includes("trusted_proxies"), false);

  const devNobody = baseMeta();
  devNobody.dev = { ...devMeta.dev };
  devNobody.user = { email: "", roles: [], dev_mode: true, insecure: true, signed_in: false };
  await load(devNobody, [record()]);
  check("with nobody picked it says so", notices().includes("Nobody is selected"), true);
  check("and does not claim you are unauthenticated by proxy",
        notices().includes("expects an authenticating proxy"), false);

  const devRefused = baseMeta();
  devRefused.dev = {
    enabled: false, configured: true,
    why_not: "dev mode is limited to loopback; set auth.dev_allow_remote to widen it",
    cookie: "dev_user", identities: [],
  };
  await load(devRefused, [record()]);
  check("a configured-but-refused dev mode explains itself",
        notices().includes("not in effect"), true);
  check("quoting the guard that refused it", notices().includes("loopback"), true);

  console.log("\n=== inactive records ===");

  await load(baseMeta(), [record(), record({ sl_no: 2, active: false })]);
  check("an inactive row is marked", body().includes('class="is-inactive"'), true);
  check("and carries an Inactive chip", body().includes(">Inactive<"), true);
  check("the active row is not marked",
        (body().match(/class="is-inactive"/g) || []).length, 1);
  const scope = () => els.get("mig-scope").textContent;
  check("the count says how many are inactive", scope().includes("1 inactive"), true);
  check("and warns that others may be hidden", scope().includes("inactive records hidden"), true);

  // load() clears the filters, so tick it afterwards and re-render.
  await load(baseMeta(), [record()]);
  els.get("mig-f-inactive").checked = true;
  await context.window.initMigrationsTab();
  check("the hidden-records warning goes when they are shown",
        scope().includes("inactive records hidden"), false);

  const approver = baseMeta();
  approver.user = { email: "lead@x", roles: ["approver"], dev_mode: false, insecure: false, signed_in: true };
  await load(approver, [record()]);
  els.get("mig-new")._on.click();
  check("a new-request dialog has no retire controls",
        appended[appended.length - 1].innerHTML.includes("mig-retire"), false);

  console.log("\n=== where errors appear ===");

  await load(baseMeta(), [record()]);
  els.get("mig-new")._on.click();
  const dlg = appended[appended.length - 1].innerHTML;
  const bodyAt = dlg.indexOf("mig-dialog-body");
  const slotAt = dlg.indexOf("mig-dialog-error");
  check("the new-request dialog opens", dlg.includes("mig-grid"), true);
  check("there is exactly one error slot",
        (dlg.match(/id="mig-dialog-error"/g) || []).length, 1);
  check("it lives in the footer, not the scrolling body", slotAt > bodyAt, true);
  check("the footer is where the buttons are", dlg.includes("mig-foot-actions"), true);
  check("an empty slot is present but collapsed",
        dlg.includes('class="mig-error-slot" role="alert"></div>'), true);
  check("every field carries its key for marking",
        (dlg.match(/class="mig-field[^"]*" data-field="/g) || []).length >= 8, true);

  console.log("\n=== empty and error states ===");

  await load(baseMeta(), []);
  check("an empty register says so", body().includes("No migration requests match"), true);

  console.log(
    `\n${FAILURES.length ? `${FAILURES.length} FAILURES: ${FAILURES.join(", ")}` : "ALL PASS"}`
  );
  process.exit(FAILURES.length ? 1 : 0);
})();
