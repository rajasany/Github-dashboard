"""Sign-in through Google Workspace, Microsoft Entra ID (Azure AD), or any
OpenID Connect provider.

The app does the OIDC authorization-code flow itself rather than relying on a
proxy, so it can be pointed at a provider and used. Configuration is a handful
of values; everything else — endpoints, signing keys — is discovered from the
provider's own metadata.

    auth:
      sso:
        provider: google            # or: azure, oidc
        client_id: ...
        client_secret: ...
        redirect_url: https://dashboard.example.com/auth/callback

What is checked on the way in, because an ID token is only worth what is
verified about it:

  * the **signature**, against the provider's published keys (JWKS), fetched and
    cached by key id;
  * the **issuer** and **audience**, so a token minted for another application
    cannot be replayed here;
  * the **expiry**, by the JWT library;
  * the **nonce**, tying the token to the sign-in this browser actually started;
  * the **state**, tying the callback to that same sign-in, which is what stops
    a login CSRF;
  * **PKCE**, so an intercepted authorization code is useless without the
    verifier held in this browser's cookie.

The session that results is a signed, expiring cookie holding the address and
nothing else. Roles are resolved per request from the database or config, so
removing someone's role takes effect immediately rather than when their session
happens to expire.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx

from .config import SsoConfig

# The transient cookie holding state/nonce/verifier between the redirect out and
# the callback back. Short-lived by design: it is only needed for that round trip.
FLOW_COOKIE = "sso_flow"
SESSION_COOKIE = "session"
FLOW_MAX_AGE = 600  # ten minutes to finish signing in

DISCOVERY = {
    "google": "https://accounts.google.com/.well-known/openid-configuration",
    # {tenant} is substituted; "organizations" or "common" both work.
    "azure": "https://login.microsoftonline.com/{tenant}/v2.0/.well-known/openid-configuration",
}


class SsoError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class Provider:
    """A provider's endpoints, as it describes them itself."""

    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    end_session_endpoint: str = ""


def discovery_url(cfg: SsoConfig) -> str:
    kind = (cfg.provider or "oidc").strip().lower()
    if kind in DISCOVERY:
        return DISCOVERY[kind].format(tenant=cfg.tenant or "organizations")
    issuer = (cfg.issuer or "").rstrip("/")
    if not issuer:
        raise SsoError("auth.sso.issuer is required when provider is not google or azure.", 500)
    return f"{issuer}/.well-known/openid-configuration"


