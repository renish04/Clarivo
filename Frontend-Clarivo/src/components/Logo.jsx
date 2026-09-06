import React from 'react';
import logoUrl from '../assets/clarivo-logo.png';

export default function Logo({ className = '' }) {
  return (
    <div className={`flex items-center ${className}`}>
      <img src={logoUrl} alt="Clarivo" className="h-8 w-auto object-contain" />
    </div>
  );
}

