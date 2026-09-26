from rest_framework import serializers, status
from rest_framework.authentication import TokenAuthentication
from rest_framework.generics import get_object_or_404
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from projects.models import Project

# How much of a thread to send back when the page loads.  Larger than
# the window replayed into the prompt: the model only needs recent
# turns to follow the conversation, but the user reloading the tab
# expects to find their transcript where they left it.
TRANSCRIPT_LIMIT = 50

# Opening questions, chosen by the document's stored state.
#
# These are picked in plain Python rather than generated: the useful
# openers for a flagged invoice are the same three every time, and
# spending a model call — and the second or two of latency it costs —
# to rediscover that on every page load would buy nothing.  Keyed by
# ``discrepancy_status``; a document detection has not reached yet
# falls back to ``_UNCHECKED_SUGGESTIONS``.
_SUGGESTIONS_BY_STATUS = {
    "flagged": [
        "Why was this flagged?",
        "What evidence supports this?",
        "What should I do about it?",
    ],
    "auto_resolved": [
        "How was this resolved?",
        "Which documents explained it?",
        "Can I trust this resolution?",
    ],
    "clean": [
        "What was checked on this invoice?",
        "Which documents was it matched against?",
        "Summarise this document",
    ],
    "needs_more_info": [
        "What information is missing?",
        "What was checked so far?",
        "Summarise this document",
    ],
}

_UNCHECKED_SUGGESTIONS = [
    "Summarise this document",
    "What are the key figures here?",
    "Which supplier is this from?",
]


class ChatQuestionSerializer(serializers.Serializer):
    """Validates the incoming body for a chat question."""

    question = serializers.CharField()


class DocumentChatView(APIView):
    """
    /api/projects/<project_id>/documents/<doc_id>/chat/

    One conversation, scoped to one document, addressed by one URL:

    * ``POST``   — ask a question, get an answer.
    * ``GET``    — replay the thread (what a page refresh calls).
    * ``DELETE`` — start over.
    """

    authentication_classes = [TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def _get_project(self, request, project_id):
        """Verify the project exists and belongs to the requesting user."""
        return get_object_or_404(
            Project.objects.filter(owner=request.user),
            pk=project_id,
        )

    def post(self, request, project_id, doc_id):
        project = self._get_project(request, project_id)

        serializer = ChatQuestionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        question = serializer.validated_data["question"]

        from chat.engine import answer_question
        from chat.storage import save_message
        from documents.dynamo import get_document

        # Checked before anything is written, so a mistyped doc_id
        # cannot leave a conversation stranded against a document that
        # does not exist.
        if get_document(project.pk, doc_id) is None:
            return Response(
                {"detail": "Document not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        # Save the question first: if generation then fails, the user's
        # message is still on the record rather than silently lost.
        save_message(project.pk, doc_id, "user", question)

        result = answer_question(project.pk, doc_id, question)

        save_message(
            project.pk,
            doc_id,
            "assistant",
            result["answer"],
            sources=result["sources"],
        )

        return Response(result, status=status.HTTP_200_OK)

    def get(self, request, project_id, doc_id):
        project = self._get_project(request, project_id)

        from chat.storage import get_history

        messages = get_history(project.pk, doc_id, limit=TRANSCRIPT_LIMIT)
        return Response({"messages": messages}, status=status.HTTP_200_OK)

    def delete(self, request, project_id, doc_id):
        project = self._get_project(request, project_id)

        from chat.storage import clear_history

        clear_history(project.pk, doc_id)
        return Response(status=status.HTTP_204_NO_CONTENT)


class ChatSuggestionsView(APIView):
    """
    GET /api/projects/<project_id>/documents/<doc_id>/chat/suggestions/

    Three opening questions for an empty chat panel, chosen from the
    document's stored state.  What is worth asking about a document
    depends entirely on whether detection has run on it and what it
    concluded — a flagged invoice invites "why?", one that was never
    checked invites "what does it say?".
    """

    authentication_classes = [TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request, project_id, doc_id):
        # Verify the project exists and belongs to the requesting user.
        project = get_object_or_404(
            Project.objects.filter(owner=request.user),
            pk=project_id,
        )

        from documents.dynamo import get_document

        doc = get_document(project.pk, doc_id)
        if doc is None:
            return Response(
                {"detail": "Document not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        discrepancy_status = doc.get("discrepancy_status")
        suggestions = _SUGGESTIONS_BY_STATUS.get(
            discrepancy_status,
            _UNCHECKED_SUGGESTIONS,
        )

        return Response({"suggestions": suggestions}, status=status.HTTP_200_OK)
