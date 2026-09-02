import React, { useState, useEffect } from 'react';
import { Link } from 'react-router-dom';
import client from '../api/client';

export default function ProjectList() {
  const [projects, setProjects] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [isCreating, setIsCreating] = useState(false);
  const [newProjectName, setNewProjectName] = useState('');

  const fetchProjects = async () => {
    try {
      setLoading(true);
      const response = await client.get('/projects/');
      setProjects(response.data);
      setError('');
    } catch (err) {
      console.error('Failed to fetch projects', err);
      setError('Could not load projects. Please try again.');
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
      await client.post('/projects/', { name: newProjectName });
      setNewProjectName('');
      setIsCreating(false);
      fetchProjects(); // Refresh the list
    } catch (err) {
      console.error('Failed to create project', err);
      setError('Could not create project. Please try again.');
    }
  };

  return (
    <div className="max-w-4xl mx-auto p-8">
      <div className="flex justify-between items-center mb-8">
        <h1 className="text-3xl font-bold text-gray-900">My Projects</h1>
        <button
          onClick={() => setIsCreating(!isCreating)}
          className="bg-blue-600 text-white px-4 py-2 rounded-md hover:bg-blue-700 transition-colors"
        >
          {isCreating ? 'Cancel' : '+ New Project'}
        </button>
      </div>

      {error && (
        <div className="bg-red-50 text-red-700 p-4 rounded-md mb-6 border-l-4 border-red-500">
          {error}
        </div>
      )}

      {isCreating && (
        <div className="bg-gray-50 p-6 rounded-lg mb-8 border border-gray-200 shadow-sm">
          <h2 className="text-lg font-medium text-gray-900 mb-4">Create a new project</h2>
          <form onSubmit={handleCreateSubmit} className="flex gap-4">
            <input
              type="text"
              value={newProjectName}
              onChange={(e) => setNewProjectName(e.target.value)}
              placeholder="Project Name"
              className="flex-1 px-4 py-2 border border-gray-300 rounded-md focus:outline-none focus:ring-2 focus:ring-blue-500"
              autoFocus
              required
            />
            <button
              type="submit"
              className="bg-green-600 text-white px-6 py-2 rounded-md hover:bg-green-700 transition-colors"
            >
              Create
            </button>
          </form>
        </div>
      )}

      {loading ? (
        <p className="text-gray-500 text-center py-12">Loading projects...</p>
      ) : projects.length === 0 ? (
        <div className="text-center py-12 bg-white rounded-lg border border-gray-200 border-dashed">
          <p className="text-gray-500">You don't have any projects yet.</p>
        </div>
      ) : (
        <div className="bg-white rounded-lg shadow overflow-hidden border border-gray-200">
          <ul className="divide-y divide-gray-200">
            {projects.map((project) => (
              <li key={project.id}>
                <Link
                  to={`/projects/${project.id}`}
                  className="block hover:bg-gray-50 transition-colors p-6"
                >
                  <div className="flex justify-between items-center">
                    <h3 className="text-xl font-medium text-blue-600 truncate">
                      {project.name}
                    </h3>
                    <span className="text-sm text-gray-500">
                      Created {new Date(project.created_at).toLocaleDateString()}
                    </span>
                  </div>
                </Link>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
