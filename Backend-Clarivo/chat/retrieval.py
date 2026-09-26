"""
Retrieval for "Ask Clarivo".

This is deliberately *not* detection's ``get_project_context``.  The two
differ in three ways that matter:

* **The query.** Detection embeds a whole invoice body and asks "what
  else in this project relates to this document?".  Chat embeds the
  user's question, which is short and specific, and asks "what text
  answers this?".
* **The selected document.** Detection excludes the document being
  checked, because including it would just retrieve the claims it is
  trying to verify.  Chat does the opposite — the user picked that
  document and is asking about it, so its own chunks are the single
  most relevant thing in the index and must be retrievable.
* **The budget.** Detection pulls as much of the project as it can
  afford, because a discrepancy can hide anywhere.  Chat takes a small
  top-k, because a focused question does not need the whole project and
  a tight context keeps the answer on-topic.
"""

import logging

from weaviate.classes.query import Filter

from documents.dynamo import get_document_filenames
from documents.embeddings import embed_text
from documents.weaviate_client import COLLECTION_NAME, get_weaviate_client

logger = logging.getLogger(__name__)

# Balance of keyword and vector scoring: 0.0 is pure BM25, 1.0 is pure
# semantic.  An even split suits questions that mix exact tokens the
# user copied off the document (invoice numbers, rates) with wording
# that only matches the source text semantically.
HYBRID_ALPHA = 0.5


def get_chat_context(project_id, selected_doc_id, question, top_k=15):
    """
    Retrieve the text most likely to answer *question*.

    Searches the whole project — including *selected_doc_id* — so the
    assistant can answer both "what does this invoice say?" and "does
    it match the purchase order?" through one retrieval path.

    Parameters
    ----------
    project_id : int | str
        The Django project PK.
    selected_doc_id : str
        The document the conversation is scoped to.  Not used to
        filter — it is recorded here only to make the *inclusion*
        explicit, and to let the caller reason about scope.
    question : str
        The user's message, used as both the vector query and the
        keyword query.
    top_k : int
        Maximum number of chunks to include (default 15).

    Returns
    -------
    tuple[str, list[str]]
        The labelled context string, and the filenames it draws on in
        the order they were first cited.  On a retrieval failure both
        come back empty rather than raising — the assistant can still
        answer from the stored analysis and conversation history.
    """
    print(
        f"\n--- [CHAT RETRIEVAL] Project {project_id}, "
        f"selected doc {selected_doc_id}, top_k={top_k} ---"
    )

    # 1. Embed the *question* — the query here is what the user asked,
    #    not a document body.
    print(f"[CHAT RETRIEVAL] Embedding question ({len(question)} chars)...")
    query_vector = embed_text(question)

    # 2. Hybrid search, scoped to the project but NOT excluding the
    #    selected document.
    client = get_weaviate_client()
    try:
        collection = client.collections.get(COLLECTION_NAME)
        response = collection.query.hybrid(
            query=question,          # BM25 half
            vector=query_vector,     # self-provided embedding, vector half
            alpha=HYBRID_ALPHA,
            limit=top_k,
            filters=Filter.by_property("project_id").equal(str(project_id)),
        )
        objects = response.objects
        print(f"[CHAT RETRIEVAL] SUCCESS: {len(objects)} chunk(s) returned.")
    except Exception as e:
        logger.exception(f"Weaviate hybrid search failed for project {project_id}: {e}")
        print(f"[CHAT RETRIEVAL] ERROR: Weaviate search failed: {e}")
        return "", []
    finally:
        client.close()

    if not objects:
        print("--- [CHAT RETRIEVAL] No chunks matched ---\n")
        return "", []

    # 3. Resolve filenames — one batched read for all distinct doc_ids,
    #    however many chunks each of them contributed.
    chunk_doc_ids = [
        obj.properties.get("doc_id")
        for obj in objects
        if obj.properties.get("doc_id")
    ]
    filenames = get_document_filenames(project_id, chunk_doc_ids)

    # 4. Build one labelled block per chunk, walking the results in
    #    rank order so the strongest match leads.
    context_parts = []
    used_filenames = []

    for obj in objects:
        props = obj.properties
        doc_id = props.get("doc_id")
        chunk = props.get("chunk_text", "")

        if not doc_id or not chunk:
            continue

        filename = filenames.get(doc_id, f"Unknown_{doc_id}")
        context_parts.append(f"[from: {filename}]\n{chunk}\n\n")

        # Ordered and de-duplicated: the caller shows these as the
        # sources behind an answer, so rank order is meaningful.
        if filename not in used_filenames:
            used_filenames.append(filename)

    context_string = "".join(context_parts)
    print(
        f"[CHAT RETRIEVAL] Built context from {len(context_parts)} chunk(s) "
        f"across {len(used_filenames)} document(s): {used_filenames}"
    )
    print("--- [CHAT RETRIEVAL] Complete ---\n")

    # 5.
    return context_string, used_filenames
