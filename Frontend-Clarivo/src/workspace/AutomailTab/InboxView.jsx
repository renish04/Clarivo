import { useState, useEffect, useCallback } from 'react';
import { useParams } from 'react-router-dom';
import client from '../../api/client';
import {
  describeAttachmentStatus,
  formatDateTime,
  formatRelative,
  routeReasonLabel,
} from './constants';

// The list is re-read on this interval while the view is open, so mail
// a sync ingests in the background shows up without a click.
const THREAD_POLL_INTERVAL_MS = 30000;

function PaperclipIcon({ className = '' }) {
  return (
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className={className} aria-label="Has attachments">
      <path d="m21.44 11.05-9.19 9.19a6 6 0 0 1-8.49-8.49l8.57-8.57A4 4 0 1 1 18 8.84l-8.59 8.57a2 2 0 0 1-2.83-2.83l8.49-8.48"></path>
    </svg>
  );
}

/**
 * Who a thread is with, from this user's side of it: every participant
 * except the connected mailbox itself.
 */
const sendersOf = (thread, accountEmail) => {
  const own = (accountEmail || '').toLowerCase();
  const others = (thread.participants || []).filter((address) => address.toLowerCase() !== own);
  return others.length > 0 ? others.join(', ') : 'You';
};

function AttachmentChip({ attachment }) {
  const display = describeAttachmentStatus(attachment.status);
  const label = attachment.filename || 'Attachment';

  return (
    <span className="inline-flex items-center gap-2 pl-2.5 pr-1.5 py-1 rounded-md border border-gray-200 bg-white text-xs">
      <PaperclipIcon className="text-gray-400 flex-shrink-0" />
      <span className="text-gray-800 font-medium truncate max-w-[14rem]" title={label}>
        {label}
      </span>
      <span className={`inline-flex items-center px-1.5 py-0.5 rounded-full text-[10px] font-medium border ${display.className}`}>
        {display.label}
      </span>
      {attachment.view_url && (
        <a
          href={attachment.view_url}
          target="_blank"
          rel="noopener noreferrer"
          className="px-1.5 text-blue-800 hover:underline font-medium"
        >
          Open
        </a>
      )}
    </span>
  );
}

/**
 * The project's mail, Gmail-style: threads on the left, the selected
 * conversation on the right.
 *
 * Email bodies come from outside and are never rendered as HTML — they
 * are plain text, with line breaks kept by whitespace-pre-wrap.
 */
