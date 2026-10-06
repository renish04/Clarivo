import React, { useState, useEffect, useCallback } from 'react';
import { useParams, useSearchParams, Link } from 'react-router-dom';
import client from '../api/client';
import FilesTab from '../workspace/FilesTab';
import WorkspaceTab from '../workspace/WorkspaceTab';
import ChatPanel from '../workspace/ChatPanel';
import AutomailTab from '../workspace/AutomailTab';
import { ATTENTION_STATES } from '../workspace/AutomailTab/constants';

// The tab bar.  `key` is what the URL and the deep links use; `label`
// is what the user reads.
const TABS = [
  { key: 'files', label: 'Files' },
  { key: 'workspace', label: 'Workspace' },
  { key: 'AI chat', label: 'AI chat' },
  { key: 'automail', label: 'Automail' },
];

export default function ProjectWorkspace() {
  const { id } = useParams();
  const [searchParams] = useSearchParams();
  const [project, setProject] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  // Tab state: 'files' | 'workspace' | 'AI chat' | 'automail'.  Started
  // from ?tab= so the Gmail OAuth round trip, which leaves the SPA
  // entirely, can land the user back on the tab they set out from.
  const [activeTab, setActiveTab] = useState(
    searchParams.get('tab') === 'automail' ? 'automail' : 'files'
  );

  // An optional {subTab, followupDocId, threadId} another tab asked
  // Automail to open at.  Held here rather than in Automail because the
  // request is made before that tab exists.
  const [automailTarget, setAutomailTarget] = useState(null);

  // Follow-ups needing a person's attention, for the count badge on the
  // Automail tab.  Fetched with the workspace so the badge is right
  // before the tab has ever been opened.
  const [attentionCount, setAttentionCount] = useState(0);

  useEffect(() => {
    const fetchProject = async () => {
      try {
        setLoading(true);
        const res = await client.get(`/projects/${id}/`);
        setProject(res.data);
      } catch (err) {
        console.error(err);
        setError('Failed to load project details.');
      } finally {
        setLoading(false);
      }
    };

    fetchProject();
  }, [id]);

  const fetchFollowupCount = useCallback(async () => {
    try {
      const res = await client.get(`/projects/${id}/followups/`);
      const needingAttention = (res.data || []).filter((entry) =>
        ATTENTION_STATES.includes(entry.followup_state)
      );
      setAttentionCount(needingAttention.length);
    } catch (err) {
      // A badge is not worth an error banner: the tab still works, and
      // the count corrects itself on the next read.
      console.error(err);
    }
  }, [id]);

  useEffect(() => {
    fetchFollowupCount();
  }, [fetchFollowupCount]);

  /**
   * Open the Automail tab, optionally at a particular follow-up or
   * thread.  Handed to the other tabs so a flagged invoice can be
   * followed up from where it was found.
   */
  const navigateToAutomail = useCallback((target) => {
    setAutomailTarget(target || null);
    setActiveTab('automail');
  }, []);

  if (loading) {
    return <div className="h-full flex items-center justify-center text-gray-500">Loading project...</div>;
  }

  if (error || !project) {
    return (
      <div className="h-full flex flex-col items-center justify-center">
        <div className="text-red-500 mb-4">{error}</div>
        <Link to="/projects" className="text-blue-800 hover:underline">
          Select another project
        </Link>
      </div>
    );
  }

  return (
    <div className="h-full flex flex-col bg-white w-full">
      {/* Header */}
      <div className="px-8 pt-6 pb-2 border-b border-gray-200 flex-shrink-0 bg-white relative">
        <div className="flex justify-between items-start">
          <h1 className="text-2xl font-bold text-gray-900 mb-4">{project.name}</h1>
          <Link to="/projects" className="text-sm font-medium text-gray-500 hover:text-gray-900 transition-colors mt-1">
            Back &larr;
          </Link>
        </div>

        {/* Tabs */}
        <nav className="-mb-px flex space-x-8" aria-label="Tabs">
          {TABS.map((tab) => (
            <button
              key={tab.key}
              onClick={() => setActiveTab(tab.key)}
              className={`
                whitespace-nowrap py-3 px-1 border-b-2 font-medium text-sm capitalize transition-colors
                ${
                  activeTab === tab.key
                    ? 'border-blue-800 text-blue-800'
                    : 'border-transparent text-gray-500 hover:text-gray-700 hover:border-gray-300'
                }
              `}
            >
              {tab.label}
              {tab.key === 'automail' && attentionCount > 0 && (
                <span className="ml-2 inline-flex items-center justify-center px-2 py-0.5 text-xs font-bold leading-none text-white bg-blue-800 rounded-full">
                  {attentionCount}
                </span>
              )}
            </button>
          ))}
        </nav>
      </div>

      {/* Tab Content Area */}
      <div className="flex-1 overflow-hidden relative bg-white">
        {activeTab === 'files' && <FilesTab navigateToAutomail={navigateToAutomail} />}
        {activeTab === 'workspace' && <WorkspaceTab navigateToAutomail={navigateToAutomail} />}
        {activeTab === 'AI chat' && <ChatPanel />}
        {activeTab === 'automail' && (
          <AutomailTab
            target={automailTarget}
            onTargetConsumed={() => setAutomailTarget(null)}
            onFollowupsChanged={fetchFollowupCount}
          />
        )}
      </div>
    </div>
  );
}
