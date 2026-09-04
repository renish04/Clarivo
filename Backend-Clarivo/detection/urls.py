from django.urls import path
from .views import CheckProjectView, DiscrepancyTableView

urlpatterns = [
    path("check/", CheckProjectView.as_view(), name="project-check"),
    path("discrepancy-table/", DiscrepancyTableView.as_view(), name="project-discrepancy-table"),
]
