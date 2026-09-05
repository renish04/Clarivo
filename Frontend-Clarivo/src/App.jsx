import React from 'react';
import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom';
import Login from './pages/Login';
import ProjectList from './pages/ProjectList';
import ProjectWorkspace from './pages/ProjectWorkspace';
import Landing from './pages/Landing';

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
        {/* Public Routes */}
        <Route path="/" element={<Landing />} />
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
        
        {/* Catch-all redirect to home */}
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </BrowserRouter>
  );
}
