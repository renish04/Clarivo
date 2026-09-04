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
    # 1. Embed query_text
    query_vector = embed_text(query_text)
    
    # 2. Run hybrid search against Weaviate
    client = get_weaviate_client()
    try:
        collection = client.collections.get(COLLECTION_NAME)
        
        response = collection.query.hybrid(
            query=query_text,
            vector=query_vector,
            alpha=0.7 #1.0 is pure semantic, 0.0 is pure keyword!
            limit=500,  # Generous max limit
            filters=(
                Filter.by_property("project_id").equal(str(project_id)) &
                Filter.by_property("doc_id").not_equal(str(exclude_doc_id))
            )
        )
        
        objects = response.objects
    except Exception as e:
        logger.error(f"Weaviate search failed: {e}")
        objects = []
    finally:
        client.close()
        
    # 3. Look up filenames efficiently
    doc_filenames = {}
    
    def get_filename(doc_id):
        if doc_id not in doc_filenames:
            doc_item = get_document(project_id, doc_id)
            if doc_item and "filename" in doc_item:
                doc_filenames[doc_id] = doc_item["filename"]
            else:
                doc_filenames[doc_id] = f"Unknown_{doc_id}"
        return doc_filenames[doc_id]

    # 4. Walk the ranked results in order, building a labeled context string
    context_parts = []
    included_filenames = set()
    current_tokens = 0
    
    for obj in objects:
        props = obj.properties
        chunk_doc_id = props.get("doc_id")
        chunk_text = props.get("chunk_text", "")
        
        if not chunk_doc_id or not chunk_text:
            continue
            
        filename = get_filename(chunk_doc_id)
        
        # Format the block
        block = f"[from: {filename}]\n{chunk_text}\n\n"
        
        # Approximate tokens (roughly word_count * 1.3)
        words = len(block.split())
        approx_tokens = int(words * 1.3)
        
        if current_tokens + approx_tokens > token_budget:
            # We've reached or are about to exceed the budget, stop adding.
            break
            
        context_parts.append(block)
        included_filenames.add(filename)
        current_tokens += approx_tokens

    context_string = "".join(context_parts)
    return context_string, list(included_filenames)

