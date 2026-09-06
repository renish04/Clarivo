import React, { useState, useEffect } from 'react';
import { useParams } from 'react-router-dom';
import apiClient from '../../api/client';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';

const getStatusBadgeClass = (status) => {
  const s = status?.toLowerCase() || '';
  if (s === 'clean') return 'bg-green-100 text-green-800 border-green-200';
  if (s === 'auto_resolved') return 'bg-teal-100 text-teal-800 border-teal-200';
  if (s === 'flagged') return 'bg-red-100 text-red-800 border-red-200';
  if (s === 'needs_more_info') return 'bg-purple-100 text-purple-800 border-purple-200';
  return 'bg-gray-100 text-gray-800 border-gray-200';
};

export default function WorkspaceTab() {
  const { id } = useParams();
  const [isChecking, setIsChecking] = useState(false);
  const [tableMarkdown, setTableMarkdown] = useState('');
  const [documents, setDocuments] = useState([]);
  const [summary, setSummary] = useState(null);
  const [error, setError] = useState(null);

  const [activeSubTab, setActiveSubTab] = useState('overview');

  const fetchData = async () => {
    try {
      const [tableRes, docsRes] = await Promise.all([
        apiClient.get(`/projects/${id}/discrepancy-table/`),
        apiClient.get(`/projects/${id}/documents/`)
      ]);
      setTableMarkdown(tableRes.data.markdown || '');
      if (tableRes.data.summary) {
        setSummary(tableRes.data.summary);
      }
      setDocuments(docsRes.data || []);
    } catch (err) {
      console.error("Failed to fetch data", err);
    }
  };

  useEffect(() => {
    if (id) {
      fetchData();
    }
  }, [id]);

  const handleCheckProject = async () => {
    setIsChecking(true);
    setError(null);
    try {
      const response = await apiClient.post(`/projects/${id}/check/`);
      setSummary(response.data);
      await fetchData();
    } catch (err) {
      console.error("Check failed", err);
      setError("Failed to run project check.");
    } finally {
      setIsChecking(false);
    }
  };

  const filenameToUrl = documents.reduce((acc, doc) => ({...acc, [doc.filename]: doc.view_url}), {});
  const docsWithFindings = documents.filter(doc => 
    doc.status === "checked" && 
    ["flagged", "auto_resolved", "needs_more_info"].includes(doc.discrepancy_status) &&
    ((doc.findings && doc.findings.length > 0) || doc.resolution)
  );

  return (
    <div className="h-full flex flex-col p-8 bg-gray-50/50 overflow-hidden">
      <div className="flex items-center justify-between flex-shrink-0 mb-6">
        <div className="flex items-center gap-6">
          <h2 className="text-2xl font-bold text-gray-800">Project Discrepancies</h2>
          
          <div className="flex space-x-1 bg-gray-200/60 p-1 rounded-lg">
            <button
              onClick={() => setActiveSubTab('overview')}
              className={`px-4 py-1.5 text-sm font-medium rounded-md transition-colors ${
                activeSubTab === 'overview'
                  ? 'bg-white text-gray-900 shadow-sm'
                  : 'text-gray-600 hover:text-gray-900 hover:bg-gray-200'
              }`}
            >
              Overview Table
            </button>
            <button
              onClick={() => setActiveSubTab('details')}
              className={`px-4 py-1.5 text-sm font-medium rounded-md transition-colors ${
                activeSubTab === 'details'
                  ? 'bg-white text-gray-900 shadow-sm'
                  : 'text-gray-600 hover:text-gray-900 hover:bg-gray-200'
              }`}
            >
              Detailed Findings
              {docsWithFindings.length > 0 && (
                <span className="ml-2 inline-flex items-center justify-center px-2 py-0.5 text-xs font-bold leading-none text-white bg-blue-800 rounded-full">
                  {docsWithFindings.length}
                </span>
              )}
            </button>
          </div>
        </div>

        <button 
          onClick={handleCheckProject} 
          disabled={isChecking}
          className="bg-blue-800 hover:bg-blue-900 text-white font-medium px-4 py-2 rounded shadow-sm disabled:opacity-50 transition-colors"
        >
          {isChecking ? "Checking..." : "Check Project"}
        </button>
      </div>

      <div className="flex-shrink-0 space-y-4 mb-6">
        {summary && (
          <div className="flex items-center gap-8 p-6 bg-white border border-gray-200 rounded-lg shadow-sm">
            <div className="flex flex-col items-start pr-8 border-r border-gray-200">
              <span className="text-4xl font-bold text-blue-900">
                {summary.touchless_rate !== null && summary.touchless_rate !== undefined ? `${summary.touchless_rate}%` : '--'}
              </span>
              <span className="text-sm font-medium text-gray-500 mt-1">resolved without human review</span>
            </div>
            <div className="flex flex-wrap items-center gap-4 text-sm font-medium text-gray-600">
              <span className="text-green-700">{summary.clean} clean</span>
              <span>&middot;</span>
              <span className="text-teal-700">{summary.auto_resolved} auto-resolved</span>
              <span>&middot;</span>
              <span className="text-red-700">{summary.flagged} flagged</span>
              <span>&middot;</span>
              <span className="text-purple-700">{summary.needs_more_info} needs review</span>
            </div>
          </div>
        )}

        {isChecking && (
          <div className="p-4 bg-yellow-50 text-yellow-800 rounded-md border border-yellow-200 shadow-sm animate-pulse">
            <p className="font-medium">Checking documents... this may take a minute.</p>
            <p className="text-sm mt-1">We are retrieving project context and running discrepancy detection across your invoices.</p>
          </div>
        )}

        {error && (
          <div className="p-4 bg-red-50 text-red-800 rounded-md border border-red-200 shadow-sm">
            {error}
          </div>
        )}
      </div>

      <div className="flex-1 flex flex-col min-h-0">
        
        {activeSubTab === 'overview' && (
          <div className="flex-1 flex flex-col min-h-0 gap-6">
            {tableMarkdown ? (
              <div className="flex-1 flex flex-col min-h-0">
                <div className="bg-white rounded-md shadow-sm border border-gray-200 overflow-y-auto custom-scrollbar flex-1">
                  <ReactMarkdown 
                    remarkPlugins={[remarkGfm]}
                    components={{
                      table: ({node, ...props}) => <table className="w-full text-left text-sm border-collapse" {...props} />,
                      thead: ({node, ...props}) => <thead className="bg-gray-50 border-b border-gray-200 sticky top-0 z-10" {...props} />,
                      th: ({node, ...props}) => <th className="px-4 py-3 font-semibold text-gray-700 border-b border-gray-200" {...props} />,
                      td: ({node, ...props}) => {
                        const content = (props.children && props.children[0]) ? String(props.children[0]).trim() : '';
                        const lower = content.toLowerCase();
                        const knownStatuses = ['clean', 'auto_resolved', 'flagged', 'needs_more_info'];
                        
                        if (knownStatuses.includes(lower)) {
                          return (
                            <td className="px-4 py-3 border-b border-gray-100 align-top">
                              <span className={`px-2 py-1 border rounded-full text-xs font-semibold uppercase tracking-wider whitespace-nowrap ${getStatusBadgeClass(lower)}`}>
                                {content.replace('_', ' ')}
                              </span>
                            </td>
                          );
                        }
                        return <td className="px-4 py-3 border-b border-gray-100 align-top text-gray-800" {...props} />;
                      },
                      tr: ({node, ...props}) => <tr className="hover:bg-gray-50 transition-colors" {...props} />
                    }}
                  >
                    {tableMarkdown}
                  </ReactMarkdown>
                </div>
              </div>
            ) : (
              !isChecking && (
                <div className="flex-1 flex flex-col items-center justify-center p-12 text-center text-gray-500 border-2 border-dashed border-gray-200 rounded-lg bg-gray-50">
                  <p className="text-lg">No discrepancy data available yet.</p>
                  <p className="text-sm mt-2">Click "Check Project" to run the detection engine on classified invoices.</p>
                </div>
              )
            )}
          </div>
        )}

        {activeSubTab === 'details' && (
          <div className="flex-1 flex flex-col min-h-0">
            {docsWithFindings.length > 0 ? (
              <div className="overflow-y-auto custom-scrollbar flex-1 pr-2 space-y-4">
                {docsWithFindings.map(doc => (
                  <details key={doc.SK} className="bg-white border border-gray-200 rounded-md shadow-sm group">
                    <summary className="p-4 font-semibold cursor-pointer select-none hover:bg-gray-50 flex items-center justify-between">
                      <span>
                        {doc.filename} 
                        <span className={`ml-3 text-xs px-2.5 py-1 border rounded-full font-bold uppercase tracking-wider ${getStatusBadgeClass(doc.discrepancy_status)}`}>
                          {doc.discrepancy_status.replace('_', ' ')}
                        </span>
                      </span>
                      <span className="text-gray-400 group-open:rotate-180 transition-transform">▼</span>
                    </summary>
                    <div className="p-4 border-t border-gray-200 space-y-4">
                      {doc.resolution && (
                        <div className="p-4 bg-teal-50 rounded border border-teal-200">
                          <h4 className="text-sm font-bold text-teal-800 uppercase tracking-wider mb-1">Auto Resolution</h4>
                          <p className="text-sm text-teal-900">{doc.resolution}</p>
                        </div>
                      )}

                      {doc.findings && doc.findings.map((finding, idx) => (
                        <div key={idx} className="p-4 bg-gray-50 rounded border border-gray-200">
                          <p className="font-bold text-gray-800 mb-2 capitalize">Issue: {finding.type.replace('_', ' ')}</p>
                          <p className="text-sm text-gray-700 mb-3">{finding.description}</p>
                          
                          {finding.evidence && finding.evidence.length > 0 && (
                            <div className="space-y-2 mt-3">
                              <h4 className="text-xs font-bold text-gray-500 uppercase tracking-wider">Evidence</h4>
                              <ul className="space-y-2">
                                {finding.evidence.map((ev, evIdx) => (
                                  <li key={evIdx} className="text-sm flex items-start gap-2 bg-white p-2 rounded border border-gray-200 shadow-sm">
                                    <div className="mt-0.5">
                                      {ev.verified === false ? (
                                        <span title="Warning: AI claim not found exactly in source text" className="text-red-500 text-base leading-none">⚠️</span>
                                      ) : (
                                        <span title="Verified exactly in source text" className="text-green-500 text-base leading-none">✅</span>
                                      )}
                                    </div>
                                    <div>
                                      <span className="text-gray-800 font-medium">"{ev.claim}"</span>
                                      <span className="text-gray-400 mx-2">—</span>
                                      {filenameToUrl[ev.source_doc] ? (
                                        <a href={filenameToUrl[ev.source_doc]} target="_blank" rel="noopener noreferrer" className="text-blue-700 hover:underline font-medium">
                                          {ev.source_doc}
                                        </a>
                                      ) : (
                                        <span className="text-gray-600 italic">{ev.source_doc}</span>
                                      )}
                                    </div>
                                  </li>
                                ))}
                              </ul>
                            </div>
                          )}
                        </div>
                      ))}
                    </div>
                  </details>
                ))}
              </div>
            ) : (
              <div className="flex-1 flex flex-col items-center justify-center p-12 text-center text-gray-500 border-2 border-dashed border-gray-200 rounded-lg bg-gray-50">
                <p className="text-lg">No detailed findings available.</p>
                <p className="text-sm mt-2">Any flagged or unresolved discrepancies will appear here.</p>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

