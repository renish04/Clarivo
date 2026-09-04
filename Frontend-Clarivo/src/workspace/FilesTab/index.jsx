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
    <div className="p-4">
      <div className="flex items-center justify-between mb-4">
        <h2 className="text-lg font-semibold text-gray-900">Documents</h2>
        <div className="flex items-center gap-3">
          {uploading && (
            <span className="text-sm text-gray-500">Uploading...</span>
          )}
          <label
            className={`px-3 py-1.5 text-sm rounded transition-colors cursor-pointer ${
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
            className="px-3 py-1.5 text-sm bg-gray-100 text-gray-700 rounded hover:bg-gray-200 transition-colors disabled:opacity-50"
          >
            Refresh
          </button>
        </div>
      </div>

      {documents.length === 0 ? (
        <div className="p-8 text-center text-gray-500 border-2 border-dashed border-gray-200 rounded-lg">
          No documents uploaded yet.
        </div>
      ) : (
        <table className="w-full text-sm text-left">
          <thead>
            <tr className="border-b border-gray-200 text-gray-500 uppercase text-xs">
              <th className="py-3 px-4 font-medium">Filename</th>
              <th className="py-3 px-4 font-medium">Type</th>
              <th className="py-3 px-4 font-medium">Status</th>
            </tr>
          </thead>
          <tbody>
            {documents.map((doc) => {
              const docId = doc.SK.replace('DOC#', '');
              const isIndexing = indexingDocs.has(docId);
              const isClassifying = classifyingDocs.has(docId);
              
              return (
                <tr key={doc.SK} className="border-b border-gray-100 hover:bg-gray-50">
                  <td className="py-3 px-4">
                    <a
                      href={doc.view_url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="text-blue-600 hover:underline"
                    >
                      {doc.filename}
                    </a>
                  </td>
                  <td className="py-3 px-4 text-gray-600">
                    {doc.doc_type ? (
                      <span className="capitalize">{doc.doc_type}</span>
                    ) : (
                      <span className="text-gray-400 italic">Unknown</span>
                    )}
                  </td>
                  <td className="py-3 px-4">
                    {isIndexing ? (
                      <span className="inline-block px-2 py-0.5 text-xs font-medium rounded-full bg-blue-100 text-blue-700">
                        Indexing...
                      </span>
                    ) : isClassifying ? (
                      <span className="inline-block px-2 py-0.5 text-xs font-medium rounded-full bg-teal-100 text-teal-700">
                        Classifying...
                      </span>
                    ) : (
                      <span
                        className={`inline-block px-2 py-0.5 text-xs font-medium rounded-full ${
                          doc.status === 'classified'
                            ? 'bg-emerald-100 text-emerald-700'
                            : doc.status === 'embedded'
                              ? 'bg-purple-100 text-purple-700'
                              : doc.status === 'extracted'
                                ? 'bg-green-100 text-green-700'
                                : doc.status === 'pending_extraction' || doc.status === 'pending_ocr'
                                  ? 'bg-amber-100 text-amber-700'
                                  : 'bg-gray-100 text-gray-600'
                        }`}
                      >
                        {doc.status}
                      </span>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </div>
  );
}
