"""Every data endpoint must be attributable to someone.

The point of these is the *default*: a route added tomorrow should be closed
until someone deliberately lists it as public. Guarding endpoints one at a time
is how `/api/tags/push` came to be reachable by anyone.

Run with:  .venv/bin/python tests/test_auth_gate.py
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from app import main as M  # noqa: E402
from app.config import AuthConfig, SsoConfig  # noqa: E402

FAILURES: list[str] = []


def check(name, got, want) -> None:
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {name}")
    if not ok:
        print(f"        got  {got!r}\n        want {want!r}")
        FAILURES.append(name)


def routes() -> list[tuple[str, str]]:
    out = []
    for route in M.app.routes:
        path = getattr(route, "path", "")
        methods = getattr(route, "methods", set()) - {"HEAD", "OPTIONS"}
        if path.startswith(("/api", "/auth")) and methods:
            # A concrete value for each path parameter, so the route matches.
            concrete = re.sub(r"\{[^}]+\}", "1", path)
            out.append((sorted(methods)[0], concrete))
    return sorted(set(out))


async def run() -> None:
    M.settings.auth = AuthConfig(
        roles={"developer": ["dev@example.com"], "approver": [], "devops": [], "admin": []},
        trusted_proxies=["127.0.0.1"],
    )
    M.settings.auth.sso = SsoConfig()

    def client(email: str | None) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=M.app, client=("127.0.0.1", 5000)),
            base_url="http://t", timeout=30,
            headers={"X-Forwarded-Email": email} if email else {},
        )

    async with M.lifespan(M.app):
        print("=== with authentication configured ===")
        check("it is enforced", M.settings.auth.enforced, True)

        async with client(None) as c:
            public, closed, leaked = [], [], []
            for method, path in routes():
                r = await c.request(method, path)
                (public if r.status_code != 401 else closed).append(path)
                allowed = path in M.PUBLIC_PATHS or path.startswith(M.PUBLIC_PREFIXES)
                if r.status_code != 401 and not allowed:
                    leaked.append(f"{method} {path}")

            check("nothing outside the public list answers a stranger", leaked, [])
            # The sign-in routes are public by necessity, and nothing else is.
            check("only the sign-in routes are public by prefix",
                  sorted(p for p in public if p.startswith("/auth/")),
                  ["/auth/callback", "/auth/gcloud", "/auth/login", "/auth/logout"])
            check("and plenty is closed", len(closed) > 20, True)

            # The ones that matter most, named so a regression is legible.
            for method, path in [("POST", "/api/tags/push/1"), ("POST", "/api/tags/stage"),
                                 ("GET", "/api/feed"), ("GET", "/api/timeline"),
                                 ("GET", "/api/summary"), ("POST", "/api/report"),
                                 ("GET", "/api/migrations/records")]:
                r = await c.request(method, path)
                check(f"{method} {path} refuses a stranger", r.status_code, 401)

            # And the ones that must stay open, or nobody could ever sign in.
            for path in ["/api/health", "/api/auth/whoami", "/"]:
                r = await c.get(path)
                check(f"{path} stays reachable", r.status_code < 400, True)
            check("the refusal says how to sign in",
                  "proxy" in (await c.get("/api/feed")).json()["detail"], True)

        async with client("dev@example.com") as c:
            r = await c.get("/api/config")
            check("a signed-in user gets through", r.status_code, 200)
            check("and is recognised",
                  (await c.get("/api/auth/whoami")).json()["email"], "dev@example.com")

        print("\n=== gcloud sign-in is a laptop affordance ===")
        async with client("dev@example.com") as c:
            r = await c.post("/api/gcloud/login")
            check("offered from the machine itself", r.status_code != 403, True)
        remote = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=M.app, client=("10.1.2.3", 5000)),
            base_url="http://t", headers={"X-Forwarded-Email": "dev@example.com"}, timeout=30)
        async with remote as c:
            r = await c.post("/api/gcloud/login")
            # Off-box the header is not trusted either, so either refusal is right;
            # what matters is that it does not run.
            check("but never from elsewhere", r.status_code in (401, 403), True)

        print("\n=== with no authentication configured ===")
        M.settings.auth = AuthConfig()
        check("nothing is enforced", M.settings.auth.enforced, False)
        async with client(None) as c:
            check("so a laptop still works", (await c.get("/api/config")).status_code, 200)

        M.settings.auth = AuthConfig(require_sign_in=True)
        check("unless it is demanded outright", M.settings.auth.enforced, True)
        async with client(None) as c:
            check("and then nothing answers", (await c.get("/api/config")).status_code, 401)


def main() -> int:
    asyncio.run(run())
    print(f"\n{'ALL PASS' if not FAILURES else f'{len(FAILURES)} FAILURES: ' + ', '.join(FAILURES)}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
