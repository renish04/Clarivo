import { useState, useEffect, useCallback, useRef } from 'react';
import client from '../../api/client';
import { formatRelative } from './constants';

// How often to pull the mailbox when Gmail push is switched off.  With
// push on, the backend is told about new mail and this is unnecessary.
const SYNC_POLL_INTERVAL_MS = 60000;

// A push watch lapses after about a week and is renewed whenever status
// is read.  Inside this window it is worth telling the user, because a
// lapsed watch means new mail silently stops arriving.
const WATCH_WARNING_MS = 24 * 60 * 60 * 1000;

/**
 * The ?gmail= outcome the OAuth callback left in the URL, if any.
 *
 * Read once, in a state initialiser, so it is captured before the
 * effect below strips it from the address bar.
 */
const readOAuthOutcome = () => {
  const outcome = new URLSearchParams(window.location.search).get('gmail');
  if (outcome === 'connected') return { tone: 'success', text: 'Gmail connected' };
  if (outcome === 'error') return { tone: 'error', text: 'Gmail could not be connected. Please try again.' };
  return null;
};

function Spinner() {
  return (
    <svg className="animate-spin h-3.5 w-3.5" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24">
      <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4"></circle>
      <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
    </svg>
  );
}

/**
 * The Gmail connection strip at the top of the Automail tab.
 *
 * Connecting leaves the SPA entirely: the consent screen is Google's own
 * page, and the backend sends the browser back to the path it started
 * from with ?gmail=connected|error appended.
 *
 * *onSynced* is called after a sync that may have changed the project's
 * mail, so the parent can refresh both views.  *onStatusChange* reports
 * the status upward (the inbox needs the connected address).  Bumping
 * *reloadKey* makes the bar re-read status, which is how a 409 from a
 * send elsewhere brings the reconnect banner up here.
 */
