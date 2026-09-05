import React, { useState, useEffect } from 'react';
import { useParams } from 'react-router-dom';
import apiClient from '../../api/client';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';

export default function WorkspaceTab() {
  const { id } = useParams();
  const [isChecking, setIsChecking] = useState(false);
  const [tableMarkdown, setTableMarkdown] = useState('');
  const [documents, setDocuments] = useState([]);
  const [summary, setSummary] = useState(null);
  const [error, setError] = useState(null);

  const fetchData = async () => {
    try {
      const [tableRes, docsRes] = await Promise.all([
        apiClient.get(`/projects/${id}/discrepancy-table/`),
        apiClient.get(`/projects/${id}/documents/`)
      ]);
      setTableMarkdown(tableRes.data.markdown || '');
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
    setSummary(null);
    try {
      const response = await apiClient.post(`/projects/${id}/check/`);
      setSummary(response.data);
      // Once it completes, fetch the updated data
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
    doc.findings && doc.findings.length > 0
  );

  return (
    <div className="h-full flex flex-col p-8 overflow-y-auto custom-scrollbar bg-gray-50/50 space-y-8">
      <div className="flex items-center justify-between">
        <h2 className="text-2xl font-bold text-gray-800">Project Discrepancies</h2>
        <button 
          onClick={handleCheckProject} 
          disabled={isChecking}
          className="bg-indigo-600 hover:bg-indigo-700 text-white font-medium px-4 py-2 rounded shadow-sm disabled:opacity-50 transition-colors"
        >
          {isChecking ? "Checking..." : "Check Project"}
        </button>
      </div>

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

      {summary && (
        <div className="flex gap-6 p-4 bg-white border border-gray-200 rounded-md shadow-sm text-sm">
          <div><span className="font-bold text-green-700">Clean:</span> {summary.clean}</div>
          <div><span className="font-bold text-red-700">Flagged:</span> {summary.flagged}</div>
          <div><span className="font-bold text-blue-700">Auto Resolved:</span> {summary.auto_resolved}</div>
          <div><span className="font-bold text-gray-700">Needs Info:</span> {summary.needs_more_info}</div>
        </div>
      )}

      {tableMarkdown ? (
        <div className="bg-white rounded-md shadow-sm border border-gray-200 overflow-hidden">
          <div className="overflow-x-auto p-1">
            <ReactMarkdown 
              remarkPlugins={[remarkGfm]}
              components={{
                table: ({node, ...props}) => <table className="w-full text-left text-sm border-collapse" {...props} />,
                thead: ({node, ...props}) => <thead className="bg-gray-50 border-b border-gray-200" {...props} />,
                th: ({node, ...props}) => <th className="px-4 py-3 font-semibold text-gray-700 border-b border-gray-200" {...props} />,
                td: ({node, ...props}) => <td className="px-4 py-3 border-b border-gray-100 align-top text-gray-800" {...props} />,
                tr: ({node, ...props}) => <tr className="hover:bg-gray-50 transition-colors" {...props} />
              }}
            >
              {tableMarkdown}
            </ReactMarkdown>
          </div>
        </div>
      ) : (
        !isChecking && (
          <div className="p-12 text-center text-gray-500 border-2 border-dashed border-gray-200 rounded-lg bg-gray-50">
            <p className="text-lg">No discrepancy data available yet.</p>
            <p className="text-sm mt-2">Click "Check Project" to run the detection engine on classified invoices.</p>
          </div>
        )
      )}

      {/* Findings Details Section */}
      {docsWithFindings.length > 0 && (
        <div className="space-y-4 mt-8">
          <h3 className="text-xl font-bold text-gray-800 border-b pb-2">Detailed Findings</h3>
          {docsWithFindings.map(doc => (
            <details key={doc.SK} className="bg-white border border-gray-200 rounded-md shadow-sm group">
              <summary className="p-4 font-semibold cursor-pointer select-none hover:bg-gray-50 flex items-center justify-between">
                <span>{doc.filename} <span className="ml-2 text-xs px-2 py-1 bg-gray-200 rounded-full font-normal uppercase tracking-wider">{doc.discrepancy_status.replace('_', ' ')}</span></span>
                <span className="text-gray-400 group-open:rotate-180 transition-transform">▼</span>
              </summary>
              <div className="p-4 border-t border-gray-200 space-y-4">
                {doc.findings.map((finding, idx) => (
                  <div key={idx} className="p-4 bg-gray-50 rounded border border-gray-100">
                    <p className="font-medium text-gray-800 mb-2 capitalize">Issue: {finding.type.replace('_', ' ')}</p>
                    <p className="text-sm text-gray-700 mb-3">{finding.description}</p>
                    
                    {finding.evidence && finding.evidence.length > 0 && (
                      <div className="space-y-2">
                        <h4 className="text-xs font-semibold text-gray-500 uppercase tracking-wider">Evidence</h4>
                        <ul className="space-y-2">
                          {finding.evidence.map((ev, evIdx) => (
                            <li key={evIdx} className="text-sm flex items-start gap-2 bg-white p-2 rounded border border-gray-200">
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
                                  <a href={filenameToUrl[ev.source_doc]} target="_blank" rel="noopener noreferrer" className="text-indigo-600 hover:underline">
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
      )}
    </div>
  );
}
