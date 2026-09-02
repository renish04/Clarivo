import React from 'react';
import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom';
import Login from './pages/Login';
import ProjectList from './pages/ProjectList';
import ProjectWorkspace from './pages/ProjectWorkspace';

// Simple Route Guard Component
function PrivateRoute({ children }) {
  const token = localStorage.getItem('clarivo_token');
  
  if (!token) {
    // If not authenticated, redirect to login
    return <Navigate to="/login" replace />;
  }
  
  // If authenticated, render the children (the protected component)
  return children;
}

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/login" element={<Login />} />
        
        {/* Protected Routes */}
        <Route 
          path="/projects" 
          element={
            <PrivateRoute>
              <ProjectList />
            </PrivateRoute>
          } 
        />
        <Route 
          path="/projects/:id" 
          element={
            <PrivateRoute>
              <ProjectWorkspace />
            </PrivateRoute>
          } 
        />

        {/* Redirect root to projects by default */}
        <Route path="/" element={<Navigate to="/projects" replace />} />
        
        {/* Catch-all redirect */}
        <Route path="*" element={<Navigate to="/projects" replace />} />
      </Routes>
    </BrowserRouter>
  );
}
