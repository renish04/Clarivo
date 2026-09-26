from django.urls import path

from .views import ChatSuggestionsView, DocumentChatView

urlpatterns = [
    path("", DocumentChatView.as_view(), name="document-chat"),
    path(
        "suggestions/",
        ChatSuggestionsView.as_view(),
        name="document-chat-suggestions",
    ),
]
