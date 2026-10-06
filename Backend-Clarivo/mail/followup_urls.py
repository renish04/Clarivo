"""Project-scoped follow-up routes.

A third URL module in this app, for the same reason there is a second:
these mount somewhere else in the tree. ``mail/urls.py`` holds the
per-user Gmail connection endpoints under ``/api/gmail/``,
``mail/project_urls.py`` the inbox under
``/api/projects/<project_id>/mail/``, and these sit at
``/api/projects/<project_id>/followups/``.

Follow-ups are not under ``/mail/`` deliberately. Gmail is how a
follow-up leaves the building, but a follow-up is a step in checking an
invoice, not a feature of the mailbox -- and if sending ever moved to
another provider, these URLs should not have to change.
"""

from django.urls import path

from .views import FollowupDraftView, FollowupListView, FollowupSendView

urlpatterns = [
    path("", FollowupListView.as_view(), name="followup-list"),
    path(
        "<str:doc_id>/draft/",
        FollowupDraftView.as_view(),
        name="followup-draft",
    ),
    path(
        "<str:doc_id>/send/",
        FollowupSendView.as_view(),
        name="followup-send",
    ),
]
