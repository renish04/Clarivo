import json
import logging
import re
from google import genai
from google.genai import types

from documents.dynamo import get_document
from detection.retrieval import get_project_context
from detection.prompts import DETECTION_SYSTEM_PROMPT

logger = logging.getLogger(__name__)
gemini_client = genai.Client()

def _normalize_text(text: str) -> str:
    """Strips currency symbols, commas, extra whitespace, and lowercases text."""
    if not text:
        return ""
    # Lowercase
    text = text.lower()
    # Remove currency symbols and commas
    text = re.sub(r'[$£€,]', '', text)
    # Collapse whitespace
    text = re.sub(r'\s+', ' ', text).strip()
    return text

def verify_grounding(findings, context_text):
    """
    Checks each evidence claim against the context text.
    Marks evidence['verified'] = True/False.
    """
    norm_context = _normalize_text(context_text)
    
    for finding in findings:
        evidence_list = finding.get("evidence", [])
        for evidence in evidence_list:
            claim = evidence.get("claim", "")
            if not claim:
                evidence["verified"] = False
                continue
                
            norm_claim = _normalize_text(claim)
            
            if norm_claim and norm_claim in norm_context:
                evidence["verified"] = True
            else:
                evidence["verified"] = False
                
    return findings

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
    # 1. Fetch main document
    doc_item = get_document(project_id, doc_id)
    if not doc_item:
        logger.error(f"Document {doc_id} for project {project_id} not found.")
        return fallback_response(doc_id)
    
    body = doc_item.get("body", "")
    filename = doc_item.get("filename", "Unknown")
    
    if not body:
        logger.warning(f"Document {doc_id} has no body.")
        return fallback_response(filename)

    # 2. Retrieve related context
    context_str, included_filenames = get_project_context(
        project_id=project_id,
        exclude_doc_id=doc_id,
        query_text=body
    )
    
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
        response = gemini_client.models.generate_content(
            model='gemini-3.8-flash',
            contents=user_message,
            config=types.GenerateContentConfig(
                system_instruction=DETECTION_SYSTEM_PROMPT,
                temperature=0.4,
            )
        )
        
        raw_text = response.text.strip() if response.text else ""
        
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
        
        # Verify grounding against the user_message which contains both the invoice and the context
        if "findings" in result_dict:
            result_dict["findings"] = verify_grounding(result_dict["findings"], user_message)
            
        return result_dict
        
    except json.JSONDecodeError as e:
        logger.error(
            f"Failed to parse Gemini response as JSON for doc {doc_id}. Error: {e}\n"
            f"Raw response:\n{raw_text}"
        )
        return fallback_response(filename)
    except Exception as e:
        logger.exception(f"Gemini API error during detection for doc {doc_id}: {e}")
        return fallback_response(filename)

