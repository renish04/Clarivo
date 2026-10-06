import { useState, useEffect, useCallback } from 'react';
import GmailConnectionBar from './GmailConnectionBar';
import InboxView from './InboxView';
import FollowupsView from './FollowupsView';

const SUB_TABS = [
  { key: 'inbox', label: 'Inbox' },
  { key: 'followups', label: 'Follow-ups' },
];

/**
 * Automail: the project's mail and the follow-ups Clarivo sends about it.
 *
 * *target* is a deep link handed down from the workspace — another tab
 * asking for a particular sub-view, follow-up or thread.  It is applied
 * once and then reported as consumed, so re-selecting this tab later
 * does not jump the user back to where somebody else sent them.
 */
export default function AutomailTab({ target, onTargetConsumed, onFollowupsChanged }) {
  const [subTab, setSubTab] = useState(target?.subTab || 'inbox');

  // Passed to the sub-views as their starting selection.
  const [followupDocId, setFollowupDocId] = useState(target?.followupDocId || null);
  const [threadId, setThreadId] = useState(target?.threadId || null);

  // Bumped after a sync so both lists refetch without either of them
  // having to know what the connection bar did.
  const [refreshKey, setRefreshKey] = useState(0);

  // Bumped to make the connection bar re-read status — after a send
  // fails with needs_reconnect, so its reconnect banner appears.
  const [statusReloadKey, setStatusReloadKey] = useState(0);

  // The connected mailbox, so the inbox can leave it out of senders.
  const [accountEmail, setAccountEmail] = useState('');

  useEffect(() => {
    if (!target) return;
    if (target.followupDocId) setFollowupDocId(target.followupDocId);
    if (target.threadId) setThreadId(target.threadId);
    setSubTab(target.subTab || (target.followupDocId ? 'followups' : 'inbox'));
    if (onTargetConsumed) onTargetConsumed();
  }, [target, onTargetConsumed]);

  const handleSynced = useCallback(() => {
    setRefreshKey((key) => key + 1);
    if (onFollowupsChanged) onFollowupsChanged();
  }, [onFollowupsChanged]);

  const handleStatusChange = useCallback((status) => {
    setAccountEmail(status?.connected ? status.email || '' : '');
  }, []);

  const handleNeedsReconnect = useCallback(() => {
    setStatusReloadKey((key) => key + 1);
  }, []);

  const openThread = useCallback((gmailThreadId) => {
    setThreadId(gmailThreadId);
    setSubTab('inbox');
  }, []);

  const openFollowup = useCallback((docId) => {
    setFollowupDocId(docId);
    setSubTab('followups');
  }, []);

  return (
    <div className="h-full flex flex-col p-8 gap-6 overflow-hidden bg-gray-50/50">
      <GmailConnectionBar
        onSynced={handleSynced}
        onStatusChange={handleStatusChange}
        reloadKey={statusReloadKey}
      />

      <div className="flex-shrink-0 flex space-x-1 bg-gray-200/60 p-1 rounded-lg self-start">
        {SUB_TABS.map((tab) => (
          <button
            key={tab.key}
            onClick={() => setSubTab(tab.key)}
            className={`px-4 py-1.5 text-sm font-medium rounded-md transition-colors ${
              subTab === tab.key
                ? 'bg-white text-gray-900 shadow-sm'
                : 'text-gray-600 hover:text-gray-900 hover:bg-gray-200'
            }`}
          >
            {tab.label}
          </button>
        ))}
      </div>

      {subTab === 'inbox' ? (
        <InboxView
          initialThreadId={threadId}
          refreshKey={refreshKey}
          accountEmail={accountEmail}
          onOpenFollowup={openFollowup}
        />
      ) : (
        <FollowupsView
          initialDocId={followupDocId}
          refreshKey={refreshKey}
          onChanged={onFollowupsChanged}
          onOpenThread={openThread}
          onNeedsReconnect={handleNeedsReconnect}
        />
      )}
    </div>
  );
}
