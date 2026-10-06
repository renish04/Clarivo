from django.urls import path

from .views import (
    GmailDisconnectView,
    GmailOAuthCallbackView,
    GmailOAuthStartView,
    GmailStatusView,
)

urlpatterns = [
    path("oauth/start/", GmailOAuthStartView.as_view(), name="gmail-oauth-start"),
    # Must match GOOGLE_OAUTH_REDIRECT_URI exactly, and must also be
    # registered verbatim as an authorised redirect URI on the OAuth
    # client in the Google Cloud console.
    path(
        "oauth/callback/",
        GmailOAuthCallbackView.as_view(),
        name="gmail-oauth-callback",
    ),
    path("status/", GmailStatusView.as_view(), name="gmail-status"),
    path("disconnect/", GmailDisconnectView.as_view(), name="gmail-disconnect"),
]
