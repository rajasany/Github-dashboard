"""Who is making this request, and what are they allowed to do.

Identity comes from a header set by an SSO proxy sitting in front of the app —
`X-Forwarded-Email` by default. The app never sees a password.

The safety of that arrangement rests entirely on one thing: **the app must not be
reachable except through the proxy**. A header is just bytes the client sends, so
anyone who can open a socket to uvicorn directly can claim to be anybody. Two
guards follow from that:

  * `auth.trusted_proxies` — when set, the header is honoured only when the
    connection came from one of those addresses. Anything else is anonymous.
  * when it is *not* set, the app still works but reports `insecure: true`, and
    the UI shows a standing banner. Silence would let an unprotected deployment
    look exactly like a protected one.

The peer address is used for that check, never `X-Forwarded-For`, which is itself
client-supplied and so cannot vouch for anything.

For testing without a proxy there is `auth.dev_mode`, where the caller simply
declares who they are. That is a complete bypass, so it is off by default and
carries the guards in `dev_mode_available` — see there.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from fnmatch import fnmatch

from .config import ROLES, Settings


class AuthError(Exception):
    """Raised when a request cannot be attributed to a permitted user."""

    def __init__(self, message: str, status: int = 403) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class User:
    email: str
    roles: frozenset[str] = field(default_factory=frozenset)
    # True when the identity came from config rather than the proxy header.
    dev_mode: bool = False
    # True when the header was accepted without a trusted-proxy allowlist.
    insecure: bool = False

    @property
    def is_admin(self) -> bool:
        return "admin" in self.roles

    @property
    def is_developer(self) -> bool:
        return self.has_any("developer")

    @property
    def is_approver(self) -> bool:
        return self.has_any("approver")

    @property
    def is_devops(self) -> bool:
        return self.has_any("devops")

    @property
    def signed_in(self) -> bool:
        return bool(self.email)

    def has_any(self, *roles: str) -> bool:
        """Admin satisfies every check, so this is the single place it is granted.

        The roles actually held are left as configured, so the UI keeps showing
        "admin" rather than pretending the person was given all four.
        """
        if self.is_admin:
            return True
        return any(r in self.roles for r in roles)

    def as_dict(self) -> dict:
        return {
            "email": self.email,
            "roles": sorted(self.roles),
            "dev_mode": self.dev_mode,
            "insecure": self.insecure,
            "signed_in": self.signed_in,
        }


ANONYMOUS = User(email="")


def roles_for(email: str, settings: Settings) -> frozenset[str]:
    """Every role whose patterns match this address.

    A pattern is an exact address or a glob such as `*@example.com`. Matching is
    case-insensitive because mail addresses are not case-sensitive in practice and
    an SSO provider may not preserve the case a person typed.
    """
    who = (email or "").strip().casefold()
    if not who:
        return frozenset()

    held = set()
    for role in ROLES:
        for pattern in settings.auth.roles.get(role, []):
            if fnmatch(who, pattern.strip().casefold()):
                held.add(role)
                break
    return frozenset(held)


def _peer_is_trusted(peer: str | None, allowlist: list[str]) -> bool:
    """Is the immediate connecting address one we accept identity headers from?

    Entries may be plain addresses or CIDR blocks. An unparseable entry never
    matches — a typo in the allowlist must not widen it.
    """
    if not peer:
        return False
    try:
        address = ipaddress.ip_address(peer)
    except ValueError:
        return False

    for entry in allowlist:
        entry = entry.strip()
        if not entry:
            continue
        try:
            if "/" in entry:
                if address in ipaddress.ip_network(entry, strict=False):
                    return True
            elif address == ipaddress.ip_address(entry):
                return True
        except ValueError:
            continue
    return False


DEV_COOKIE = "dev_user"
DEV_HEADER = "X-Dev-User"


def _is_loopback(peer: str | None) -> bool:
    try:
        return ipaddress.ip_address(peer or "").is_loopback
    except ValueError:
        return False


def dev_mode_available(peer: str | None, settings: Settings) -> tuple[bool, str]:
    """Is self-declared identity allowed for this request? (allowed, why not).

    Three conditions, because this is a complete authentication bypass:

      1. `auth.dev_mode` must be set — it is never on by default;
      2. `auth.trusted_proxies` must be *empty*. If you have configured a real
         proxy you are not testing, and a proxy on the same host would otherwise
         satisfy the loopback check below while forwarding anyone's request;
      3. the request must come from loopback, unless `dev_allow_remote` says
         otherwise — so a forgotten flag is not reachable from the network.
    """
    auth = settings.auth
    if not auth.dev_mode:
        return False, "dev mode is off"
    if auth.trusted_proxies:
        return False, "auth.trusted_proxies is set, so this is not a test deployment"
    if not auth.dev_allow_remote and not _is_loopback(peer):
        return False, "dev mode is limited to loopback; set auth.dev_allow_remote to widen it"
    return True, ""


def resolve_user(headers, peer: str | None, settings: Settings, cookies=None) -> User:
    """Identify the caller. Returns ANONYMOUS rather than raising."""
    auth = settings.auth
    allowlist = auth.trusted_proxies

    # Dev mode wins where it applies: the point of it is to choose an identity,
    # and its guards already establish that no real proxy is in play.
    allowed, _ = dev_mode_available(peer, settings)
    if allowed:
        chosen = (
            headers.get(DEV_HEADER)
            or headers.get(DEV_HEADER.lower())
            or (cookies or {}).get(DEV_COOKIE)
            or auth.dev_user
        )
        chosen = (chosen or "").strip()
        return User(
            email=chosen,
            roles=roles_for(chosen, settings) if chosen else frozenset(),
            dev_mode=True,
            insecure=True,
        )

    raw = ""
    for name in (auth.header, "X-Forwarded-Email", "X-Forwarded-User", "X-Auth-Request-Email"):
        if not name:
            continue
        value = headers.get(name) or headers.get(name.lower()) or ""
        if value:
            raw = value
            break

    if raw:
        if allowlist and not _peer_is_trusted(peer, allowlist):
            # The header is present but arrived from somewhere not entitled to
            # assert it. Treating it as anonymous is the whole point of the list.
            return ANONYMOUS
        email = raw.split(",")[0].strip()
        return User(
            email=email,
            roles=roles_for(email, settings),
            dev_mode=False,
            insecure=not allowlist,
        )

    return ANONYMOUS


SIGN_IN_HELP = (
    "Not signed in. Either put an authenticating proxy in front of this app so it "
    "receives an identity header, or — to test without one — set `auth.dev_mode: true` "
    "and leave `auth.trusted_proxies` unset. The two are mutually exclusive: a "
    "configured proxy allowlist turns dev mode off. See the `auth:` section of config.yaml."
)


def require_signed_in(user: User) -> None:
    if not user.signed_in:
        raise AuthError(SIGN_IN_HELP, status=401)


def require_role(user: User, *roles: str) -> None:
    require_signed_in(user)
    if not user.has_any(*roles):
        held = ", ".join(sorted(user.roles)) or "none"
        want = " or ".join(roles)
        raise AuthError(f"This needs the {want} role. You hold: {held}.", status=403)
