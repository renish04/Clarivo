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
    return <div className="h-full flex items-center justify-center text-gray-500">Loading project...</div>;
  }

  if (error || !project) {
    return (
      <div className="h-full flex flex-col items-center justify-center">
        <div className="text-red-500 mb-4">{error}</div>
        <Link to="/projects" className="text-blue-600 hover:underline">
          Select another project
        </Link>
      </div>
    );
  }

  return (
    <div className="h-full flex flex-col bg-white w-full">
      {/* Header */}
      <div className="px-8 pt-6 pb-2 border-b border-gray-200 flex-shrink-0 bg-white">
        <h1 className="text-2xl font-bold text-gray-900 mb-4">{project.name}</h1>
        
        {/* Tabs */}
        <nav className="-mb-px flex space-x-8" aria-label="Tabs">
          {['files', 'workspace', 'chat'].map((tab) => (
            <button
              key={tab}
              onClick={() => setActiveTab(tab)}
              className={`
                whitespace-nowrap py-3 px-1 border-b-2 font-medium text-sm capitalize transition-colors
                ${
                  activeTab === tab
                    ? 'border-blue-600 text-blue-600'
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
      <div className="flex-1 overflow-hidden relative bg-white">
        {activeTab === 'files' && <FilesTab />}
        {activeTab === 'workspace' && <WorkspaceTab />}
        {activeTab === 'chat' && <ChatPanel />}
      </div>
    </div>
  );
}
