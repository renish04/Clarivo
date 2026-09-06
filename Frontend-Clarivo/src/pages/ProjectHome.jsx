import React, { useState } from 'react';
import { useNavigate, useOutletContext } from 'react-router-dom';
import client from '../api/client';
import clarivoIcon from '../assets/clarivo-icon-only.png';

export default function ProjectHome() {
  const [newProjectName, setNewProjectName] = useState('');
  const [isCreating, setIsCreating] = useState(false);
  const navigate = useNavigate();
  const { fetchProjects } = useOutletContext(); // Passed from ProjectsLayout

  const handleCreateSubmit = async (e) => {
    e.preventDefault();
    if (!newProjectName.trim()) return;

    try {
      setIsCreating(true);
      const res = await client.post('/projects/', { name: newProjectName });
      const newProject = res.data;
      await fetchProjects();
      
      if (newProject && newProject.id) {
        navigate(`/projects/${newProject.id}`);
      }
    } catch (err) {
      console.error('Failed to create project', err);
      alert('Could not create project. Please try again.');
      setIsCreating(false);
    }
  };

  return (
    <div className="flex-1 flex items-center justify-center p-8 h-full bg-white">
      <div className="max-w-2xl w-full text-center">
        <div className="mb-8">
          <div className="w-20 h-20 bg-white rounded-2xl flex items-center justify-center mx-auto mb-6">
            <img src={clarivoIcon} alt="Clarivo Icon" className="w-12 h-12 object-contain" />
          </div>
          <h1 className="text-4xl font-extrabold text-gray-900 tracking-tight mb-4">
            Create Your Personalized <br /> Procurement Project
          </h1>
          <p className="text-lg text-gray-500 max-w-xl mx-auto">
            Start by giving your project a name. Once created, you can upload invoices, classify them, and run discrepancy checks automatically.
          </p>
        </div>

        <form onSubmit={handleCreateSubmit} className="max-w-md mx-auto">
          <div className="flex flex-col sm:flex-row gap-3">
            <input
              type="text"
              value={newProjectName}
              onChange={(e) => setNewProjectName(e.target.value)}
              placeholder="e.g. Q3 Vendor Invoices"
              className="flex-1 px-4 py-3 border border-gray-300 rounded-lg text-base focus:outline-none focus:ring-2 focus:ring-blue-800 shadow-sm transition-shadow"
              disabled={isCreating}
              autoFocus
            />
            <button
              type="submit"
              disabled={!newProjectName.trim() || isCreating}
              className="px-6 py-3 bg-blue-800 text-white font-medium rounded-lg hover:bg-blue-900 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-blue-800 transition-colors disabled:opacity-50 shadow-sm flex items-center justify-center min-w-[120px]"
            >
              {isCreating ? 'Creating...' : 'Create Project'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

