"""Who a tag should be attributed to.

A tag created from this dashboard was created by a person, not by the dashboard.
Each provider knows who that is:

  CSR/gcloud → the signed-in account (`gcloud config get-value account`)
  GitHub     → the owner of the token (`GET /user`)

`TAGGER_NAME` / `TAGGER_EMAIL` override both, for a deliberate bot identity.
The generic fallback is used only when nothing else can be established, and the
`source` field says which of these applied so the UI can be honest about it.
"""

from __future__ import annotations

from typing import Any

from . import csr as csr_provider
from . import github as github_provider
from .config import Settings

FALLBACK_NAME = "Repo Change Dashboard"
FALLBACK_EMAIL = "noreply@example.invalid"


def _name_from_email(email: str) -> str:
    """The local part, verbatim.

    Deliberately no prettifying — turning `a.b@x` into "A B" would invent a
    human name that nothing in the account actually states.
    """
    return email.split("@", 1)[0] or email


async def resolve_tagger(
    settings: Settings,
    provider: str,
    gh_client: github_provider.GitHubClient | None = None,
    mirror: csr_provider.GitMirror | None = None,
) -> dict[str, Any]:
    """Return {name, email, source} for the identity to stamp on a tag."""
    # An explicit configuration is a deliberate choice; respect it fully.
    if settings.tagger_name and settings.tagger_email:
        return {
            "name": settings.tagger_name,
            "email": settings.tagger_email,
            "source": "config",
        }

    name = email = ""
    source = "fallback"

    if provider == "csr" and mirror is not None:
        try:
            account = await mirror.tokens.active_account()
        except Exception:
            account = None
        if account:
            email = account
            name = _name_from_email(account)
            source = "gcloud"

    elif provider == "github" and gh_client is not None:
        user = await gh_client.get_authenticated_user()
        login = (user or {}).get("login")
        if login:
            name = (user or {}).get("name") or login
            # A private profile email is absent from the API; GitHub's own
            # no-reply address is the documented stand-in and routes correctly.
            email = (user or {}).get("email") or (
                f"{user.get('id')}+{login}@users.noreply.github.com"
                if user.get("id")
                else f"{login}@users.noreply.github.com"
            )
            source = "github"

    # A partial override still wins over the provider for the field it sets.
    return {
        "name": settings.tagger_name or name or FALLBACK_NAME,
        "email": settings.tagger_email or email or FALLBACK_EMAIL,
        "source": "config" if (settings.tagger_name or settings.tagger_email) and source == "fallback" else source,
    }
