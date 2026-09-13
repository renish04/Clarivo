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


def confirm_document(project_id, doc_id, filename, s3_key, file_type):
    """
    Record the metadata for a freshly uploaded document.

    This deliberately uses ``update_item`` rather than ``put_item``.  The
    S3 PutObject event fires the extraction Lambda the moment the browser
    finishes uploading, which can beat this call — and a ``put_item`` is
    a whole-item replace, so it would overwrite the ``body`` and
    ``extracted`` status the Lambda had just written, stranding the
    document at ``pending_extraction`` forever.

    Writing only the metadata attributes leaves ``body`` untouched, and
    ``if_not_exists`` on the status sets ``pending_extraction`` only when
    the Lambda has not already moved the document past it.

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

    Returns
    -------
    dict
        The full updated item.
    """
    response = _table.update_item(
        Key={
            "PK": f"PROJECT#{project_id}",
            "SK": f"DOC#{doc_id}",
        },
        UpdateExpression=(
            "SET filename = :filename, s3_key = :s3_key, "
            "file_type = :file_type, uploaded_at = :uploaded_at, "
            "#s = if_not_exists(#s, :pending)"
        ),
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":filename": filename,
            ":s3_key": s3_key,
            ":file_type": file_type,
            ":uploaded_at": datetime.now(timezone.utc).isoformat(),
            ":pending": "pending_extraction",
        },
        ReturnValues="ALL_NEW",
    )
    return response.get("Attributes")


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


def get_document(project_id, doc_id):
    """
    Fetch a single document record from DynamoDB.

    Returns the item dict, or ``None`` if no matching record exists.
    """
    response = _table.get_item(
        Key={
            "PK": f"PROJECT#{project_id}",
            "SK": f"DOC#{doc_id}",
        },
    )
    return response.get("Item")


def update_document_status(project_id, doc_id, new_status):
    """
    Set the ``status`` attribute on an existing document record.

    Returns the full updated item dict.
    """
    response = _table.update_item(
        Key={
            "PK": f"PROJECT#{project_id}",
            "SK": f"DOC#{doc_id}",
        },
        UpdateExpression="SET #s = :status",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":status": new_status},
        ReturnValues="ALL_NEW",
    )
    return response.get("Attributes")


def update_document_classification(project_id, doc_id, doc_type, new_status, supplier=None):
    """
    Set the doc_type, status, and optionally supplier attributes on an existing document record.

    Returns the full updated item dict.
    """
    if supplier:
        update_expr = "SET #t = :doc_type, #s = :status, supplier = :supplier"
        expr_vals = {":doc_type": doc_type, ":status": new_status, ":supplier": supplier}
    else:
        update_expr = "SET #t = :doc_type, #s = :status"
        expr_vals = {":doc_type": doc_type, ":status": new_status}

    response = _table.update_item(
        Key={
            "PK": f"PROJECT#{project_id}",
            "SK": f"DOC#{doc_id}",
        },
        UpdateExpression=update_expr,
        ExpressionAttributeNames={"#t": "doc_type", "#s": "status"},
        ExpressionAttributeValues=expr_vals,
        ReturnValues="ALL_NEW",
    )
    return response.get("Attributes")

def update_document_check_results(project_id, doc_id, discrepancy_status, findings, resolution, table_row_markdown, new_status):
    """
    Save the results from detect_discrepancies into the document record,
    and update its status to 'checked'.
    """
    response = _table.update_item(
        Key={
            "PK": f"PROJECT#{project_id}",
            "SK": f"DOC#{doc_id}",
        },
        UpdateExpression="SET discrepancy_status = :ds, findings = :f, resolution = :r, table_row_markdown = :trm, #s = :status",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":ds": discrepancy_status,
            ":f": findings,
            ":r": resolution,
            ":trm": table_row_markdown,
            ":status": new_status
        },
        ReturnValues="ALL_NEW",
    )
    return response.get("Attributes")

def reset_document_check_results(project_id, doc_id):
    """
    Reset a checked document back to classified, removing previous check results.
    """
    response = _table.update_item(
        Key={
            "PK": f"PROJECT#{project_id}",
            "SK": f"DOC#{doc_id}",
        },
        UpdateExpression="SET #s = :status REMOVE discrepancy_status, findings, resolution, table_row_markdown",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":status": "classified"
        },
        ReturnValues="ALL_NEW",
    )
    return response.get("Attributes")
