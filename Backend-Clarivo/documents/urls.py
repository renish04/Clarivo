from django.urls import path

from .views import ConfirmUploadView, DocumentListView, PresignUploadView

urlpatterns = [
    path("", DocumentListView.as_view(), name="document-list"),
    path("presign/", PresignUploadView.as_view(), name="document-presign"),
    path("confirm/", ConfirmUploadView.as_view(), name="document-confirm"),
]

