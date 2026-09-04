import os
from google import genai
from google.genai import types
from documents.dynamo import get_document, update_document_classification

# Initialize the Gemini client. It will automatically pick up GEMINI_API_KEY from the environment.
gemini_client = genai.Client()

def classify_document(project_id, doc_id):
    """
    Fetches the document from DynamoDB, calls Gemini to classify its intent based on the text,
    and updates the DynamoDB record with doc_type and a 'classified' status.
    """
    doc_item = get_document(project_id, doc_id)
    if not doc_item:
        raise ValueError(f"Document {doc_id} for project {project_id} not found in DynamoDB.")
    
    body = doc_item.get("body", "")
    if not body:
        # If there is no body, it's safer to default to "other"
        update_document_classification(project_id, doc_id, "other", "classified")
        return "other"
    
    system_instruction = (
        "You are an expert document classifier for a business application. Your main aim is to carefully detect "
        "if a given document is an 'invoice' or not. Then consider the other options. "
        "Based on the content and intent of the document, you must classify it as exactly one of the following five types:\n"
        "1. invoice: A document requesting payment for goods or services provided.\n"
        "2. order: A document requesting goods or services (e.g., purchase order, work order).\n"
        "3. delivery: A document confirming the delivery of goods (e.g., delivery note, receipt, packing slip).\n"
        "4. governing: A document that sets baseline terms rather than recording a specific transaction "
        "(e.g., rate agreement, contract, bill of quantities).\n"
        "5. other: Any document that does not cleanly fit into the above categories.\n\n"
        "Your response must be a single word, strictly one of: 'invoice', 'order', 'delivery', 'governing', or 'other'. "
        "Do not include any punctuation, explanation, or additional text. Detect the core intent of the document."
    )
    
    try:
        response = gemini_client.models.generate_content(
            model='gemini-3.8-flash',
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
                temperature=0.2,
            )
        )
        
        result_text = response.text.strip().lower()
        # Clean up any trailing punctuation just in case
        result_text = ''.join(c for c in result_text if c.isalnum())
        
        allowed_types = {"invoice", "order", "delivery", "governing", "other"}
        if result_text not in allowed_types:
            result_text = "other"
            
    except Exception as e:
        # Default to "other" rather than crashing on API errors
        print(f"Gemini API error during classification: {e}")
        result_text = "other"
        
    update_document_classification(project_id, doc_id, result_text, "classified")
    return result_text

