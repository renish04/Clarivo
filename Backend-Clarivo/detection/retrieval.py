import logging
from weaviate.classes.query import Filter
from documents.embeddings import embed_text
from documents.weaviate_client import get_weaviate_client, COLLECTION_NAME
from documents.dynamo import get_document

logger = logging.getLogger(__name__)

def get_project_context(project_id, exclude_doc_id, query_text, token_budget=200000):
    """
    Retrieves context for a given project from Weaviate using a hybrid search,
    excluding the specified document. Assembles a text block containing the
    most relevant chunks, keeping the total size approximately within token_budget.
    
    Returns:
        tuple: (context_string, list_of_included_filenames)
    """
    print(f"\n--- [RETRIEVAL] Fetching context for project {project_id}, excluding doc {exclude_doc_id} ---")
    
    # 1. Embed query_text
    print(f"[RETRIEVAL] Generating embedding for query_text (length: {len(query_text)} chars)...")
    query_vector = embed_text(query_text)
    
    # 2. Run hybrid search against Weaviate
    print(f"[RETRIEVAL] Running Weaviate hybrid search...")
    client = get_weaviate_client()
    try:
        collection = client.collections.get(COLLECTION_NAME)
        
        response = collection.query.hybrid(
            query=query_text,
            vector=query_vector,
            alpha=0.5,  # 0.0 is pure BM25 (keyword), 1.0 is pure semantic, 0.5 is equal weight
            limit=500,  # Generous max limit
            filters=(
                Filter.by_property("project_id").equal(str(project_id)) &
                Filter.by_property("doc_id").not_equal(str(exclude_doc_id))
            )
        )
        
        objects = response.objects
        print(f"[RETRIEVAL] SUCCESS: Weaviate search returned {len(objects)} chunk results.")
    except Exception as e:
        logger.error(f"Weaviate search failed: {e}")
        print(f"[RETRIEVAL] ERROR: Weaviate search failed: {e}")
        objects = []
    finally:
        client.close()
        
    # 3. Look up each source document's filename and type efficiently
    doc_meta = {}
    
    def get_doc_meta(doc_id):
        """Return (filename, doc_type) for a chunk's source document.

        Cached per doc_id, because one document contributes many
        chunks and re-reading it per chunk would turn a single lookup
        into dozens.  doc_type comes out of the same item the filename
        already came from, so labelling correspondence costs no extra
        DynamoDB reads.
        """
        if doc_id not in doc_meta:
            doc_item = get_document(project_id, doc_id)
            if doc_item and "filename" in doc_item:
                doc_meta[doc_id] = (
                    doc_item["filename"],
                    doc_item.get("doc_type", ""),
                    doc_item.get("reply_to_invoice_doc_id", ""),
                )
            else:
                doc_meta[doc_id] = (f"Unknown_{doc_id}", "", "")
        return doc_meta[doc_id]

    # 4. Walk the ranked results in order, building a labeled context string
    print(f"[RETRIEVAL] Assembling context blocks (budget: {token_budget} tokens)...")
    context_parts = []
    included_filenames = set()
    current_tokens = 0
    
    for obj in objects:
        props = obj.properties
        chunk_doc_id = props.get("doc_id")
        chunk_text = props.get("chunk_text", "")
        
        if not chunk_doc_id or not chunk_text:
            continue
            
        filename, doc_type, reply_to_invoice = get_doc_meta(chunk_doc_id)
        
        # Format the block.  Correspondence is marked in the label so
        # the detection prompt can weigh an email as a claim somebody
        # made, rather than as a procurement record of equal standing.
        # A reply to this very invoice's follow-up says so, because the
        # follow-up it answers is deliberately not in the context.
        if doc_type == "correspondence" and reply_to_invoice and reply_to_invoice == str(exclude_doc_id):
            label = f"[from: {filename} — SUPPLIER CORRESPONDENCE — REPLY TO A FOLLOW-UP ABOUT THIS INVOICE]"
        elif doc_type == "correspondence":
            label = f"[from: {filename} — SUPPLIER CORRESPONDENCE]"
        else:
            label = f"[from: {filename}]"

        block = f"{label}\n{chunk_text}\n\n"
        
        # Approximate tokens (roughly word_count * 1.3)
        words = len(block.split())
        approx_tokens = int(words * 1.3)
        
        if current_tokens + approx_tokens > token_budget:
            print(f"[RETRIEVAL] Token budget reached. Stopping early.")
            break
            
        context_parts.append(block)
        included_filenames.add(filename)
        current_tokens += approx_tokens

    context_string = "".join(context_parts)
    print(f"[RETRIEVAL] Assembly complete. Included {len(included_filenames)} unique documents: {list(included_filenames)}")
    print(f"[RETRIEVAL] Total approximate tokens used: {current_tokens}")
    print(f"--- [RETRIEVAL] Complete ---\n")
    return context_string, list(included_filenames)
