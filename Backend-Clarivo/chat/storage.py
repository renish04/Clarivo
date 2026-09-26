"""
Chat history persistence for "Ask Clarivo".

Conversations live in the same ``clarivo-documents`` DynamoDB table as
the documents they are about, under the same ``PROJECT#<id>`` partition
key.  Keeping them in one partition means a project's documents and its
chat threads are fetched from the same place with the same credentials,
and a message is never orphaned from the project it belongs to.

What separates them is the sort key.  Documents use ``DOC#<doc_id>``;
chat messages use ``CHAT#<doc_id>#<ISO timestamp>``.  Because DynamoDB
sorts items lexicographically by sort key, that layout gives two things
for free: a ``begins_with(SK, "CHAT#<doc_id>#")`` query returns exactly
one document's thread, and — since ISO-8601 timestamps sort in the same
order as the instants they represent — that thread comes back already in
chronological order, with no client-side sorting.
"""

from datetime import datetime, timezone

import boto3
from boto3.dynamodb.conditions import Key
from django.conf import settings

# Module-level resource — created once on first import, then reused.
# Same pattern (and same table) as documents/dynamo.py.
_dynamodb = boto3.resource(
    "dynamodb",
    region_name=settings.AWS_REGION,
    aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
    aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
)
_table = _dynamodb.Table(settings.DYNAMODB_TABLE_NAME)

# Sort-key prefix for chat messages.  Single source of truth so the
# write, read and delete paths can never disagree about the layout.
CHAT_SK_PREFIX = "CHAT#"


def _thread_prefix(doc_id):
    """Return the sort-key prefix shared by every message in one thread."""
    return f"{CHAT_SK_PREFIX}{doc_id}#"


def save_message(project_id, doc_id, role, content, sources=None):
    """
    Append one message to a document's chat thread.

    Parameters
    ----------
    project_id : int | str
        The Django project PK.
    doc_id : str
        The UUID of the document this conversation is scoped to.
    role : str
        ``"user"`` or ``"assistant"``.
    content : str
        The message text.
    sources : list[str] | None
        Filenames the assistant drew on when answering.  Empty for user
        messages; may also be empty for an assistant message that used
        no retrieved context.

    Returns
    -------
    dict
        The item as written, including its keys — so the caller can
        return the saved message straight to the client without a
        second read.
    """
    created_at = datetime.now(timezone.utc).isoformat()

    item = {
        "PK": f"PROJECT#{project_id}",
        "SK": f"{_thread_prefix(doc_id)}{created_at}",
        "role": role,
        "content": content,
        "sources": sources or [],
        "created_at": created_at,
    }

    _table.put_item(Item=item)
    return item


def get_history(project_id, doc_id, limit=20):
    """
    Fetch the most recent messages in a document's chat thread.

    The query runs *backwards* (``ScanIndexForward=False``) so DynamoDB
    can stop after ``limit`` items at the newest end of the thread
    rather than reading it from the beginning; the page is then
    reversed so the caller still receives oldest-first, which is both
    the order a transcript is displayed in and the order an LLM expects
    its conversation history.

    Returns a list of plain dicts, oldest first.
    """
    response = _table.query(
        KeyConditionExpression=(
            Key("PK").eq(f"PROJECT#{project_id}")
            & Key("SK").begins_with(_thread_prefix(doc_id))
        ),
        ScanIndexForward=False,  # newest first, so Limit trims the oldest
        Limit=limit,
    )

    items = response.get("Items", [])
    items.reverse()  # back to chronological order
    return items


def clear_history(project_id, doc_id):
    """
    Delete every message in a document's chat thread.

    Paginates the key query explicitly: a long thread can exceed
    DynamoDB's 1 MB response limit, and a partial delete would leave
    stray messages behind to reappear in the next conversation.

    Returns the number of messages deleted.
    """
    pk = f"PROJECT#{project_id}"
    key_condition = Key("PK").eq(pk) & Key("SK").begins_with(_thread_prefix(doc_id))

    keys = []
    start_key = None
    while True:
        kwargs = {
            "KeyConditionExpression": key_condition,
            # Only the keys are needed to delete — no point paying to
            # read every message body back out.
            "ProjectionExpression": "PK, SK",
        }
        if start_key:
            kwargs["ExclusiveStartKey"] = start_key

        response = _table.query(**kwargs)
        keys.extend(response.get("Items", []))

        start_key = response.get("LastEvaluatedKey")
        if not start_key:
            break

    with _table.batch_writer() as batch:
        for key in keys:
            batch.delete_item(Key={"PK": key["PK"], "SK": key["SK"]})

    return len(keys)
