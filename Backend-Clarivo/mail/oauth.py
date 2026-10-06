"""
OAuth 2.0 flow construction for connecting a Gmail account.

Clarivo has no ``client_secret.json`` on disk: the client id and secret
come from ``.env`` via Django settings, and the client config Google's
library expects is assembled here instead.  That keeps one secret in one
place rather than splitting it between an env file and a JSON file that
would have to be gitignored separately.

Two defaults in ``google-auth-oauthlib`` need overriding for a
server-side flow that spans two separate HTTP requests, and both are
documented at their call sites below: PKCE autogeneration, and oauthlib's
strict scope checking.
"""

import os
import secrets
from datetime import datetime, timedelta, timezone

from django.conf import settings
from google_auth_oauthlib.flow import Flow

from .gmail_client import SCOPES, TOKEN_URI

# oauthlib raises a bare ``Warning`` exception from
# ``validate_token_parameters`` when the scopes granted differ at all from
# the scopes requested, unless this is set.  We ask for
# ``include_granted_scopes="true"``, which tells Google to issue a token
# carrying *every* scope the user has ever granted this client — so for
# any returning user the granted set legitimately differs from the
# requested set, and the token exchange would fail on a successful
# consent.  Relaxing the check is the documented remedy; the scopes we
# actually rely on are verified by the API calls themselves.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

# Google's OAuth 2.0 authorization endpoint.
AUTH_URI = "https://accounts.google.com/o/oauth2/auth"

# Endpoint that invalidates a grant.  Documented to take a POST with
# ``Content-Type: application/x-www-form-urlencoded`` and a ``token``
# parameter.
REVOKE_URI = "https://oauth2.googleapis.com/revoke"

# How long a pending OAuth handshake stays redeemable.  Long enough to
# read a consent screen, short enough that an abandoned one is not left
# lying around as a valid credential.
STATE_MAX_AGE = timedelta(minutes=10)


def build_client_config():
    """Assemble the Google "client secrets" structure from settings."""
    return {
        "web": {
            "client_id": settings.GOOGLE_CLIENT_ID,
            "client_secret": settings.GOOGLE_CLIENT_SECRET,
            "auth_uri": AUTH_URI,
            "token_uri": TOKEN_URI,
            "redirect_uris": [settings.GOOGLE_OAUTH_REDIRECT_URI],
        }
    }


def build_flow():
    """Return a ``Flow`` configured for Clarivo's scopes and redirect URI."""
    return Flow.from_client_config(
        build_client_config(),
        scopes=SCOPES,
        redirect_uri=settings.GOOGLE_OAUTH_REDIRECT_URI,
        # PKCE must be off, and this is not optional.  Left at its default
        # of True, ``authorization_url`` invents a ``code_verifier``,
        # sends its SHA-256 as a ``code_challenge``, and keeps the
        # verifier on *that* Flow object — which lives only for the
        # duration of the /start/ request.  The /callback/ request builds
        # a brand new Flow with no verifier, so Google would reject every
        # single token exchange with ``invalid_grant``.  PKCE exists to
        # protect clients that cannot keep a secret; this is a
        # confidential web client whose code exchange is already
        # authenticated by ``client_secret``.
        autogenerate_code_verifier=False,
    )


def new_state():
    """Return a fresh, unguessable OAuth ``state`` value."""
    return secrets.token_urlsafe(32)


def state_is_expired(oauth_state):
    """True if *oauth_state* is too old to be redeemed."""
    return datetime.now(timezone.utc) - oauth_state.created_at > STATE_MAX_AGE


def safe_return_to(return_to):
    """Reduce *return_to* to a path that is safe to redirect to.

    ``return_to`` is supplied by the caller of /start/ and later appended
    to ``FRONTEND_URL`` by /callback/, so it must not be able to steer
    the browser somewhere else.  Only a single-slash-rooted path is
    allowed through: ``//evil.example`` and ``https://evil.example`` are
    both discarded in favour of the frontend root.
    """
    if not return_to:
        return "/"
    if not return_to.startswith("/") or return_to.startswith("//"):
        return "/"
    return return_to


def frontend_redirect_url(return_to, outcome):
    """Build the frontend URL to hand the browser back to.

    *outcome* lands in the query string as ``gmail=<outcome>`` so the SPA
    can show a result banner on whatever page the user started from.
    """
    path = safe_return_to(return_to)
    separator = "&" if "?" in path else "?"
    return f"{settings.FRONTEND_URL.rstrip('/')}{path}{separator}gmail={outcome}"