class SsoClient:
    """One configured provider, with its metadata and keys cached."""

    METADATA_TTL = 3600

    def __init__(self, cfg: SsoConfig) -> None:
        self.cfg = cfg
        self._provider: Provider | None = None
        self._fetched = 0.0
        self._jwks: Any = None

    # -- provider metadata --------------------------------------------------- #

    def provider(self) -> Provider:
        fresh = self._provider and (time.monotonic() - self._fetched) < self.METADATA_TTL
        if fresh:
            return self._provider  # type: ignore[return-value]

        url = discovery_url(self.cfg)
        try:
            response = httpx.get(url, timeout=10, follow_redirects=True)
            response.raise_for_status()
            data = response.json()
        except Exception as exc:  # noqa: BLE001 - reported with the URL that failed
            raise SsoError(f"Could not read the provider's metadata at {url}: {exc}", 502) from exc

        missing = [
            key for key in ("issuer", "authorization_endpoint", "token_endpoint", "jwks_uri")
            if not data.get(key)
        ]
        if missing:
            raise SsoError(f"{url} is missing {', '.join(missing)}.", 502)

        self._provider = Provider(
            issuer=data["issuer"],
            authorization_endpoint=data["authorization_endpoint"],
            token_endpoint=data["token_endpoint"],
            jwks_uri=data["jwks_uri"],
            end_session_endpoint=data.get("end_session_endpoint", ""),
        )
        self._fetched = time.monotonic()
        self._jwks = None
        return self._provider

    def _keys(self) -> Any:
        if self._jwks is None:
            import jwt

            self._jwks = jwt.PyJWKClient(self.provider().jwks_uri, cache_keys=True)
        return self._jwks

    # -- the flow ------------------------------------------------------------ #

    def begin(self, next_url: str = "/") -> tuple[str, dict[str, str]]:
        """Where to send the browser, and the flow secrets to remember."""
        verifier = secrets.token_urlsafe(64)
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .decode()
            .rstrip("=")
        )
        flow = {
            "state": secrets.token_urlsafe(24),
            "nonce": secrets.token_urlsafe(24),
            "verifier": verifier,
            "next": next_url or "/",
        }
        query = {
            "response_type": "code",
            "client_id": self.cfg.client_id,
            "redirect_uri": self.cfg.redirect_url,
            "scope": " ".join(self.cfg.scopes or ["openid", "email", "profile"]),
            "state": flow["state"],
            "nonce": flow["nonce"],
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        if self.cfg.provider == "google":
            # Ask Google for the account chooser rather than silently reusing
            # whichever session the browser happens to hold.
            query["prompt"] = "select_account"
        if self.cfg.hosted_domain:
            query["hd"] = self.cfg.hosted_domain
        return f"{self.provider().authorization_endpoint}?{urlencode(query)}", flow

    def finish(self, code: str, flow: dict[str, str]) -> dict[str, Any]:
        """Exchange the code and return the verified claims."""
        provider = self.provider()
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.cfg.redirect_url,
            "client_id": self.cfg.client_id,
            "code_verifier": flow.get("verifier", ""),
        }
        if self.cfg.client_secret:
            form["client_secret"] = self.cfg.client_secret

        try:
            response = httpx.post(provider.token_endpoint, data=form, timeout=15)
        except Exception as exc:  # noqa: BLE001
            raise SsoError(f"Could not reach {provider.token_endpoint}: {exc}", 502) from exc
        if response.status_code >= 400:
            # The provider's own error is far more useful than a generic one:
            # redirect_uri_mismatch and invalid_client are the usual causes.
            raise SsoError(f"The provider rejected the sign-in: {response.text[:400]}", 400)

        token = response.json().get("id_token")
        if not token:
            raise SsoError("The provider returned no id_token.", 502)
        return self.verify(token, flow.get("nonce", ""))

    def verify(self, token: str, nonce: str) -> dict[str, Any]:
        import jwt

        try:
            key = self._keys().get_signing_key_from_jwt(token).key
            claims = jwt.decode(
                token,
                key,
                algorithms=["RS256", "RS384", "RS512", "ES256", "ES384"],
                audience=self.cfg.client_id,
                issuer=self.provider().issuer,
                options={"require": ["exp", "iat", "iss", "aud"]},
            )
        except Exception as exc:  # noqa: BLE001 - any failure here is a refusal
            raise SsoError(f"The sign-in token could not be verified: {exc}", 401) from exc

        if nonce and claims.get("nonce") != nonce:
            raise SsoError("The sign-in token belongs to a different sign-in attempt.", 401)
        return claims


def email_from(claims: dict[str, Any]) -> str:
    """The address to identify someone by.

    Entra ID often omits `email` and carries the address in `preferred_username`
    or `upn` instead, so those are accepted rather than failing on a token that
    plainly identifies someone.
    """
    for key in ("email", "preferred_username", "upn"):
        value = str(claims.get(key) or "").strip()
        if "@" in value:
            return value
    return ""


def domain_allowed(email: str, cfg: SsoConfig) -> bool:
    if not cfg.allowed_domains:
        return True
    domain = email.rsplit("@", 1)[-1].casefold()
    return any(domain == d.strip().casefold().lstrip("@") for d in cfg.allowed_domains)


def roles_from_claims(claims: dict[str, Any], cfg: SsoConfig) -> set[str]:
    """Roles carried by the token itself, where the provider is set up for it.

    Entra ID can return app roles or group ids; `role_claim` names which claim to
    read and `role_map` says what each value means here. Optional: without it,
    roles come from the address alone.
    """
    if not cfg.role_claim or not cfg.role_map:
        return set()
    raw = claims.get(cfg.role_claim)
    if raw is None:
        return set()
    values = raw if isinstance(raw, (list, tuple)) else [raw]
    mapped = set()
    for value in values:
        role = cfg.role_map.get(str(value))
        if role:
            mapped.add(role)
    return mapped
