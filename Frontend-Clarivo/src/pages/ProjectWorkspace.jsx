import React, { useState, useEffect } from 'react';
import { useParams, Link } from 'react-router-dom';
import client from '../api/client';
import FilesTab from '../workspace/FilesTab';
import WorkspaceTab from '../workspace/WorkspaceTab';
import ChatPanel from '../workspace/ChatPanel';

export default function ProjectWorkspace() {
  const { id } = useParams();
  const [project, setProject] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  
  // Tab state: 'files' | 'workspace' | 'chat'
  const [activeTab, setActiveTab] = useState('files');

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

  if (loading) {
    return <div className="p-8 text-center text-gray-500">Loading project...</div>;
  }

  if (error || !project) {
    return (
      <div className="p-8 text-center">
        <div className="text-red-500 mb-4">{error}</div>
        <Link to="/projects" className="text-blue-600 hover:underline">
          &larr; Back to Projects
        </Link>
      </div>
    );
  }

  return (
    <div className="max-w-6xl mx-auto p-4 sm:p-8 flex flex-col h-[calc(100vh-2rem)]">
      {/* Header */}
      <div className="mb-6 flex items-center justify-between flex-shrink-0">
        <div>
          <Link to="/projects" className="text-sm text-gray-500 hover:text-blue-600 mb-2 inline-block">
            &larr; Back to Projects
          </Link>
          <h1 className="text-3xl font-bold text-gray-900">{project.name}</h1>
        </div>
      </div>

      {/* Tabs */}
      <div className="border-b border-gray-200 mb-6 flex-shrink-0">
        <nav className="-mb-px flex space-x-8" aria-label="Tabs">
          {['files', 'workspace', 'chat'].map((tab) => (
            <button
              key={tab}
              onClick={() => setActiveTab(tab)}
              className={`
                whitespace-nowrap py-4 px-1 border-b-2 font-medium text-sm capitalize transition-colors
                ${
                  activeTab === tab
                    ? 'border-blue-500 text-blue-600'
                    : 'border-transparent text-gray-500 hover:text-gray-700 hover:border-gray-300'
                }
              `}
            >
              {tab}
            </button>
          ))}
        </nav>
      </div>

      {/* Tab Content Area */}
      <div className="flex-1 bg-white flex flex-col overflow-hidden">
        {activeTab === 'files' && <FilesTab />}
        {activeTab === 'workspace' && <WorkspaceTab />}
        {activeTab === 'chat' && <ChatPanel />}
      </div>
    </div>
  );
}
