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
    // Enough of a selector engine to reach the row buttons: they are the only
    // way into the record dialog, and that is where the workflow stages render.
    querySelectorAll(selector) {
      if (selector === ".mig-pick") {
        if (this._pickCache?.html === this._html) return this._pickCache.nodes;
        const nodes = [];
        for (const m of this._html.matchAll(/class="mig-pick" data-sl="(\d+)"([^>]*)/g)) {
          const el = makeEl("input");
          el.dataset.sl = m[1];
          el.checked = m[2].includes("checked");
          nodes.push(el);
        }
        this._pickCache = { html: this._html, nodes };
        return nodes;
      }
      if (selector === ".mig-input") {
        if (this._inputCache?.html === this._html) return this._inputCache.nodes;
        const nodes = [];
        for (const m of this._html.matchAll(/data-key="([a-z_]+)"/g)) {
          const el = makeEl("input");
          el.dataset.key = m[1];
          nodes.push(el);
        }
        this._inputCache = { html: this._html, nodes };
        return nodes;
      }
      if (selector !== ".mig-open") return [];
      // Memoised against the current markup: the caller attaches listeners to
      // what it gets back, so a fresh set each call would drop them.
      if (this._openCache?.html === this._html) return this._openCache.nodes;
      const nodes = [];
      for (const m of this._html.matchAll(/class="[^"]*mig-open[^"]*" data-sl="(\d+)"/g)) {
        const el = makeEl("button");
        el.dataset.sl = m[1];
        nodes.push(el);
      }
      this._openCache = { html: this._html, nodes };
      return nodes;
    },
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
// Elements reached by CSS selector rather than id. They must be stable across
// calls: code that opens a dialog and *then* fills its body in does so through
// document.querySelector(".mig-dialog-body"), and a fresh object each time
// would silently swallow everything it wrote.
const selected = new Map();
const pick = (sel) => {
  if (!selected.has(sel)) selected.set(sel, makeEl(sel));
  return selected.get(sel);
};

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
    querySelector: (sel) => pick(sel),
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
        selected.clear(); // a new dialog means new body and footer nodes
        // The stub has no tree, so split the dialog's markup into the parts the
        // code reaches for by selector. Without this, content written into
        // .mig-dialog-body after the dialog opens goes nowhere.
        const html = c.innerHTML || "";
        const start = html.indexOf('<div class="mig-dialog-body">');
        const end = html.indexOf('<footer class="mig-dialog-foot">');
        if (start !== -1 && end !== -1) {
          pick(".mig-dialog-body").innerHTML = html
            .slice(start + '<div class="mig-dialog-body">'.length, end)
            .replace(/<\/div>\s*$/, "");
        }
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
  { key: "change_requestor", label: "Change Requestor", kind: "enum", stage: "request", roles: ["developer"], options: "change_requestors", required: true, required_if: null, amber_when: "", depends_on: "", default: "", help: "" },
  { key: "reason", label: "Reason for Movement", kind: "longtext", stage: "request", roles: ["developer"], options: "", required: false, required_if: null, amber_when: "", depends_on: "", default: "", help: "" },
  { key: "code_image_change", label: "Code & Image Change?", kind: "yesno", stage: "request", roles: ["developer"], options: "", required: false, required_if: null, amber_when: "Yes", depends_on: "", default: "No", help: "" },
  { key: "commit_hash", label: "Commit Hash", kind: "text", stage: "request", roles: ["developer"], options: "", required: false, required_if: ["code_image_change", "Yes"], amber_when: "", depends_on: "", default: "", help: "Required when there is a code or image change." },
  { key: "ddl_dml", label: "DDL/DML", kind: "yesno", stage: "request", roles: ["developer"], options: "", required: false, required_if: null, amber_when: "Yes", depends_on: "", default: "No", help: "" },
  { key: "ready_for_qa", label: "Ready for QA", kind: "yesno", stage: "approval", roles: ["approver"], options: "", required: false, required_if: null, amber_when: "", depends_on: "", default: "No", help: "" },
  { key: "approved_by", label: "Approved By", kind: "auto", stage: "approval", roles: [], options: "", required: false, required_if: null, amber_when: "", depends_on: "", default: "", help: "" },
  { key: "qa_date_planned", label: "QA Migration Date Planned", kind: "date", stage: "approval", roles: ["approver"], options: "", required: false, required_if: null, amber_when: "", depends_on: "", default: "", help: "" },
  { key: "executed_in_qa", label: "Executed in QA", kind: "yesno", stage: "qa", roles: ["devops"], options: "", required: false, required_if: null, amber_when: "", depends_on: "", default: "No", help: "" },
  { key: "qa_migration_date", label: "QA Migration Date", kind: "auto", stage: "qa", roles: [], options: "", required: false, required_if: null, amber_when: "", depends_on: "", default: "", help: "" },
  { key: "qa_migration_remarks", label: "QA Migration Remarks", kind: "longtext", stage: "qa", roles: ["devops"], options: "", required: false, required_if: null, amber_when: "", depends_on: "", default: "", help: "" },
  { key: "executed_in_prod", label: "Executed in PROD", kind: "yesno", stage: "prod", roles: ["devops"], options: "", required: false, required_if: null, amber_when: "", depends_on: "", default: "No", help: "" },
  { key: "prod_migration_remarks", label: "Prod Migration Remarks", kind: "longtext", stage: "prod", roles: ["devops"], options: "", required: false, required_if: null, amber_when: "", depends_on: "", default: "", help: "" },
  { key: "ready_for_prod", label: "Ready for Prod", kind: "yesno", stage: "prod_gate", roles: ["approver"], options: "", required: false, required_if: null, amber_when: "", depends_on: "", default: "No", help: "" },
  { key: "prod_date_planned", label: "Prod Migration Date Planned", kind: "date", stage: "prod_gate", roles: ["approver"], options: "", required: false, required_if: null, amber_when: "", depends_on: "", default: "", help: "" },
];

