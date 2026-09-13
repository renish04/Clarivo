import { useState, useEffect, useCallback } from 'react';
import { useParams } from 'react-router-dom';
import axios from 'axios';
import client from '../../api/client';
import UploadMenu from './UploadMenu';

const POLL_INTERVAL_MS = 2000;

// What the extraction Lambda can actually read — see
// lambda_functions/extraction/handler.py.  Anything else would be
// uploaded only to fail, so it is filtered out before we start.  A
// folder pick in particular sweeps up plenty that does not belong.
const SUPPORTED_EXTENSIONS = ['.pdf', '.jpg', '.jpeg'];

// S3 keys embed the filename, and the presign endpoint caps it at 255.
const MAX_FILENAME_LENGTH = 255;

const DEFAULT_CONTENT_TYPE = 'application/octet-stream';

// How each backend status is presented. `spin` marks a status the
// pipeline is actively working through — those also drive polling.
const STATUS_DISPLAY = {
  pending_extraction: {
    label: 'Extracting',
    className: 'bg-amber-50 text-amber-700 border-amber-200',
    spin: true,
  },
  extracted: {
    label: 'Queued',
    className: 'bg-amber-50 text-amber-700 border-amber-200',
    spin: true,
  },
  embedding: {
    label: 'Indexing',
    className: 'bg-blue-50 text-blue-800 border-blue-200',
    spin: true,
  },
  embedded: {
    label: 'Indexed',
    className: 'bg-purple-50 text-purple-700 border-purple-200',
    spin: true,
  },
  classifying: {
    label: 'Classifying',
    className: 'bg-teal-50 text-teal-800 border-teal-200',
    spin: true,
  },
  classified: {
    label: 'Classified',
    className: 'bg-emerald-50 text-emerald-700 border-emerald-200',
  },
  checked: {
    label: 'Checked',
    className: 'bg-emerald-50 text-emerald-700 border-emerald-200',
  },
  pending_ocr: {
    label: 'Unsupported file',
    className: 'bg-gray-50 text-gray-600 border-gray-200',
  },
  failed_extraction: {
    label: 'Extraction failed',
    className: 'bg-red-50 text-red-700 border-red-200',
  },
  failed_embedding: {
    label: 'Indexing failed',
    className: 'bg-red-50 text-red-700 border-red-200',
  },
  failed_classification: {
    label: 'Classification failed',
    className: 'bg-red-50 text-red-700 border-red-200',
  },
};

const describeStatus = (status) =>
  STATUS_DISPLAY[status] || {
    label: (status || 'unknown').replace(/_/g, ' '),
    className: 'bg-gray-50 text-gray-600 border-gray-200',
  };

/**
 * The name a document is stored and listed under.
 *
 * For a folder pick this is the path within the chosen folder, so two
 * files both called `invoice.pdf` in different subfolders stay tellable
 * apart in the list.  The extraction Lambda parses the key with a
 * greedy `.+`, so embedded slashes come through fine.
 */
const documentNameFor = (file) => {
  const relativePath = file.webkitRelativePath || '';
  if (relativePath && relativePath.length <= MAX_FILENAME_LENGTH) {
    return relativePath;
  }
  return file.name;
};

const isSupported = (file) =>
  SUPPORTED_EXTENSIONS.some((ext) => file.name.toLowerCase().endsWith(ext));

// Folder picks include OS metadata (.DS_Store) and directories like
// .git that the user never meant to hand over.
const isHidden = (file) =>
  (file.webkitRelativePath || file.name)
    .split('/')
    .some((segment) => segment.startsWith('.'));

function Spinner() {
  return (
    <svg
      className="animate-spin -ml-0.5 mr-1.5 h-3 w-3"
      xmlns="http://www.w3.org/2000/svg"
      fill="none"
      viewBox="0 0 24 24"
    >
      <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4"></circle>
      <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
    </svg>
  );
}

