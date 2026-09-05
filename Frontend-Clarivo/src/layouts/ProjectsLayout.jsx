import React, { useState, useEffect } from 'react';
import { Outlet, Link, useLocation, useNavigate } from 'react-router-dom';
import client from '../api/client';
import Logo from '../components/Logo';

export default function ProjectsLayout() {
  const [projects, setProjects] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  
  const [isCreating, setIsCreating] = useState(false);
  const [newProjectName, setNewProjectName] = useState('');
  
  const location = useLocation();
  const navigate = useNavigate();

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
                className="w-full px-3 py-2 border border-gray-300 rounded-md text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
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
                  className="flex-1 py-1.5 px-2 bg-blue-600 text-white rounded text-xs font-medium hover:bg-blue-700 transition-colors disabled:opacity-50"
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
              return (
                <Link
                  key={project.id}
                  to={`/projects/${project.id}`}
                  className={`
                    block px-3 py-2.5 rounded-lg text-sm truncate transition-colors
                    ${isActive 
                      ? 'bg-white font-medium text-gray-900 shadow-sm border border-gray-200' 
                      : 'text-gray-700 hover:bg-gray-200 hover:text-gray-900 border border-transparent'
                    }
                  `}
                >
                  {project.name}
                </Link>
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

