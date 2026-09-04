from django.urls import path

from .views import ConfirmUploadView, DocumentListView, EmbedDocumentView, PresignUploadView

urlpatterns = [
    path("", DocumentListView.as_view(), name="document-list"),
    path("presign/", PresignUploadView.as_view(), name="document-presign"),
    path("confirm/", ConfirmUploadView.as_view(), name="document-confirm"),
    path("<str:doc_id>/embed/", EmbedDocumentView.as_view(), name="document-embed"),
]

