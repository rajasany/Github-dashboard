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
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return iso;
    const day = String(date.getDate()).padStart(2, "0");
    const year = String(date.getFullYear()).slice(-2);
    return `${day}-${MONTHS[date.getMonth()]}-${year}`;
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
      parts.push(note("warn", `No role is mapped to ${esc(user.email)}, so this register is read-only for you.`));
    }

    if (user.insecure && user.signed_in && !dev.enabled) {
      parts.push(
        note("warn",
          "The identity header is being accepted from <strong>any</strong> address. Set <code>auth.trusted_proxies</code>, " +
          "or anyone who can reach this app directly can claim to be anybody.")
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

    params.set("sort", state.sort.field);
    params.set("dir", state.sort.dir);
    return params;
  }

  /* --------------------------------------------------------------- table -- */

  const STATUS_CLASS = {
    submitted: "wait", approved: "ok", in_qa: "ok",
    ready_for_prod: "ok", in_prod: "done",
  };

  function renderTable() {
    const body = $("mig-body");
    if (!state.rows.length) {
      body.innerHTML =
        `<div class="empty"><p>No migration requests match these filters.</p></div>`;
      $("mig-scope").textContent = "";
      return;
    }

    const specs = fieldsByKey();
    const rows = state.rows
      .map((row) => {
        const amber = row.amber
          .map((key) => `<span class="mig-amber">${esc(specs[key].label.replace(/\?$/, ""))}</span>`)
          .join("");
        return (
          `<tr data-sl="${row.sl_no}"${row.active ? "" : ' class="is-inactive"'}>` +
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

    body.innerHTML =
      `<div class="table-scroll"><table class="data-table mig-table">` +
      `<thead><tr>${head}</tr></thead><tbody>${rows}</tbody></table></div>`;

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

  function fieldBlock(field, value, values, editable) {
    const required =
      field.required || (field.required_if && values[field.required_if[0]] === field.required_if[1]);
    return (
      `<div class="mig-field${field.kind === "longtext" ? " wide" : ""}" data-field="${field.key}">` +
      `<label for="mig-in-${field.key}">${esc(field.label)}` +
      (required ? `<span class="mig-req" title="Required">*</span>` : "") +
      (field.amber_when ? `<span class="mig-flagmark" title="Marked amber when ${esc(field.amber_when)}">▲</span>` : "") +
      `</label>` +
      inputFor(field, value, values, editable) +
      (field.help && editable ? `<p class="mig-hint">${esc(field.help)}</p>` : "") +
      `</div>`
    );
  }

  const STAGE_TITLES = {
    request: "Request",
    approval: "Approval",
    qa: "QA migration",
    prod_gate: "Ready for production",
    prod: "Production migration",
  };

  /* --------------------------------------------------------- new request -- */

  function newRequest() {
    const specs = state.meta.fields.filter((f) => state.meta.request_fields.includes(f.key));
    const values = {};
    for (const field of specs) values[field.key] = field.default || "";

    const render = () =>
      `<div class="mig-grid">${specs.map((f) => fieldBlock(f, values[f.key], values, true)).join("")}</div>`;

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
            values.repo_name = "";
            values.track_lead = "";
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

    const sections = Object.entries(STAGE_TITLES)
      .map(([stage, title]) => {
        const fields = stages[stage] || [];
        const anyEditable = fields.some((f) => editable.has(f.key));
        return (
          `<section class="mig-stage${anyEditable ? " is-open" : ""}">` +
          `<h3>${esc(title)}${anyEditable ? `<span class="mig-yours">you can edit this</span>` : ""}</h3>` +
          `<div class="mig-grid">` +
          fields.map((f) => fieldBlock(f, values[f.key], values, editable.has(f.key))).join("") +
          `</div></section>`
        );
      })
      .join("");

    // Retiring and deleting sit on the left, away from Save, so neither is the
    // button your hand is already heading for.
    const retire = state.canRetire
      ? `<div class="mig-foot-left">` +
        `<button class="btn tiny" id="mig-retire">${row.active ? "Deactivate" : "Reactivate"}</button>` +
        `<button class="btn tiny danger" id="mig-delete">Delete…</button>` +
        `</div>`
      : "";

    dialog(
      `SL# ${slNo} · ${row.microservice} · ${row.status_label}${row.active ? "" : " · inactive"}`,
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
    for (const input of body.querySelectorAll(".mig-input")) {
      input.addEventListener("change", () => {
        state.dirty[input.dataset.key] = input.value;
        const save = $("mig-save");
        if (save) save.disabled = false;
      });
    }

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

  /* --------------------------------------------------------------- load --- */

  async function refreshMetaOnly() {
    state.meta = await api("/api/migrations/meta");
    renderNotices();
    applyPermissions();
  }

  function applyPermissions() {
    const { user, frozen } = state.meta;
    const canRaise = user.signed_in && (user.roles.includes("developer") || user.roles.includes("approver"));
    for (const [id, allowed, why] of [
      ["mig-new", canRaise && !frozen, frozen ? "Record entry is frozen" : "Needs the developer role"],
      ["mig-upload", canRaise && !frozen, frozen ? "Record entry is frozen" : "Needs the developer role"],
    ]) {
      const button = $(id);
      if (!button) continue;
      button.disabled = !allowed;
      button.title = allowed ? "" : why;
    }
    const freeze = $("mig-freeze");
    if (freeze) freeze.textContent = state.meta.frozen ? "Frozen — manage…" : "Freeze…";
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
