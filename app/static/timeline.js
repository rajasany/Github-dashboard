/* Folder timeline: when work happened in one folder of one repository.
 *
 * Kept out of app.js for the same reason the migrations tab is — it is its own
 * view with its own pickers, and app.js is long enough.
 *
 * Commits are grouped into days *here* rather than on the server. A commit at
 * 23:40 UTC falls on a different day in Auckland than in Los Angeles, and only
 * the browser knows which one the reader means.
 */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const esc = (value) =>
    String(value ?? "").replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
    );

  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

  // `loaded` holds the whole repo+branch+window, every folder included. Picking
  // a folder then filters what is already here rather than asking again.
  const state = { repos: [], loading: false, loaded: null };

  async function api(path) {
    const response = await fetch(path);
    const text = await response.text();
    let body = null;
    try {
      body = text ? JSON.parse(text) : null;
    } catch {
      body = null;
    }
    if (!response.ok) {
      throw new Error((body && body.detail) || text || response.statusText);
    }
    return body;
  }

  /* ------------------------------------------------------------- moments -- */

  // <input type="datetime-local"> holds wall-clock with no zone. Converting here
  // makes the instant explicit rather than leaving the server to guess.
  const toEpoch = (value) => (value ? Date.parse(value) / 1000 : null);

  function toLocalInput(date) {
    const pad = (n) => String(n).padStart(2, "0");
    return (
      `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}` +
      `T${pad(date.getHours())}:${pad(date.getMinutes())}`
    );
  }

  const dayKey = (d) => `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`;

  function dayLabel(date) {
    const today = new Date();
    const yesterday = new Date(today.getTime() - 86400000);
    if (dayKey(date) === dayKey(today)) return "Today";
    if (dayKey(date) === dayKey(yesterday)) return "Yesterday";
    const weekday = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
                     "Saturday"][date.getDay()];
    return `${weekday} ${String(date.getDate()).padStart(2, "0")}-${MONTHS[date.getMonth()]}-${date.getFullYear()}`;
  }

  const clockTime = (date) =>
    `${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}`;

  /* -------------------------------------------------------------- pickers -- */

  async function fillRepos() {
    const select = $("tl-repo");
    if (select.options.length) return;
    // app.js already resolved these for its own pickers; use the same list so
    // this tab cannot disagree with the others about what is tracked.
    const repos = window.__dash?.repoOptions?.() || [];
    if (repos.length) {
      window.__dash.fillRepoSelect(select, "");
      return;
    }
    try {
      const cfg = await api("/api/config");
      state.repos = [
        ...(cfg.repos || []).map((name) => ({ key: `github:${name}`, name })),
        ...(cfg.csr_repos || []).map((r) => ({ key: r.key, name: `${r.project}/${r.repo}` })),
      ];
    } catch {
      state.repos = [];
    }
    select.innerHTML = state.repos
      .map((r) => `<option value="${esc(r.key)}">${esc(r.name)}</option>`)
      .join("");
  }

  async function fillBranches() {
    const select = $("tl-branch");
    const key = $("tl-repo").value;
    select.innerHTML = `<option value="">Choose a branch…</option>`;
    if (!key) return;
    try {
      const body = await api(`/api/branches?key=${encodeURIComponent(key)}`);
      for (const name of body.branches || []) {
        select.insertAdjacentHTML("beforeend", `<option value="${esc(name)}">${esc(name)}</option>`);
      }
    } catch {
      // A branch list is a convenience; all-branches still works without it.
    }
  }

  function setDefaultWindow() {
    const days = Number(window.__dash?.config?.()?.defaults?.days) || 14;
    const now = new Date();
    $("tl-to").value = "";
    $("tl-from").value = toLocalInput(new Date(now.getTime() - days * 86400000));
    return days;
  }

  /* ------------------------------------------------------------ rendering -- */

  function render(commits) {
    const body = $("tl-body");
    if (!commits.length) {
      body.innerHTML =
        `<div class="empty"><p>No commits in that folder within the window.</p>` +
        `<p class="muted">Try a longer period, another branch, or a different folder.</p></div>`;
      return;
    }

    // Day rows keep it a timeline rather than a flat list, while the columns
    // make it a table. The grouping is done here because only the browser knows
    // which calendar day a given instant falls on for the person reading it.
    const rows = [];
    let day = null;
    for (const commit of commits) {
      const when = new Date(commit.date);
      if (day !== dayKey(when)) {
        day = dayKey(when);
        const sameDay = commits.filter((c) => dayKey(new Date(c.date)) === day).length;
        rows.push(
          `<tr class="tl-dayrow"><th colspan="7" scope="colgroup">${esc(dayLabel(when))}` +
          `<span class="tl-daycount">${sameDay}</span></th></tr>`
        );
      }

      // The shared chip, so this tab keeps the recorded-versus-mentioned
      // distinction the rest of the app makes: only `git cherry-pick -x` records
      // a source commit, and a message that merely mentions one proves nothing.
      const picked = commit.cherry_pick?.is_cherry_pick;
      const marks =
        (commit.tags || [])
          .map((tag) => `<span class="chip tag">${esc(tag.name || tag)}</span>`)
          .join("") +
        (window.__dash?.cherryChip?.(commit.cherry_pick) ??
          (picked ? `<span class="chip cherry">cherry-pick</span>` : ""));

      rows.push(
        // Marked on the row as well as in the cell: a chip is easy to miss when
        // scanning a column of commit messages.
        `<tr${picked ? ' class="is-cherry"' : ""}>` +
        `<td class="tl-when">${esc(clockTime(when))}</td>` +
        `<td class="tl-comment">` +
        (commit.url
          ? `<a href="${esc(commit.url)}" target="_blank" rel="noopener">${esc(commit.title) || "(no message)"}</a>`
          : esc(commit.title) || "(no message)") +
        (marks ? `<div class="tl-chips">${marks}</div>` : "") +
        `</td>` +
        `<td class="mono">${esc(commit.short)}</td>` +
        `<td>${esc(commit.author)}</td>` +
        `<td class="num">${commit.files_changed || 0}</td>` +
        `<td>${(commit.branches || []).map((b) => `<span class="chip branch">${esc(b)}</span>`).join("") || '<span class="muted">—</span>'}</td>` +
        `<td>${esc((commit.folders || []).join(", ")) || '<span class="muted">—</span>'}</td>` +
        `</tr>`
      );
    }

    body.innerHTML =
      `<div class="table-scroll"><table class="data-table tl-table"><thead><tr>` +
      `<th>Time</th><th>Comment</th><th>Hash</th><th>Created by</th>` +
      `<th class="num">Files</th><th>Branch</th><th>Folders</th>` +
      `</tr></thead><tbody>${rows.join("")}</tbody></table></div>`;
  }

  function prompt(message) {
    $("tl-body").innerHTML = `<div class="empty"><p>${esc(message)}</p></div>`;
    $("tl-scope").textContent = "";
  }

  function scopeLine(data, shown) {
    const from = new Date(data.since);
    const to = data.until ? new Date(data.until) : null;
    const authors = new Set(shown.map((c) => c.author)).size;
    const files = shown.reduce((n, c) => n + (c.files_changed || 0), 0);
    const tagged = shown.filter((c) => (c.tags || []).length).length;
    const picks = shown.filter((c) => c.cherry_pick?.is_cherry_pick).length;

    const parts = [
      `${shown.length} commit${shown.length === 1 ? "" : "s"}`,
      esc(data.folder || $("tl-folder").value || "all folders"),
      `on ${esc(data.branch || $("tl-branch").value)}`,
      `${dayLabel(from)} ${clockTime(from)} → ${to ? `${dayLabel(to)} ${clockTime(to)}` : "now"}`,
    ];
    if (authors) parts.push(`${authors} author${authors === 1 ? "" : "s"}`);
    if (files) parts.push(`${files} file${files === 1 ? "" : "s"} touched`);
    if (tagged) parts.push(`${tagged} tagged`);
    if (picks) parts.push(`${picks} cherry-picked`);

    $("tl-scope").innerHTML =
      parts.join(" · ") +
      (data.capped
        ? ` · <strong>the branch page filled up, so older commits in this window may be missing</strong>`
        : "") +
      (data.errors && data.errors.length
        ? ` · <strong>${esc(data.errors.join("; "))}</strong>`
        : "");
  }

  function fillFolders(data) {
    const select = $("tl-folder");
    const chosen = select.value;
    const folders = data.folders_available || [];
    select.disabled = folders.length === 0;
    select.innerHTML =
      `<option value="">${folders.length ? "Choose a folder…" : "No folders in this window"}</option>` +
      folders.map((f) => `<option value="${esc(f)}">${esc(f)}</option>`).join("");
    if (folders.includes(chosen)) select.value = chosen;
  }

  /* ----------------------------------------------------------------- run -- */

  /* Repo + branch + window is one fetch; the folder narrows what came back.
   * Changing folder is then instant and costs the provider nothing. */
  async function load() {
    const key = $("tl-repo").value;
    const branch = $("tl-branch").value;
    state.loaded = null;

    if (!key || !branch) {
      $("tl-folder").disabled = true;
      $("tl-folder").innerHTML = `<option value="">Choose a branch first…</option>`;
      prompt(key ? "Choose a branch, then a folder." : "Choose a repository, branch and folder.");
      return;
    }

    state.loading = true;
    $("tl-run").disabled = true;
    $("tl-scope").textContent = "Loading…";
    try {
      const params = new URLSearchParams({ key, branch });
      const from = toEpoch($("tl-from").value);
      const to = toEpoch($("tl-to").value);
      if (from) params.set("since_epoch", String(from));
      if (to) params.set("until_epoch", String(to));

      state.loaded = await api(`/api/timeline?${params}`);
      fillFolders(state.loaded);
      show();
    } catch (error) {
      $("tl-scope").textContent = "";
      $("tl-body").innerHTML = `<div class="banner error">${esc(error.message)}</div>`;
    } finally {
      state.loading = false;
      $("tl-run").disabled = false;
    }
  }

  function show() {
    const data = state.loaded;
    if (!data) return;
    const folder = $("tl-folder").value;
    if (!folder) {
      $("tl-scope").textContent =
        `${data.count} commit${data.count === 1 ? "" : "s"} on ${data.branch} in this window, ` +
        `across ${data.folders_available.length} folder${data.folders_available.length === 1 ? "" : "s"}.`;
      $("tl-body").innerHTML =
        `<div class="empty"><p>Choose a folder to see its timeline.</p></div>`;
      return;
    }
    const shown = data.commits.filter((c) => (c.folders || []).includes(folder));
    scopeLine(data, shown);
    render(shown);
  }

  let wired = false;

  async function initTimelineTab() {
    if (!wired) {
      wired = true;
      $("tl-run")?.addEventListener("click", load);
      $("tl-reset")?.addEventListener("click", () => {
        setDefaultWindow();
        load();
      });
      $("tl-repo")?.addEventListener("change", async () => {
        await fillBranches();
        await load();
      });
      // A folder change re-reads what is already loaded; the rest refetches.
      $("tl-folder")?.addEventListener("change", show);
      for (const id of ["tl-branch", "tl-from", "tl-to"]) {
        $(id)?.addEventListener("change", load);
      }
    }

    await fillRepos();
    if (!state.loaded) {
      setDefaultWindow();
      await fillBranches();
      await load();
    }
  }

  window.initTimelineTab = initTimelineTab;
})();
