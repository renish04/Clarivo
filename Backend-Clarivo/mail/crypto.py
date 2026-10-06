"""
Symmetric encryption for OAuth tokens at rest.

A Gmail refresh token is a long-lived credential: it does not expire on
its own, and anyone holding one can mint access tokens and read the
user's mail until the grant is revoked.  Django's database is a plain
SQLite file sitting in the repository directory, so storing refresh
tokens in it verbatim would mean a single stray copy of ``db.sqlite3``
hands over every connected mailbox.  They are therefore encrypted
before they are written and decrypted only in the moment they are used.

Fernet is AES-128-CBC with an HMAC-SHA256 authentication tag, so a token
that has been tampered with fails to decrypt rather than decrypting to
garbage.  The key lives in ``FIELD_ENCRYPTION_KEY`` in ``.env`` — which
is gitignored, and so is never committed next to the database it
protects.  Generate one with::

    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""

from cryptography.fernet import Fernet
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

# Built on first use rather than at import time.  This module is imported
# by mail/models.py, which Django loads during startup, so raising on a
# missing key here would stop the whole project from booting — including
# the management commands and endpoints that have nothing to do with
# Gmail.  Deferring it means only the code paths that actually touch a
# token complain.
_fernet = None


def _get_fernet():
    """Return the shared Fernet instance, building it on first call."""
    global _fernet

    if _fernet is None:
        key = settings.FIELD_ENCRYPTION_KEY
        if not key:
            raise ImproperlyConfigured(
                "FIELD_ENCRYPTION_KEY is not set — OAuth tokens cannot be "
                "encrypted or decrypted.  Add it to Backend-Clarivo/.env."
            )
        # Fernet validates the key's format and length here, so a
        # malformed key fails loudly at configuration time rather than
        # silently producing unreadable ciphertext.
        _fernet = Fernet(key.encode() if isinstance(key, str) else key)

    return _fernet


def encrypt(text):
    """Encrypt *text* and return the token as a ``str``.

    An empty or ``None`` input returns ``""`` unchanged — there is
    nothing secret about the absence of a token, and it keeps "no token
    stored" distinguishable from "a token that decrypts to nothing".
    """
    if not text:
        return ""
    return _get_fernet().encrypt(text.encode()).decode()


def decrypt(token):
    """Decrypt *token* and return the original ``str``.

    Empty input returns ``""``.  Anything else that cannot be decrypted —
    a truncated value, or one encrypted under a different
    ``FIELD_ENCRYPTION_KEY`` — raises ``cryptography.fernet.InvalidToken``
    rather than being swallowed, because quietly returning ``""`` would
    make a key mix-up look exactly like a mailbox that was never
    connected.
    """
    if not token:
        return ""
    return _get_fernet().decrypt(token.encode()).decode()
