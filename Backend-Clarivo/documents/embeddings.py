"""
Shared text-embedding and chunking utilities.

This module is the single source of truth for embedding in the project.
Any code that needs to compute embeddings — indexing, querying, etc. —
should import ``embed_text`` from here so the same model and
dimensionality are used everywhere.

The fastembed model is loaded once at module level (the slow part)
and reused across calls.
"""

from fastembed import TextEmbedding

# BAAI/bge-small-en-v1.5 — 384-dim, strong accuracy-to-size ratio.
# Loaded once; subsequent calls to embed_text() are fast.
_model = TextEmbedding("BAAI/bge-small-en-v1.5")


def embed_text(text: str) -> list[float]:
    """Return a 384-dimension embedding for *text* as a plain list of floats.

    Parameters
    ----------
    text : str
        The input string to embed.  Can be a short query or a longer
        document chunk — the underlying model handles both.

    Returns
    -------
    list[float]
        A flat list of 384 float values.
    """
    # TextEmbedding.embed() accepts a list and yields numpy arrays.
    embedding = next(_model.embed([text]))
    return embedding.tolist()


def chunk_text(
    text: str,
    chunk_size: int = 500,
    overlap: int = 100,
) -> list[str]:
    """Split *text* into overlapping windows based on word count.

    Parameters
    ----------
    text : str
        The full document text to chunk.
    chunk_size : int
        Maximum number of words per chunk (default 500).
    overlap : int
        Number of words shared between consecutive chunks (default 100).
        Must be less than *chunk_size*.

    Returns
    -------
    list[str]
        A list of chunk strings.  If the text contains fewer than
        *chunk_size* words, a single-element list is returned.
    """
    words = text.split()

    if len(words) <= chunk_size:
        return [text]

    step = chunk_size - overlap
    chunks = []
    for start in range(0, len(words), step):
        chunk_words = words[start : start + chunk_size]
        chunks.append(" ".join(chunk_words))
        # Stop once we've captured the tail of the document.
        if start + chunk_size >= len(words):
            break

    return chunks

