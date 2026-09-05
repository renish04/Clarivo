import { useState, useEffect, useCallback } from 'react';
import { useParams } from 'react-router-dom';
import axios from 'axios';
import client from '../../api/client';

export default function FilesTab() {
  const { id: projectId } = useParams();
  const [documents, setDocuments] = useState([]);
  const [loading, setLoading] = useState(true);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState('');
  const [indexingDocs, setIndexingDocs] = useState(new Set());
  const [classifyingDocs, setClassifyingDocs] = useState(new Set());

  const fetchDocuments = useCallback(async (silent = false) => {
    const isSilent = silent === true;
    try {
      if (!isSilent) setLoading(true);
      setError('');
      const res = await client.get(`/projects/${projectId}/documents/`);
      setDocuments(res.data);

      const extractedDocs = res.data.filter(doc => doc.status === 'extracted');
      if (extractedDocs.length > 0) {
        const docIds = extractedDocs.map(doc => doc.SK.replace('DOC#', ''));
        
        setIndexingDocs(prev => {
          const next = new Set(prev);
          docIds.forEach(id => next.add(id));
          return next;
        });

        Promise.allSettled(
          docIds.map(id => client.post(`/projects/${projectId}/documents/${id}/embed/`))
        ).then(() => {
          client.get(`/projects/${projectId}/documents/`)
            .then(refreshRes => {
              setDocuments(refreshRes.data);
            })
            .catch(console.error)
            .finally(() => {
              setIndexingDocs(prev => {
                const next = new Set(prev);
                docIds.forEach(id => next.delete(id));
                return next;
              });
            });
        });
      }

      const embeddedDocs = res.data.filter(doc => doc.status === 'embedded');
      if (embeddedDocs.length > 0) {
        const docIds = embeddedDocs.map(doc => doc.SK.replace('DOC#', ''));
        
        setClassifyingDocs(prev => {
          const next = new Set(prev);
          docIds.forEach(id => next.add(id));
          return next;
        });

        Promise.allSettled(
          docIds.map(id => client.post(`/projects/${projectId}/documents/${id}/classify/`))
        ).then(() => {
          client.get(`/projects/${projectId}/documents/`)
            .then(refreshRes => {
              setDocuments(refreshRes.data);
            })
            .catch(console.error)
            .finally(() => {
              setClassifyingDocs(prev => {
                const next = new Set(prev);
                docIds.forEach(id => next.delete(id));
                return next;
              });
            });
        });
      }
    } catch (err) {
      console.error(err);
      if (!isSilent) setError('Failed to load documents.');
    } finally {
      if (!isSilent) setLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    fetchDocuments();
  }, [fetchDocuments]);

  const handleRefreshClick = () => fetchDocuments(false);

  const handleFileSelect = async (e) => {
    const file = e.target.files[0];
    if (!file) return;

    try {
      setUploading(true);
      setError('');

      // 1. Get presigned upload URL from our backend
      const presignRes = await client.post(
        `/projects/${projectId}/documents/presign/`,
        { filename: file.name, content_type: file.type }
      );
      const { upload_url, s3_key, doc_id } = presignRes.data;

      // 2. PUT file bytes directly to S3 (plain axios, no auth header)
      await axios.put(upload_url, file, {
        headers: { 'Content-Type': file.type },
      });

      // 3. Confirm upload with our backend
      await client.post(`/projects/${projectId}/documents/confirm/`, {
        doc_id,
        filename: file.name,
        s3_key,
        file_type: file.type,
      });

      // 4. Refresh the list
      await fetchDocuments();
    } catch (err) {
      console.error(err);
      setError('Upload failed. Please try again.');
    } finally {
      setUploading(false);
      // Reset the input so the same file can be re-selected
      e.target.value = '';
    }
  };

  if (loading) {
    return <div className="p-8 text-center text-gray-500">Loading documents...</div>;
  }

  if (error) {
    return (
      <div className="p-8 text-center">
        <p className="text-red-500 mb-4">{error}</p>
        <button
          onClick={fetchDocuments}
          className="text-blue-600 hover:underline"
        >
          Try again
        </button>
      </div>
    );
  }

  return (
    <div className="h-full flex flex-col p-8 overflow-hidden bg-white">
      <div className="flex items-center justify-between mb-6 flex-shrink-0">
        <h2 className="text-xl font-semibold text-gray-900">Documents</h2>
        <div className="flex items-center gap-3">
          {uploading && (
            <span className="text-sm font-medium text-gray-500 animate-pulse">Uploading...</span>
          )}
          <label
            className={`px-4 py-2 text-sm font-medium rounded-md transition-colors cursor-pointer shadow-sm ${
              uploading
                ? 'bg-gray-100 text-gray-400 cursor-not-allowed'
                : 'bg-blue-600 text-white hover:bg-blue-700'
            }`}
          >
            Upload File
            <input
              type="file"
              className="hidden"
              onChange={handleFileSelect}
              disabled={uploading}
            />
          </label>
          <button
            onClick={handleRefreshClick}
            disabled={uploading}
            className="px-4 py-2 text-sm font-medium bg-white border border-gray-300 text-gray-700 rounded-md hover:bg-gray-50 transition-colors disabled:opacity-50 shadow-sm"
          >
            Refresh
          </button>
        </div>
      </div>

      {documents.length === 0 ? (
        <div className="flex-1 flex flex-col items-center justify-center p-12 text-center border-2 border-dashed border-gray-200 rounded-xl bg-gray-50">
          <div className="w-16 h-16 bg-white rounded-full flex items-center justify-center mb-4 shadow-sm border border-gray-100">
            <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="text-gray-400">
              <path d="M13 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"></path>
              <polyline points="13 2 13 9 20 9"></polyline>
            </svg>
          </div>
          <h3 className="text-lg font-medium text-gray-900 mb-1">No documents yet</h3>
          <p className="text-gray-500">Upload your first invoice to get started.</p>
        </div>
      ) : (
        <div className="flex-1 overflow-y-auto custom-scrollbar border border-gray-200 rounded-lg shadow-sm">
          <table className="w-full text-sm text-left whitespace-nowrap">
            <thead className="bg-gray-50 sticky top-0 z-10 border-b border-gray-200">
              <tr className="text-gray-500 uppercase text-xs tracking-wider">
                <th className="py-3 px-6 font-medium">Filename</th>
                <th className="py-3 px-6 font-medium">Type</th>
                <th className="py-3 px-6 font-medium">Status</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-200 bg-white">
              {documents.map((doc) => {
                const docId = doc.SK.replace('DOC#', '');
                const isIndexing = indexingDocs.has(docId);
                const isClassifying = classifyingDocs.has(docId);
                
                return (
                  <tr key={doc.SK} className="hover:bg-gray-50 transition-colors">
                    <td className="py-4 px-6">
                      <a
                        href={doc.view_url}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="text-blue-600 font-medium hover:text-blue-800 hover:underline flex items-center gap-2"
                      >
                        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="text-gray-400">
                          <path d="M13 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"></path>
                          <polyline points="13 2 13 9 20 9"></polyline>
                        </svg>
                        {doc.filename}
                      </a>
                    </td>
                    <td className="py-4 px-6 text-gray-600">
                      {doc.doc_type ? (
                        <span className="capitalize">{doc.doc_type}</span>
                      ) : (
                        <span className="text-gray-400 italic">Unknown</span>
                      )}
                    </td>
                    <td className="py-4 px-6">
                      {isIndexing ? (
                        <span className="inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-blue-100 text-blue-800 border border-blue-200">
                          <svg className="animate-spin -ml-0.5 mr-1.5 h-3 w-3 text-blue-800" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24">
                            <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4"></circle>
                            <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
                          </svg>
                          Indexing
                        </span>
                      ) : isClassifying ? (
                        <span className="inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-teal-100 text-teal-800 border border-teal-200">
                          <svg className="animate-spin -ml-0.5 mr-1.5 h-3 w-3 text-teal-800" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24">
                            <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4"></circle>
                            <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
                          </svg>
                          Classifying
                        </span>
                      ) : (
                        <span
                          className={`inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium border ${
                            doc.status === 'classified'
                              ? 'bg-emerald-50 text-emerald-700 border-emerald-200'
                              : doc.status === 'embedded'
                                ? 'bg-purple-50 text-purple-700 border-purple-200'
                                : doc.status === 'extracted'
                                  ? 'bg-green-50 text-green-700 border-green-200'
                                  : doc.status === 'pending_extraction' || doc.status === 'pending_ocr'
                                    ? 'bg-amber-50 text-amber-700 border-amber-200'
                                    : 'bg-gray-50 text-gray-600 border-gray-200'
                          }`}
                        >
                          <span className="capitalize">{doc.status.replace('_', ' ')}</span>
                        </span>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
