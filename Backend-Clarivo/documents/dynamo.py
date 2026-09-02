"""
Thin wrapper around boto3 DynamoDB for document metadata.

All document data lives in DynamoDB — Django's own database is never
used for document content.  The table name and AWS region are read
from Django settings (which load from .env).
"""

from datetime import datetime, timezone

import boto3
from django.conf import settings

# Module-level resource — created once on first import, then reused.
_dynamodb = boto3.resource(
    "dynamodb",
    region_name=settings.AWS_REGION,
    aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
    aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
)
_table = _dynamodb.Table(settings.DYNAMODB_TABLE_NAME)


def put_document_stub(project_id, doc_id, filename, s3_key, file_type):
    """
    Write a new document record to DynamoDB with status
    ``pending_extraction``.

    Parameters
    ----------
    project_id : int | str
        The Django project PK.
    doc_id : str
        A UUID (as a string) uniquely identifying this document.
    filename : str
        Original filename the user uploaded.
    s3_key : str
        Full S3 object key, e.g.
        ``projects/<project_id>/documents/<doc_id>/<filename>``.
    file_type : str
        MIME type or extension (e.g. ``application/pdf``).
    """
    item = {
        "PK": f"PROJECT#{project_id}",
        "SK": f"DOC#{doc_id}",
        "filename": filename,
        "s3_key": s3_key,
        "file_type": file_type,
        "status": "pending_extraction",
        "uploaded_at": datetime.now(timezone.utc).isoformat(),
    }
    _table.put_item(Item=item)
    return item


def list_documents(project_id):
    """
    Query all document records for a given project.

    Returns a list of plain dicts (one per document).
    """
    response = _table.query(
        KeyConditionExpression="PK = :pk",
        ExpressionAttributeValues={":pk": f"PROJECT#{project_id}"},
    )
    return response.get("Items", [])

