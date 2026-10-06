"""
Project-scoped mail routes.

Kept separate from ``mail/urls.py`` because the two sets live at
different places in the URL tree: the Gmail *connection* endpoints are
per-user and sit under ``/api/gmail/``, while these are per-project and
nest under ``/api/projects/<project_id>/mail/``, matching how the
documents and detection apps are mounted.
"""

from django.urls import path

from .views import MailThreadDetailView, MailThreadListView

urlpatterns = [
    path("threads/", MailThreadListView.as_view(), name="mail-thread-list"),
    path(
        "threads/<str:thread_id>/",
        MailThreadDetailView.as_view(),
        name="mail-thread-detail",
    ),
]
