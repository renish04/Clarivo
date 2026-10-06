"""
Django models for Gmail connections.

These are the one part of the mail feature that *does* belong in
Django's own database: a Gmail connection is account state, not document
data.  Everything an ingested email produces — the ``EMAIL#`` records,
the correspondence documents, the attachments — lives in DynamoDB and S3
with the rest of the project's documents.
"""

from django.contrib.auth.models import User
from django.db import models

from .crypto import decrypt, encrypt


class GmailAccount(models.Model):
    """One connected Gmail mailbox, owned by one Clarivo user.

    A ``OneToOneField`` rather than a foreign key: Clarivo ingests from
    "the user's inbox", singular, and the sync code would otherwise have
    to decide which of several mailboxes an incoming notification
    belongs to on every call.
    """

    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name="gmail_account",
    )

    # The Gmail address itself.  Unique because a Pub/Sub notification
    # identifies the mailbox *only* by its email address, so this is the
    # column the webhook looks the account up by — two users sharing one
    # address would make that lookup ambiguous.
    email = models.EmailField(unique=True)

    # Never written to directly; go through the refresh_token property
    # below, which encrypts on the way in and decrypts on the way out.
    refresh_token_encrypted = models.TextField()

    # The short-lived access token and its expiry are cached so that a
    # sync triggered seconds after the last one does not have to make a
    # round trip to Google's token endpoint first.  Not encrypted: it is
    # worth an hour at most, and it is refreshed from the refresh token,
    # which is the credential that actually needs protecting.
    access_token = models.TextField(blank=True)
    token_expiry = models.DateTimeField(null=True, blank=True)

    # The Gmail history cursor.  Every sync asks history.list for
    # everything that changed since this ID, then advances it — which is
    # what makes the sync idempotent in the face of the duplicate and
    # out-of-order notifications Pub/Sub is allowed to deliver.
    history_id = models.CharField(max_length=64, blank=True)

    # When the current users.watch registration lapses.  Gmail expires a
    # watch after seven days, and silently stops pushing once it does, so
    # this is what tells us a watch needs renewing before that happens.
    watch_expiration = models.DateTimeField(null=True, blank=True)

    last_synced_at = models.DateTimeField(null=True, blank=True)

    # Set when Google rejects our refresh token — the user revoked
    # access, or changed their password.  No amount of retrying fixes
    # that; the user has to walk through consent again, so the sync stops
    # trying and the UI prompts them to reconnect.
    needs_reconnect = models.BooleanField(default=False)

    connected_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.email} ({self.user.username})"

    @property
    def refresh_token(self):
        """The refresh token in plain text, decrypted on read."""
        return decrypt(self.refresh_token_encrypted)

    @refresh_token.setter
    def refresh_token(self, value):
        """Encrypt *value* and store it in ``refresh_token_encrypted``.

        Assigning the plain token is all the calling code ever does, so
        the unencrypted value never reaches a model field and cannot be
        written to the database by accident.
        """
        self.refresh_token_encrypted = encrypt(value)


class OAuthState(models.Model):
    """A one-time ``state`` value pending an OAuth callback.

    Google's callback arrives as a plain browser redirect: no
    Authorization header, so no DRF token, so no ``request.user``.  The
    ``state`` parameter is the only thing that comes back under our
    control, and it has to do two jobs — prove the callback answers a
    request *we* started (CSRF protection, which is why it must be
    unguessable) and tell us which Clarivo user it belongs to.

    Rather than encode the user into the state value, the row stores it:
    the state stays opaque, and the callback resolves it by lookup.
    """

    # unique=True is also what indexes this column — the callback's only
    # query is an exact-match lookup on it.
    state = models.CharField(max_length=128, unique=True)

    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="oauth_states",
    )

    # Where to send the browser once consent is done, so the user lands
    # back on the page they started from instead of a generic screen.
    return_to = models.CharField(max_length=500, blank=True)

    # Used to expire states that were never redeemed — an abandoned
    # consent screen should not leave a valid state lying around forever.
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"OAuth state for {self.user.username} ({self.created_at:%Y-%m-%d %H:%M})"
