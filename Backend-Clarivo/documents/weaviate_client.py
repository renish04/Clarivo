"""
Weaviate Cloud client and collection management.

Provides a thin wrapper around the ``weaviate-client`` v4 library,
configured via Django settings (``WEAVIATE_URL``, ``WEAVIATE_API_KEY``).
The connection is created on demand and re-used within each caller's
context-manager block.
"""

import weaviate
from django.conf import settings
from weaviate.classes.config import Configure, DataType, Property
from weaviate.classes.data import DataObject
from weaviate.classes.init import Auth

# Collection name — single source of truth so every module agrees.
COLLECTION_NAME = "DocumentChunk"


def get_weaviate_client() -> weaviate.WeaviateClient:
    """Return a *connected* ``WeaviateClient`` for Weaviate Cloud.

    The caller **must** close the client when finished, either by
    calling ``client.close()`` or (preferred) by using the client as a
    context manager::

        client = get_weaviate_client()
        try:
            ...
        finally:
            client.close()

    Returns
    -------
    weaviate.WeaviateClient
        An already-connected client instance.
    """
    client = weaviate.connect_to_weaviate_cloud(
        cluster_url=settings.WEAVIATE_URL,
        auth_credentials=Auth.api_key(settings.WEAVIATE_API_KEY),
    )
    return client


def ensure_collection_exists(client: weaviate.WeaviateClient) -> None:
    """Create the ``DocumentChunk`` collection if it does not yet exist.

    The collection is configured for **self-provided vectors**
    (``Configure.Vectorizer.none()``) because we compute embeddings
    ourselves with fastembed.

    Properties
    ----------
    project_id : TEXT
        The Django project PK (stored as text).
    doc_id : TEXT
        The UUID of the parent document in DynamoDB.
    chunk_text : TEXT
        The text content of this chunk.
    chunk_index : INT
        Zero-based position of this chunk within its parent document.
    """
    if client.collections.exists(COLLECTION_NAME):
        return

    client.collections.create(
        name=COLLECTION_NAME,
        vectorizer_config=Configure.Vectorizer.none(),
        properties=[
            Property(name="project_id", data_type=DataType.TEXT),
            Property(name="doc_id", data_type=DataType.TEXT),
            Property(name="chunk_text", data_type=DataType.TEXT),
            Property(name="chunk_index", data_type=DataType.INT),
        ],
    )


def embed_document(project_id: str, doc_id: str, body_text: str) -> int:
    """Chunk, embed, and index a document into Weaviate.

    1. Splits *body_text* into overlapping chunks via ``chunk_text``.
    2. Computes a 384-dim embedding for each chunk via ``embed_text``.
    3. Batch-inserts all chunks into the ``DocumentChunk`` collection
       with their self-provided vectors.

    Parameters
    ----------
    project_id : str
        The Django project PK (as a string).
    doc_id : str
        The UUID of the document in DynamoDB.
    body_text : str
        The full extracted text of the document.

    Returns
    -------
    int
        The number of chunks successfully inserted.
    """
    from documents.embeddings import chunk_text, embed_text

    chunks = chunk_text(body_text)

    data_objects = []
    for idx, chunk in enumerate(chunks):
        vector = embed_text(chunk)
        data_objects.append(
            DataObject(
                properties={
                    "project_id": str(project_id),
                    "doc_id": doc_id,
                    "chunk_text": chunk,
                    "chunk_index": idx,
                },
                vector=vector,
            )
        )

    client = get_weaviate_client()
    try:
        ensure_collection_exists(client)
        collection = client.collections.get(COLLECTION_NAME)
        collection.data.insert_many(data_objects)
    finally:
        client.close()

    return len(data_objects)

