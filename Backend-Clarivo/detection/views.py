import time

from rest_framework import status
from rest_framework.authentication import TokenAuthentication
from rest_framework.generics import get_object_or_404
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from projects.models import Project
from documents.dynamo import list_documents, update_document_check_results
from detection.detect import detect_discrepancies


class CheckProjectView(APIView):
    """
    POST /api/projects/<project_id>/check/

    For every invoice in 'classified' status, run discrepancy detection
    and update it to 'checked' with the findings.
    """
    authentication_classes = [TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request, project_id):
        print(f"\n=== [CHECK ENDPOINT] Triggered for Project {project_id} ===")
        # Verify the project exists and belongs to the requesting user.
        get_object_or_404(
            Project.objects.filter(owner=request.user),
            pk=project_id,
        )

        all_docs = list_documents(project_id)
        
        # Filter for invoice & classified
        invoices_to_check = [
            doc for doc in all_docs
            if doc.get("doc_type") == "invoice" and doc.get("status") == "classified"
        ]
        
        print(f"[CHECK ENDPOINT] Found {len(invoices_to_check)} invoices waiting to be checked.")

        summary = {
            "clean": 0,
            "flagged": 0,
            "auto_resolved": 0,
            "needs_more_info": 0
        }

        for i, doc in enumerate(invoices_to_check):
            doc_id = doc["SK"].replace("DOC#", "")
            print(f"\n[CHECK ENDPOINT] Processing invoice {i+1}/{len(invoices_to_check)} (Doc {doc_id})...")
            
            # Detect discrepancies (verify_grounding is already baked inside detect_discrepancies)
            result = detect_discrepancies(project_id, doc_id)
            
            discrepancy_status = result.get("status", "needs_more_info")
            
            # Update counts safely
            if discrepancy_status in summary:
                summary[discrepancy_status] += 1
            else:
                summary["needs_more_info"] += 1
                discrepancy_status = "needs_more_info"

            print(f"[CHECK ENDPOINT] Saving results to DynamoDB (Status: {discrepancy_status})...")
            # Write back to DynamoDB
            update_document_check_results(
                project_id=project_id,
                doc_id=doc_id,
                discrepancy_status=discrepancy_status,
                findings=result.get("findings", []),
                resolution=result.get("resolution", ""),
                table_row_markdown=result.get("table_row_markdown", ""),
                new_status="checked"
            )

            # Gemini free-tier rate limiting safety (delay 2 seconds between docs, except after the last one)
            if i < len(invoices_to_check) - 1:
                print(f"[CHECK ENDPOINT] Waiting 2 seconds to respect rate limits...")
                time.sleep(2)

        print(f"\n=== [CHECK ENDPOINT] Complete. Summary: {summary} ===\n")
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
        checked_docs = [doc for doc in all_docs if doc.get("status") == "checked" and doc.get("table_row_markdown")]

        if not checked_docs:
            return Response({"markdown": ""}, status=status.HTTP_200_OK)

        # Assemble the markdown table
        header = "| Document | Status | Issue | Details |\n|---|---|---|---|\n"
        rows = []
        for doc in checked_docs:
            row = doc.get("table_row_markdown", "").strip()
            if row:
                rows.append(row)

        combined_markdown = header + "\n".join(rows)

        return Response({"markdown": combined_markdown}, status=status.HTTP_200_OK)

