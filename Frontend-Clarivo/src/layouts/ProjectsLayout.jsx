import React, { useState, useEffect, useRef } from 'react';
import { Outlet, Link, useLocation, useNavigate } from 'react-router-dom';
import client from '../api/client';
import Logo from '../components/Logo';

export default function ProjectsLayout() {
  const [projects, setProjects] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  
  const [isCreating, setIsCreating] = useState(false);
  const [newProjectName, setNewProjectName] = useState('');

  // Per-row controls: which row's menu is open, which row is being
  // renamed, and the draft name while it is.
  const [menuOpenId, setMenuOpenId] = useState(null);
  const [renamingId, setRenamingId] = useState(null);
  const [renameValue, setRenameValue] = useState('');
  const [busyId, setBusyId] = useState(null);

  const menuRef = useRef(null);

  const location = useLocation();
  const navigate = useNavigate();

  // Close the open row menu on any click outside it.
  useEffect(() => {
    if (menuOpenId === null) return;

    const handleClickOutside = (event) => {
      if (menuRef.current && !menuRef.current.contains(event.target)) {
        setMenuOpenId(null);
      }
    };

    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, [menuOpenId]);

  const startRename = (project) => {
    setMenuOpenId(null);
    setRenamingId(project.id);
    setRenameValue(project.name);
  };

  const cancelRename = () => {
    setRenamingId(null);
    setRenameValue('');
  };

  const handleRenameSubmit = async (e, project) => {
    e.preventDefault();

    const name = renameValue.trim();
    if (!name || name === project.name) {
      cancelRename();
      return;
    }

    try {
      setBusyId(project.id);
      await client.patch(`/projects/${project.id}/`, { name });
      cancelRename();
      await fetchProjects();
    } catch (err) {
      console.error('Failed to rename project', err);
      alert('Could not rename the project. Please try again.');
    } finally {
      setBusyId(null);
    }
  };

  const handleDelete = async (project) => {
    setMenuOpenId(null);

    const confirmed = window.confirm(
      `Delete "${project.name}"?\n\n` +
        'This permanently deletes the project and every document in it - ' +
        'the uploaded files, their extracted text, the discrepancy findings ' +
        'and all chat history. This cannot be undone.'
    );
    if (!confirmed) return;

    try {
      setBusyId(project.id);
      await client.delete(`/projects/${project.id}/`);
      await fetchProjects();

      // The open project just stopped existing, so don't leave the
      // workspace sitting on a dead route.
      if (location.pathname === `/projects/${project.id}`) {
        navigate('/projects');
      }
    } catch (err) {
      console.error('Failed to delete project', err);
      // A partial-cleanup failure comes back with an explanation and
      // the project intact — worth showing verbatim rather than
      // replacing with a generic message.
      alert(
        err.response?.data?.detail ||
          'Could not delete the project. Please try again.'
      );
    } finally {
      setBusyId(null);
    }
  };

  const fetchProjects = async () => {
    try {
      const response = await client.get('/projects/');
      setProjects(response.data);
      setError('');
    } catch (err) {
      console.error('Failed to fetch projects', err);
      setError('Could not load projects.');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchProjects();
  }, []);

  const handleCreateSubmit = async (e) => {
    e.preventDefault();
    if (!newProjectName.trim()) return;

    try {
      const res = await client.post('/projects/', { name: newProjectName });
      const newProject = res.data;
      setNewProjectName('');
      setIsCreating(false);
      await fetchProjects();
      
      // Navigate to the new project immediately
      if (newProject && newProject.id) {
        navigate(`/projects/${newProject.id}`);
      }
    } catch (err) {
      console.error('Failed to create project', err);
      alert('Could not create project. Please try again.');
    }
  };

  const handleLogout = () => {
    localStorage.removeItem('clarivo_token');
    navigate('/login');
  };

  return (
    <div className="flex h-screen bg-white overflow-hidden font-sans">
      {/* Left Sidebar */}
      <div className="w-[260px] flex-shrink-0 bg-gray-50 border-r border-gray-200 flex flex-col transition-all">
        {/* Sidebar Header */}
        <div className="p-4 border-b border-gray-200">
          <Link to="/projects" className="block mb-6">
            <Logo />
          </Link>
          
          {!isCreating ? (
            <button
              onClick={() => setIsCreating(true)}
              className="w-full flex items-center justify-center gap-2 py-2 px-4 bg-white border border-gray-300 rounded-md text-sm font-medium text-gray-700 hover:bg-gray-50 transition-colors shadow-sm"
            >
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <line x1="12" y1="5" x2="12" y2="19"></line>
                <line x1="5" y1="12" x2="19" y2="12"></line>
              </svg>
              New Project
            </button>
          ) : (
            <form onSubmit={handleCreateSubmit} className="flex flex-col gap-2">
              <input
                type="text"
                value={newProjectName}
                onChange={(e) => setNewProjectName(e.target.value)}
                placeholder="Project Name..."
                className="w-full px-3 py-2 border border-gray-300 rounded-md text-sm focus:outline-none focus:ring-2 focus:ring-blue-800"
                autoFocus
              />
              <div className="flex gap-2">
                <button
                  type="button"
                  onClick={() => {
                    setIsCreating(false);
                    setNewProjectName('');
                  }}
                  className="flex-1 py-1.5 px-2 bg-gray-100 text-gray-600 rounded text-xs font-medium hover:bg-gray-200 transition-colors"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  disabled={!newProjectName.trim()}
                  className="flex-1 py-1.5 px-2 bg-blue-800 text-white rounded text-xs font-medium hover:bg-blue-900 transition-colors disabled:opacity-50"
                >
                  Create
                </button>
              </div>
            </form>
          )}
        </div>

        {/* Project List */}
        <div className="flex-1 overflow-y-auto p-3 space-y-1 custom-scrollbar">
          {loading ? (
            <div className="text-sm text-gray-500 p-2 text-center">Loading...</div>
          ) : error ? (
            <div className="text-sm text-red-500 p-2 text-center">{error}</div>
          ) : projects.length === 0 ? (
            <div className="text-sm text-gray-400 p-2 text-center mt-4">
              No projects yet.
            </div>
          ) : (
            projects.map((project) => {
              const isActive = location.pathname === `/projects/${project.id}`;
              const isMenuOpen = menuOpenId === project.id;
              const isBusy = busyId === project.id;

              // Renaming replaces the row with an input, the same way
              // creating a project replaces the button above.
              if (renamingId === project.id) {
                return (
                  <form
                    key={project.id}
                    onSubmit={(e) => handleRenameSubmit(e, project)}
                    className="px-1 py-1"
                  >
                    <input
                      type="text"
                      value={renameValue}
                      onChange={(e) => setRenameValue(e.target.value)}
                      onBlur={() => cancelRename()}
                      onKeyDown={(e) => {
                        if (e.key === 'Escape') cancelRename();
                      }}
                      disabled={isBusy}
                      className="w-full px-2 py-1.5 border border-blue-800 rounded-md text-sm focus:outline-none focus:ring-2 focus:ring-blue-800 disabled:opacity-50"
                      autoFocus
                    />
                  </form>
                );
              }

              return (
                <div key={project.id} className="relative group">
                  <Link
                    to={`/projects/${project.id}`}
                    className={`
                      block pl-3 pr-9 py-2.5 rounded-lg text-sm truncate transition-colors
                      ${isActive
                        ? 'bg-white font-medium text-gray-900 shadow-sm border border-gray-200'
                        : 'text-gray-700 hover:bg-gray-200 hover:text-gray-900 border border-transparent'
                      }
                      ${isBusy ? 'opacity-50 pointer-events-none' : ''}
                    `}
                  >
                    {project.name}
                  </Link>

                  {/* Hidden until the row is hovered or focused — and
                      kept visible while its own menu is open, so the
                      button does not vanish under the pointer. */}
                  <button
                    type="button"
                    onClick={(e) => {
                      e.preventDefault();
                      setMenuOpenId(isMenuOpen ? null : project.id);
                    }}
                    aria-label={`Options for ${project.name}`}
                    className={`
                      absolute right-1.5 top-1/2 -translate-y-1/2 p-1 rounded
                      text-gray-500 hover:text-gray-900 hover:bg-gray-300/60 transition-all
                      ${isMenuOpen ? 'opacity-100' : 'opacity-0 group-hover:opacity-100 focus:opacity-100'}
                    `}
                  >
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
                      <circle cx="12" cy="5" r="1.75" />
                      <circle cx="12" cy="12" r="1.75" />
                      <circle cx="12" cy="19" r="1.75" />
                    </svg>
                  </button>

                  {isMenuOpen && (
                    <div
                      ref={menuRef}
                      className="absolute right-1 top-[calc(100%-4px)] z-30 w-36 bg-white border border-gray-200 rounded-md shadow-lg py-1"
                    >
                      <button
                        type="button"
                        onClick={() => startRename(project)}
                        className="w-full text-left px-3 py-1.5 text-sm text-gray-700 hover:bg-gray-50 transition-colors"
                      >
                        Rename
                      </button>
                      <button
                        type="button"
                        onClick={() => handleDelete(project)}
                        className="w-full text-left px-3 py-1.5 text-sm text-red-600 hover:bg-red-50 transition-colors"
                      >
                        Delete
                      </button>
                    </div>
                  )}
                </div>
              );
            })
          )}
        </div>

        {/* Sidebar Footer (User / Settings) */}
        <div className="p-4 border-t border-gray-200">
          <button 
            onClick={handleLogout}
            className="flex items-center gap-2 text-sm text-gray-600 hover:text-gray-900 transition-colors w-full p-2 hover:bg-gray-100 rounded-md"
          >
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"></path>
              <polyline points="16 17 21 12 16 7"></polyline>
              <line x1="21" y1="12" x2="9" y2="12"></line>
            </svg>
            Sign out
          </button>
        </div>
      </div>

      {/* Main Content Area */}
      <div className="flex-1 flex flex-col overflow-hidden bg-white relative">
        <Outlet context={{ fetchProjects }} />
      </div>
    </div>
  );
}