export default function InboxView({ initialThreadId, refreshKey, accountEmail, onOpenFollowup }) {
  const { id: projectId } = useParams();

  const [threads, setThreads] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  const [selectedThreadId, setSelectedThreadId] = useState(initialThreadId || null);
  const [messages, setMessages] = useState([]);
  const [messagesLoading, setMessagesLoading] = useState(false);
  const [messagesError, setMessagesError] = useState('');

  const fetchThreads = useCallback(
    async (silent = false) => {
      try {
        if (!silent) setLoading(true);
        setError('');
        const res = await client.get(`/projects/${projectId}/mail/threads/`);
        setThreads(res.data || []);
      } catch (err) {
        console.error(err);
        if (!silent) setError('Failed to load mail for this project.');
      } finally {
        if (!silent) setLoading(false);
      }
    },
    [projectId]
  );

  useEffect(() => {
    fetchThreads(false);
  }, [fetchThreads]);

  // A sync elsewhere bumps refreshKey; the poll covers everything else.
  // Both are silent, and neither touches the selection.
  useEffect(() => {
    if (refreshKey) fetchThreads(true);
  }, [refreshKey, fetchThreads]);

  useEffect(() => {
    const timer = setInterval(() => fetchThreads(true), THREAD_POLL_INTERVAL_MS);
    return () => clearInterval(timer);
  }, [fetchThreads]);

  // A deep link that arrives while the view is already open.
  useEffect(() => {
    if (initialThreadId) setSelectedThreadId(initialThreadId);
  }, [initialThreadId]);

  const selectedThread = threads.find((thread) => thread.thread_id === selectedThreadId);

  // Re-read the open conversation when its message count moves, so a
  // reply arriving during a poll appears without reselecting the thread.
  const selectedMessageCount = selectedThread?.message_count;

  useEffect(() => {
    if (!selectedThreadId) {
      setMessages([]);
      return undefined;
    }

    let cancelled = false;

    const fetchMessages = async () => {
      try {
        setMessagesLoading(true);
        setMessagesError('');
        const res = await client.get(
          `/projects/${projectId}/mail/threads/${encodeURIComponent(selectedThreadId)}/`
        );
        if (!cancelled) setMessages(res.data || []);
      } catch (err) {
        console.error(err);
        if (!cancelled) setMessagesError('Failed to load this conversation.');
      } finally {
        if (!cancelled) setMessagesLoading(false);
      }
    };

    fetchMessages();
    // Switching threads mid-load must not let the slower response win.
    return () => {
      cancelled = true;
    };
  }, [projectId, selectedThreadId, selectedMessageCount]);

  if (loading) {
    return <div className="flex-1 flex items-center justify-center text-gray-500 text-sm">Loading mail…</div>;
  }

  if (error) {
    return (
      <div className="flex-1 flex flex-col items-center justify-center">
        <p className="text-red-500 mb-3 text-sm">{error}</p>
        <button onClick={() => fetchThreads(false)} className="text-blue-800 hover:underline text-sm">
          Try again
        </button>
      </div>
    );
  }

  if (threads.length === 0) {
    return (
      <div className="flex-1 flex flex-col items-center justify-center p-12 text-center border-2 border-dashed border-gray-200 rounded-xl bg-gray-50">
        <div className="w-16 h-16 bg-white rounded-full flex items-center justify-center mb-4 shadow-sm border border-gray-100">
          <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="text-gray-400">
            <rect x="2" y="4" width="20" height="16" rx="2"></rect>
            <path d="m22 7-10 5L2 7"></path>
          </svg>
        </div>
        <h3 className="text-lg font-medium text-gray-900 mb-2">No mail in this project yet</h3>
        <p className="text-gray-500 text-sm max-w-lg">
          Only emails matched to this project are shown here: replies to Clarivo follow-ups, emails
          that mention the project name, or emails from a known supplier. All other mail is ignored
          and never stored.
        </p>
      </div>
    );
  }

  return (
    <div className="flex-1 min-h-0 flex gap-6">
      {/* Thread list */}
      <div className="w-1/3 min-w-[18rem] flex-shrink-0 border border-gray-200 rounded-lg shadow-sm bg-white overflow-y-auto custom-scrollbar">
        <ul className="divide-y divide-gray-200">
          {threads.map((thread) => {
            const isSelected = thread.thread_id === selectedThreadId;
            const reason = routeReasonLabel(thread.route_reason);
            return (
              <li key={thread.thread_id || thread.subject}>
                <button
                  onClick={() => setSelectedThreadId(thread.thread_id)}
                  className={`w-full text-left px-4 py-3 border-l-4 transition-colors ${
                    isSelected ? 'bg-blue-50 border-blue-800' : 'border-transparent hover:bg-gray-50'
                  }`}
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-sm text-gray-700 truncate">{sendersOf(thread, accountEmail)}</span>
                    <span className="flex items-center gap-1.5 flex-shrink-0 text-xs text-gray-400">
                      {thread.has_attachments && <PaperclipIcon />}
                      {formatRelative(thread.last_message_at)}
                    </span>
                  </div>

                  <p className="text-sm font-semibold text-gray-900 truncate mt-0.5">
                    {thread.subject || '(no subject)'}
                  </p>

                  {thread.snippet && <p className="text-xs text-gray-500 truncate mt-0.5">{thread.snippet}</p>}

                  {reason && (
                    <span className="inline-flex items-center mt-2 px-2 py-0.5 rounded text-[10px] font-medium bg-gray-100 text-gray-500">
                      {reason}
                    </span>
                  )}
                </button>
              </li>
            );
          })}
        </ul>
      </div>

      {/* Conversation */}
      <div className="flex-1 min-w-0 border border-gray-200 rounded-lg shadow-sm bg-white flex flex-col overflow-hidden">
        {!selectedThreadId ? (
          <div className="flex-1 flex items-center justify-center text-sm text-gray-500 p-8 text-center">
            Select a conversation to read it.
          </div>
        ) : (
          <>
            <div className="flex-shrink-0 px-6 py-4 border-b border-gray-200">
              <div className="flex items-start justify-between gap-4">
                <h3 className="text-base font-semibold text-gray-900">
                  {selectedThread?.subject || messages[0]?.subject || '(no subject)'}
                </h3>
                {selectedThread?.linked_invoice_doc_id && onOpenFollowup && (
                  <button
                    onClick={() => onOpenFollowup(selectedThread.linked_invoice_doc_id)}
                    className="flex-shrink-0 inline-flex items-center px-2.5 py-1 rounded-full text-xs font-medium border bg-blue-50 text-blue-800 border-blue-200 hover:bg-blue-100 transition-colors"
                  >
                    Linked follow-up &rarr;
                  </button>
                )}
              </div>
              {selectedThread && (
                <p className="text-xs text-gray-500 mt-1">
                  {selectedThread.message_count} message{selectedThread.message_count === 1 ? '' : 's'}
                  {selectedThread.route_reason && ` · ${routeReasonLabel(selectedThread.route_reason)}`}
                </p>
              )}
            </div>

            <div className="flex-1 overflow-y-auto custom-scrollbar p-6 space-y-4">
              {messagesLoading && messages.length === 0 && (
                <p className="text-sm text-gray-500">Loading conversation…</p>
              )}

              {messagesError && (
                <div className="p-3 bg-red-50 border border-red-200 rounded-md text-sm text-red-800">
                  {messagesError}
                </div>
              )}

              {messages.map((message) => {
                const isOutbound = message.direction === 'outbound';
                return (
                  <div
                    key={message.gmail_message_id}
                    className={`rounded-lg border border-gray-200 p-4 ${
                      isOutbound ? 'border-l-4 border-l-blue-300 bg-blue-50/30' : 'bg-white'
                    }`}
                  >
                    <div className="flex items-start justify-between gap-3 mb-3">
                      <div className="min-w-0">
                        <p className="text-sm">
                          <span className="font-medium text-gray-900">
                            {message.from_name || message.from_email || 'Unknown sender'}
                          </span>
                          {message.from_name && message.from_email && (
                            <span className="text-gray-500 ml-1.5">&lt;{message.from_email}&gt;</span>
                          )}
                        </p>
                        {message.to && <p className="text-xs text-gray-500 mt-0.5 truncate">to {message.to}</p>}
                      </div>
                      <div className="flex items-center gap-2 flex-shrink-0">
                        {isOutbound && (
                          <span className="inline-flex items-center px-2 py-0.5 rounded-full text-[10px] font-medium border bg-blue-50 text-blue-800 border-blue-200">
                            Sent by Clarivo
                          </span>
                        )}
                        <span className="text-xs text-gray-400" title={formatDateTime(message.received_at)}>
                          {formatDateTime(message.received_at)}
                        </span>
                      </div>
                    </div>

                    {message.body_text ? (
                      <div className="text-sm text-gray-800 whitespace-pre-wrap break-words">{message.body_text}</div>
                    ) : (
                      <p className="text-sm text-gray-400 italic">No message text.</p>
                    )}

                    {message.attachments?.length > 0 && (
                      <div className="flex flex-wrap gap-2 mt-3 pt-3 border-t border-gray-100">
                        {message.attachments.map((attachment) => (
                          <AttachmentChip key={attachment.doc_id} attachment={attachment} />
                        ))}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          </>
        )}
      </div>
    </div>
  );
}