export default function GmailConnectionBar({ onSynced, onStatusChange, reloadKey }) {
  const [status, setStatus] = useState(null);
  const [loading, setLoading] = useState(true);
  const [syncing, setSyncing] = useState(false);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState(readOAuthOutcome);

  // Kept in a ref so the polling interval always calls the latest
  // callbacks without being torn down and restarted every render.
  const callbacksRef = useRef({ onSynced, onStatusChange });
  useEffect(() => {
    callbacksRef.current = { onSynced, onStatusChange };
  }, [onSynced, onStatusChange]);

  // Worked out when status arrives, not during render, so the clock is
  // read once per status rather than on every re-render.
  const [watchExpiresSoon, setWatchExpiresSoon] = useState(false);

  const fetchStatus = useCallback(async () => {
    try {
      const res = await client.get('/gmail/status/');
      const data = res.data;
      setStatus(data);
      setWatchExpiresSoon(
        Boolean(data?.connected && data?.push_enabled && data?.watch_expires_at) &&
          new Date(data.watch_expires_at).getTime() - Date.now() < WATCH_WARNING_MS
      );
      callbacksRef.current.onStatusChange?.(data);
    } catch (err) {
      console.error(err);
      setNotice({ tone: 'error', text: 'Could not read the Gmail connection status.' });
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchStatus();
  }, [fetchStatus, reloadKey]);

  // Strip ?gmail= once its notice has been captured, so a refresh does
  // not report the same connection twice.  Everything else in the query
  // string (notably ?tab=automail) is left as it was.
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    if (!params.has('gmail')) return;
    params.delete('gmail');
    const query = params.toString();
    const url = `${window.location.pathname}${query ? `?${query}` : ''}${window.location.hash}`;
    window.history.replaceState(window.history.state, '', url);
  }, []);

  /**
   * Pull the mailbox.  *silent* is the background poll: it reports
   * nothing unless the connection has broken, and only asks the parent
   * to refresh when something was actually ingested.
   */
  const runSync = useCallback(
    async (silent) => {
      try {
        const res = await client.post('/gmail/sync/');
        await fetchStatus();
        const ingested = res.data?.ingested || 0;
        if (!silent || ingested > 0) callbacksRef.current.onSynced?.();
        if (!silent) {
          const fetched = res.data?.fetched || 0;
          setNotice({
            tone: 'success',
            text:
              fetched === 0
                ? 'No new mail.'
                : `${fetched} new message${fetched === 1 ? '' : 's'} · ${ingested} matched to projects.`,
          });
        }
      } catch (err) {
        // 409: the stored Google grant is no longer usable.  Re-reading
        // status brings up the reconnect banner, which says what to do.
        if (err.response?.status === 409) {
          await fetchStatus();
          return;
        }
        console.error(err);
        if (!silent) setNotice({ tone: 'error', text: 'The sync could not be completed.' });
      }
    },
    [fetchStatus]
  );

  // Without push, nothing else would ever notice new mail arriving.
  const shouldPoll = Boolean(status?.connected) && !status?.needs_reconnect && !status?.push_enabled;

  useEffect(() => {
    if (!shouldPoll) return undefined;
    const timer = setInterval(() => runSync(true), SYNC_POLL_INTERVAL_MS);
    return () => clearInterval(timer);
  }, [shouldPoll, runSync]);

  const handleConnect = async () => {
    setBusy(true);
    try {
      // Come back to this very tab rather than the project's first one.
      const returnTo = `${window.location.pathname}?tab=automail`;
      const res = await client.get('/gmail/oauth/start/', { params: { return_to: returnTo } });
      window.location.href = res.data.auth_url;
    } catch (err) {
      console.error(err);
      setNotice({ tone: 'error', text: 'Could not start the Gmail connection.' });
      setBusy(false);
    }
  };

  const handleSync = async () => {
    setSyncing(true);
    setNotice(null);
    await runSync(false);
    setSyncing(false);
  };

  const handleDisconnect = async () => {
    // Disconnecting revokes the grant at Google, so it is worth asking.
    const confirmed = window.confirm(
      'Disconnect Gmail? Clarivo will stop reading new mail and cannot send follow-ups. Emails already ingested stay with the project.'
    );
    if (!confirmed) return;

    setBusy(true);
    try {
      await client.post('/gmail/disconnect/');
      setNotice({ tone: 'success', text: 'Gmail disconnected.' });
      await fetchStatus();
    } catch (err) {
      console.error(err);
      setNotice({ tone: 'error', text: 'Could not disconnect Gmail.' });
    } finally {
      setBusy(false);
    }
  };

  let body;
  if (loading) {
    body = (
      <div className="px-5 py-4 bg-white border border-gray-200 rounded-lg shadow-sm text-sm text-gray-500">
        Checking Gmail…
      </div>
    );
  } else if (!status?.connected) {
    body = (
      <div className="flex items-center justify-between gap-4 px-5 py-4 bg-white border border-gray-200 rounded-lg shadow-sm">
        <p className="text-sm text-gray-700">
          Connect Gmail so Clarivo can read supplier emails and send follow-ups from your address
        </p>
        <button
          onClick={handleConnect}
          disabled={busy}
          className="flex-shrink-0 bg-blue-800 hover:bg-blue-900 text-white text-sm font-medium px-4 py-2 rounded shadow-sm disabled:opacity-50 transition-colors"
        >
          {busy ? 'Opening Google…' : 'Connect Gmail'}
        </button>
      </div>
    );
  } else if (status.needs_reconnect) {
    body = (
      <div className="flex items-center justify-between gap-4 px-5 py-4 bg-amber-50 border border-amber-200 rounded-lg">
        <p className="text-sm font-medium text-amber-800">
          Gmail access expired — reconnect to keep syncing
          <span className="ml-2 font-normal text-amber-700">({status.email})</span>
        </p>
        <button
          onClick={handleConnect}
          disabled={busy}
          className="flex-shrink-0 bg-amber-600 hover:bg-amber-700 text-white text-sm font-medium px-4 py-2 rounded shadow-sm disabled:opacity-50 transition-colors"
        >
          {busy ? 'Opening Google…' : 'Reconnect'}
        </button>
      </div>
    );
  } else {
    body = (
      <div className="flex items-center justify-between gap-4 px-5 py-4 bg-white border border-gray-200 rounded-lg shadow-sm">
        <div className="flex items-center gap-3 min-w-0">
          <span className="w-2.5 h-2.5 rounded-full flex-shrink-0 bg-emerald-500" aria-hidden="true" />
          <div className="min-w-0">
            <p className="text-sm text-gray-900 truncate">
              Connected as <span className="font-medium">{status.email}</span>
            </p>
            <p className="text-xs text-gray-500 mt-0.5">
              {status.push_enabled ? 'Live notifications' : 'Checking every minute'}
              {' · '}
              {status.last_synced_at ? `Last synced ${formatRelative(status.last_synced_at)}` : 'Not synced yet'}
            </p>
          </div>
        </div>

        <div className="flex items-center gap-4 flex-shrink-0">
          <button
            onClick={handleDisconnect}
            disabled={busy || syncing}
            className="text-xs text-gray-400 hover:text-red-700 hover:underline transition-colors disabled:opacity-50"
          >
            Disconnect
          </button>
          <button
            onClick={handleSync}
            disabled={busy || syncing}
            className="inline-flex items-center gap-2 px-4 py-2 text-sm font-medium bg-white border border-gray-300 text-gray-700 rounded-md hover:bg-gray-50 transition-colors disabled:opacity-50 shadow-sm"
          >
            {syncing && <Spinner />}
            {syncing ? 'Syncing…' : 'Sync now'}
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="flex-shrink-0 space-y-2">
      {body}

      {watchExpiresSoon && (
        <p className="px-1 text-xs text-amber-700">
          Live notifications expire within 24 hours. They renew automatically while Clarivo is open; if new mail stops appearing, use Sync now.
        </p>
      )}

      {notice && (
        <div
          className={`flex items-start justify-between gap-4 px-4 py-2.5 rounded-lg border text-sm ${
            notice.tone === 'error'
              ? 'bg-red-50 border-red-200 text-red-700'
              : 'bg-emerald-50 border-emerald-200 text-emerald-800'
          }`}
        >
          <span>{notice.text}</span>
          <button
            onClick={() => setNotice(null)}
            className="opacity-60 hover:opacity-100 transition-opacity flex-shrink-0"
            aria-label="Dismiss"
          >
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <line x1="18" y1="6" x2="6" y2="18"></line>
              <line x1="6" y1="6" x2="18" y2="18"></line>
            </svg>
          </button>
        </div>
      )}
    </div>
  );
}
