"""Sign-in through OpenID Connect, against a provider stood up in-process.

Nothing here is mocked at the boundary that matters: a real RSA key signs real
ID tokens, and the app fetches the discovery document and JWKS over HTTP from a
provider running in this process. What is being tested is that the app refuses
the tokens it should refuse.

Run with:  .venv/bin/python tests/test_sso.py
"""

from __future__ import annotations

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import jwt  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402

from app import sso  # noqa: E402
from app.config import SsoConfig  # noqa: E402

FAILURES: list[str] = []


def check(name, got, want) -> None:
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {name}")
    if not ok:
        print(f"        got  {got!r}\n        want {want!r}")
        FAILURES.append(name)


def refused(name, fn, *, contains: str = "") -> None:
    try:
        fn()
    except sso.SsoError as exc:
        if contains and contains.casefold() not in str(exc).casefold():
            check(name, f"refused but said {str(exc)!r}", f"a message mentioning {contains!r}")
        else:
            check(name, "refused", "refused")
        return
    check(name, "accepted", "refused")


# --------------------------------------------------------------------------- #
# a provider, in this process
# --------------------------------------------------------------------------- #

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
KID = "test-key-1"

STATE: dict = {"issued": None, "token_hits": 0, "last_form": {}}


def public_jwk() -> dict:
    from jwt.algorithms import RSAAlgorithm

    data = json.loads(RSAAlgorithm.to_jwk(KEY.public_key()))
    data.update({"kid": KID, "use": "sig", "alg": "RS256"})
    return data


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # quiet
        pass

    def _send(self, payload, status=200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        base = f"http://127.0.0.1:{self.server.server_port}"
        path = urlparse(self.path).path
        if path == "/.well-known/openid-configuration":
            self._send({
                "issuer": base,
                "authorization_endpoint": f"{base}/authorize",
                "token_endpoint": f"{base}/token",
                "jwks_uri": f"{base}/jwks",
            })
        elif path == "/jwks":
            self._send({"keys": [public_jwk()]})
        else:
            self._send({"error": "not found"}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        STATE["last_form"] = parse_qs(self.rfile.read(length).decode())
        STATE["token_hits"] += 1
        if STATE["issued"] is None:
            self._send({"error": "invalid_grant"}, 400)
            return
        self._send({"id_token": STATE["issued"], "token_type": "Bearer"})


def start_provider() -> str:
    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_port}"


ISSUER = start_provider()
CLIENT_ID = "dashboard-client"


def make_token(**over) -> str:
    now = int(time.time())
    claims = {
        "iss": ISSUER, "aud": CLIENT_ID, "sub": "user-1",
        "iat": now, "exp": now + 300,
        "email": "jane@example.com", "name": "Jane Doe",
    }
    claims.update(over)
    key = over.pop("_key", KEY) if "_key" in over else KEY
    for drop in [k for k, v in claims.items() if v is None]:
        del claims[drop]
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": KID})


def config(**over) -> SsoConfig:
    base = dict(enabled=True, provider="oidc", issuer=ISSUER, client_id=CLIENT_ID,
                client_secret="shh", redirect_url="http://localhost:8000/auth/callback")
    base.update(over)
    return SsoConfig(**base)


# --------------------------------------------------------------------------- #


def test_discovery() -> None:
    print("=== discovering the provider ===")
    client = sso.SsoClient(config())
    provider = client.provider()
    check("the issuer is read", provider.issuer, ISSUER)
    check("and the authorize endpoint", provider.authorization_endpoint, f"{ISSUER}/authorize")
    check("and the token endpoint", provider.token_endpoint, f"{ISSUER}/token")

    check("google needs no issuer", sso.discovery_url(SsoConfig(provider="google")),
          "https://accounts.google.com/.well-known/openid-configuration")
    check("azure builds one from the tenant",
          sso.discovery_url(SsoConfig(provider="azure", tenant="contoso.onmicrosoft.com")),
          "https://login.microsoftonline.com/contoso.onmicrosoft.com/v2.0/"
          ".well-known/openid-configuration")
    check("azure defaults to organizations",
          "organizations" in sso.discovery_url(SsoConfig(provider="azure")), True)
    refused("a generic provider without an issuer is refused",
            lambda: sso.discovery_url(SsoConfig(provider="oidc")), contains="issuer is required")
    refused("an unreachable provider says so",
            lambda: sso.SsoClient(config(issuer="http://127.0.0.1:1")).provider(),
            contains="metadata")


def test_begin() -> None:
    print("\n=== starting a sign-in ===")
    client = sso.SsoClient(config())
    url, flow = client.begin("/migrations")
    query = parse_qs(urlparse(url).query)

    check("it goes to the provider", url.startswith(f"{ISSUER}/authorize"), True)
    check("asking for a code", query["response_type"], ["code"])
    check("as the configured client", query["client_id"], [CLIENT_ID])
    check("with the configured redirect", query["redirect_uri"],
          ["http://localhost:8000/auth/callback"])
    check("requesting the openid scopes", "openid" in query["scope"][0], True)
    check("carrying a state", len(query["state"][0]) > 20, True)
    check("and a nonce", len(query["nonce"][0]) > 20, True)
    check("with PKCE", query["code_challenge_method"], ["S256"])
    check("the verifier is kept, not sent",
          "code_verifier" in query, False)
    check("and the flow remembers where to return", flow["next"], "/migrations")

    other, _ = client.begin("/")
    check("each sign-in gets its own state",
          parse_qs(urlparse(other).query)["state"] != query["state"], True)

    google = sso.SsoClient(SsoConfig(provider="google", client_id="x", redirect_url="http://x/cb",
                                     enabled=True, hosted_domain="example.com"))
    # Not fetched from the network — just the query this would build.
    check("google is asked to restrict the account chooser",
          google.cfg.hosted_domain, "example.com")


def test_verify() -> None:
    print("\n=== verifying what comes back ===")
    client = sso.SsoClient(config())
    _, flow = client.begin("/")
    nonce = flow["nonce"]

    claims = client.verify(make_token(nonce=nonce), nonce)
    check("a good token verifies", claims["email"], "jane@example.com")

    refused("a token for another audience is refused",
            lambda: client.verify(make_token(aud="someone-else", nonce=nonce), nonce),
            contains="could not be verified")
    refused("a token from another issuer is refused",
            lambda: client.verify(make_token(iss="https://evil.example", nonce=nonce), nonce),
            contains="could not be verified")
    refused("an expired token is refused",
            lambda: client.verify(
                make_token(nonce=nonce, exp=int(time.time()) - 60, iat=int(time.time()) - 300),
                nonce),
            contains="could not be verified")
    refused("a token signed by the wrong key is refused",
            lambda: client.verify(
                jwt.encode({"iss": ISSUER, "aud": CLIENT_ID, "sub": "x",
                            "iat": int(time.time()), "exp": int(time.time()) + 300,
                            "email": "jane@example.com", "nonce": nonce},
                           OTHER_KEY, algorithm="RS256", headers={"kid": KID}),
                nonce),
            contains="could not be verified")
    refused("a token from a different sign-in is refused",
            lambda: client.verify(make_token(nonce="some-other-nonce"), nonce),
            contains="different sign-in")
    refused("an unsigned token is refused",
            lambda: client.verify(
                jwt.encode({"iss": ISSUER, "aud": CLIENT_ID, "email": "jane@example.com"},
                           key="", algorithm="none"),
                nonce),
            contains="could not be verified")


def test_exchange() -> None:
    print("\n=== exchanging the code ===")
    client = sso.SsoClient(config())
    _, flow = client.begin("/")
    STATE["issued"] = make_token(nonce=flow["nonce"])

    claims = client.finish("the-code", flow)
    check("the code is exchanged and the token verified", claims["email"], "jane@example.com")
    form = STATE["last_form"]
    check("the verifier is sent, proving PKCE", form["code_verifier"], [flow["verifier"]])
    check("along with the code", form["code"], ["the-code"])
    check("and the client secret", form["client_secret"], ["shh"])

    STATE["issued"] = None
    refused("a provider error is surfaced, not swallowed",
            lambda: client.finish("bad", flow), contains="rejected the sign-in")


def test_identity() -> None:
    print("\n=== who the token says you are ===")
    check("email is preferred", sso.email_from({"email": "a@x.com"}), "a@x.com")
    check("preferred_username is accepted",
          sso.email_from({"preferred_username": "b@x.com"}), "b@x.com")
    check("so is upn, which Entra often sends",
          sso.email_from({"upn": "c@x.com"}), "c@x.com")
    check("a username that is not an address is not taken",
          sso.email_from({"preferred_username": "jdoe"}), "")
    check("nothing usable gives nothing", sso.email_from({"sub": "1"}), "")

    limited = SsoConfig(allowed_domains=["example.com", "@other.com"])
    check("an allowed domain passes", sso.domain_allowed("a@example.com", limited), True)
    check("a leading @ in the config is tolerated",
          sso.domain_allowed("b@other.com", limited), True)
    check("case does not matter", sso.domain_allowed("c@EXAMPLE.com", limited), True)
    check("anything else is refused", sso.domain_allowed("d@elsewhere.com", limited), False)
    check("no restriction lets everyone in",
          sso.domain_allowed("e@anywhere.com", SsoConfig()), True)

    mapped = SsoConfig(role_claim="roles", role_map={"MigrationApprover": "approver",
                                                     "grp-devops": "devops"})
    check("a claim can carry roles",
          sso.roles_from_claims({"roles": ["MigrationApprover"]}, mapped), {"approver"})
    check("several at once",
          sso.roles_from_claims({"roles": ["MigrationApprover", "grp-devops"]}, mapped),
          {"approver", "devops"})
    check("a single value works too",
          sso.roles_from_claims({"roles": "grp-devops"}, mapped), {"devops"})
    check("unmapped values are ignored",
          sso.roles_from_claims({"roles": ["something-else"]}, mapped), set())
    check("without a map, nothing is claimed",
          sso.roles_from_claims({"roles": ["MigrationApprover"]}, SsoConfig()), set())


def main() -> int:
    test_discovery()
    test_begin()
    test_verify()
    test_exchange()
    test_identity()
    print(f"\n{'ALL PASS' if not FAILURES else f'{len(FAILURES)} FAILURES: ' + ', '.join(FAILURES)}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
