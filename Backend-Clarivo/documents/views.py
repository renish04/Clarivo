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

    Called by the frontend after a successful S3 upload.  Creates a
    document record in DynamoDB with status ``pending_extraction``.
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

        from documents.dynamo import put_document_stub

        item = put_document_stub(
            project_id=project.pk,
            doc_id=serializer.validated_data["doc_id"],
            filename=serializer.validated_data["filename"],
            s3_key=serializer.validated_data["s3_key"],
            file_type=serializer.validated_data["file_type"],
        )

        return Response(item, status=status.HTTP_201_CREATED)


class DocumentListView(APIView):
    """
    GET /api/projects/<project_id>/documents/

    Returns all document records for the project from DynamoDB, each
    enriched with a short-lived presigned GET URL (``view_url``).
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

        items = list_documents(project.pk)

        s3_client = boto3.client(
            "s3",
            region_name=settings.AWS_REGION,
            aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
            aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
        )

        for item in items:
            item["view_url"] = s3_client.generate_presigned_url(
                "get_object",
                Params={
                    "Bucket": settings.S3_BUCKET_NAME,
                    "Key": item["s3_key"],
                },
                ExpiresIn=600,  # 10 minutes
            )

        return Response(items, status=status.HTTP_200_OK)
