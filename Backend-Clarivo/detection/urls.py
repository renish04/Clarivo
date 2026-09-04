from django.urls import path
from .views import CheckProjectView

urlpatterns = [
    path("check/", CheckProjectView.as_view(), name="project-check"),
]
