import uuid

import boto3
from django.conf import settings
from rest_framework import serializers, status
from rest_framework.authentication import TokenAuthentication
from rest_framework.generics import get_object_or_404
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from projects.models import Project


class PresignRequestSerializer(serializers.Serializer):
    """Validates the incoming body for a presigned-URL request."""

    filename = serializers.CharField(max_length=255)
    content_type = serializers.CharField(max_length=127)


class PresignUploadView(APIView):
    """
    POST /api/projects/<project_id>/documents/presign/

    Returns a presigned S3 PUT URL so the browser can upload the file
    directly to S3 — the file bytes never pass through Django.
    """

    authentication_classes = [TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request, project_id):
        # Verify the project exists and belongs to the requesting user.
        project = get_object_or_404(
            Project.objects.filter(owner=request.user),
            pk=project_id,
        )

        serializer = PresignRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        filename = serializer.validated_data["filename"]
        content_type = serializer.validated_data["content_type"]

        doc_id = str(uuid.uuid4())
        s3_key = f"projects/{project.pk}/documents/{doc_id}/{filename}"

        s3_client = boto3.client(
            "s3",
            region_name=settings.AWS_REGION,
            aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
            aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
        )

        upload_url = s3_client.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": settings.S3_BUCKET_NAME,
                "Key": s3_key,
                "ContentType": content_type,
            },
            ExpiresIn=300,  # 5 minutes
        )

        return Response(
            {"upload_url": upload_url, "s3_key": s3_key, "doc_id": doc_id},
            status=status.HTTP_200_OK,
        )


class ConfirmRequestSerializer(serializers.Serializer):
    """Validates the incoming body for an upload-confirmation request."""

    doc_id = serializers.CharField()
    filename = serializers.CharField(max_length=255)
    s3_key = serializers.CharField()
    file_type = serializers.CharField(max_length=127)


class ConfirmUploadView(APIView):
    """
    POST /api/projects/<project_id>/documents/confirm/

    Called by the frontend after a successful S3 upload.  Creates the
    document record in DynamoDB and hands the document to the background
    ingestion worker, which carries it through extraction, embedding and
    classification without any further calls from the client.

    Returns immediately — the client watches progress by polling the
    document list.
    """

    authentication_classes = [TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request, project_id):
        # Verify the project exists and belongs to the requesting user.
        project = get_object_or_404(
            Project.objects.filter(owner=request.user),
            pk=project_id,
        )

        serializer = ConfirmRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        from documents.dynamo import confirm_document
        from documents.pipeline import enqueue_document

        doc_id = serializer.validated_data["doc_id"]

        item = confirm_document(
            project_id=project.pk,
            doc_id=doc_id,
            filename=serializer.validated_data["filename"],
            s3_key=serializer.validated_data["s3_key"],
            file_type=serializer.validated_data["file_type"],
        )

        enqueue_document(project.pk, doc_id)

        return Response(item, status=status.HTTP_201_CREATED)


class DocumentListView(APIView):
    """
    GET /api/projects/<project_id>/documents/

    Returns all document records for the project from DynamoDB, each
    enriched with a short-lived presigned GET URL (``view_url``).

    This is also the endpoint the frontend polls while an upload is being
    processed, so it doubles as the recovery point: any document left
    mid-pipeline by a server restart is re-queued here.
    """

    authentication_classes = [TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request, project_id):
        # Verify the project exists and belongs to the requesting user.
        project = get_object_or_404(
            Project.objects.filter(owner=request.user),
            pk=project_id,
        )

        from documents.dynamo import list_documents
        from documents.pipeline import resume_stalled_documents

        items = list_documents(project.pk)

        # Pass the items we already have — this costs no extra read, and
        # documents the worker is already handling are skipped by the
        # in-flight guard.
        resume_stalled_documents(project.pk, items)

        s3_client = boto3.client(
            "s3",
            region_name=settings.AWS_REGION,
            aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
            aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
        )

        for item in items:
            # Not every document is a file.  A correspondence record is
            # the text of an ingested email: it has a body but no object
            # in the bucket, so there is nothing to presign and no
            # ``s3_key`` to presign it from.  Asking for one unguarded
            # would raise a KeyError and take the whole Files tab down
            # for any project that has ever ingested an email.
            s3_key = item.get("s3_key")
            item["view_url"] = (
                s3_client.generate_presigned_url(
                    "get_object",
                    Params={
                        "Bucket": settings.S3_BUCKET_NAME,
                        "Key": s3_key,
                    },
                    ExpiresIn=600,  # 10 minutes
                )
                if s3_key
                else None
            )

        return Response(items, status=status.HTTP_200_OK)


class EmbedDocumentView(APIView):
    """
    POST /api/projects/<project_id>/documents/<doc_id>/embed/

    Chunks and embeds the document's extracted body into Weaviate and
    moves it to ``embedded``.

    Delegates to ``documents.services.embed_and_mark``, which is the same
    function the background pipeline and the email sync call — so a
    document claimed by one of them is left alone here rather than
    embedded a second time.
    """

    authentication_classes = [TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request, project_id, doc_id):
        # Verify the project exists and belongs to the requesting user.
        get_object_or_404(
            Project.objects.filter(owner=request.user),
            pk=project_id,
        )

        from documents.dynamo import get_document
        from documents.services import embed_and_mark

        doc = get_document(project_id, doc_id)
        if doc is None:
            return Response(
                {"detail": "Document not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        if not doc.get("body", ""):
            return Response(
                {"detail": "Document has no extracted text yet."},
                status=status.HTTP_409_CONFLICT,
            )

        embed_and_mark(project_id, doc_id)

        # Returned whether or not this call did the embedding: either way
        # the record now reflects the truth, and a caller that lost the
        # claim wants the current state, not an error.
        return Response(get_document(project_id, doc_id), status=status.HTTP_200_OK)


class ClassifyDocumentView(APIView):
    """
    POST /api/projects/<project_id>/documents/<doc_id>/classify/

    Calls Gemini to classify the document and updates the status to 'classified'.

    Delegates to ``documents.services.classify_and_mark``, so this shares
    the claim with the background pipeline and the email sync and cannot
    classify a document another caller is already working on.
    """

    authentication_classes = [TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request, project_id, doc_id):
        # Verify the project exists and belongs to the requesting user.
        get_object_or_404(
            Project.objects.filter(owner=request.user),
            pk=project_id,
        )

        from documents.dynamo import get_document
        from documents.services import classify_and_mark

        doc = get_document(project_id, doc_id)
        if doc is None:
            return Response(
                {"detail": "Document not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        classify_and_mark(project_id, doc_id)

        # Return the updated document to the client.
        updated_doc = get_document(project_id, doc_id)
        return Response(updated_doc, status=status.HTTP_200_OK)
