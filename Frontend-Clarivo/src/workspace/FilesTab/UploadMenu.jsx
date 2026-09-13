import { useEffect, useRef, useState } from 'react';

/**
 * Split upload control: a button that opens a small menu offering
 * "Upload Files" (multi-select) or "Upload Folder".
 *
 * Two hidden inputs back it.  The folder input gets `webkitdirectory`
 * set on the DOM node directly — it is not a prop React knows about —
 * which makes the browser walk the chosen directory and hand back every
 * file in it, including everything in nested subfolders.  Each file
 * carries its path within the folder on `webkitRelativePath`.
 */
export default function UploadMenu({ disabled, onFilesSelected }) {
  const [open, setOpen] = useState(false);
  const menuRef = useRef(null);
  const fileInputRef = useRef(null);
  const folderInputRef = useRef(null);

  useEffect(() => {
    const el = folderInputRef.current;
    if (!el) return;
    el.webkitdirectory = true;
    el.directory = true;
  }, []);

  // Close on an outside click or Escape.
  useEffect(() => {
    if (!open) return undefined;

    const handleClick = (event) => {
      if (menuRef.current && !menuRef.current.contains(event.target)) {
        setOpen(false);
      }
    };
    const handleKey = (event) => {
      if (event.key === 'Escape') setOpen(false);
    };

    document.addEventListener('mousedown', handleClick);
    document.addEventListener('keydown', handleKey);
    return () => {
      document.removeEventListener('mousedown', handleClick);
      document.removeEventListener('keydown', handleKey);
    };
  }, [open]);

  const openPicker = (ref) => {
    setOpen(false);
    ref.current?.click();
  };

  const handleChange = (event) => {
    const { files } = event.target;
    if (files && files.length > 0) {
      onFilesSelected(files);
    }
    // Reset so picking the same file or folder again still fires change.
    event.target.value = '';
  };

  return (
    <div className="relative" ref={menuRef}>
      <button
        type="button"
        onClick={() => setOpen((prev) => !prev)}
        disabled={disabled}
        aria-haspopup="menu"
        aria-expanded={open}
        className={`px-4 py-2 text-sm font-medium rounded-md transition-colors shadow-sm flex items-center gap-2 ${
          disabled
            ? 'bg-gray-100 text-gray-400 cursor-not-allowed'
            : 'bg-blue-800 text-white hover:bg-blue-900'
        }`}
      >
        Upload
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
          <polyline points="6 9 12 15 18 9"></polyline>
        </svg>
      </button>

      {open && !disabled && (
        <div
          role="menu"
          className="absolute right-0 mt-2 w-52 bg-white border border-gray-200 rounded-lg shadow-lg py-1 z-20"
        >
          <button
            type="button"
            role="menuitem"
            onClick={() => openPicker(fileInputRef)}
            className="w-full text-left px-4 py-2.5 text-sm text-gray-700 hover:bg-gray-50 flex items-center gap-3 transition-colors"
          >
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="text-gray-400">
              <path d="M13 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"></path>
              <polyline points="13 2 13 9 20 9"></polyline>
            </svg>
            Upload Files
          </button>
          <button
            type="button"
            role="menuitem"
            onClick={() => openPicker(folderInputRef)}
            className="w-full text-left px-4 py-2.5 text-sm text-gray-700 hover:bg-gray-50 flex items-center gap-3 transition-colors"
          >
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="text-gray-400">
              <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"></path>
            </svg>
            Upload Folder
          </button>
        </div>
      )}

      <input
        ref={fileInputRef}
        type="file"
        multiple
        className="hidden"
        onChange={handleChange}
      />
      <input
        ref={folderInputRef}
        type="file"
        multiple
        className="hidden"
        onChange={handleChange}
      />
    </div>
  );
}
