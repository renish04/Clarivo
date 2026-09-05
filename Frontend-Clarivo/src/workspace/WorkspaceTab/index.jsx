import React, { useState, useEffect } from 'react';
import { useParams } from 'react-router-dom';
import apiClient from '../../api/client';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';

export default function WorkspaceTab() {
  const { id } = useParams();
  const [isChecking, setIsChecking] = useState(false);
  const [tableMarkdown, setTableMarkdown] = useState('');
  const [summary, setSummary] = useState(null);
  const [error, setError] = useState(null);

  const fetchTable = async () => {
    try {
      const response = await apiClient.get(`/projects/${id}/discrepancy-table/`);
      setTableMarkdown(response.data.markdown || '');
    } catch (err) {
      console.error("Failed to fetch table", err);
    }
  };

  useEffect(() => {
    if (id) {
      fetchTable();
    }
  }, [id]);

  const handleCheckProject = async () => {
    setIsChecking(true);
    setError(null);
    setSummary(null);
    try {
      const response = await apiClient.post(`/projects/${id}/check/`);
      setSummary(response.data);
      // Once it completes, fetch the updated markdown table
      await fetchTable();
    } catch (err) {
      console.error("Check failed", err);
      setError("Failed to run project check.");
    } finally {
      setIsChecking(false);
    }
  };

  return (
    <div className="p-6 space-y-6 max-w-6xl mx-auto">
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
    </div>
  );
}
