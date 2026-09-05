import React from 'react';
import { Link } from 'react-router-dom';
import Logo from '../components/Logo';

export default function Landing() {
  return (
    <div className="min-h-screen bg-white flex flex-col">
      {/* Navigation */}
      <nav className="border-b border-gray-100">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
          <div className="flex justify-between h-16 items-center">
            <Logo />
            <div className="flex items-center gap-4">
              <Link
                to="/login"
                className="text-gray-600 hover:text-gray-900 font-medium px-3 py-2 text-sm transition-colors"
              >
                Sign in
              </Link>
              <Link
                to="/login"
                className="bg-blue-600 hover:bg-blue-700 text-white px-4 py-2 rounded-md text-sm font-medium transition-colors"
              >
                Get Started
              </Link>
            </div>
          </div>
        </div>
      </nav>

      {/* Hero Section */}
      <main className="flex-grow flex items-center justify-center">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-20 text-center">
          <h1 className="text-5xl md:text-6xl font-extrabold text-gray-900 tracking-tight mb-6">
            Simplify your document <br className="hidden md:block" />
            <span className="text-blue-600">analysis and retrieval.</span>
          </h1>
          <p className="mt-4 max-w-2xl text-xl text-gray-500 mx-auto mb-10">
            Clarivo is the enterprise solution for managing, searching, and analyzing your documents with intelligent insights.
          </p>
          <div className="flex justify-center gap-4">
            <Link
              to="/login"
              className="bg-blue-600 hover:bg-blue-700 text-white px-8 py-3 rounded-lg text-lg font-medium transition-colors shadow-sm"
            >
              Start for free
            </Link>
            <Link
              to="/login"
              className="bg-white hover:bg-gray-50 text-gray-700 border border-gray-200 px-8 py-3 rounded-lg text-lg font-medium transition-colors shadow-sm"
            >
              Book a demo
            </Link>
          </div>
        </div>
      </main>

      {/* Footer */}
      <footer className="border-t border-gray-100 py-8">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 text-center text-gray-400 text-sm">
          &copy; {new Date().getFullYear()} Clarivo Inc. All rights reserved.
        </div>
      </footer>
    </div>
  );
}

