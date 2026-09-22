import json
import logging
from google import genai
from google.genai import types

from documents.dynamo import get_document
from detection.retrieval import get_project_context
from detection.prompts import DETECTION_SYSTEM_PROMPT
from detection.grounding import apply_grounding_policy, verify_grounding

logger = logging.getLogger(__name__)
gemini_client = genai.Client()


def fallback_response(filename):
    """Returns a safe fallback dict if generation or parsing fails."""
    return {
        "status": "needs_more_info",
        "findings": [],
        "resolution": "detection response could not be parsed",
        "table_row_markdown": f"| {filename} | needs_more_info | Parse error | See logs |"
    }

def detect_discrepancies(project_id, doc_id):
    """
    Fetches the invoice, retrieves related context, and asks Gemini to detect
    any discrepancies (rate, quantity, tax, duplicates).
    """
    print(f"\n--- [DETECT] Starting discrepancy detection for invoice Doc {doc_id} ---")
    # 1. Fetch main document
    doc_item = get_document(project_id, doc_id)
    if not doc_item:
        print(f"[DETECT] ERROR: Document {doc_id} for project {project_id} not found.")
        logger.error(f"Document {doc_id} for project {project_id} not found.")
        return fallback_response(doc_id)
    
    body = doc_item.get("body", "")
    filename = doc_item.get("filename", "Unknown")
    
    if not body:
        print(f"[DETECT] WARNING: Document {doc_id} has no body.")
        logger.warning(f"Document {doc_id} has no body.")
        return fallback_response(filename)

    # 2. Retrieve related context
    print(f"[DETECT] Fetching related context for {filename}...")
    context_str, included_filenames, chunks_by_filename = get_project_context(
        project_id=project_id,
        exclude_doc_id=doc_id,
        query_text=body
    )

    # The invoice under review is itself a citable source, so it belongs
    # in the map grounding verification looks things up in.
    docs_by_name = dict(chunks_by_filename)
    docs_by_name[filename] = body
    
    # 3. Construct the prompt
    user_message = (
        f"Main document ({filename}):\n"
        f"{body}\n\n"
        f"Related project context:\n"
        f"{context_str}"
    )
    
    # 4. Call Gemini
    raw_text = ""
    try:
        print(f"[DETECT] Calling Gemini to analyze {filename}...")
        response = gemini_client.models.generate_content(
            model='gemini-3.5-flash-lite',
            contents=user_message,
            config=types.GenerateContentConfig(
                system_instruction=DETECTION_SYSTEM_PROMPT,
                temperature=0.4,
            )
        )
        
        raw_text = response.text.strip() if response.text else ""
        print(f"[DETECT] Gemini raw response received:\n{raw_text}\n")
        
        # Defensive: Strip markdown fences if present
        if raw_text.startswith("```"):
            lines = raw_text.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            raw_text = "\n".join(lines).strip()
            
        # Parse JSON
        result_dict = json.loads(raw_text)
        print(f"[DETECT] SUCCESS: Parsed JSON response successfully.")
        
        # Verify each claim against the document it was attributed to,
        # then let the result constrain the model's own verdict.
        if "findings" in result_dict:
            result_dict["findings"] = verify_grounding(
                result_dict["findings"], docs_by_name
            )
            result_dict, policy_notes = apply_grounding_policy(result_dict)
            if policy_notes:
                result_dict["grounding_notes"] = policy_notes

        print(f"--- [DETECT] Complete for {filename} ---")
        return result_dict
        
    except json.JSONDecodeError as e:
        print(f"[DETECT] ERROR: Failed to parse Gemini response as JSON. {e}")
        logger.error(
            f"Failed to parse Gemini response as JSON for doc {doc_id}. Error: {e}\n"
            f"Raw response:\n{raw_text}"
        )
        return fallback_response(filename)
    except Exception as e:
        print(f"[DETECT] ERROR: Gemini API error: {e}")
        logger.exception(f"Gemini API error during detection for doc {doc_id}: {e}")
        return fallback_response(filename)

