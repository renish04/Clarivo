from rest_framework import status
from rest_framework.authentication import TokenAuthentication
from rest_framework.generics import get_object_or_404
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from projects.models import Project
from documents.dynamo import list_documents
from detection.services import run_detection


class CheckProjectView(APIView):
    """
    POST /api/projects/<project_id>/check/

    For every invoice in 'classified' status, run discrepancy detection
    and update it to 'checked' with the findings.

    The work itself lives in ``detection.services.run_detection`` so that
    an email sync can run exactly the same check with no browser
    involved.  This view only does the ownership check.
    """
    authentication_classes = [TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request, project_id):
        # Verify the project exists and belongs to the requesting user.
        get_object_or_404(
            Project.objects.filter(owner=request.user),
            pk=project_id,
        )

        summary = run_detection(project_id)
        return Response(summary, status=status.HTTP_200_OK)


class DiscrepancyTableView(APIView):
    """
    GET /api/projects/<project_id>/discrepancy-table/

    Returns a combined markdown table of all checked documents.
    """
    authentication_classes = [TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request, project_id):
        # Verify the project exists and belongs to the requesting user.
        get_object_or_404(
            Project.objects.filter(owner=request.user),
            pk=project_id,
        )

        all_docs = list_documents(project_id)
        checked_docs = [doc for doc in all_docs if doc.get("status") == "checked"]

        summary = {
            "clean": 0,
            "flagged": 0,
            "auto_resolved": 0,
            "needs_more_info": 0
        }

        for doc in checked_docs:
            d_status = doc.get("discrepancy_status")
            if d_status in summary:
                summary[d_status] += 1

        total_checked = sum(summary.values())
        if total_checked == 0:
            summary["touchless_rate"] = None
        else:
            touchless = summary["clean"] + summary["auto_resolved"]
            summary["touchless_rate"] = round((touchless / total_checked) * 100, 1)

        docs_with_markdown = [doc for doc in checked_docs if doc.get("table_row_markdown")]

        if not docs_with_markdown:
            combined_markdown = ""
        else:
            # Assemble the markdown table
            header = "| Document | Status | Issue | Details |\n|---|---|---|---|\n"
            rows = []
            for doc in docs_with_markdown:
                row = doc.get("table_row_markdown", "").strip()
                if row:
                    rows.append(row)
            combined_markdown = header + "\n".join(rows)

        return Response({
            "markdown": combined_markdown,
            "summary": summary
        }, status=status.HTTP_200_OK)

