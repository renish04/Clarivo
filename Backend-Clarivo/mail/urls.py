from django.urls import path

from .views import (
    GmailDisconnectView,
    GmailOAuthCallbackView,
    GmailOAuthStartView,
    GmailPushView,
    GmailStatusView,
    GmailSyncNowView,
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
    # The Pub/Sub push subscription posts here, with ?token=<secret>
    # appended to the endpoint URL configured in Google Cloud.
    path("push/", GmailPushView.as_view(), name="gmail-push"),
    path("sync/", GmailSyncNowView.as_view(), name="gmail-sync"),
    path("disconnect/", GmailDisconnectView.as_view(), name="gmail-disconnect"),
]
