"""
Full teardown of a project's data across every store it touches.

A project is only a row in SQLite, but its actual contents live in
three places that know nothing about Django: the uploaded files in S3,
the document and chat records in DynamoDB, and the embedded chunks in
Weaviate.  Deleting the row alone leaves all three behind with nothing
left pointing at them — they are keyed by ``project_id``, and once the
row is gone that id is never issued again.

So deletion runs here first, and the caller only removes the Django
row once this reports no failures.  If a store is unreachable the
project stays exactly as it was and the delete can simply be retried:
every step below is idempotent, so a retry after a partial success
finishes the job rather than erroring on what is already gone.
"""

import logging

import boto3
from django.conf import settings

logger = logging.getLogger(__name__)


def delete_project_files(project_id):
    """
    Delete every S3 object belonging to a project.

    Uploads are keyed ``projects/<project_id>/documents/<doc_id>/<name>``
    (see ``PresignUploadView``), so one prefix covers the project.

    Returns the number of objects deleted.
    """
    s3_client = boto3.client(
        "s3",
        region_name=settings.AWS_REGION,
        aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
    )

    bucket = settings.S3_BUCKET_NAME
    prefix = f"projects/{project_id}/"

    deleted = 0
    paginator = s3_client.get_paginator("list_objects_v2")

    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        contents = page.get("Contents", [])
        if not contents:
            continue

        # DeleteObjects takes at most 1000 keys, which is exactly the
        # page size the paginator yields.
        response = s3_client.delete_objects(
            Bucket=bucket,
            Delete={"Objects": [{"Key": obj["Key"]} for obj in contents]},
        )
        deleted += len(response.get("Deleted", []))

        for failure in response.get("Errors", []):
            # Raised rather than swallowed: a key left in the bucket is
            # exactly the orphan this module exists to prevent.
            raise RuntimeError(
                f"S3 refused to delete {failure.get('Key')}: {failure.get('Message')}"
            )

    return deleted


def delete_project_data(project_id):
    """
    Remove a project's data from S3, DynamoDB and Weaviate.

    Every store is attempted even if an earlier one fails, so a single
    outage does not hide how much else would also need cleaning up.

    Returns
    -------
    tuple[dict, dict]
        ``(deleted, errors)`` — how many objects each store gave up,
        and the error message for any store that failed.  An empty
        *errors* dict means the project is fully cleaned and the
        caller may delete the Django row.
    """
    from documents.dynamo import delete_project_items
    from documents.weaviate_client import delete_project_chunks

    deleted = {}
    errors = {}

    steps = (
        ("s3", lambda: delete_project_files(project_id)),
        ("dynamodb", lambda: delete_project_items(project_id)),
        ("weaviate", lambda: delete_project_chunks(str(project_id))),
    )

    for name, step in steps:
        try:
            deleted[name] = step()
            print(f"[PROJECT CLEANUP] {name}: deleted {deleted[name]} object(s).")
        except Exception as e:
            logger.exception(
                f"Project {project_id} cleanup failed while clearing {name}: {e}"
            )
            print(f"[PROJECT CLEANUP] ERROR: {name} cleanup failed: {e}")
            errors[name] = str(e)

    return deleted, errors
