import React from 'react';

export default function Logo({ className = '' }) {
  return (
    <div className={`flex items-center gap-2 ${className}`}>
      <svg
        width="28"
        height="28"
        viewBox="0 0 32 32"
        fill="none"
        xmlns="http://www.w3.org/2000/svg"
        className="text-blue-600"
      >
        <rect width="32" height="32" rx="8" fill="currentColor" fillOpacity="0.1" />
        <path
          d="M21 11L16 16L21 21M11 11L16 16L11 21"
          stroke="currentColor"
          strokeWidth="2.5"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </svg>
      <span className="text-xl font-bold tracking-tight text-gray-900">
        Clarivo
      </span>
    </div>
  );
}

