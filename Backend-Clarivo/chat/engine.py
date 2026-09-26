"""
The "Ask Clarivo" answering engine.

The point of this module is the second input it assembles.  A generic
document Q&A bot, asked "why was this invoice flagged?", would re-read
the invoice and reason its way to a fresh opinion — which might not be
the opinion Clarivo actually recorded, and would leave the user with
two different answers from the same system.

So the detection pipeline's stored verdict, findings, cited evidence
and resolution are formatted and handed to the model as fact.  Asked
about a checked document, the assistant explains the analysis that is
already on the record — including which pieces of evidence failed
grounding verification — rather than deriving a new one.
"""

import logging

from google import genai
from google.genai import types

from chat.prompts import CHAT_SYSTEM_PROMPT
from chat.retrieval import get_chat_context
from chat.storage import get_history
from documents.dynamo import get_document

logger = logging.getLogger(__name__)

# Same client construction as detection/classify — picks GEMINI_API_KEY
# up from the environment loaded by settings.py.
gemini_client = genai.Client()

# Same model the rest of the pipeline uses.  Low temperature: this is
# explanation of recorded facts, not composition.
CHAT_MODEL = "gemini-3.5-flash-lite"
CHAT_TEMPERATURE = 0.3

# How many past messages to replay into the prompt.
HISTORY_LIMIT = 10


def _format_analysis(doc):
    """
    Render the detection pipeline's stored verdict as readable text.

    Returns a block describing the verdict, every finding with its
    evidence and grounding status, and the resolution — or a plain
    statement that the document has not been checked, which is itself
    useful context: it tells the model not to imply a verdict exists.
    """
    discrepancy_status = doc.get("discrepancy_status")

    if not discrepancy_status:
        return (
            "This document has not been checked by Clarivo's discrepancy "
            "detection yet, so there is no stored verdict, finding or "
            "resolution for it."
        )

    lines = [f"Verdict: {discrepancy_status}"]

    findings = doc.get("findings") or []
    if findings:
        lines.append(f"\nFindings ({len(findings)}):")
        for i, finding in enumerate(findings, start=1):
            finding_type = finding.get("type", "other")
            description = finding.get("description", "")
            lines.append(f"\n{i}. {finding_type}: {description}")

            evidence_list = finding.get("evidence") or []
            if not evidence_list:
                lines.append("   Evidence: none cited.")
                continue

            lines.append("   Evidence:")
            for evidence in evidence_list:
                claim = evidence.get("claim", "")
                source_doc = evidence.get("source_doc", "unknown document")
                # Grounding verification runs at detection time; an
                # unverified claim is one the system could not find
                # verbatim in the source text, and the assistant is
                # required to pass that caveat on to the user.
                if evidence.get("verified"):
                    mark = "VERIFIED against source text"
                else:
                    mark = (
                        "UNVERIFIED - this claim was not found verbatim in "
                        "the source text"
                    )
                lines.append(f'   - "{claim}" (from {source_doc}) [{mark}]')
    else:
        lines.append("\nFindings: none recorded.")

    resolution = doc.get("resolution")
    if resolution:
        lines.append(f"\nResolution: {resolution}")

    return "\n".join(lines)


def _format_history(messages):
    """Render stored messages as a simple labelled transcript."""
    if not messages:
        return (
            "(no previous messages - this is the first question in this "
            "conversation)"
        )

    lines = []
    for message in messages:
        speaker = "User" if message.get("role") == "user" else "Assistant"
        lines.append(f"{speaker}: {message.get('content', '')}")
    return "\n".join(lines)


def _failure_response():
    """The answer returned when the model call cannot be completed."""
    return {
        "answer": (
            "Sorry - I could not generate an answer just now. Please try "
            "asking again in a moment."
        ),
        "sources": [],
    }


def answer_question(project_id, doc_id, question):
    """
    Answer *question* about one document, in the context of its project.

    Parameters
    ----------
    project_id : int | str
        The Django project PK.
    doc_id : str
        The document the conversation is scoped to.
    question : str
        The user's message.

    Returns
    -------
    dict
        ``{"answer": str, "sources": list[str]}``.  Always returns a
        dict — a model failure produces an apologetic answer with no
        sources rather than an exception, so the endpoint stays up and
        the conversation survives one bad call.
    """
    print(f"\n=== [CHAT] Question about doc {doc_id} (project {project_id}) ===")

    # 1. The selected document, in full.
    doc = get_document(project_id, doc_id)
    if doc is None:
        logger.error(f"Chat: document {doc_id} (project {project_id}) not found.")
        print(f"[CHAT] ERROR: Document {doc_id} not found.")
        return {
            "answer": "I could not find that document in this project.",
            "sources": [],
        }

    filename = doc.get("filename", "Unknown document")
    body = doc.get("body", "")
    doc_type = doc.get("doc_type") or "unclassified"

    if not body:
        body = "(no extracted text is available for this document)"

    # 2. Clarivo's own prior analysis, if detection has run.
    analysis_block = _format_analysis(doc)
    print(
        f"[CHAT] Document: {filename} (type: {doc_type}, "
        f"checked: {bool(doc.get('discrepancy_status'))})"
    )

    # 3. Relevant excerpts from the rest of the project.
    context_str, context_filenames = get_chat_context(
        project_id=project_id,
        selected_doc_id=doc_id,
        question=question,
    )
    if not context_str:
        context_str = "(no related excerpts were retrieved for this question)"

    # 4. Recent conversation, so follow-up questions make sense.
    history = get_history(project_id, doc_id, limit=HISTORY_LIMIT)
    history_block = _format_history(history)
    print(f"[CHAT] Replaying {len(history)} previous message(s) into the prompt.")

    # 5. Assemble the user message in the order the system prompt tells
    #    the model to expect it.
    user_message = (
        f"Selected document ({filename}):\n{body}\n\n"
        f"Clarivo's analysis of this document:\n{analysis_block}\n\n"
        f"Related project context:\n{context_str}\n\n"
        f"Conversation so far:\n{history_block}\n\n"
        f"User question: {question}"
    )

    # 6 + 7. Call Gemini, never letting a failure reach the endpoint.
    try:
        print(f"[CHAT] Calling Gemini ({CHAT_MODEL})...")
        response = gemini_client.models.generate_content(
            model=CHAT_MODEL,
            contents=user_message,
            config=types.GenerateContentConfig(
                system_instruction=CHAT_SYSTEM_PROMPT,
                temperature=CHAT_TEMPERATURE,
            ),
        )

        answer = response.text.strip() if response.text else ""
        if not answer:
            # A blocked or empty completion is a failure to answer, not
            # an answer of "".
            logger.warning(f"Chat: Gemini returned no text for doc {doc_id}.")
            print("[CHAT] WARNING: Gemini returned an empty response.")
            return _failure_response()

    except Exception as e:
        logger.exception(f"Chat: Gemini API error for doc {doc_id}: {e}")
        print(f"[CHAT] ERROR: Gemini API error: {e}")
        return _failure_response()

    # The selected document is always a source — it was passed in full,
    # whether or not retrieval also surfaced chunks of it.
    sources = list(context_filenames)
    if filename not in sources:
        sources.append(filename)

    print(f"[CHAT] SUCCESS: answered in {len(answer)} chars. Sources: {sources}")
    print("=== [CHAT] Complete ===\n")

    return {"answer": answer, "sources": sources}
