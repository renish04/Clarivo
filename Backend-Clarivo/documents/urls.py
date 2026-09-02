from django.urls import path

from .views import PresignUploadView

urlpatterns = [
    path("presign/", PresignUploadView.as_view(), name="document-presign"),
]

