import React, { useState, useEffect, useRef } from 'react';
import { useParams } from 'react-router-dom';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import apiClient from '../../api/client';

// Same colour scheme as the Workspace tab, so a document that reads as
// "flagged" there reads as flagged here too.
const getStatusBadgeClass = (status) => {
  const s = status?.toLowerCase() || '';
  if (s === 'clean') return 'bg-green-100 text-green-800 border-green-200';
  if (s === 'auto_resolved') return 'bg-teal-100 text-teal-800 border-teal-200';
  if (s === 'flagged') return 'bg-red-100 text-red-800 border-red-200';
  if (s === 'needs_more_info') return 'bg-purple-100 text-purple-800 border-purple-200';
  return 'bg-gray-100 text-gray-800 border-gray-200';
};

// A checked document is best described by its verdict; one that has not
// been checked yet falls back to its position in the ingestion
// pipeline, which lands in the neutral grey above.
const getDocStatus = (doc) => doc.discrepancy_status || doc.status || '';

const StatusBadge = ({ doc }) => {
  const status = getDocStatus(doc);
  if (!status) return null;
  return (
    <span
      className={`ml-2 flex-shrink-0 px-2 py-0.5 border rounded-full text-[10px] font-semibold uppercase tracking-wider whitespace-nowrap ${getStatusBadgeClass(
        status
      )}`}
    >
      {status.replace(/_/g, ' ')}
    </span>
  );
};