export default function FilesTab() {
  const { id: projectId } = useParams();
  const [documents, setDocuments] = useState([]);
  const [loading, setLoading] = useState(true);
  const [uploading, setUploading] = useState(false);
  const [progress, setProgress] = useState(null);
  const [notice, setNotice] = useState(null);
  const [error, setError] = useState('');

  // Read-only: the backend owns the pipeline, this just reports on it.
  const fetchDocuments = useCallback(async (silent = false) => {
    const isSilent = silent === true;
    try {
      if (!isSilent) setLoading(true);
      setError('');
      const res = await client.get(`/projects/${projectId}/documents/`);
      setDocuments(res.data);
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

  // Poll only while something is actually in flight, and stop as soon as
  // every document has settled.  Being unmounted mid-poll is harmless —
  // the work continues server-side either way.
  const isProcessing = documents.some((doc) => describeStatus(doc.status).spin);

  useEffect(() => {
    if (!isProcessing) return undefined;
    const timer = setInterval(() => fetchDocuments(true), POLL_INTERVAL_MS);
    return () => clearInterval(timer);
  }, [isProcessing, fetchDocuments]);

  const handleRefreshClick = () => fetchDocuments(false);

  /** Presign, PUT to S3, confirm. Confirming hands it to the worker. */
  const uploadOne = async (file) => {
    const name = documentNameFor(file);
    // The content type must be identical in the presign and the PUT, or
    // S3 rejects the signature.  Folder picks often report no type.
    const contentType = file.type || DEFAULT_CONTENT_TYPE;

    const presignRes = await client.post(
      `/projects/${projectId}/documents/presign/`,
      { filename: name, content_type: contentType }
    );
    const { upload_url, s3_key, doc_id } = presignRes.data;

    await axios.put(upload_url, file, {
      headers: { 'Content-Type': contentType },
    });

    await client.post(`/projects/${projectId}/documents/confirm/`, {
      doc_id,
      filename: name,
      s3_key,
      file_type: contentType,
    });
  };

  /**
   * Upload a selection one file at a time.
   *
   * Sequential on purpose: it keeps the list filling in visibly, one row
   * per file, and matches the single-worker queue on the backend.  One
   * file failing never abandons the rest of the batch.
   */
  const handleFilesSelected = async (fileList) => {
    const selected = Array.from(fileList);
    const eligible = selected.filter((file) => !isHidden(file) && isSupported(file));
    const skipped = selected.length - eligible.length;

    setNotice(null);

    if (eligible.length === 0) {
      setNotice({
        tone: 'error',
        text: `No supported documents found in that selection. Clarivo reads ${SUPPORTED_EXTENSIONS.join(', ')} files.`,
      });
      return;
    }

    setUploading(true);
    const failed = [];

    for (let i = 0; i < eligible.length; i += 1) {
      const file = eligible[i];
      setProgress({
        current: i + 1,
        total: eligible.length,
        name: documentNameFor(file),
      });

      try {
        await uploadOne(file);
        // Surface the new row as soon as it exists, so a long batch
        // visibly fills in rather than landing all at once at the end.
        await fetchDocuments(true);
      } catch (err) {
        console.error(`Upload failed for ${file.name}`, err);
        failed.push(documentNameFor(file));
      }
    }

    setProgress(null);
    setUploading(false);

    const uploaded = eligible.length - failed.length;
    const parts = [`${uploaded} file${uploaded === 1 ? '' : 's'} uploaded`];
    if (skipped > 0) parts.push(`${skipped} unsupported skipped`);
    if (failed.length > 0) parts.push(`${failed.length} failed`);

    setNotice({
      tone: failed.length > 0 ? 'error' : 'info',
      text: `${parts.join(' · ')}.`,
    });

    await fetchDocuments(true);
  };

  if (loading) {
    return <div className="p-8 text-center text-gray-500">Loading documents...</div>;
  }

  if (error) {
    return (
      <div className="p-8 text-center">
        <p className="text-red-500 mb-4">{error}</p>
        <button
          onClick={handleRefreshClick}
          className="text-blue-800 hover:underline"
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
          <UploadMenu disabled={uploading} onFilesSelected={handleFilesSelected} />
          <button
            onClick={handleRefreshClick}
            disabled={uploading}
            className="px-4 py-2 text-sm font-medium bg-white border border-gray-300 text-gray-700 rounded-md hover:bg-gray-50 transition-colors disabled:opacity-50 shadow-sm"
          >
            Refresh
          </button>
        </div>
      </div>

      {progress && (
        <div className="mb-4 flex-shrink-0 p-4 bg-blue-50 border border-blue-200 rounded-lg">
          <div className="flex items-baseline justify-between gap-4 mb-2">
            <span className="text-sm font-medium text-blue-900 whitespace-nowrap">
              Uploading {progress.current} of {progress.total}
            </span>
            <span className="text-sm text-blue-800/70 truncate" title={progress.name}>
              {progress.name}
            </span>
          </div>
          <div className="h-1.5 w-full bg-blue-200 rounded-full overflow-hidden">
            <div
              className="h-full bg-blue-800 rounded-full transition-all duration-300"
              style={{ width: `${(progress.current / progress.total) * 100}%` }}
            />
          </div>
        </div>
      )}

      {notice && (
        <div
          className={`mb-4 flex-shrink-0 flex items-start justify-between gap-4 px-4 py-3 rounded-lg border text-sm ${
            notice.tone === 'error'
              ? 'bg-red-50 border-red-200 text-red-700'
              : 'bg-emerald-50 border-emerald-200 text-emerald-800'
          }`}
        >
          <span>{notice.text}</span>
          <button
            onClick={() => setNotice(null)}
            className="opacity-60 hover:opacity-100 transition-opacity flex-shrink-0"
            aria-label="Dismiss"
          >
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <line x1="18" y1="6" x2="6" y2="18"></line>
              <line x1="6" y1="6" x2="18" y2="18"></line>
            </svg>
          </button>
        </div>
      )}

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
                const display = describeStatus(doc.status);

                return (
                  <tr key={doc.SK} className="hover:bg-gray-50 transition-colors">
                    <td className="py-4 px-6">
                      <a
                        href={doc.view_url}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="text-blue-800 font-medium hover:text-blue-900 hover:underline flex items-center gap-2"
                      >
                        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="text-gray-400 flex-shrink-0">
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
                      <span
                        className={`inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium border ${display.className}`}
                      >
                        {display.spin && <Spinner />}
                        {display.label}
                      </span>
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
