import { useState, useEffect, useCallback } from 'react';
import { useParams } from 'react-router-dom';
import client from '../../api/client';
import FollowupEditor from './FollowupEditor';
import { describeFollowupState, formatRelative } from './constants';

/** The pill text for a row.  "Awaiting reply" also says when it went. */
const stateLabel = (entry) => {
  const { label } = describeFollowupState(entry.followup_state);
  if (entry.followup_state === 'awaiting_reply' && entry.sent_at) {
    return `${label} · sent ${formatRelative(entry.sent_at)}`;
  }
  return label;
};

/**
 * Follow-ups: the invoices to chase on the left, the selected one's
 * draft on the right.
 *
 * The backend already orders the list to be worked from the top — what
 * needs a person first, finished cases last — so it is shown as given.
 */
export default function FollowupsView({ initialDocId, refreshKey, onChanged, onOpenThread, onNeedsReconnect }) {
  const { id: projectId } = useParams();

  const [followups, setFollowups] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [selectedDocId, setSelectedDocId] = useState(initialDocId || null);

  const fetchFollowups = useCallback(
    async (silent = false) => {
      try {
        if (!silent) setLoading(true);
        setError('');
        const res = await client.get(`/projects/${projectId}/followups/`);
        setFollowups(res.data || []);
      } catch (err) {
        console.error(err);
        if (!silent) setError('Failed to load follow-ups.');
      } finally {
        if (!silent) setLoading(false);
      }
    },
    [projectId]
  );

  useEffect(() => {
    fetchFollowups(false);
  }, [fetchFollowups]);

  useEffect(() => {
    if (refreshKey) fetchFollowups(true);
  }, [refreshKey, fetchFollowups]);

  // A deep link that arrives while the view is already open.
  useEffect(() => {
    if (initialDocId) setSelectedDocId(initialDocId);
  }, [initialDocId]);

  // The editor changed something the list shows: re-read it quietly, and
  // let the workspace update the tab badge.
  const handleEditorChanged = useCallback(() => {
    fetchFollowups(true);
    if (onChanged) onChanged();
  }, [fetchFollowups, onChanged]);

  if (loading) {
    return <div className="flex-1 flex items-center justify-center text-gray-500 text-sm">Loading follow-ups…</div>;
  }

  if (error) {
    return (
      <div className="flex-1 flex flex-col items-center justify-center">
        <p className="text-red-500 mb-3 text-sm">{error}</p>
        <button onClick={() => fetchFollowups(false)} className="text-blue-800 hover:underline text-sm">
          Try again
        </button>
      </div>
    );
  }

  if (followups.length === 0) {
    return (
      <div className="flex-1 flex flex-col items-center justify-center p-12 text-center border-2 border-dashed border-gray-200 rounded-xl bg-gray-50">
        <div className="w-16 h-16 bg-white rounded-full flex items-center justify-center mb-4 shadow-sm border border-gray-100">
          <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="text-gray-400">
            <path d="M22 2 11 13"></path>
            <path d="M22 2 15 22l-4-9-9-4Z"></path>
          </svg>
        </div>
        <h3 className="text-lg font-medium text-gray-900 mb-1">Nothing to follow up</h3>
        <p className="text-gray-500 text-sm">
          Invoices that are flagged or need more information will show up here, ready to chase.
        </p>
      </div>
    );
  }

  // The deep-link target if it is in the list, otherwise whatever was
  // last picked, otherwise the first row.  Derived rather than stored,
  // so a refresh that drops the selected row cannot leave it dangling.
  const activeDocId = followups.some((entry) => entry.invoice_doc_id === selectedDocId)
    ? selectedDocId
    : followups[0].invoice_doc_id;
  const activeEntry = followups.find((entry) => entry.invoice_doc_id === activeDocId);

  return (
    <div className="flex-1 min-h-0 flex gap-6">
      {/* Follow-up list */}
      <div className="w-1/3 min-w-[18rem] flex-shrink-0 border border-gray-200 rounded-lg shadow-sm bg-white overflow-y-auto custom-scrollbar">
        <ul className="divide-y divide-gray-200">
          {followups.map((entry) => {
            const display = describeFollowupState(entry.followup_state);
            const isSelected = entry.invoice_doc_id === activeDocId;
            return (
              <li key={entry.invoice_doc_id}>
                <button
                  onClick={() => setSelectedDocId(entry.invoice_doc_id)}
                  className={`w-full text-left px-4 py-3 border-l-4 transition-colors ${
                    isSelected ? 'bg-blue-50 border-blue-800' : 'border-transparent hover:bg-gray-50'
                  }`}
                >
                  <p className="text-sm font-semibold text-gray-900 truncate" title={entry.invoice_filename}>
                    {entry.invoice_filename || entry.invoice_doc_id}
                  </p>
                  <p className="text-xs text-gray-500 mt-0.5 truncate">{entry.supplier || 'Unknown supplier'}</p>
                  <div className="flex flex-wrap items-center gap-2 mt-2">
                    <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-[10px] font-medium border ${display.className}`}>
                      {stateLabel(entry)}
                    </span>
                    {entry.round > 1 && <span className="text-[10px] text-gray-500">Round {entry.round}</span>}
                  </div>
                </button>
              </li>
            );
          })}
        </ul>
      </div>

      {/* Editor — keyed by invoice so switching rows starts it fresh */}
      <div className="flex-1 min-w-0 border border-gray-200 rounded-lg shadow-sm bg-white flex flex-col overflow-hidden">
        <FollowupEditor
          key={activeDocId}
          projectId={projectId}
          docId={activeDocId}
          entry={activeEntry}
          onChanged={handleEditorChanged}
          onOpenThread={onOpenThread}
          onNeedsReconnect={onNeedsReconnect}
        />
      </div>
    </div>
  );
}