export default function ChatPanel() {
  const { id } = useParams();

  const [documents, setDocuments] = useState([]);
  const [docsLoading, setDocsLoading] = useState(true);
  const [docsError, setDocsError] = useState('');

  const [selectedDocId, setSelectedDocId] = useState(null);
  const [dropdownOpen, setDropdownOpen] = useState(false);

  const [messages, setMessages] = useState([]);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [suggestions, setSuggestions] = useState([]);

  const [input, setInput] = useState('');
  const [sending, setSending] = useState(false);
  const [clearing, setClearing] = useState(false);
  const [chatError, setChatError] = useState('');

  const dropdownRef = useRef(null);
  const messagesEndRef = useRef(null);

  const selectedDoc = documents.find(
    (doc) => doc.SK?.replace('DOC#', '') === selectedDocId
  );

  // Answers cite their sources by filename, but opening one needs the
  // presigned S3 link that arrives on the document list — the same
  // `view_url` the Files tab links to.
  const filenameToUrl = documents.reduce(
    (acc, doc) => ({ ...acc, [doc.filename]: doc.view_url }),
    {}
  );

  // -- Load the project's documents for the selector ------------------
  useEffect(() => {
    let cancelled = false;

    const fetchDocuments = async () => {
      try {
        setDocsLoading(true);
        const res = await apiClient.get(`/projects/${id}/documents/`);
        if (cancelled) return;
        setDocuments(res.data || []);
        setDocsError('');
      } catch (err) {
        if (cancelled) return;
        console.error('Failed to load documents', err);
        setDocsError('Failed to load documents.');
      } finally {
        if (!cancelled) setDocsLoading(false);
      }
    };

    fetchDocuments();
    return () => {
      cancelled = true;
    };
  }, [id]);

  // Openers are shown on any empty thread — a brand new one, or one
  // that has just been cleared — so fetching them lives here rather
  // than inside either caller.  A failure is logged and swallowed:
  // they are a convenience, not part of the conversation.
  const loadSuggestions = async (docId) => {
    try {
      const res = await apiClient.get(
        `/projects/${id}/documents/${docId}/chat/suggestions/`
      );
      return res.data.suggestions || [];
    } catch (err) {
      console.error('Failed to load suggestions', err);
      return [];
    }
  };

  // -- Load the selected document's own thread ------------------------
  // Each document has a separate conversation, so switching the
  // selector swaps the transcript rather than continuing the old one.
  useEffect(() => {
    if (!selectedDocId) {
      setMessages([]);
      setSuggestions([]);
      return;
    }

    // Guards against a slow response for a document the user has since
    // switched away from landing in the new document's thread.
    let cancelled = false;

    const fetchHistory = async () => {
      try {
        setHistoryLoading(true);
        setMessages([]);
        setSuggestions([]);
        setChatError('');
        const res = await apiClient.get(
          `/projects/${id}/documents/${selectedDocId}/chat/`
        );
        if (cancelled) return;

        const history = res.data.messages || [];
        setMessages(history);

        // Only for an empty thread — there is no reason to ask for
        // openers for a conversation already under way.
        if (history.length === 0) {
          const openers = await loadSuggestions(selectedDocId);
          if (cancelled) return;
          setSuggestions(openers);
        }
      } catch (err) {
        if (cancelled) return;
        console.error('Failed to load chat history', err);
        setChatError('Failed to load this document’s conversation.');
      } finally {
        if (!cancelled) setHistoryLoading(false);
      }
    };

    fetchHistory();
    return () => {
      cancelled = true;
    };
  }, [id, selectedDocId]);

  // -- Close the dropdown on an outside click -------------------------
  useEffect(() => {
    if (!dropdownOpen) return;

    const handleClickOutside = (event) => {
      if (dropdownRef.current && !dropdownRef.current.contains(event.target)) {
        setDropdownOpen(false);
      }
    };

    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, [dropdownOpen]);

  // Keep the newest message in view as the thread grows.
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages, sending]);

  // Takes the question as an argument rather than reading the input,
  // so a suggestion chip can send one that was never typed.
  const sendQuestion = async (rawQuestion) => {
    const question = rawQuestion.trim();
    if (!question || !selectedDocId || sending) return;

    // The openers belong to an empty thread; asking anything at all
    // ends that, whether or not the question came from one of them.
    setSuggestions([]);

    // Show the question immediately rather than after the round trip —
    // answering takes a few seconds and an input that just empties
    // itself looks like a dropped message.
    const pending = {
      SK: `pending-${Date.now()}`,
      role: 'user',
      content: question,
      sources: [],
    };
    setMessages((prev) => [...prev, pending]);
    setInput('');
    setSending(true);
    setChatError('');

    try {
      const res = await apiClient.post(
        `/projects/${id}/documents/${selectedDocId}/chat/`,
        { question }
      );
      setMessages((prev) => [
        ...prev,
        {
          SK: `answer-${Date.now()}`,
          role: 'assistant',
          content: res.data.answer,
          sources: res.data.sources || [],
        },
      ]);
    } catch (err) {
      console.error('Failed to send message', err);
      // The question stays on screen and the error is shown against
      // it in the transcript, so the failure is attached to the thing
      // that failed.  Reloading re-syncs with what the backend
      // actually stored.
      setChatError(
        err.response?.status
          ? `Failed to get an answer (error ${err.response.status}). Please try again.`
          : 'Failed to get an answer. Check your connection and try again.'
      );
    } finally {
      setSending(false);
    }
  };

  const handleSend = (event) => {
    event.preventDefault();
    sendQuestion(input);
  };

  const handleClear = async () => {
    if (!selectedDocId || clearing) return;

    // Deleting a thread cannot be undone, so it is worth one prompt.
    const confirmed = window.confirm(
      `Clear the conversation about ${selectedDoc?.filename ?? 'this document'}? This cannot be undone.`
    );
    if (!confirmed) return;

    setClearing(true);
    setChatError('');

    try {
      await apiClient.delete(`/projects/${id}/documents/${selectedDocId}/chat/`);
      setMessages([]);
      // The thread is empty again, so it gets its openers back.
      setSuggestions(await loadSuggestions(selectedDocId));
    } catch (err) {
      console.error('Failed to clear conversation', err);
      // The messages stay on screen: the delete did not happen, and
      // emptying the list anyway would claim otherwise.
      setChatError('Failed to clear the conversation. Please try again.');
    } finally {
      setClearing(false);
    }
  };

  return (
    <div className="h-full flex flex-col bg-gray-50/50">
      {/* -- Document selector -------------------------------------- */}
      <div className="flex-shrink-0 px-8 pt-6 pb-4 border-b border-gray-200 bg-white">
        <div className="flex items-center justify-between mb-2">
          <label className="block text-xs font-semibold text-gray-500 uppercase tracking-wider">
            Ask about
          </label>

          {/* Nothing to clear until there is a thread to clear. */}
          {selectedDocId && messages.length > 0 && (
            <button
              type="button"
              onClick={handleClear}
              disabled={clearing || sending}
              className="text-xs font-medium text-gray-500 hover:text-red-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
            >
              {clearing ? 'Clearing…' : 'Clear'}
            </button>
          )}
        </div>

        <div className="relative max-w-2xl" ref={dropdownRef}>
          <button
            type="button"
            onClick={() => setDropdownOpen((open) => !open)}
            disabled={docsLoading || documents.length === 0}
            className="w-full flex items-center justify-between gap-2 px-4 py-2.5 bg-white border border-gray-300 rounded-md shadow-sm text-left hover:border-gray-400 focus:outline-none focus:ring-2 focus:ring-blue-800 focus:border-blue-800 disabled:opacity-60 disabled:cursor-not-allowed transition-colors"
          >
            <span className="flex items-center min-w-0 flex-1">
              {selectedDoc ? (
                <>
                  <span className="truncate text-gray-900" title={selectedDoc.filename}>
                    {selectedDoc.filename}
                  </span>
                  <StatusBadge doc={selectedDoc} />
                </>
              ) : (
                <span className="text-gray-500">
                  {docsLoading
                    ? 'Loading documents…'
                    : documents.length === 0
                    ? 'No documents in this project yet'
                    : 'Select a document…'}
                </span>
              )}
            </span>
            <span
              className={`flex-shrink-0 text-gray-400 text-xs transition-transform ${
                dropdownOpen ? 'rotate-180' : ''
              }`}
            >
              ▼
            </span>
          </button>

          {dropdownOpen && (
            <ul className="absolute z-20 mt-1 w-full max-h-72 overflow-y-auto custom-scrollbar bg-white border border-gray-200 rounded-md shadow-lg py-1">
              {documents.map((doc) => {
                const docId = doc.SK?.replace('DOC#', '');
                const isSelected = docId === selectedDocId;
                return (
                  <li key={doc.SK}>
                    <button
                      type="button"
                      onClick={() => {
                        setSelectedDocId(docId);
                        setDropdownOpen(false);
                      }}
                      className={`w-full flex items-center px-4 py-2 text-left text-sm hover:bg-gray-50 transition-colors ${
                        isSelected ? 'bg-blue-50' : ''
                      }`}
                    >
                      <span
                        className={`truncate flex-1 ${
                          isSelected ? 'text-blue-900 font-medium' : 'text-gray-800'
                        }`}
                        title={doc.filename}
                      >
                        {doc.filename}
                      </span>
                      <StatusBadge doc={doc} />
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
        </div>

        {docsError && <p className="mt-2 text-sm text-red-600">{docsError}</p>}
      </div>

      {/* -- Messages ------------------------------------------------- */}
      <div className="flex-1 overflow-y-auto custom-scrollbar px-8 py-6">
        {!selectedDocId ? (
          <div className="h-full flex flex-col items-center justify-center text-center text-gray-500">
            <p className="text-lg">Select a document to start.</p>
            <p className="text-sm mt-2">
              Ask Clarivo about any document in this project — including why it
              was flagged, and what evidence it relied on.
            </p>
          </div>
        ) : historyLoading ? (
          <div className="h-full flex items-center justify-center text-gray-500">
            Loading conversation…
          </div>
        ) : messages.length === 0 && !chatError ? (
          <div className="h-full flex flex-col items-center justify-center text-center text-gray-500">
            <p className="text-lg">No questions yet.</p>
            <p className="text-sm mt-2">
              Ask anything about{' '}
              <span className="font-medium text-gray-700">
                {selectedDoc?.filename}
              </span>
              .
            </p>

            {suggestions.length > 0 && (
              <div className="mt-6 flex flex-wrap justify-center gap-2">
                {suggestions.map((suggestion) => (
                  <button
                    key={suggestion}
                    type="button"
                    onClick={() => sendQuestion(suggestion)}
                    disabled={sending}
                    className="px-3 py-1.5 bg-white border border-gray-300 rounded-full text-sm text-gray-700 shadow-sm hover:border-blue-800 hover:text-blue-800 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
                  >
                    {suggestion}
                  </button>
                ))}
              </div>
            )}
          </div>
        ) : (
          <div className="max-w-3xl mx-auto space-y-6">
            {messages.map((message) => {
              const isUser = message.role === 'user';

              // User messages sit right, in a light bubble; answers run
              // full-width and unboxed, so a long explanation or a
              // comparison table has the room to read as a document
              // rather than as a chat balloon.
              if (isUser) {
                return (
                  <div key={message.SK} className="flex justify-end">
                    <div className="max-w-[80%] rounded-lg px-4 py-2.5 bg-blue-50 border border-blue-100">
                      <p className="text-sm text-gray-900 whitespace-pre-wrap">
                        {message.content}
                      </p>
                    </div>
                  </div>
                );
              }

              return (
                <div key={message.SK} className="w-full">
                  <div className="text-sm text-gray-800 space-y-2">
                    <ReactMarkdown
                      remarkPlugins={[remarkGfm]}
                      components={{
                        p: ({ node, ...props }) => <p className="leading-relaxed" {...props} />,
                        ul: ({ node, ...props }) => (
                          <ul className="list-disc pl-5 space-y-1" {...props} />
                        ),
                        ol: ({ node, ...props }) => (
                          <ol className="list-decimal pl-5 space-y-1" {...props} />
                        ),
                        table: ({ node, ...props }) => (
                          <table
                            className="w-full text-left text-xs border-collapse my-2"
                            {...props}
                          />
                        ),
                        th: ({ node, ...props }) => (
                          <th
                            className="px-2 py-1 bg-gray-50 border border-gray-200 font-semibold"
                            {...props}
                          />
                        ),
                        td: ({ node, ...props }) => (
                          <td className="px-2 py-1 border border-gray-200" {...props} />
                        ),
                        code: ({ node, ...props }) => (
                          <code className="px-1 py-0.5 bg-gray-100 rounded text-xs" {...props} />
                        ),
                      }}
                    >
                      {message.content}
                    </ReactMarkdown>

                    {message.sources?.length > 0 && (
                      <div className="pt-2 flex flex-wrap items-center gap-1.5">
                        <span className="text-[10px] font-semibold uppercase tracking-wider text-gray-400">
                          Sources
                        </span>
                        {message.sources.map((source) => {
                          const url = filenameToUrl[source];

                          // Provenance, not content: subdued by default,
                          // and only reaching for colour on hover.
                          const chipClass =
                            'max-w-[16rem] truncate px-2 py-0.5 border rounded-full text-[11px] transition-colors';

                          return url ? (
                            <a
                              key={source}
                              href={url}
                              target="_blank"
                              rel="noopener noreferrer"
                              title={`Open ${source}`}
                              className={`${chipClass} border-gray-200 text-gray-500 hover:border-blue-200 hover:text-blue-800 hover:bg-blue-50`}
                            >
                              {source}
                            </a>
                          ) : (
                            // A cited document with no link — it may have
                            // been removed since the answer was stored.
                            <span
                              key={source}
                              title={source}
                              className={`${chipClass} border-gray-200 text-gray-400`}
                            >
                              {source}
                            </span>
                          );
                        })}
                      </div>
                    )}
                  </div>
                </div>
              );
            })}

            {sending && (
              <div className="w-full text-sm text-gray-500 animate-pulse">Thinking…</div>
            )}

            {chatError && (
              <div className="p-3 bg-red-50 border border-red-200 rounded-md text-sm text-red-800">
                {chatError}
              </div>
            )}

            <div ref={messagesEndRef} />
          </div>
        )}
      </div>

      {/* -- Composer ------------------------------------------------- */}
      <div className="flex-shrink-0 border-t border-gray-200 bg-white px-8 py-4">
        <form onSubmit={handleSend} className="max-w-3xl mx-auto flex items-center gap-3">
          <input
            type="text"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            disabled={!selectedDocId || sending}
            placeholder={
              selectedDocId
                ? 'Ask a question about this document…'
                : 'Select a document first…'
            }
            className="flex-1 px-4 py-2.5 border border-gray-300 rounded-md shadow-sm text-sm focus:outline-none focus:ring-2 focus:ring-blue-800 focus:border-blue-800 disabled:bg-gray-50 disabled:cursor-not-allowed transition-colors"
          />
          <button
            type="submit"
            disabled={!selectedDocId || sending || !input.trim()}
            className="bg-blue-800 hover:bg-blue-900 text-white font-medium px-5 py-2.5 rounded shadow-sm disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
          >
            {sending ? 'Sending…' : 'Send'}
          </button>
        </form>
      </div>
    </div>
  );
}
