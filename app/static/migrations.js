/* Migration requests tab.
 *
 * Kept out of app.js because it is a form application rather than a view over
 * git, and app.js is already long. Nothing here is a security control: the
 * server re-checks every field against the caller's role on each write. What
 * this file does is stop people being offered controls that would be refused.
 */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);

  const esc = (value) =>
    String(value ?? "").replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
    );

  const state = {
    meta: null,
    rows: [],
    sort: { field: "sl_no", dir: "desc" },
    selected: new Set(),
    archiveView: false,
    open: null, // the record shown in the detail dialog
    dirty: {},
    loading: false,
  };

  /* ---------------------------------------------------------------- api --- */

  async function api(path, options = {}) {
    const response = await fetch(path, {
      headers: options.body instanceof FormData ? {} : { "Content-Type": "application/json" },
      ...options,
    });
    const text = await response.text();
    let body = null;
    try {
      body = text ? JSON.parse(text) : null;
    } catch {
      body = null;
    }
    if (!response.ok) {
      let detail = (body && (body.detail || body.message)) || text || response.statusText;
      // FastAPI's own validation errors put a list here rather than a sentence.
      if (Array.isArray(detail)) {
        detail = detail.map((d) => `${(d.loc || []).slice(-1)[0] || "field"}: ${d.msg}`).join("; ");
      }
      const error = new Error(detail);
      error.status = response.status;
      error.fields = (body && body.fields) || {};
      throw error;
    }
    return body;
  }

  /* ------------------------------------------------------------ helpers --- */

  const fieldsByKey = () => {
    const map = {};
    for (const f of state.meta.fields) map[f.key] = f;
    return map;
  };

  function optionsFor(field, values) {
    const meta = state.meta;
    if (field.kind === "yesno") return ["Yes", "No"];
    if (field.depends_on) {
      const chosen = values[field.depends_on] || "";
      const service = meta.services.find((s) => s.name === chosen);
      if (!service) return [];
      return field.key === "repo_name" ? service.repos : service.track_leads;
    }
    return (field.options && meta.options[field.options]) || [];
  }

  // Fixed month names rather than toLocaleString: the format was asked for as
  // dd-mon-yyyy, and a locale-driven "short" month yields "sept." or "9月".
  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

  function dmy(iso) {
    if (!iso) return "";
    // A bare YYYY-MM-DD parses as UTC midnight, which renders as the day before
    // anywhere west of Greenwich. Planned dates are calendar days, not instants,
    // so pin them to local midnight instead.
    const text = /^\d{4}-\d{2}-\d{2}$/.test(iso) ? `${iso}T00:00:00` : iso;
    const date = new Date(text);
    if (Number.isNaN(date.getTime())) return iso;
    const day = String(date.getDate()).padStart(2, "0");
    const year = String(date.getFullYear()).slice(-2);
    return `${day}-${MONTHS[date.getMonth()]}-${year}`;
  }

  /* Keep the dependent fields honest whenever what they depend on changes.
   *
   * A value that is no longer on offer is cleared — otherwise the form would
   * keep showing the previous microservice's repo and the server would reject
   * the pairing. And where a service has exactly one repo or one track lead
   * there is no choice to make, so it is filled in rather than left as a
   * one-item dropdown to click through.
   *
   * Returns the keys it filled in, so the form can say it did. */
  function settleDependents(values) {
    const filled = [];
    for (const field of state.meta.fields) {
      if (!field.depends_on) continue;
      const options = optionsFor(field, values);
      if (options.length === 1) {
        if (values[field.key] !== options[0]) filled.push(field.key);
        values[field.key] = options[0];
      } else if (!options.includes(values[field.key])) {
        values[field.key] = "";
      }
    }
    return filled;
  }

  function shortDate(iso) {
    if (!iso) return "";
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return iso;
    return date.toLocaleString(undefined, {
      year: "numeric", month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit",
    });
  }

  /* ------------------------------------------------------------ notices --- */

  /* Dev mode: the caller declares who they are. Stored in a cookie rather than
   * only a header, so plain navigations — the CSV and template downloads —
   * carry the choice too. */
  function setDevUser(email) {
    const safe = String(email || "").replace(/[;,\s]/g, "");
    const name = state.meta.dev.cookie;
    document.cookie = `${name}=${safe}; path=/; SameSite=Lax; max-age=604800`;
  }

  function devStrip() {
    const dev = state.meta.dev;
    const current = state.meta.user.email;
    const known = dev.identities.some((i) => i.email === current);
    const options = dev.identities
      .map(
        (i) =>
          `<option value="${esc(i.email)}"${i.email === current ? " selected" : ""}>` +
          `${esc(i.email)}${i.roles.length ? ` — ${esc(i.roles.join(", "))}` : " — no roles"}</option>`
      )
      .join("");
    return (
      `<div class="mig-devbar">` +
      `<span class="mig-devtag">dev mode</span>` +
      `<label for="mig-dev-as">Acting as</label>` +
      `<select id="mig-dev-as" class="select">` +
      `<option value=""${current ? "" : " selected"}>— nobody —</option>` +
      options +
      (current && !known ? `<option value="${esc(current)}" selected>${esc(current)}</option>` : "") +
      `</select>` +
      `<button class="btn tiny" id="mig-dev-other">Other…</button>` +
      `</div>`
    );
  }

  function wireDevStrip() {
    const select = $("mig-dev-as");
    if (!select) return;
    select.addEventListener("change", async () => {
      setDevUser(select.value);
      await refresh();
    });
    $("mig-dev-other")?.addEventListener("click", async () => {
      const typed = window.prompt?.("Act as which address?", state.meta.user.email || "");
      if (typed === null || typed === undefined) return;
      setDevUser(typed);
      await refresh();
    });
  }

  function renderNotices() {
    const { user, freeze, auth_configured: configured, dev } = state.meta;
    const parts = [];

    if (dev.enabled) parts.push(devStrip());

    const roles = user.roles.length ? user.roles.join(", ") : "no roles";
    parts.push(
      `<div class="mig-whoami">` +
        `<span class="mig-who">${esc(user.email || "Not signed in")}</span>` +
        `<span class="mig-roles">${esc(roles)}</span>` +
      `</div>`
    );

    if (dev.enabled) {
      parts.push(
        note("warn",
          "<strong>Dev mode.</strong> There is no sign-in — identity is whatever is picked above, " +
          "so any role can be assumed. For testing only; set <code>auth.dev_mode: false</code> " +
          "and put an SSO proxy in front before anyone relies on this.")
      );
      if (!user.signed_in) {
        parts.push(note("warn", "Nobody is selected. Pick an identity above to use the register."));
      }
    } else if (dev.configured) {
      // Configured but refused — say which guard turned it down, or it looks broken.
      parts.push(note("warn", `<code>auth.dev_mode</code> is set but not in effect: ${esc(dev.why_not)}.`));
    }

    if (!user.signed_in && !dev.enabled) {
      parts.push(
        note("warn",
          "<strong>Not signed in.</strong> No identity header reached the app, so the register " +
          "is read-only. Either put an authenticating proxy in front of it, or — to test " +
          "without one — set <code>auth.dev_mode: true</code> <em>and</em> remove " +
          "<code>auth.trusted_proxies</code> from config.yaml. Those two are mutually " +
          "exclusive: a configured proxy allowlist turns dev mode off.")
      );
    } else if (!configured) {
      parts.push(note("warn", "No roles are configured, so nobody can do anything. Add <code>auth.roles</code> to config.yaml."));
    } else if (!user.roles.length) {
      // A greyed-out button with a tooltip is easy to miss and invisible on
      // touch, so the reason and the fix go on the page itself.
      parts.push(
        note("warn",
          `<strong>No role is mapped to ${esc(user.email)}</strong>, so the register is ` +
          `read-only — <em>New request</em> and <em>Upload sheet</em> are disabled. ` +
          `Add the address under <code>auth.roles</code> in config.yaml and restart:` +
          `<pre class="mig-snippet">auth:\n  roles:\n    developer: [${esc(user.email)}]\n` +
          `    approver:  [${esc(user.email)}]\n    devops:    [${esc(user.email)}]</pre>`)
      );
    }

    if (user.insecure && user.signed_in && !dev.enabled) {
      parts.push(
        note("warn",
          "The identity header is being accepted from <strong>any</strong> address. Set <code>auth.trusted_proxies</code>, " +
          "or anyone who can reach this app directly can claim to be anybody.")
      );
    }

    // A broken microservice file leaves the dependent dropdowns empty, which
    // otherwise looks like a bug in the form rather than a fixable typo.
    const source = state.meta.services_source;
    if (source && source.error) {
      parts.push(
        note("warn",
          `<strong>The microservice list could not be read.</strong> ${esc(source.error)} ` +
          (source.count
            ? "Falling back to the list in config.yaml."
            : "Repo Name and Track Lead Name will have nothing to offer until this is fixed."))
      );
    }

    if (state.archiveView) {
      parts.push(
        note("archive",
          "<strong>Viewing the archive.</strong> These records were migrated to production " +
          "and filed away. They cannot be changed, deactivated or deleted. " +
          "Press <em>Back to the register</em> to leave.")
      );
    }

    if (freeze) {
      parts.push(
        note("freeze",
          `<strong>Record entry is frozen</strong> until ${esc(shortDate(freeze.ends_at))}` +
          (freeze.reason ? ` — ${esc(freeze.reason)}` : "") +
          `. Set by ${esc(freeze.created_by)}. No one can create or edit records until it lifts.`)
      );
    }

    $("mig-notices").innerHTML = parts.join("");
    wireDevStrip();
  }

  const note = (kind, html) => `<div class="mig-note ${kind}">${html}</div>`;

  /* ------------------------------------------------------------- filters -- */

  function fillFilters() {
    const meta = state.meta;
    const fill = (id, values) => {
      const select = $(id);
      const keep = select.value;
      select.innerHTML =
        `<option value="">All</option>` +
        values.map((v) => `<option value="${esc(v)}">${esc(v)}</option>`).join("");
      if (keep) select.value = keep;
    };
    fill("mig-f-release", meta.options.releases);
    fill("mig-f-service", meta.options.microservices);
    fill("mig-f-path", meta.options.migration_paths);

    const status = $("mig-f-status");
    const keep = status.value;
    status.innerHTML =
      `<option value="">All</option>` +
      meta.statuses.map((s) => `<option value="${esc(s.key)}">${esc(s.label)}</option>`).join("");
    if (keep) status.value = keep;
  }

  function filterQuery() {
    const params = new URLSearchParams();
    const add = (key, id) => {
      const value = $(id).value;
      if (value) params.set(key, value);
    };
    add("release", "mig-f-release");
    add("microservice", "mig-f-service");
    add("migration_path", "mig-f-path");
    add("status", "mig-f-status");
    if ($("mig-f-mine").checked) params.set("mine", "true");
    if ($("mig-f-inactive").checked) params.set("include_inactive", "true");

    // Sent as instants computed from the viewer's own midnight, so the day a
    // record is filtered into matches the day printed beside it.
    const from = $("mig-f-from").value;
    const to = $("mig-f-to").value;
    if (from) params.set("created_from_epoch", Date.parse(`${from}T00:00:00`) / 1000);
    if (to) params.set("created_to_epoch", Date.parse(`${to}T23:59:59.999`) / 1000);

    if (state.archiveView) params.set("archived", "true");
    params.set("sort", state.sort.field);
    params.set("dir", state.sort.dir);
    return params;
  }

  /* --------------------------------------------------------------- table -- */

  /* The four bulk actions. `from` is the status a record must be in for the
   * action to move it — read off the status the server computed rather than
   * re-deriving the stage gates here. The server decides for real and reports
   * anything this preview got wrong. */
  const BULK = {
    qa_approve: {
      endpoint: "approve", stage: "qa", need: "can_approve", from: "submitted",
      button: "Approve %n for QA", title: "Approve %n for QA",
      lead: "This sets <strong>Ready for QA</strong> on each, and records you as the approver.",
      extra: "qa_date_planned", kind: "date",
    },
    prod_approve: {
      endpoint: "approve", stage: "prod", need: "can_approve", from: "in_qa",
      button: "Approve %n for prod", title: "Approve %n for production",
      lead: "This sets <strong>Ready for Prod</strong> on each, and records you as the approver.",
      extra: "prod_date_planned", kind: "date",
    },
    qa_execute: {
      endpoint: "execute", stage: "qa", need: "can_execute", from: "approved",
      button: "Mark %n migrated to QA", title: "Record %n as migrated to QA",
      lead: "This sets <strong>Executed in QA</strong> on each, and records you and the time against every one.",
      extra: "qa_migration_remarks", kind: "text",
    },
    prod_execute: {
      endpoint: "execute", stage: "prod", need: "can_execute", from: "ready_for_prod",
      button: "Mark %n migrated to prod", title: "Record %n as migrated to production",
      lead: "This sets <strong>Executed in PROD</strong> on each, and records you and the time against every one.",
      extra: "prod_migration_remarks", kind: "text",
    },
  };

  function eligible(row, action) {
    if (action === "archive") return row.active && row.status === "in_prod" && !row.archived;
    return row.active && row.status === BULK[action].from;
  }

  const allowedActions = () => {
    const can = state.meta.permissions || {};
    if (state.archiveView) return [];
    const keys = Object.keys(BULK).filter((key) => can[BULK[key].need]);
    if (can.can_archive) keys.push("archive");
    return keys;
  };

  const STATUS_CLASS = {
    submitted: "wait", approved: "ok", in_qa: "ok",
    ready_for_prod: "ok", in_prod: "done",
  };

  function renderTable() {
    const body = $("mig-body");
    if (!state.rows.length) {
      body.innerHTML = state.archiveView
        ? `<div class="empty"><p>Nothing has been archived yet.</p>` +
          `<p class="muted">Records can be archived once they have been migrated to production.</p></div>`
        : `<div class="empty"><p>No migration requests match these filters.</p></div>`;
      $("mig-scope").textContent = "";
      return;
    }

    const specs = fieldsByKey();
    const canPick = allowedActions().length > 0;
    // Drop anything no longer on screen, so a filter change cannot leave
    // invisible records selected and quietly approve them.
    const visible = new Set(state.rows.map((r) => r.sl_no));
    for (const sl of [...state.selected]) if (!visible.has(sl)) state.selected.delete(sl);

    const rows = state.rows
      .map((row) => {
        const amber = row.amber
          .map((key) => `<span class="mig-amber">${esc(specs[key].label.replace(/\?$/, ""))}</span>`)
          .join("");
        const pick = canPick
          ? `<td class="mig-pickcol"><input type="checkbox" class="mig-pick" data-sl="${row.sl_no}"` +
            `${state.selected.has(row.sl_no) ? " checked" : ""}` +
            `${state.meta.frozen ? " disabled" : ""} aria-label="Select SL# ${row.sl_no}" /></td>`
          : "";
        return (
          `<tr data-sl="${row.sl_no}"${row.active ? "" : ' class="is-inactive"'}>` + pick +
          `<td class="num"><button class="linkish mig-open" data-sl="${row.sl_no}">${row.sl_no}</button></td>` +
          `<td>${esc(row.release)}</td>` +
          `<td>${esc(row.migration_path)}</td>` +
          `<td>${esc(row.microservice)}</td>` +
          `<td class="mono">${esc(row.repo_name)}</td>` +
          `<td>${esc(row.track_lead)}</td>` +
          `<td class="mig-risk">${amber || '<span class="muted">—</span>'}</td>` +
          `<td>${esc(row.created_by)}</td>` +
          `<td class="mig-date" title="${esc(shortDate(row.date_created))}">${esc(dmy(row.date_created))}</td>` +
          `<td><span class="mig-status ${STATUS_CLASS[row.status] || ""}">${esc(row.status_label)}</span>` +
          (row.active ? "" : ` <span class="mig-status retired">Inactive</span>`) +
          (row.archived
            ? ` <span class="mig-status archived" title="Archived by ${esc(row.archived_by)}">` +
              `Archived ${esc(dmy(row.archived_at))}</span>`
            : "") +
          `</td>` +
          `<td class="num"><button class="btn tiny mig-open" data-sl="${row.sl_no}">${row.editable.length ? "Open" : "View"}</button></td>` +
          `</tr>`
        );
      })
      .join("");

    const columns = [
      ["sl_no", "SL#"],
      ["release", "Rel#"],
      ["migration_path", "Migration path"],
      ["microservice", "Micro service"],
      ["repo_name", "Repo"],
      ["track_lead", "Track lead"],
      [null, "Risk"],
      ["created_by", "Created by"],
      ["date_created", "Created"],
      ["status", "Status"],
      [null, ""],
    ];
    const sortable = new Set(state.meta?.sortable || []);
    const allPicked = state.rows.length > 0 && state.rows.every((r) => state.selected.has(r.sl_no));
    const pickHead = canPick
      ? `<th class="mig-pickcol"><input type="checkbox" id="mig-pick-all"` +
        `${allPicked ? " checked" : ""}${state.meta.frozen ? " disabled" : ""}` +
        ` aria-label="Select every row shown" /></th>`
      : "";
    const head = columns
      .map(([field, label]) => {
        if (!field || !sortable.has(field)) return `<th>${esc(label)}</th>`;
        const active = state.sort.field === field;
        const arrow = active ? (state.sort.dir === "asc" ? "▲" : "▼") : "";
        return (
          `<th class="mig-sortable${active ? " is-sorted" : ""}" ` +
          `aria-sort="${active ? (state.sort.dir === "asc" ? "ascending" : "descending") : "none"}">` +
          `<button class="mig-sort" data-sort="${field}">${esc(label)}` +
          `<span class="mig-arrow">${arrow}</span></button></th>`
        );
      })
      .join("");

    // A freeze stops every change, so the register is shown as the read-only
    // thing it currently is rather than leaving controls that would be refused.
    const frozen = Boolean(state.meta.frozen);
    body.innerHTML =
      bulkBar(canPick) +
      `<div class="table-scroll${frozen ? " is-frozen" : ""}"` +
      (frozen ? ` title="Record entry is frozen"` : "") +
      `><table class="data-table mig-table">` +
      `<thead><tr>${pickHead}${head}</tr></thead><tbody>${rows}</tbody></table></div>`;

    if (canPick) wireSelection(body);

    for (const button of body.querySelectorAll(".mig-sort")) {
      button.addEventListener("click", () => {
        const field = button.dataset.sort;
        // Same column flips direction; a new one starts descending, which is
        // newest-first for dates and highest-first for SL#.
        state.sort =
          state.sort.field === field
            ? { field, dir: state.sort.dir === "asc" ? "desc" : "asc" }
            : { field, dir: "desc" };
        refresh();
      });
    }

    const amberRows = state.rows.filter((r) => r.amber.length).length;
    const retired = state.rows.filter((r) => !r.active).length;
    $("mig-scope").textContent =
      `${state.rows.length} request${state.rows.length === 1 ? "" : "s"}` +
      (amberRows ? ` · ${amberRows} carrying flagged changes` : "") +
      (retired ? ` · ${retired} inactive` : "") +
      ($("mig-f-inactive").checked ? "" : " · inactive records hidden");

    for (const button of body.querySelectorAll(".mig-open")) {
      button.addEventListener("click", () => openRecord(Number(button.dataset.sl)));
    }
  }

  const ARCHIVE = {
    button: "Archive %n",
    title: "Archive %n record%s",
    lead:
      "Archived records leave the register and can be read only from <strong>Archive</strong>. " +
      "<strong>This cannot be undone</strong> — an archived record can never be changed, " +
      "deactivated or deleted again.",
  };

  function bulkBar(canPick) {
    if (!canPick || !state.selected.size) return "";
    const picked = state.rows.filter((r) => state.selected.has(r.sl_no));
    const frozen = state.meta.frozen;

    const button = (key) => {
      const spec = key === "archive" ? ARCHIVE : BULK[key];
      const n = picked.filter((r) => eligible(r, key)).length;
      const off = frozen || n === 0;
      const why = frozen
        ? "Record entry is frozen."
        : n === 0
          ? key === "archive"
            ? "Only records already migrated to production can be archived."
            : "None of the selected records is at that stage."
          : "";
      return (
        `<button class="btn tiny ${key === "archive" ? "" : "primary"}" ` +
        `id="mig-bulk-${key}"${off ? " disabled" : ""}` +
        `${why ? ` title="${esc(why)}"` : ""}>${esc(spec.button.replace("%n", n))}</button>`
      );
    };

    return (
      `<div class="mig-bulkbar">` +
      `<span class="mig-bulkcount">${picked.length} selected</span>` +
      allowedActions().map(button).join("") +
      `<button class="btn tiny" id="mig-bulk-clear">Clear</button>` +
      `</div>`
    );
  }

  function wireSelection(body) {
    for (const box of body.querySelectorAll(".mig-pick")) {
      box.addEventListener("change", () => {
        const sl = Number(box.dataset.sl);
        if (box.checked) state.selected.add(sl);
        else state.selected.delete(sl);
        renderTable();
      });
    }
    $("mig-pick-all")?.addEventListener("change", () => {
      const all = $("mig-pick-all").checked;
      for (const row of state.rows) {
        if (all) state.selected.add(row.sl_no);
        else state.selected.delete(row.sl_no);
      }
      renderTable();
    });
    $("mig-bulk-clear")?.addEventListener("click", () => {
      state.selected.clear();
      renderTable();
    });
    for (const key of allowedActions()) {
      $(`mig-bulk-${key}`)?.addEventListener("click", () =>
        key === "archive" ? confirmArchive() : confirmBulk(key)
      );
    }
  }

  /* --------------------------------------------------------- bulk approve -- */

  function confirmBulk(key) {
    const spec = BULK[key];
    const extraLabel = fieldsByKey()[spec.extra]?.label || "Notes";
    const picked = state.rows.filter((r) => state.selected.has(r.sl_no));
    const going = picked.filter((r) => eligible(r, key));
    const staying = picked.filter((r) => !eligible(r, key));
    const already = going.filter((r) => r[spec.extra]).length;

    const list = (rows, muted) =>
      rows
        .map(
          (r) =>
            `<tr><td class="num">${r.sl_no}</td><td>${esc(r.microservice)}</td>` +
            `<td class="mono">${esc(r.repo_name)}</td>` +
            `<td class="${spec.kind === "date" ? "mig-date" : ""}">` +
            `${esc(spec.kind === "date" ? dmy(r[spec.extra]) : r[spec.extra]) || '<span class="muted">—</span>'}</td>` +
            `<td>${muted ? `<span class="muted">${esc(r.status_label)}</span>` : esc(r.status_label)}</td></tr>`
        )
        .join("");

    const table = (rows, muted) =>
      `<div class="table-scroll"><table class="data-table"><thead><tr>` +
      `<th>SL#</th><th>Micro service</th><th>Repo</th><th>${esc(extraLabel)}</th><th>Status</th>` +
      `</tr></thead><tbody>${list(rows, muted)}</tbody></table></div>`;

    const input =
      spec.kind === "date"
        ? `<input id="mig-bulk-extra" class="input" type="date" />`
        : `<textarea id="mig-bulk-extra" class="input" rows="2"></textarea>`;

    dialog(
      spec.title.replace("%n", `${going.length} record${going.length === 1 ? "" : "s"}`),
      `<p class="scope-line">${spec.lead}</p>` +
        (going.length
          ? `<div class="mig-grid mig-bulk-when"><div class="mig-field${spec.kind === "date" ? "" : " wide"}">` +
            `<label for="mig-bulk-extra">${esc(extraLabel)}</label>${input}` +
            `<p class="mig-hint">Applied to all ${going.length}. ` +
            (already
              ? `<strong>${already}</strong> already ${already === 1 ? "has one" : "have one"} — ` +
                `entering something here replaces ${already === 1 ? "it" : "them"}. `
              : "") +
            `Leave it blank to keep each record's own.</p></div></div>` + table(going, false)
          : `<p class="mig-note warn">None of the selected records is at that stage.</p>`) +
        (staying.length
          ? `<details class="mig-audit"><summary>${staying.length} will be left alone</summary>` +
            table(staying, true) + `</details>`
          : ""),
      `<button class="btn" id="mig-cancel">Cancel</button>` +
        (going.length
          ? `<button class="btn primary" id="mig-bulk-go">${esc(spec.button.replace("%n", going.length))}</button>`
          : "")
    );

    $("mig-cancel").addEventListener("click", close);
    $("mig-bulk-go")?.addEventListener("click", async () => {
      const go = $("mig-bulk-go");
      go.disabled = true;
      setError("");
      const value = $("mig-bulk-extra")?.value || "";
      try {
        const result = await api(`/api/migrations/records/${spec.endpoint}`, {
          method: "POST",
          body: JSON.stringify({
            sl_nos: going.map((r) => r.sl_no),
            stage: spec.stage,
            [spec.endpoint === "approve" ? "planned" : "remarks"]: value,
          }),
        });
        // Only clear what actually moved, so anything refused stays selected
        // and can be dealt with.
        for (const row of result.moved) state.selected.delete(row.sl_no);
        close();
        await refresh();
        reportBulk(result, spec);
      } catch (error) {
        setError(esc(error.message), error.fields);
        go.disabled = false;
      }
    });
  }

  function confirmArchive() {
    const picked = state.rows.filter((r) => state.selected.has(r.sl_no));
    const going = picked.filter((r) => eligible(r, "archive"));
    const staying = picked.filter((r) => !eligible(r, "archive"));

    const table = (rows, muted) =>
      `<div class="table-scroll"><table class="data-table"><thead><tr>` +
      `<th>SL#</th><th>Micro service</th><th>Repo</th><th>Prod migrated</th><th>Status</th>` +
      `</tr></thead><tbody>` +
      rows
        .map(
          (r) =>
            `<tr><td class="num">${r.sl_no}</td><td>${esc(r.microservice)}</td>` +
            `<td class="mono">${esc(r.repo_name)}</td>` +
            `<td class="mig-date">${esc(dmy(r.prod_migration_date)) || '<span class="muted">—</span>'}</td>` +
            `<td>${muted ? `<span class="muted">${esc(r.status_label)}</span>` : esc(r.status_label)}</td></tr>`
        )
        .join("") +
      `</tbody></table></div>`;

    dialog(
      ARCHIVE.title.replace("%n", going.length).replace("%s", going.length === 1 ? "" : "s"),
      `<p class="scope-line">${ARCHIVE.lead}</p>` +
        (going.length
          ? table(going, false)
          : `<p class="mig-note warn">None of the selected records has been migrated to ` +
            `production, so there is nothing to archive.</p>`) +
        (staying.length
          ? `<details class="mig-audit"><summary>${staying.length} will be left alone</summary>` +
            table(staying, true) + `</details>`
          : ""),
      `<button class="btn" id="mig-cancel">Cancel</button>` +
        (going.length
          ? `<button class="btn danger" id="mig-archive-go">Archive ${going.length} permanently</button>`
          : "")
    );

    $("mig-cancel").addEventListener("click", close);
    $("mig-archive-go")?.addEventListener("click", async () => {
      const go = $("mig-archive-go");
      go.disabled = true;
      setError("");
      try {
        const result = await api("/api/migrations/records/archive", {
          method: "POST",
          body: JSON.stringify({ sl_nos: going.map((r) => r.sl_no) }),
        });
        for (const row of result.archived) state.selected.delete(row.sl_no);
        close();
        await refresh();
        const done = result.archived.length;
        const left = result.skipped.length;
        $("mig-scope").textContent =
          `Archived ${done} record${done === 1 ? "" : "s"}` +
          (left ? ` · ${left} left alone` : "") +
          ". They are now under Archive.";
        if (left) {
          $("mig-notices").insertAdjacentHTML(
            "beforeend",
            note("warn",
              `<strong>${left} not archived.</strong> ` +
              result.skipped.map((s) => `SL# ${esc(s.sl_no)} — ${esc(s.reason)}`).join("; "))
          );
        }
      } catch (error) {
        setError(esc(error.message), error.fields);
        go.disabled = false;
      }
    });
  }

  function reportBulk(result, spec) {
    const done = result.moved.length;
    const left = result.skipped.length;
    const detail =
      result.extra && spec.kind === "date" ? `, planned for ${dmy(result.extra)}` : "";
    $("mig-scope").textContent =
      `Did “${result.verb}” on ${done} record${done === 1 ? "" : "s"}${detail}` +
      (left ? ` · ${left} left alone` : "") + ".";
    if (!left) return;
    $("mig-notices").insertAdjacentHTML(
      "beforeend",
      note("warn",
        `<strong>${left} record${left === 1 ? " was" : "s were"} left alone.</strong> ` +
        result.skipped.map((s) => `SL# ${esc(s.sl_no)} — ${esc(s.reason)}`).join("; "))
    );
  }

  /* -------------------------------------------------------------- dialog -- */

  /* A native <dialog>, as the tag flow already uses: it brings the backdrop,
   * the focus trap and Escape-to-close with it, none of which are worth
   * reimplementing by hand. */
  function dialog(title, bodyHtml, footerHtml) {
    close();
    const host = document.createElement("dialog");
    host.className = "dialog mig-dialog";
    host.id = "mig-overlay";
    host.innerHTML =
      `<header class="mig-dialog-head"><h2>${esc(title)}</h2>` +
      `<button class="mig-x" id="mig-close" aria-label="Close">\u00d7</button></header>` +
      `<div class="mig-dialog-body">${bodyHtml}</div>` +
      // The footer is sticky; the body scrolls. An error rendered inside the body
      // lands above the fold once the form is long enough to scroll, so pressing
      // the button looks like it did nothing at all.
      `<footer class="mig-dialog-foot">` +
      `<div id="mig-dialog-error" class="mig-error-slot" role="alert"></div>` +
      `<div class="mig-foot-actions">${footerHtml || ""}</div>` +
      `</footer>`;
    document.body.appendChild(host);
    $("mig-close").addEventListener("click", close);
    // Escape fires `cancel` on a native dialog; clean up the same way either way.
    host.addEventListener("close", () => {
      host.remove();
      state.open = null;
      state.dirty = {};
    });
    host.addEventListener("mousedown", (event) => {
      // Clicks land on the dialog itself only when they are on the backdrop.
      if (event.target === host) close();
    });
    if (host.showModal) host.showModal();
    else host.setAttribute("open", "");
    return host;
  }

  function close() {
    const existing = $("mig-overlay");
    if (!existing) return;
    if (existing.close) existing.close();
    else existing.remove();
  }

  function setError(html, fields) {
    const slot = $("mig-dialog-error");
    if (slot) slot.innerHTML = html ? `<div class="mig-note warn">${html}</div>` : "";
    markFields(fields);
  }

  /* Mark the offending controls. A sentence listing six labels is hard to act on
   * in a 28-field form; the marks say where to look. */
  function markFields(fields) {
    const dialog = $("mig-overlay");
    if (!dialog) return;
    for (const node of dialog.querySelectorAll(".mig-field.is-bad")) {
      node.classList.remove("is-bad");
      node.querySelector(".mig-field-error")?.remove();
    }
    if (!fields) return;

    let first = null;
    for (const [key, why] of Object.entries(fields)) {
      const node = dialog.querySelector(`.mig-field[data-field="${key}"]`);
      if (!node) continue;
      node.classList.add("is-bad");
      const message = document.createElement("p");
      message.className = "mig-field-error";
      message.textContent = why;
      node.appendChild(message);
      first = first || node;
    }
    first?.scrollIntoView?.({ block: "center", behavior: "smooth" });
  }

  /* --------------------------------------------------------- field input -- */

  function inputFor(field, value, values, editable) {
    const id = `mig-in-${field.key}`;
    const amber = field.amber_when && value === field.amber_when ? " is-amber" : "";

    if (!editable) {
      const shown = field.kind === "auto" && /date/.test(field.key) ? shortDate(value) : value;
      return `<div id="${id}" class="mig-readonly${amber}">${esc(shown) || '<span class="muted">—</span>'}</div>`;
    }

    if (field.kind === "yesno" || field.kind === "enum") {
      const list = optionsFor(field, values);
      const blank = field.kind === "yesno" ? "" : `<option value="">—</option>`;
      const chosen = list.includes(value) ? value : "";
      return (
        `<select id="${id}" class="select mig-input${amber}" data-key="${field.key}">${blank}` +
        list.map((v) => `<option value="${esc(v)}"${v === chosen ? " selected" : ""}>${esc(v)}</option>`).join("") +
        `</select>` +
        (!list.length && field.depends_on
          ? `<p class="mig-hint">Select a ${esc(fieldsByKey()[field.depends_on].label)} first.</p>`
          : "")
      );
    }
    if (field.kind === "longtext") {
      return `<textarea id="${id}" class="input mig-input" rows="3" data-key="${field.key}">${esc(value)}</textarea>`;
    }
    if (field.kind === "date") {
      return `<input id="${id}" class="input mig-input" type="date" value="${esc(value)}" data-key="${field.key}" />`;
    }
    return `<input id="${id}" class="input mig-input" type="text" spellcheck="false" value="${esc(value)}" data-key="${field.key}" />`;
  }

  function fieldBlock(field, value, values, editable, autofilled = false) {
    const required =
      field.required || (field.required_if && values[field.required_if[0]] === field.required_if[1]);
    return (
      `<div class="mig-field${field.kind === "longtext" ? " wide" : ""}" data-field="${field.key}">` +
      `<label for="mig-in-${field.key}">${esc(field.label)}` +
      (required ? `<span class="mig-req" title="Required">*</span>` : "") +
      (field.amber_when ? `<span class="mig-flagmark" title="Marked amber when ${esc(field.amber_when)}">▲</span>` : "") +
      `</label>` +
      inputFor(field, value, values, editable) +
      // Say so rather than filling it in silently: the value is still the
      // person's to change, they just did not have to pick from a list of one.
      (autofilled && editable
        ? `<p class="mig-hint mig-autofilled">${
            field.key === "change_requestor"
              ? "That is you — change it if you are raising this for someone else."
              : "Only one — filled in for you."
          }</p>`
        : field.help && editable
          ? `<p class="mig-hint">${esc(field.help)}</p>`
          : "") +
      `</div>`
    );
  }

  // Supplied by the server so the headings cannot drift from the field spec.
  const stageTitles = () =>
    (state.meta.stages || []).reduce((map, s) => ((map[s.key] = s.label), map), {});

  /* --------------------------------------------------------- new request -- */

  function newRequest() {
    const specs = state.meta.fields.filter((f) => state.meta.request_fields.includes(f.key));
    const values = {};
    for (const field of specs) values[field.key] = field.default || "";
    let autofilled = [];

    // Most requests are raised by the person filling the form in, so start
    // there — and say so, since it is still theirs to change.
    const me = state.meta.me_employee || "";
    if (me && (state.meta.options.change_requestors || []).includes(me)) {
      values.change_requestor = me;
      autofilled.push("change_requestor");
    }

    const render = () =>
      `<div class="mig-grid">` +
      specs.map((f) => fieldBlock(f, values[f.key], values, true, autofilled.includes(f.key))).join("") +
      `</div>`;

    dialog(
      "New migration request",
      render(),
      `<button class="btn" id="mig-cancel">Cancel</button>` +
      `<button class="btn primary" id="mig-save">Raise request</button>`
    );

    const body = document.querySelector(".mig-dialog-body");

    function rewire() {
      for (const input of body.querySelectorAll(".mig-input")) {
        input.addEventListener("change", () => {
          values[input.dataset.key] = input.value;
          // The dependent lists become wrong the moment the service changes.
          if (input.dataset.key === "microservice") {
            const keptRequestor = autofilled.includes("change_requestor")
              && values.change_requestor === me;
            autofilled = settleDependents(values);
            if (keptRequestor) autofilled.push("change_requestor");
            body.innerHTML = render();
            rewire();
          } else if (input.dataset.key === "code_image_change" || input.dataset.key === "ddl_dml") {
            body.innerHTML = render(); // requiredness and the amber tint change
            rewire();
          } else {
            input.classList.toggle(
              "is-amber",
              Boolean(specs.find((f) => f.key === input.dataset.key)?.amber_when) &&
                input.value === specs.find((f) => f.key === input.dataset.key).amber_when
            );
          }
        });
      }
    }
    rewire();

    $("mig-cancel").addEventListener("click", close);
    $("mig-save").addEventListener("click", async () => {
      const button = $("mig-save");
      button.disabled = true;
      setError("");
      try {
        await api("/api/migrations/records", { method: "POST", body: JSON.stringify(values) });
        close();
        await refresh();
      } catch (error) {
        setError(esc(error.message), error.fields);
        button.disabled = false;
      }
    });
  }

  /* -------------------------------------------------------- record detail -- */

  async function openRecord(slNo) {
    const row = state.rows.find((r) => r.sl_no === slNo);
    if (!row) return;
    state.open = slNo;
    state.dirty = {};

    const editable = new Set(row.editable);
    const values = {};
    for (const field of state.meta.fields) values[field.key] = row[field.key] || "";

    const stages = {};
    for (const field of state.meta.fields) (stages[field.stage] ||= []).push(field);
    let autofilled = [];

    const renderSections = () =>
      Object.entries(stageTitles())
        .map(([stage, title]) => {
          const fields = stages[stage] || [];
          const anyEditable = fields.some((f) => editable.has(f.key));
          return (
            `<section class="mig-stage${anyEditable ? " is-open" : ""}">` +
            `<h3>${esc(title)}${anyEditable ? `<span class="mig-yours">you can edit this</span>` : ""}</h3>` +
            `<div class="mig-grid">` +
            fields
              .map((f) =>
                fieldBlock(f, values[f.key], values, editable.has(f.key), autofilled.includes(f.key))
              )
              .join("") +
            `</div></section>`
          );
        })
        .join("");

    const sections = renderSections();

    // Retiring and deleting sit on the left, away from Save, so neither is the
    // button your hand is already heading for. Both are changes, so a freeze
    // removes them as it removes everything else.
    const retire = state.canRetire && !state.meta.frozen
      ? `<div class="mig-foot-left">` +
        `<button class="btn tiny" id="mig-retire">${row.active ? "Deactivate" : "Reactivate"}</button>` +
        `<button class="btn tiny danger" id="mig-delete">Delete…</button>` +
        `</div>`
      : "";

    dialog(
      `SL# ${slNo} · ${row.microservice} · ${row.status_label}${row.active ? "" : " · inactive"}`,
      (state.meta.frozen
        ? note("freeze",
            "<strong>Record entry is frozen.</strong> Nothing on this record can be " +
            "changed — by anyone — until the freeze lifts. An approver can lift it under " +
            "<em>Frozen — manage…</em>.")
        : "") +
      (row.active
        ? ""
        : note("warn",
            "This record is <strong>inactive</strong>. It is read-only and hidden from the " +
            "register unless “Show inactive” is ticked. Reactivate it to make changes.")) +
        `${sections}` +
        `<details class="mig-audit"><summary>History</summary><div id="mig-audit-body">Loading…</div></details>`,
      retire +
        `<button class="btn" id="mig-cancel">Close</button>` +
        (editable.size ? `<button class="btn primary" id="mig-save" disabled>Save changes</button>` : "")
    );

    const body = document.querySelector(".mig-dialog-body");
    const sectionHost = body;

    function wireFields() {
      for (const input of body.querySelectorAll(".mig-input")) {
        input.addEventListener("change", () => {
          const key = input.dataset.key;
          values[key] = input.value;
          state.dirty[key] = input.value;
          const save = $("mig-save");
          if (save) save.disabled = false;

          // Changing the microservice invalidates the repo and track lead. This
          // used to leave the old service's options on screen, which the server
          // then refused as a mismatched pairing.
          if (key === "microservice") {
            autofilled = settleDependents(values);
            for (const dependent of state.meta.fields.filter((f) => f.depends_on)) {
              state.dirty[dependent.key] = values[dependent.key];
            }
            redraw();
          }
        });
      }
    }

    function redraw() {
      const audit = sectionHost.querySelector(".mig-audit");
      sectionHost.innerHTML =
        renderSections() +
        `<details class="mig-audit"><summary>History</summary>` +
        `<div id="mig-audit-body">${audit ? "Loading…" : "Loading…"}</div></details>`;
      wireFields();
      wireAudit();
    }

    wireFields();

    $("mig-cancel").addEventListener("click", close);
    const save = $("mig-save");
    if (save) {
      save.addEventListener("click", async () => {
        save.disabled = true;
        setError("");
        try {
          await api(`/api/migrations/records/${slNo}`, {
            method: "PATCH",
            body: JSON.stringify(state.dirty),
          });
          close();
          await refresh();
        } catch (error) {
          setError(esc(error.message), error.fields);
          save.disabled = false;
        }
      });
    }

    $("mig-retire")?.addEventListener("click", async () => {
      const button = $("mig-retire");
      button.disabled = true;
      setError("");
      try {
        await api(`/api/migrations/records/${slNo}/active`, {
          method: "POST",
          body: JSON.stringify({ active: !row.active }),
        });
        close();
        await refresh();
      } catch (error) {
        setError(esc(error.message), error.fields);
        button.disabled = false;
      }
    });

    // Deleting is irreversible, so it asks in place rather than acting on the
    // first click — and says plainly what the reversible alternative is.
    $("mig-delete")?.addEventListener("click", () => {
      const actions = document.querySelector(".mig-foot-actions");
      if (!actions) return;
      actions.innerHTML =
        `<span class="mig-confirm">Delete SL# ${slNo} and its whole history? ` +
        `This cannot be undone — <strong>Deactivate</strong> is the reversible option.</span>` +
        `<button class="btn" id="mig-del-no">Cancel</button>` +
        `<button class="btn danger" id="mig-del-yes">Delete permanently</button>`;

      $("mig-del-no").addEventListener("click", () => openRecord(slNo));
      $("mig-del-yes").addEventListener("click", async () => {
        $("mig-del-yes").disabled = true;
        setError("");
        try {
          await api(`/api/migrations/records/${slNo}`, { method: "DELETE" });
          close();
          await refresh();
        } catch (error) {
          setError(esc(error.message), error.fields);
          $("mig-del-yes").disabled = false;
        }
      });
    });

    function wireAudit() {
    body.querySelector(".mig-audit").addEventListener(
      "toggle",
      async (event) => {
        if (!event.target.open) return;
        try {
          const data = await api(`/api/migrations/records/${slNo}/audit`);
          $("mig-audit-body").innerHTML = data.entries.length
            ? `<table class="data-table mig-audit-table"><thead><tr><th>When</th><th>Who</th><th>Field</th><th>From</th><th>To</th></tr></thead><tbody>` +
              data.entries
                .map(
                  (e) =>
                    `<tr><td>${esc(shortDate(e.at))}</td><td>${esc(e.who)}</td><td>${esc(e.label)}</td>` +
                    `<td class="muted">${esc(e.old_value) || "—"}</td><td>${esc(e.new_value) || "—"}</td></tr>`
                )
                .join("") +
              `</tbody></table>`
            : `<p class="muted">Nothing recorded yet.</p>`;
        } catch (error) {
          $("mig-audit-body").innerHTML = `<p class="mig-note warn">${esc(error.message)}</p>`;
        }
      },
      { once: true }
    );
    }

    wireAudit();
  }

  /* -------------------------------------------------------------- upload -- */

  function uploadSheet() {
    dialog(
      "Upload migration requests",
      `<p class="scope-line">Only the fields a developer fills in can be uploaded — approvals, ` +
        `QA and production results are recorded in the register, not in a spreadsheet. ` +
        `<a href="/api/migrations/template">Download the template</a>, which carries the permitted ` +
        `values as dropdowns.</p>` +
        `<label class="field block"><span>Spreadsheet</span>` +
        `<input id="mig-file" class="input" type="file" accept=".xlsx,.xlsm,.csv,.tsv" /></label>` +
        `<div id="mig-preview"></div>`,
      `<button class="btn" id="mig-cancel">Cancel</button>` +
        `<button class="btn" id="mig-check">Check the sheet</button>` +
        `<button class="btn primary" id="mig-import" disabled>Import valid rows</button>`
    );

    $("mig-cancel").addEventListener("click", close);

    async function send(commit) {
      const file = $("mig-file").files[0];
      if (!file) {
        setError("Choose a file first.");
        return null;
      }
      const form = new FormData();
      form.append("file", file);
      form.append("commit", commit ? "true" : "false");
      return api("/api/migrations/upload", { method: "POST", body: form });
    }

    $("mig-check").addEventListener("click", async () => {
      setError("");
      $("mig-preview").innerHTML = "<p class='muted'>Checking…</p>";
      try {
        const data = await send(false);
        if (!data) return ($("mig-preview").innerHTML = "");
        renderPreview(data);
        $("mig-import").disabled = data.valid === 0;
      } catch (error) {
        $("mig-preview").innerHTML = "";
        setError(esc(error.message));
      }
    });

    $("mig-import").addEventListener("click", async () => {
      setError("");
      $("mig-import").disabled = true;
      try {
        const data = await send(true);
        if (!data) return;
        close();
        await refresh();
        $("mig-scope").textContent =
          `Imported ${data.created.length} request${data.created.length === 1 ? "" : "s"}.`;
      } catch (error) {
        setError(esc(error.message));
        $("mig-import").disabled = false;
      }
    });
  }

  function renderPreview(data) {
    const rows = data.results
      .map((result) => {
        const problems = Object.entries(result.problems)
          .map(([label, why]) => `<li><strong>${esc(label)}</strong> — ${esc(why)}</li>`)
          .join("");
        return (
          `<tr class="${result.ok ? "" : "is-bad"}">` +
          `<td class="num">${esc(result.row)}</td>` +
          `<td>${esc(result.values.microservice || "")}</td>` +
          `<td class="mono">${esc(result.values.repo_name || "")}</td>` +
          `<td>${esc(result.values.release || "")}</td>` +
          `<td>${result.ok ? `<span class="mig-status ok">Ready</span>` : `<ul class="mig-problems">${problems}</ul>`}</td>` +
          `</tr>`
        );
      })
      .join("");

    $("mig-preview").innerHTML =
      `<p class="scope-line">Header on row ${data.sheet.header_row} · ${data.total} row${data.total === 1 ? "" : "s"} · ` +
      `<strong>${data.valid} ready</strong>` +
      (data.invalid ? ` · ${data.invalid} with problems` : "") +
      (data.sheet.ignored ? ` · ${data.sheet.ignored} blank skipped` : "") +
      `</p>` +
      `<div class="table-scroll"><table class="data-table"><thead><tr>` +
      `<th>Sheet row</th><th>Micro service</th><th>Repo</th><th>Rel#</th><th>Result</th>` +
      `</tr></thead><tbody>${rows}</tbody></table></div>` +
      (data.invalid ? `<p class="mig-hint">Only the ready rows are imported; fix the rest and upload again.</p>` : "");
  }

  /* -------------------------------------------------------------- freezes -- */

  async function manageFreezes() {
    const canManage = state.meta.user.roles.includes("approver");
    dialog(
      "Freeze record entry",
      `<p class="scope-line">While a freeze is active nobody can create or edit a migration ` +
        `request — including approvers and DevOps. Approvers can still lift a freeze.</p>` +
        (canManage
          ? `<div class="mig-grid mig-freeze-form">` +
            `<div class="mig-field"><label for="mig-fz-from">From</label>` +
            `<input id="mig-fz-from" class="input" type="datetime-local" /></div>` +
            `<div class="mig-field"><label for="mig-fz-to">To</label>` +
            `<input id="mig-fz-to" class="input" type="datetime-local" /></div>` +
            `<div class="mig-field wide"><label for="mig-fz-why">Reason</label>` +
            `<input id="mig-fz-why" class="input" type="text" placeholder="Release window" /></div>` +
            `</div>`
          : `<p class="mig-note warn">Only approvers can set or lift a freeze.</p>`) +
        `<div id="mig-freeze-list">Loading…</div>`,
      `<button class="btn" id="mig-cancel">Close</button>` +
        (canManage ? `<button class="btn primary" id="mig-fz-add">Add freeze</button>` : "")
    );
    $("mig-cancel").addEventListener("click", close);

    async function list() {
      try {
        const data = await api("/api/migrations/freezes?include_past=true");
        const rows = data.freezes
          .map(
            (f) =>
              `<tr class="${f.active ? "is-active" : f.past ? "is-past" : ""}">` +
              `<td>${esc(shortDate(f.starts_at))}</td><td>${esc(shortDate(f.ends_at))}</td>` +
              `<td>${esc(f.reason) || '<span class="muted">—</span>'}</td>` +
              `<td>${esc(f.created_by)}</td>` +
              `<td>${f.active ? `<span class="mig-status wait">Active</span>` : f.past ? `<span class="muted">Finished</span>` : `<span class="muted">Scheduled</span>`}</td>` +
              `<td class="num">${data.can_manage && !f.past ? `<button class="btn tiny mig-fz-del" data-id="${f.id}">Lift</button>` : ""}</td>` +
              `</tr>`
          )
          .join("");
        $("mig-freeze-list").innerHTML = data.freezes.length
          ? `<div class="table-scroll"><table class="data-table"><thead><tr>` +
            `<th>From</th><th>To</th><th>Reason</th><th>Set by</th><th>State</th><th></th>` +
            `</tr></thead><tbody>${rows}</tbody></table></div>`
          : `<p class="muted">No freeze windows.</p>`;

        for (const button of $("mig-freeze-list").querySelectorAll(".mig-fz-del")) {
          button.addEventListener("click", async () => {
            button.disabled = true;
            try {
              await api(`/api/migrations/freezes/${button.dataset.id}`, { method: "DELETE" });
              await list();
              await refreshMetaOnly();
            } catch (error) {
              setError(esc(error.message));
              button.disabled = false;
            }
          });
        }
      } catch (error) {
        $("mig-freeze-list").innerHTML = `<p class="mig-note warn">${esc(error.message)}</p>`;
      }
    }
    await list();

    const add = $("mig-fz-add");
    if (add) {
      add.addEventListener("click", async () => {
        setError("");
        const from = $("mig-fz-from").value;
        const to = $("mig-fz-to").value;
        if (!from || !to) return setError("Give both a start and an end.");
        // datetime-local is wall-clock with no zone; converting here makes the
        // instant explicit rather than leaving the server to guess a timezone.
        const starts = new Date(from).getTime() / 1000;
        const ends = new Date(to).getTime() / 1000;
        add.disabled = true;
        try {
          await api("/api/migrations/freezes", {
            method: "POST",
            body: JSON.stringify({
              starts_epoch: starts,
              ends_epoch: ends,
              reason: $("mig-fz-why").value,
            }),
          });
          await list();
          await refreshMetaOnly();
        } catch (error) {
          setError(esc(error.message));
        }
        add.disabled = false;
      });
    }
  }

  /* ----------------------------------------------------- reference lists -- */

  // Change Requestor is not here: it comes from the employee list below.
  const LIST_TITLES = {
    releases: "Rel#",
    migration_paths: "Migration paths",
  };

  async function manageLists() {
    dialog("Reference lists", `<p class="muted">Loading…</p>`,
           `<button class="btn" id="mig-cancel">Close</button>`);
    $("mig-cancel").addEventListener("click", close);

    let data;
    try {
      data = await api("/api/migrations/lists");
    } catch (error) {
      document.querySelector(".mig-dialog-body").innerHTML =
        `<div class="mig-note warn">${esc(error.message)}</div>`;
      return;
    }
    render(data);

    function render(data) {
      const editable = data.can_edit;
      const simple = Object.entries(LIST_TITLES)
        .map(([key, title]) => {
          const values = data[key] || [];
          return (
            `<div class="mig-field wide" data-list="${key}">` +
            `<label for="mig-list-${key}">${esc(title)}` +
            `<span class="mig-roles">${values.length}</span></label>` +
            (editable
              ? `<textarea id="mig-list-${key}" class="input" rows="${Math.min(8, Math.max(3, values.length + 1))}"` +
                ` spellcheck="false">${esc(values.join("\n"))}</textarea>` +
                `<p class="mig-hint">One per line, in the order they should appear. ` +
                `<button class="linkish mig-list-save" data-list="${key}">Save ${esc(title)}</button></p>`
              : `<div class="mig-readonly">${esc(values.join(", ")) || '<span class="muted">—</span>'}</div>`) +
            (values.length === 0
              ? `<p class="mig-field-error">Empty — no one can pick a value for this.</p>`
              : "") +
            `</div>`
          );
        })
        .join("");

      const services = (data.microservices || [])
        .map(
          (s) =>
            `<tr><td>${esc(s.name)}</td>` +
            `<td class="mono">${esc(s.repos.join(", ")) || '<span class="muted">—</span>'}</td>` +
            `<td>${esc(s.track_leads.join(", ")) || '<span class="muted">—</span>'}</td>` +
            (editable
              ? `<td class="num"><button class="btn tiny mig-svc-edit" data-name="${esc(s.name)}">Edit</button>` +
                ` <button class="btn tiny danger mig-svc-del" data-name="${esc(s.name)}">Delete</button></td>`
              : "<td></td>") +
            `</tr>`
        )
        .join("");

      const staff = (data.employees || [])
        .map(
          (e) =>
            `<tr><td class="mono">${esc(e.number)}</td><td>${esc(e.name)}</td>` +
            `<td>${esc(e.email) || '<span class="muted">—</span>'}</td>` +
            (editable
              ? `<td class="num"><button class="btn tiny mig-emp-edit" data-num="${esc(e.number)}">Edit</button>` +
                ` <button class="btn tiny danger mig-emp-del" data-num="${esc(e.number)}">Delete</button></td>`
              : "<td></td>") +
            `</tr>`
        )
        .join("");

      const employeeSection =
        `<section class="mig-stage is-open"><h3>Employees` +
        `<span class="mig-yours">the Change Requestor list</span></h3>` +
        `<p class="mig-hint">The email is how someone raising a request is matched to ` +
        `their own entry, so the form can fill Change Requestor in for them.</p>` +
        `<div class="table-scroll"><table class="data-table"><thead><tr>` +
        `<th>Number</th><th>Name</th><th>Email</th><th></th></tr></thead>` +
        `<tbody>${staff || '<tr><td colspan="4" class="muted">None yet — Change Requestor will have nothing to offer.</td></tr>'}</tbody>` +
        `</table></div>` +
        (editable ? `<p class="mig-hint"><button class="linkish" id="mig-emp-add">Add an employee</button></p>` : "") +
        `</section>`;

      document.querySelector(".mig-dialog-body").innerHTML =
        `<p class="scope-line">These drive the dropdowns on every migration request. ` +
        `Stored in <strong>${esc(data.storage)}</strong>` +
        (editable ? "." : " — you can read them, but changing them needs the approver role.") +
        `</p>` +
        `<div class="mig-grid">${simple}</div>` +
        `<section class="mig-stage is-open"><h3>Micro services` +
        (editable ? `<span class="mig-yours">repo and track lead per service</span>` : "") +
        `</h3>` +
        `<div class="table-scroll"><table class="data-table"><thead><tr>` +
        `<th>Name</th><th>Repos</th><th>Track leads</th><th></th></tr></thead>` +
        `<tbody>${services || '<tr><td colspan="4" class="muted">None yet.</td></tr>'}</tbody>` +
        `</table></div>` +
        (editable ? `<p class="mig-hint"><button class="linkish" id="mig-svc-add">Add a micro service</button></p>` : "") +
        `</section>` +
        employeeSection;
      wire(data);
    }

    function wire(data) {
      const body = document.querySelector(".mig-dialog-body");

      for (const button of body.querySelectorAll(".mig-list-save")) {
        button.addEventListener("click", async () => {
          const key = button.dataset.list;
          const values = ($(`mig-list-${key}`).value || "").split("\n");
          await save(() =>
            api(`/api/migrations/lists/${key}`, {
              method: "PUT",
              body: JSON.stringify({ values }),
            })
          );
        });
      }

      $("mig-emp-add")?.addEventListener("click", () => editEmployee(null, data));
      for (const button of body.querySelectorAll(".mig-emp-edit")) {
        button.addEventListener("click", () =>
          editEmployee(data.employees.find((e) => e.number === button.dataset.num), data)
        );
      }
      for (const button of body.querySelectorAll(".mig-emp-del")) {
        button.addEventListener("click", async () => {
          const number = button.dataset.num;
          if (!window.confirm?.(`Remove employee ${number}?`)) return;
          await save(() =>
            api(`/api/migrations/lists/employees/${encodeURIComponent(number)}`, {
              method: "DELETE",
            })
          );
        });
      }

      $("mig-svc-add")?.addEventListener("click", () => editService(null, data));
      for (const button of body.querySelectorAll(".mig-svc-edit")) {
        button.addEventListener("click", () =>
          editService(data.microservices.find((s) => s.name === button.dataset.name), data)
        );
      }
      for (const button of body.querySelectorAll(".mig-svc-del")) {
        button.addEventListener("click", async () => {
          const name = button.dataset.name;
          if (!window.confirm?.(`Remove “${name}” from the list?`)) return;
          await save(() =>
            api(`/api/migrations/lists/microservices/${encodeURIComponent(name)}`, {
              method: "DELETE",
            })
          );
        });
      }
    }

    async function save(call) {
      setError("");
      try {
        render(Object.assign(await call(), { can_edit: true, storage: data.storage }));
        // The form's dropdowns come from these, so the register behind reloads.
        await refresh();
      } catch (error) {
        setError(esc(error.message), error.fields);
      }
    }

    function editEmployee(employee, data) {
      const body = document.querySelector(".mig-dialog-body");
      const keep = body.innerHTML;
      body.innerHTML =
        `<div class="mig-grid">` +
        `<div class="mig-field"><label for="mig-emp-num">Employee number</label>` +
        `<input id="mig-emp-num" class="input" type="text" value="${esc(employee?.number || "")}" /></div>` +
        `<div class="mig-field"><label for="mig-emp-name">Name</label>` +
        `<input id="mig-emp-name" class="input" type="text" value="${esc(employee?.name || "")}" /></div>` +
        `<div class="mig-field wide"><label for="mig-emp-mail">Email</label>` +
        `<input id="mig-emp-mail" class="input" type="email" spellcheck="false" value="${esc(employee?.email || "")}" />` +
        `<p class="mig-hint">Optional, but without it this person's requests will not ` +
        `default to them.</p></div>` +
        `<div class="mig-field wide"><button class="btn primary" id="mig-emp-save">` +
        `${employee ? "Save" : "Add"} employee</button> ` +
        `<button class="btn" id="mig-emp-back">Back</button></div>` +
        `</div>`;

      $("mig-emp-back").addEventListener("click", () => {
        body.innerHTML = keep;
        wire(data);
      });
      $("mig-emp-save").addEventListener("click", async () => {
        await save(() =>
          api(`/api/migrations/lists/employees/${encodeURIComponent(employee?.number || "new")}`, {
            method: "PUT",
            body: JSON.stringify({
              number: $("mig-emp-num").value,
              name: $("mig-emp-name").value,
              email: $("mig-emp-mail").value,
            }),
          })
        );
      });
    }

    function editService(service, data) {
      const body = document.querySelector(".mig-dialog-body");
      const keep = body.innerHTML;
      body.innerHTML =
        `<div class="mig-grid">` +
        `<div class="mig-field"><label for="mig-svc-name">Micro service name</label>` +
        `<input id="mig-svc-name" class="input" type="text" value="${esc(service?.name || "")}" /></div>` +
        `<div class="mig-field wide"><label for="mig-svc-repos">Repos</label>` +
        `<textarea id="mig-svc-repos" class="input" rows="3" spellcheck="false">${esc((service?.repos || []).join("\n"))}</textarea>` +
        `<p class="mig-hint">One per line. These are what Repo Name offers for this service.</p></div>` +
        `<div class="mig-field wide"><label for="mig-svc-leads">Track leads</label>` +
        `<textarea id="mig-svc-leads" class="input" rows="3">${esc((service?.track_leads || []).join("\n"))}</textarea>` +
        `<p class="mig-hint">One per line.</p></div>` +
        `<div class="mig-field wide"><button class="btn primary" id="mig-svc-save">` +
        `${service ? "Save" : "Add"} micro service</button> ` +
        `<button class="btn" id="mig-svc-back">Back</button></div>` +
        `</div>`;

      $("mig-svc-back").addEventListener("click", () => {
        body.innerHTML = keep;
        wire(data);
      });
      $("mig-svc-save").addEventListener("click", async () => {
        const lines = (id) => ($(id).value || "").split("\n");
        await save(() =>
          api(
            `/api/migrations/lists/microservices/${encodeURIComponent(service?.name || "new")}`,
            {
              method: "PUT",
              body: JSON.stringify({
                name: $("mig-svc-name").value,
                repos: lines("mig-svc-repos"),
                track_leads: lines("mig-svc-leads"),
              }),
            }
          )
        );
      });
    }
  }

  /* --------------------------------------------------------------- load --- */

  async function refreshMetaOnly() {
    state.meta = await api("/api/migrations/meta");
    renderNotices();
    applyPermissions();
  }

  function applyPermissions() {
    const { frozen } = state.meta;
    // Decided by the server. Working it out from the role list here meant
    // reimplementing the rules, and getting admin wrong.
    const can = state.meta.permissions || {};
    const allowed = Boolean(can.can_raise) && !frozen;
    const why = frozen ? "Record entry is frozen." : can.why_not_raise || "";

    for (const id of ["mig-new", "mig-upload"]) {
      const button = $(id);
      if (!button) continue;
      button.disabled = !allowed;
      button.title = allowed ? "" : why;
    }
    const freeze = $("mig-freeze");
    if (freeze) freeze.textContent = state.meta.frozen ? "Frozen — manage…" : "Freeze…";

    // In the archive there is nothing to create, upload or freeze against.
    const viewing = state.archiveView;
    const archiveButton = $("mig-archive-view");
    if (archiveButton) {
      archiveButton.textContent = viewing ? "Back to the register" : "Archive";
      archiveButton.classList.toggle("is-active", viewing);
    }
    for (const id of ["mig-new", "mig-upload"]) {
      const button = $(id);
      if (button && viewing) {
        button.disabled = true;
        button.title = "Not available while viewing the archive.";
      }
    }
  }

  /* Meta is re-read alongside the records, not just once when the tab opens.
   * A freeze can start, or a role can change, while someone sits on this page;
   * without this they would keep being offered buttons the server now refuses. */
  async function refresh() {
    if (state.loading) return;
    state.loading = true;
    try {
      const [meta, data] = await Promise.all([
        api("/api/migrations/meta"),
        api(`/api/migrations/records?${filterQuery()}`),
      ]);
      state.meta = meta;
      state.meta.sortable = data.sortable || [];
      state.canRetire = Boolean(data.can_retire);
      if (data.sort) state.sort = { field: data.sort.field, dir: data.sort.dir };
      state.rows = data.rows;
      renderNotices();
      applyPermissions();
      renderTable();
    } catch (error) {
      $("mig-body").innerHTML = `<div class="mig-note warn">${esc(error.message)}</div>`;
      $("mig-scope").textContent = "";
    } finally {
      state.loading = false;
    }
  }

  let wired = false;

  async function initMigrationsTab() {
    if (!wired) {
      wired = true;
      const bind = (id, handler) => $(id)?.addEventListener("click", handler);
      bind("mig-new", newRequest);
      bind("mig-upload", uploadSheet);
      bind("mig-freeze", manageFreezes);
      bind("mig-lists", manageLists);
      bind("mig-archive-view", async () => {
        state.archiveView = !state.archiveView;
        state.selected.clear();
        await refresh();
      });
      bind("mig-csv", () => {
        window.location.href = `/api/migrations/export.csv?${filterQuery()}`;
      });
      for (const id of ["mig-f-release", "mig-f-service", "mig-f-path", "mig-f-status",
                        "mig-f-mine", "mig-f-from", "mig-f-to", "mig-f-inactive"]) {
        $(id)?.addEventListener("change", refresh);
      }
    }

    if (!state.meta) {
      try {
        state.meta = await api("/api/migrations/meta");
      } catch (error) {
        $("mig-body").innerHTML =
          `<div class="mig-note warn">${esc(error.message)}</div>`;
        return;
      }
      // Built once: rebuilding them under someone mid-selection would be rude.
      fillFilters();
    }
    await refresh();
  }

  window.initMigrationsTab = initMigrationsTab;
})();