const baseMeta = () => ({
  user: { email: "dev@example.com", roles: ["developer"], dev_mode: false, insecure: false, signed_in: true },
  auth_configured: true,
  fields: FIELDS,
  options: {
    releases: ["R2026.09"],
    migration_paths: ["SIT to QA"],
    microservices: ["payments", "cart"],
    change_requestors: ["E1001 - Jane Doe", "E1002 - John Roe"],
    approvers: ["lead@example.com"],
    yesno: ["Yes", "No"],
  },
  services: [
    { name: "payments", repos: ["demo/payments"], track_leads: ["A. Kumar"] },
    { name: "cart", repos: ["demo/cart", "demo/cart-ui"], track_leads: ["R. Iyer", "S. Rao"] },
  ],
  statuses: [
    { key: "submitted", label: "Awaiting approval" },
    { key: "in_prod", label: "Migrated to prod" },
  ],
  stages: [
    { key: "request", label: "Request" },
    { key: "approval", label: "QA Approval" },
    { key: "qa", label: "QA Migration" },
    { key: "prod_gate", label: "Prod Approval" },
    { key: "prod", label: "Production Migration" },
  ],
  me_employee: "E1001 - Jane Doe",
  employees: [
    { number: "E1001", name: "Jane Doe", label: "E1001 - Jane Doe" },
    { number: "E1002", name: "John Roe", label: "E1002 - John Roe" },
  ],
  request_fields: ["release", "microservice", "repo_name", "track_lead", "change_requestor",
                   "reason", "code_image_change", "commit_hash", "ddl_dml"],
  freeze: null,
  frozen: false,
  permissions: { can_raise: true, why_not_raise: "", can_retire: false, can_freeze: false,
                 can_approve: false, can_execute: false, can_archive: false },
  dev: { enabled: false, configured: false, why_not: "", cookie: "dev_user", identities: [] },
  services_source: { file: "", error: "", count: 2 },
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
    permissions: META.permissions,
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
  // Shared helpers. A real click flips `checked` before the change event fires,
  // and closing goes through the native dialog's own close event.
  const close_ = () => els.get("mig-overlay")?._on?.close?.();
  const tick = (el, on = true) => {
    el.checked = on;
    el._on.change();
  };

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
  noRole.permissions = {
    can_raise: false,
    why_not_raise: "No role is mapped to your address in config.yaml.",
    can_retire: false, can_freeze: false, can_approve: false, can_execute: false,
    can_archive: false,
  };
  await load(noRole, [record()]);
  check("an unmapped address is told it is read-only", notices().includes("read-only"), true);
  check("and cannot raise a request", els.get("mig-new").disabled, true);
  check("the reason is on the button", els.get("mig-new").title.includes("No role is mapped"), true);
  check("the page shows the config to add", notices().includes("auth:"), true);
  check("with their own address in it", notices().includes("developer: [x@y.com]"), true);
  check("uploading is blocked too", els.get("mig-upload").disabled, true);

  // admin holds no other role, but the server says it may raise — the UI must
  // take that answer rather than looking for "developer" in the list.
  const admin = baseMeta();
  admin.user = { email: "boss@y.com", roles: ["admin"], dev_mode: false, insecure: false, signed_in: true };
  admin.permissions = { can_raise: true, why_not_raise: "", can_retire: true, can_freeze: true,
                        can_approve: true, can_execute: true, can_archive: true };
  await load(admin, [record()]);
  check("an admin can raise, despite holding no developer role",
        els.get("mig-new").disabled, false);

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
  frozen.permissions = { can_raise: true, why_not_raise: "", can_retire: true,
                         can_freeze: true, can_approve: true, can_execute: true, can_archive: true };
  await load(frozen, [record({ editable: [] })]);
  check("the freeze is announced", notices().includes("Record entry is frozen"), true);
  check("with its reason", notices().includes("release window"), true);
  check("and who set it", notices().includes("lead@example.com"), true);
  check("raising a request is disabled", els.get("mig-new").disabled, true);
  check("uploading is disabled too", els.get("mig-upload").disabled, true);
  check("and the button says so", els.get("mig-new").title, "Record entry is frozen.");
  check("the freeze button changes label", els.get("mig-freeze").textContent, "Frozen — manage…");
  check("the register is greyed out", body().includes('class="table-scroll is-frozen"'), true);
  check("with the reason on hover", body().includes('title="Record entry is frozen"'), true);
  check("selection is disabled", /class="mig-pick"[^>]*disabled/.test(body()), true);
  check("including select-all", /id="mig-pick-all"[^>]*disabled/.test(body()), true);
  check("but the rows are still readable", body().includes("data-sl=\"1\""), true);
  check("and the freeze button stays usable, to lift it",
        els.get("mig-freeze").disabled, false);

  // Opening a record while frozen must offer nothing that would be refused.
  els.get("mig-body").querySelectorAll(".mig-open")[0]._on.click();
  const frozenRecord = appended[appended.length - 1].innerHTML;
  check("the record says it is frozen", frozenRecord.includes("Record entry is frozen"), true);
  check("there is no Save button", frozenRecord.includes('id="mig-save"'), false);
  check("no Deactivate", frozenRecord.includes('id="mig-retire"'), false);
  check("no Delete", frozenRecord.includes('id="mig-delete"'), false);
  check("and no editable control", frozenRecord.includes("mig-input"), false);
  check("only a way out", frozenRecord.includes('id="mig-cancel"'), true);
  close_();

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

  console.log("\n=== bulk approval ===");

  const boss = baseMeta();
  boss.user = { email: "lead@x", roles: ["approver"], dev_mode: false, insecure: false, signed_in: true };
  boss.permissions = { can_raise: true, why_not_raise: "", can_retire: true, can_freeze: true,
                       can_approve: true, can_execute: false, can_archive: true };

  const waiting = record({ sl_no: 1, status: "submitted", status_label: "Awaiting approval" });
  const inQa = record({ sl_no: 2, status: "in_qa", status_label: "Migrated to QA" });
  const done = record({ sl_no: 3, status: "in_prod", status_label: "Migrated to prod" });

  await load(baseMeta(), [waiting, inQa, done]);
  check("no checkboxes without the approver role", body().includes("mig-pick"), false);

  await load(boss, [waiting, inQa, done]);
  check("an approver gets a checkbox per row",
        (body().match(/class="mig-pick"/g) || []).length, 3);
  check("and a select-all in the header", body().includes('id="mig-pick-all"'), true);
  check("the bulk bar is hidden until something is picked",
        body().includes("mig-bulkbar"), false);

  // A real click flips `checked` before the change event fires.

  tick(els.get("mig-body").querySelectorAll(".mig-pick")[0]);
  check("picking a row shows the bar", body().includes("mig-bulkbar"), true);
  check("it counts the selection", body().includes("1 selected"), true);
  check("and offers QA approval for a waiting record",
        /id="mig-bulk-qa_approve"(?![^>]*disabled)/.test(body()), true);
  check("but not prod approval", /id="mig-bulk-prod_approve"[^>]*disabled/.test(body()), true);

  tick(els.get("mig-pick-all"));
  check("select-all takes every row", body().includes("3 selected"), true);
  check("QA approval counts only the waiting one", body().includes("Approve 1 for QA"), true);
  check("prod approval counts only the QA-migrated one", body().includes("Approve 1 for prod"), true);

  // The confirm dialog must ask for the date the batch is being approved for.
  els.get("mig-bulk-qa_approve")._on.click();
  let bulkDlg = appended[appended.length - 1].innerHTML;
  check("the confirm dialog asks for a planned date",
        bulkDlg.includes('id="mig-bulk-extra"'), true);
  check("labelled for QA", bulkDlg.includes("QA Migration Date Planned"), true);
  check("as a date input", /id="mig-bulk-extra"[^>]*type="date"/.test(bulkDlg), true);
  check("it says the date applies to all of them", bulkDlg.includes("Applied to all"), true);
  check("and that blank keeps each record's own",
        bulkDlg.includes("keep each record's own"), true);
  check("the listing shows each record's current planned date",
        bulkDlg.includes("mig-date"), true);
  close_(); 

  await load(boss, [waiting, inQa, done]);
  tick(els.get("mig-pick-all"));
  els.get("mig-bulk-prod_approve")._on.click();
  bulkDlg = appended[appended.length - 1].innerHTML;
  check("the prod dialog asks for the prod date",
        bulkDlg.includes("Prod Migration Date Planned"), true);
  close_();

  await load(boss, [waiting, inQa, done]);
  tick(els.get("mig-body").querySelectorAll(".mig-pick")[0]);
  els.get("mig-bulk-clear")._on.click();
  check("clearing hides the bar again", body().includes("mig-bulkbar"), false);

  // A freeze must disable the bulk actions, not just the single-record ones.
  const frozenBoss = baseMeta();
  frozenBoss.user = boss.user;
  frozenBoss.permissions = boss.permissions;
  frozenBoss.frozen = true;
  frozenBoss.freeze = { id: 1, starts_at: "2026-09-20T18:00:00Z", ends_at: "2026-09-21T06:00:00Z",
                        reason: "", created_by: "lead@x", active: true, past: false };
  await load(frozenBoss, [waiting]);
  tick(els.get("mig-body").querySelectorAll(".mig-pick")[0]);
  check("a freeze disables bulk QA approval",
        /id="mig-bulk-qa_approve"[^>]*disabled/.test(body()), true);
  check("and bulk prod approval",
        /id="mig-bulk-prod_approve"[^>]*disabled/.test(body()), true);

  console.log("\n=== bulk execution, by role ===");

  const ops = baseMeta();
  ops.user = { email: "ops@x", roles: ["devops"], dev_mode: false, insecure: false, signed_in: true };
  ops.permissions = { can_raise: false, why_not_raise: "Raising a request needs the developer role.",
                      can_retire: false, can_freeze: false, can_approve: false, can_execute: true, can_archive: false };

  const approvedRow = record({ sl_no: 4, status: "approved", status_label: "Approved" });
  const readyRow = record({ sl_no: 5, status: "ready_for_prod", status_label: "Ready for prod" });

  await load(ops, [waiting, approvedRow, readyRow]);
  check("devops gets checkboxes too", (body().match(/class="mig-pick"/g) || []).length, 3);
  tick(els.get("mig-pick-all"));
  check("but not the approval buttons", body().includes("mig-bulk-qa_approve"), false);
  check("nor the prod approval", body().includes("mig-bulk-prod_approve"), false);
  check("they get QA execution", body().includes("mig-bulk-qa_execute"), true);
  check("and prod execution", body().includes("mig-bulk-prod_execute"), true);
  check("QA execution counts the approved one", body().includes("Mark 1 migrated to QA"), true);
  check("prod execution counts the ready one", body().includes("Mark 1 migrated to prod"), true);

  els.get("mig-bulk-qa_execute")._on.click();
  let execDlg = appended[appended.length - 1].innerHTML;
  check("its dialog asks for remarks", execDlg.includes("QA Migration Remarks"), true);
  check("as a free-text box", execDlg.includes("<textarea id=\"mig-bulk-extra\""), true);
  check("and no date field", /id="mig-bulk-extra"[^>]*type="date"/.test(execDlg), false);
  check("it says who and when get recorded", execDlg.includes("records you and the time"), true);
  close_();

  // An approver sees the other pair, and only that pair.
  await load(boss, [waiting, approvedRow, readyRow]);
  tick(els.get("mig-pick-all"));
  check("an approver gets no execution buttons", body().includes("mig-bulk-qa_execute"), false);
  check("only the approvals", body().includes("mig-bulk-qa_approve"), true);

  // An admin gets all four.
  const superuser = baseMeta();
  superuser.user = { email: "boss@x", roles: ["admin"], dev_mode: false, insecure: false, signed_in: true };
  superuser.permissions = { can_raise: true, why_not_raise: "", can_retire: true, can_freeze: true,
                            can_approve: true, can_execute: true, can_archive: true };
  await load(superuser, [waiting, approvedRow, readyRow]);
  tick(els.get("mig-pick-all"));
  check("an admin gets all four actions",
        ["qa_approve", "prod_approve", "qa_execute", "prod_execute"]
          .every((k) => body().includes(`mig-bulk-${k}`)), true);

  // Someone with neither role gets no selection column at all.
  const plain = baseMeta();
  plain.permissions = { can_raise: true, why_not_raise: "", can_retire: false, can_freeze: false,
                        can_approve: false, can_execute: false };
  await load(plain, [waiting]);
  check("a developer gets no checkboxes", body().includes("mig-pick"), false);

  console.log("\n=== change requestor ===");

  await load(baseMeta(), [record()]);
  els.get("mig-new")._on.click();
  let reqForm = pick(".mig-dialog-body").innerHTML;
  const selectOf = (html, key) =>
    (html.match(new RegExp(`<select[^>]*data-key="${key}"[\\s\\S]*?</select>`)) || [""])[0];

  check("it offers the employees",
        selectOf(reqForm, "change_requestor").includes("E1002 - John Roe"), true);
  check("and starts on you",
        selectOf(reqForm, "change_requestor").includes('value="E1001 - Jane Doe" selected'), true);
  check("saying so, since it is yours to change",
        reqForm.includes("That is you — change it if you are raising this for someone else."), true);
  check("but the others are still selectable",
        (selectOf(reqForm, "change_requestor").match(/<option/g) || []).length, 3);
  close_();

  // Someone whose address is not on the employee list gets no default.
  const unmapped = baseMeta();
  unmapped.me_employee = "";
  await load(unmapped, [record()]);
  els.get("mig-new")._on.click();
  reqForm = pick(".mig-dialog-body").innerHTML;
  check("an unmapped user gets no default",
        selectOf(reqForm, "change_requestor").includes("selected"), false);
  check("and no claim that it is them",
        reqForm.includes("That is you"), false);
  close_();

  console.log("\n=== dependent fields ===");

  await load(baseMeta(), [record()]);
  els.get("mig-new")._on.click();
  let form = pick(".mig-dialog-body");
  check("repo starts with nothing to offer",
        form.innerHTML.includes("Select a Micro Service Name first"), true);

  // Just the one <select>, so a "selected" elsewhere on the form cannot match.
  const selectFor = (html, key) =>
    (html.match(new RegExp(`<select[^>]*data-key="${key}"[\\s\\S]*?</select>`)) || [""])[0];
  const service = () =>
    pick(".mig-dialog-body").querySelectorAll(".mig-input").find((i) => i.dataset.key === "microservice");

  let control = service();
  control.value = "payments";
  control._on.change();
  form = pick(".mig-dialog-body");
  check("its single repo is selected for you",
        selectFor(form.innerHTML, "repo_name").includes('value="demo/payments" selected'), true);
  check("so is its single track lead",
        selectFor(form.innerHTML, "track_lead").includes('value="A. Kumar" selected'), true);
  check("and it says it did so", form.innerHTML.includes("Only one — filled in for you."), true);
  check("twice, once per field",
        (form.innerHTML.match(/Only one — filled in for you\./g) || []).length, 2);

  // Switching to a service with several leaves the choice to the person.
  control = service();
  control.value = "cart";
  control._on.change();
  form = pick(".mig-dialog-body");
  check("a service with two repos selects neither",
        selectFor(form.innerHTML, "repo_name").includes("selected"), false);
  check("nor two leads", selectFor(form.innerHTML, "track_lead").includes("selected"), false);
  check("but both are on offer",
        selectFor(form.innerHTML, "repo_name").includes("demo/cart-ui"), true);
  check("and says nothing about filling in",
        form.innerHTML.includes("Only one — filled in for you."), false);
  check("the stale repo is gone from the options",
        form.innerHTML.includes("demo/payments"), false);
  close_();

  console.log("\n=== the archive ===");

  const filer = baseMeta();
  filer.user = { email: "lead@x", roles: ["approver"], dev_mode: false, insecure: false, signed_in: true };
  filer.permissions = { can_raise: true, why_not_raise: "", can_retire: true, can_freeze: true,
                        can_approve: true, can_execute: false, can_archive: true };

  const shipped = record({ sl_no: 9, status: "in_prod", status_label: "Migrated to prod",
                           prod_migration_date: "2026-09-10T02:10:00Z" });
  await load(filer, [waiting, shipped]);
  tick(els.get("mig-pick-all"));
  check("Archive is offered", body().includes("mig-bulk-archive"), true);
  check("counting only the production-migrated ones",
        body().includes("Archive 1<"), true);

  els.get("mig-bulk-archive")._on.click();
  const arch = appended[appended.length - 1].innerHTML;
  check("the dialog warns it cannot be undone", arch.includes("cannot be undone"), true);
  check("and says where they go", arch.includes("read only from <strong>Archive</strong>"), true);
  check("it lists the prod migration date", arch.includes("10-Sep-26"), true);
  check("the other row is set aside", arch.includes("will be left alone"), true);
  close_();

  // Someone without the role gets no Archive action at all.
  const noFile = baseMeta();
  noFile.permissions = { can_raise: true, why_not_raise: "", can_retire: false, can_freeze: false,
                         can_approve: true, can_execute: false, can_archive: false };
  noFile.user = filer.user;
  await load(noFile, [shipped]);
  tick(els.get("mig-pick-all"));
  check("without the role there is no Archive button", body().includes("mig-bulk-archive"), false);

  // The archive view itself.
  await load(filer, [record({ sl_no: 9, status: "in_prod", status_label: "Migrated to prod",
                              archived: true, archived_at: "2026-09-12T08:00:00Z",
                              archived_by: "lead@x", editable: [] })]);
  els.get("mig-archive-view")._on.click();
  await new Promise((r) => setTimeout(r, 0));
  check("the button becomes the way back",
        els.get("mig-archive-view").textContent, "Back to the register");
  check("a banner says what you are looking at", notices().includes("Viewing the archive"), true);
  check("it says they cannot be changed", notices().includes("cannot be changed"), true);
  check("rows carry an Archived chip", body().includes("mig-status archived"), true);
  check("dated", body().includes("Archived 12-Sep-26"), true);
  check("raising a request is unavailable there", els.get("mig-new").disabled, true);
  check("and there are no bulk actions", body().includes("mig-bulk-"), false);

  els.get("mig-archive-view")._on.click();
  await new Promise((r) => setTimeout(r, 0));
  check("and it toggles back", els.get("mig-archive-view").textContent, "Archive");

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

  console.log("\n=== the record's stages ===");

  const approver2 = baseMeta();
  approver2.user = { email: "lead@x", roles: ["approver"], dev_mode: false, insecure: false, signed_in: true };
  await load(approver2, [record({ editable: ["ready_for_qa", "qa_date_planned"] })]);

  // Open the record the way a person does: the button in its row.
  els.get("mig-body").querySelectorAll(".mig-open")[0]._on.click();
  const detail = appended[appended.length - 1].innerHTML;

  check("the stage headings come from the server", detail.includes(">QA Approval<"), true);
  check("and the prod gate is renamed too", detail.includes(">Prod Approval<"), true);
  check("nothing is still labelled plain Approval", />Approval</.test(detail), false);
  check("nor 'Ready for production'", detail.includes("Ready for production"), false);

  check("Ready for QA is offered to an approver", detail.includes('data-key="ready_for_qa"'), true);
  check("so is the planned QA migration date", detail.includes('data-key="qa_date_planned"'), true);
  check("and it is labelled as a migration date",
        detail.includes("QA Migration Date Planned"), true);
  check("Approved By is shown read-only, not as a control",
        detail.includes('data-key="approved_by"'), false);
  check("but it is still displayed", detail.includes(">Approved By<"), true);
  check("QA Migration Date appears under QA Migration",
        detail.indexOf("QA Migration Date<") > detail.indexOf(">QA Migration<"), true);
  check("Prod Migration Date Planned is there",
        detail.includes("Prod Migration Date Planned"), true);
  check("the stage the approver owns is marked as theirs",
        (detail.match(/mig-yours/g) || []).length >= 1, true);

  console.log("\n=== the reference lists screen ===");

  const listData = {
    releases: ["R2026.09", "R2026.10"],
    migration_paths: [],
    change_requestors: ["E1001 - Jane Doe"],
    microservices: [{ name: "payments", repos: ["demo/payments"], track_leads: ["A. Kumar"] }],
    employees: [{ number: "E1001", name: "Jane Doe", email: "jane@x.com", label: "E1001 - Jane Doe" }],
    can_edit: true,
    storage: "PostgreSQL repo_dashboard on 127.0.0.1:5432",
  };
  const prevFetch = context.fetch;
  context.fetch = async (url, opts) => {
    if (url.startsWith("/api/migrations/lists")) return jsonResponse(listData);
    return prevFetch(url, opts);
  };

  await load(boss, [waiting]);
  els.get("mig-lists")._on.click();
  await new Promise((r) => setTimeout(r, 0));
  const lists = pick(".mig-dialog-body").innerHTML;
  check("the screen names where the lists are stored",
        lists.includes("PostgreSQL repo_dashboard"), true);
  check("each list is editable as text", lists.includes('id="mig-list-releases"'), true);
  check("with its values one per line", lists.includes("R2026.09\nR2026.10"), true);
  check("an empty list is called out", lists.includes("Empty — no one can pick"), true);
  check("microservices are listed", lists.includes(">payments<"), true);
  check("employees are listed too", lists.includes(">E1001<"), true);
  check("with the email that maps them", lists.includes("jane@x.com"), true);
  check("and the mapping is explained", lists.includes("matched to"), true);
  check("change requestors are not a list of their own",
        lists.includes('id="mig-list-change_requestors"'), false);
  check("with their repos", lists.includes("demo/payments"), true);
  check("and can be added", lists.includes('id="mig-svc-add"'), true);
  close_();

  listData.can_edit = false;
  await load(boss, [waiting]);
  els.get("mig-lists")._on.click();
  await new Promise((r) => setTimeout(r, 0));
  const readOnly = pick(".mig-dialog-body").innerHTML;
  check("without the role there are no text boxes",
        readOnly.includes('id="mig-list-releases"'), false);
  check("but the values are still shown", readOnly.includes("R2026.09, R2026.10"), true);
  check("and it says why it is read-only",
        readOnly.includes("changing them needs the approver role"), true);
  close_();
  context.fetch = prevFetch;

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
