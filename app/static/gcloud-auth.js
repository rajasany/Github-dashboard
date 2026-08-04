/* Interstitial for the local `gcloud auth login` flow: kick it off, then poll
 * until the CLI's credential store shows an active account, and hop back to
 * the dashboard. See app/main.py for the endpoints this talks to. */

const titleEl = document.getElementById("auth-title");
const hintEl = document.getElementById("auth-hint");
const errorEl = document.getElementById("auth-error");
const retryBtn = document.getElementById("auth-retry");

let pollTimer = null;

async function start() {
  errorEl.classList.add("hidden");
  retryBtn.classList.add("hidden");
  titleEl.textContent = "Signing in to Google Cloud…";
  hintEl.textContent =
    "A browser window should have opened for Google sign-in. Complete it there — this page continues on its own once it's done.";
  hintEl.classList.remove("hidden");

  try {
    const res = await fetch("/api/gcloud/login", { method: "POST" });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      showError(body.detail || `Could not start sign-in (${res.status}).`);
      return;
    }
  } catch (err) {
    showError(`Could not reach the dashboard server: ${err.message}`);
    return;
  }
  scheduleCheck();
}

function scheduleCheck() {
  clearTimeout(pollTimer);
  pollTimer = setTimeout(check, 1500);
}

async function check() {
  try {
    const res = await fetch("/api/gcloud/status");
    const body = await res.json();
    if (body.authenticated) {
      titleEl.textContent = "Signed in — returning to the dashboard…";
      hintEl.classList.add("hidden");
      window.location.href = "/";
      return;
    }
    if (body.error) {
      showError(body.error);
      return;
    }
    scheduleCheck();
  } catch (err) {
    showError(`Lost contact with the dashboard server: ${err.message}`);
  }
}

function showError(message) {
  clearTimeout(pollTimer);
  titleEl.textContent = "Sign-in didn't complete";
  hintEl.classList.add("hidden");
  errorEl.textContent = message;
  errorEl.classList.remove("hidden");
  retryBtn.classList.remove("hidden");
}

retryBtn.addEventListener("click", start);
start();
