import { useEffect, useState } from 'react';
import client from './api/client';

export default function App() {
  const [status, setStatus] = useState('loading'); // 'loading' | 'ok' | 'error'
  const [message, setMessage] = useState('');

  useEffect(() => {
    client
      .get('/health/')
      .then((res) => {
        setStatus('ok');
        setMessage(JSON.stringify(res.data));
      })
      .catch((err) => {
        setStatus('error');
        setMessage(err.message);
      });
  }, []);

  return (
    <div className="flex min-h-screen items-center justify-center bg-gray-50">
      <div className="rounded-lg border bg-white p-8 shadow-sm text-center">
        <h1 className="mb-4 text-xl font-semibold text-gray-700">
          Clarivo — Backend Health Check
        </h1>

        {status === 'loading' && (
          <p className="text-gray-400">Checking backend…</p>
        )}

        {status === 'ok' && (
          <p className="text-green-600 font-medium">
            ✅ Backend connected &mdash; <code>{message}</code>
          </p>
        )}

        {status === 'error' && (
          <p className="text-red-600 font-medium">
            ❌ {message}
          </p>
        )}
      </div>
    </div>
  );
}
