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

