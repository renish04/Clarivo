"""
Admin registration for the Gmail connection models.

Both models hold OAuth credentials, so every field that is or contains a
token is kept off the admin entirely — not merely out of the list column
but out of the change form too.  The Django admin is a convenience for
inspecting connection *state* ("is this mailbox connected, when did it
last sync, does it need reconnecting"); it is not somewhere a refresh
token should ever be rendered into an HTML page, logged by a request
middleware, or editable by hand.
"""

from django.contrib import admin

from .models import GmailAccount, OAuthState


@admin.register(GmailAccount)
class GmailAccountAdmin(admin.ModelAdmin):
    list_display = (
        "email",
        "user",
        "needs_reconnect",
        "last_synced_at",
        "watch_expiration",
        "connected_at",
    )
    list_filter = ("needs_reconnect",)
    search_fields = ("email", "user__username")

    # refresh_token_encrypted and access_token are deliberately absent.
    fields = (
        "user",
        "email",
        "history_id",
        "token_expiry",
        "watch_expiration",
        "last_synced_at",
        "needs_reconnect",
        "connected_at",
    )
    readonly_fields = ("connected_at",)


@admin.register(OAuthState)
class OAuthStateAdmin(admin.ModelAdmin):
    # The state value is a CSRF token in flight: showing it would let
    # anyone with admin access complete someone else's OAuth handshake,
    # so only the fact that a handshake is pending is displayed.
    list_display = ("user", "return_to", "created_at")
    search_fields = ("user__username",)

    fields = ("user", "return_to", "created_at")
    readonly_fields = ("created_at",)
