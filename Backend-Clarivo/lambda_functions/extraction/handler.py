"""
AWS Lambda handler — text extraction for uploaded documents.

Triggered by S3 PutObject events.  Downloads the file, extracts raw
text (PDF only for now), and updates the matching DynamoDB record.

Environment variables
---------------------
DYNAMODB_TABLE_NAME : str
    Name of the DynamoDB table (e.g. ``clarivo-documents``).
AWS_REGION : str
    AWS region for the DynamoDB table (e.g. ``ap-south-1``).
"""

import json
import logging
import os
import re
import tempfile
import urllib.parse

import boto3
from pypdf import PdfReader

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Pattern: projects/<project_id>/documents/<doc_id>/<filename>
S3_KEY_PATTERN = re.compile(
    r"^projects/(?P<project_id>[^/]+)/documents/(?P<doc_id>[^/]+)/(?P<filename>.+)$"
)

# Initialise AWS clients once per container (re-used across invocations).
s3_client = boto3.client("s3")
dynamodb = boto3.resource("dynamodb", region_name=os.environ.get("AWS_REGION"))
table = dynamodb.Table(os.environ.get("DYNAMODB_TABLE_NAME", "clarivo-documents"))

IMAGE_EXTENSIONS = {".jpg", ".jpeg"}


def _extract_text_from_pdf(file_path: str) -> str:
    """Return all text from a PDF, page by page."""
    reader = PdfReader(file_path)
    pages_text = []
    for page in reader.pages:
        text = page.extract_text()
        if text:
            pages_text.append(text)
    return "\n".join(pages_text)


def _parse_s3_key(key: str) -> dict:
    """
    Parse the S3 object key into project_id, doc_id, and filename.

    Raises ValueError if the key doesn't match the expected pattern.
    """
    match = S3_KEY_PATTERN.match(key)
    if not match:
        raise ValueError(f"S3 key does not match expected pattern: {key}")
    return match.groupdict()


def lambda_handler(event, context):
    """Entry-point invoked by the Lambda runtime on each S3 event."""
    try:
        # -- 1. Read bucket and key from the S3 event -----------------
        record = event["Records"][0]
        bucket = record["s3"]["bucket"]["name"]
        raw_key = record["s3"]["object"]["key"]

        # S3 event keys are URL-encoded (spaces → +, special chars → %xx).
        key = urllib.parse.unquote_plus(raw_key)

        logger.info("Processing s3://%s/%s", bucket, key)

        # -- 2. Parse project_id, doc_id, filename --------------------
        parts = _parse_s3_key(key)
        project_id = parts["project_id"]
        doc_id = parts["doc_id"]
        filename = parts["filename"]

        logger.info(
            "Parsed key → project_id=%s, doc_id=%s, filename=%s",
            project_id,
            doc_id,
            filename,
        )

        # -- 3. Download the object from S3 ---------------------------
        _, ext = os.path.splitext(filename)
        ext = ext.lower()

        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
            tmp_path = tmp.name

        try:
            s3_client.download_file(bucket, key, tmp_path)
            logger.info("Downloaded to %s", tmp_path)

            # -- 4. Extract text or mark for OCR ----------------------
            if ext == ".pdf":
                body = _extract_text_from_pdf(tmp_path)
                new_status = "extracted"
                logger.info(
                    "Extracted %d characters of text from PDF", len(body)
                )
            elif ext in IMAGE_EXTENSIONS:
                body = None
                new_status = "pending_ocr"
                logger.info("Image file detected — marked as pending_ocr")
            else:
                body = None
                new_status = "pending_ocr"
                logger.info(
                    "Unsupported extension '%s' — marked as pending_ocr", ext
                )
        finally:
            # Always clean up the temp file.
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

        # -- 5. Update DynamoDB record --------------------------------
        update_expr_parts = ["#s = :status"]
        attr_names = {"#s": "status"}
        attr_values = {":status": new_status}

        if body is not None:
            update_expr_parts.append("body = :body")
            attr_values[":body"] = body

        table.update_item(
            Key={
                "PK": f"PROJECT#{project_id}",
                "SK": f"DOC#{doc_id}",
            },
            UpdateExpression="SET " + ", ".join(update_expr_parts),
            ExpressionAttributeNames=attr_names,
            ExpressionAttributeValues=attr_values,
        )

        logger.info(
            "DynamoDB updated — PK=PROJECT#%s, SK=DOC#%s, status=%s",
            project_id,
            doc_id,
            new_status,
        )

        return {
            "statusCode": 200,
            "body": json.dumps(
                {
                    "project_id": project_id,
                    "doc_id": doc_id,
                    "status": new_status,
                }
            ),
        }

    except Exception:
        logger.exception("Extraction Lambda failed")
        raise

