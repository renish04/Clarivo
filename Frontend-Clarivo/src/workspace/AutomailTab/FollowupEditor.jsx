import { useState, useEffect, useRef, useCallback } from 'react';
import client from '../../api/client';
import {
  describeFollowupState,
  formatDateTime,
  isValidEmail,
  TO_SOURCE_CAPTIONS,
} from './constants';

// Shown in turn while a draft is generated.  Purely cosmetic — the
// backend makes a single call — but a line that changes reads as
// progress, where one that sits still reads as a hang.
const GENERATING_LINES = [
  'Reading the discrepancy findings…',
  "Looking up the supplier's contact…",
  'Drafting the email…',
];
const GENERATING_LINE_MS = 1500;

const SAVED_FLASH_MS = 2000;

// States in which loading the editor should write a draft straight away:
// nothing has been drafted yet, or the supplier answered and the next
// round is due.
const AUTO_GENERATE_STATES = ['needs_draft', 'reply_received'];

const INPUT_CLASS =
  'w-full px-3 py-2 border border-gray-300 rounded-md shadow-sm text-sm focus:outline-none focus:ring-2 focus:ring-blue-800 focus:border-blue-800 disabled:bg-gray-50 disabled:text-gray-600 disabled:cursor-not-allowed transition-colors';

const SECONDARY_BUTTON_CLASS =
  'px-4 py-2 text-sm font-medium bg-white border border-gray-300 text-gray-700 rounded-md hover:bg-gray-50 transition-colors disabled:opacity-50 shadow-sm';

function GeneratingLoader() {
  const [index, setIndex] = useState(0);

  useEffect(() => {
    const timer = setInterval(
      () => setIndex((current) => (current + 1) % GENERATING_LINES.length),
      GENERATING_LINE_MS
    );
    return () => clearInterval(timer);
  }, []);

  return (
    <div className="flex-1 flex flex-col items-center justify-center gap-3 p-8">
      <div className="h-1 w-40 bg-blue-100 rounded-full overflow-hidden">
        <div className="h-full w-1/3 bg-blue-800 rounded-full animate-pulse" />
      </div>
      <p className="text-sm text-gray-600 animate-pulse">{GENERATING_LINES[index]}</p>
    </div>
  );
}

/**
 * The draft for one invoice: generate it, edit it, send it.
 *
 * Mounted once per selected invoice (the parent keys it by doc id), so
 * all of its state belongs to that one invoice and starts fresh when the
 * selection changes.
 *
 * Nothing is ever sent without an explicit press of Send followed by a
 * confirmation.
 */
export default function FollowupEditor({ projectId, docId, entry, onChanged, onOpenThread, onNeedsReconnect }) {
  const draftUrl = `/projects/${projectId}/followups/${docId}/draft/`;
  const sendUrl = `/projects/${projectId}/followups/${docId}/send/`;

  // 'loading' | 'generating' | 'ready' | 'empty' | 'error'
  const [phase, setPhase] = useState('loading');
  const [draft, setDraft] = useState(null);
  const [form, setForm] = useState({ to: '', subject: '', body: '' });
  const [loadError, setLoadError] = useState('');
  const [notice, setNotice] = useState(null);
  const [sending, setSending] = useState(false);
  const [savedVisible, setSavedVisible] = useState(false);

  // The last values the server holds, so a blur only saves what changed.
  const savedRef = useRef({ to: '', subject: '', body: '' });
  const savedTimerRef = useRef(null);
  const mountedRef = useRef(true);
  const loadStartedRef = useRef(false);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      clearTimeout(savedTimerRef.current);
    };
  }, []);

  const applyDraft = useCallback((data) => {
    const fields = { to: data?.to || '', subject: data?.subject || '', body: data?.body || '' };
    setDraft(data);
    setForm(fields);
    savedRef.current = fields;
    setPhase('ready');
  }, []);

  const generate = useCallback(
    async (force) => {
      setPhase('generating');
      setNotice(null);
      try {
        const res = await client.post(draftUrl, force ? { force: true } : {});
        if (!mountedRef.current) return;
        applyDraft(res.data);
        onChanged?.();
      } catch (err) {
        console.error(err);
        if (!mountedRef.current) return;
        // 502: the model's answer was unusable, so trying again is the
        // right move.  400: this invoice needs no follow-up at all.
        const text =
          err.response?.data?.detail ||
          (err.response?.status === 502
            ? 'The follow-up could not be drafted. Please try again.'
            : 'Could not draft this follow-up.');
        setLoadError(text);
        setPhase('error');
      }
    },
    [draftUrl, applyDraft, onChanged]
  );

  const load = useCallback(async () => {
    setPhase('loading');
    setLoadError('');

    let current = null;
    try {
      const res = await client.get(draftUrl);
      current = res.data;
    } catch (err) {
      // 404 is the ordinary "nothing drafted yet" case, not a failure.
      if (err.response?.status !== 404) {
        console.error(err);
        if (!mountedRef.current) return;
        setLoadError('Failed to load this draft.');
        setPhase('error');
        return;
      }
    }
    if (!mountedRef.current) return;

    const listState = entry?.followup_state;
    const state = current?.followup_state || listState;

    // A finished case with no draft has nothing to write — asking the
    // backend would only earn a 400.
    const finished = ['not_needed', 'resolved'].includes(state);

    if ((!current && !finished) || AUTO_GENERATE_STATES.includes(state)) {
      await generate(false);
      return;
    }

    if (current) applyDraft(current);
    else setPhase('empty');
  }, [draftUrl, entry?.followup_state, generate, applyDraft]);

  // Once per mount.  The guard matters under StrictMode, where effects
  // run twice in development and would otherwise generate twice.
  useEffect(() => {
    if (loadStartedRef.current) return;
    loadStartedRef.current = true;
    load();
  }, [load]);

  const state = draft?.followup_state || entry?.followup_state;
  const isSent = draft?.status === 'sent';
  const notNeeded = state === 'not_needed';
  const readOnly = isSent || notNeeded;
  const supplier = entry?.supplier || 'this supplier';
  const toValid = isValidEmail(form.to);

  const flashSaved = () => {
    setSavedVisible(true);
    clearTimeout(savedTimerRef.current);
    savedTimerRef.current = setTimeout(() => {
      if (mountedRef.current) setSavedVisible(false);
    }, SAVED_FLASH_MS);
  };

  // Save one field when it loses focus.  Only that field's saved value
  // is updated afterwards — the form is left alone, because by the time
  // the response lands the user may already be typing in the next one.
  const handleBlur = async (field) => {
    if (readOnly || sending || !draft) return;
    const value = form[field];
    if (value.trim() === (savedRef.current[field] || '').trim()) return;

    try {
      const res = await client.patch(draftUrl, { [field]: value });
      if (!mountedRef.current) return;
      savedRef.current = { ...savedRef.current, [field]: value };
      setDraft(res.data);
      flashSaved();
    } catch (err) {
      // 409: it was sent in the meantime, which the send path reports.
      if (err.response?.status === 409) return;
      console.error(err);
      if (mountedRef.current) setNotice({ tone: 'error', text: 'Could not save your changes.' });
    }
  };

  const handleRegenerate = () => {
    if (draft?.edited) {
      const confirmed = window.confirm(
        'Regenerating replaces the changes you made to this draft. Continue?'
      );
      if (!confirmed) return;
    }
    generate(true);
  };

  const handleSend = async () => {
    const to = form.to.trim();
    if (!isValidEmail(to)) return;
    if (!window.confirm(`Send this email to ${to} from your Gmail?`)) return;

    setSending(true);
    setNotice(null);
    try {
      const res = await client.post(sendUrl, { to, subject: form.subject, body: form.body });
      if (!mountedRef.current) return;
      setDraft((previous) => ({
        ...previous,
        to,
        status: 'sent',
        sent_at: res.data.sent_at,
        gmail_thread_id: res.data.gmail_thread_id || previous?.gmail_thread_id,
        followup_state: 'awaiting_reply',
      }));
      onChanged?.();
    } catch (err) {
      console.error(err);
      if (!mountedRef.current) return;
      const data = err.response?.data || {};
      if (data.needs_reconnect) {
        setNotice({
          tone: 'error',
          text: 'Gmail access has expired, so nothing was sent. Use Reconnect in the banner at the top of this tab, then send again.',
        });
        onNeedsReconnect?.();
      } else if (err.response?.status === 409) {
        // Sent from somewhere else in the meantime: show it as it is.
        setNotice({ tone: 'error', text: 'This follow-up has already been sent.' });
        try {
          const fresh = await client.get(draftUrl);
          if (mountedRef.current) applyDraft(fresh.data);
        } catch (reloadErr) {
          console.error(reloadErr);
        }
      } else {
        setNotice({ tone: 'error', text: data.detail || 'The follow-up could not be sent.' });
      }
    } finally {
      if (mountedRef.current) setSending(false);
    }
  };

  const updateField = (field) => (event) => setForm({ ...form, [field]: event.target.value });

  if (phase === 'loading') {
    return <div className="flex-1 flex items-center justify-center text-sm text-gray-500">Loading draft…</div>;
  }

  if (phase === 'generating') {
    return <GeneratingLoader />;
  }

  if (phase === 'error') {
    return (
      <div className="flex-1 flex flex-col items-center justify-center gap-3 p-8 text-center">
        <p className="text-sm text-red-600">{loadError}</p>
        <button onClick={load} className="text-sm text-blue-800 hover:underline">
          Try again
        </button>
      </div>
    );
  }

  if (phase === 'empty') {
    return (
      <div className="flex-1 flex items-center justify-center p-8">
        <div className="px-4 py-3 bg-gray-50 border border-gray-200 rounded-lg text-sm text-gray-600">
          This invoice was resolved — no follow-up needed
        </div>
      </div>
    );
  }

  const stateDisplay = describeFollowupState(state);
  const toMatchesSaved = form.to.trim().toLowerCase() === (draft?.to || '').toLowerCase();
  const toCaption = toMatchesSaved ? TO_SOURCE_CAPTIONS[draft?.to_source] : 'Entered by you';
  const excluded = draft?.excluded_findings || [];

  return (
    <div className="flex-1 flex flex-col min-h-0">
      <div className="flex-1 overflow-y-auto custom-scrollbar p-6 space-y-4">
        {/* Header */}
        <div className="flex items-start justify-between gap-4">
          <div className="min-w-0">
            <h3 className="text-base font-semibold text-gray-900 truncate">
              {entry?.invoice_filename || docId}
            </h3>
            <p className="text-xs text-gray-500 mt-1">
              {supplier}
              {draft?.followup_type &&
                ` · ${draft.followup_type === 'dispute' ? 'Dispute' : 'Information request'}`}
            </p>
          </div>
          <div className="flex items-center gap-3 flex-shrink-0">
            {savedVisible && <span className="text-xs text-emerald-700">Saved</span>}
            <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-[10px] font-medium border ${stateDisplay.className}`}>
              {stateDisplay.label}
            </span>
          </div>
        </div>

        {/* Banners */}
        {isSent && (
          <div className="flex items-center justify-between gap-4 px-4 py-3 bg-gray-50 border border-gray-200 rounded-lg text-sm text-gray-700">
            <span>
              Sent to <span className="font-medium">{draft.to}</span>
              {' · '}
              {formatDateTime(draft.sent_at)}
              {' · '}
              {state === 'resolved' ? 'resolved' : 'awaiting reply'}
            </span>
            {draft.gmail_thread_id && onOpenThread && (
              <button
                onClick={() => onOpenThread(draft.gmail_thread_id)}
                className="flex-shrink-0 text-blue-800 font-medium hover:underline"
              >
                View thread &rarr;
              </button>
            )}
          </div>
        )}

        {notNeeded && !isSent && (
          <div className="px-4 py-3 bg-gray-50 border border-gray-200 rounded-lg text-sm text-gray-600">
            This invoice was resolved — no follow-up needed
          </div>
        )}

        {draft?.round > 1 && (
          <div className="px-4 py-3 bg-blue-50 border border-blue-200 rounded-lg text-sm text-blue-900">
            Follow-up round {draft.round} — replying in the same thread
          </div>
        )}

        {state === 'draft_stale' && !readOnly && (
          <div className="flex items-center justify-between gap-4 px-4 py-3 bg-amber-50 border border-amber-200 rounded-lg text-sm text-amber-800">
            <span>The discrepancy changed since this draft was written.</span>
            <button
              onClick={handleRegenerate}
              disabled={sending}
              className="flex-shrink-0 font-medium text-amber-900 hover:underline disabled:opacity-50"
            >
              Regenerate
            </button>
          </div>
        )}

        {excluded.length > 0 && (
          <details className="group px-4 py-3 bg-gray-50 border border-gray-200 rounded-lg text-sm text-gray-700">
            <summary className="cursor-pointer select-none flex items-center justify-between gap-4">
              <span>
                {excluded.length} finding{excluded.length === 1 ? '' : 's'} left out because their evidence
                couldn&apos;t be verified against source documents
              </span>
              <span className="text-gray-400 group-open:rotate-180 transition-transform">▼</span>
            </summary>
            <ul className="mt-2 pl-5 list-disc space-y-1 text-gray-600">
              {excluded.map((label, index) => (
                <li key={index}>{typeof label === 'string' ? label : label?.description || label?.type}</li>
              ))}
            </ul>
          </details>
        )}

        {notice && (
          <div
            className={`px-4 py-3 rounded-lg border text-sm ${
              notice.tone === 'error'
                ? 'bg-red-50 border-red-200 text-red-700'
                : 'bg-emerald-50 border-emerald-200 text-emerald-800'
            }`}
          >
            {notice.text}
          </div>
        )}

        {/* Fields */}
        <div className="space-y-3">
          <div>
            <label htmlFor={`followup-to-${docId}`} className="block text-xs font-medium text-gray-500 uppercase tracking-wider mb-1">
              To
            </label>
            <input
              id={`followup-to-${docId}`}
              type="email"
              value={form.to}
              onChange={updateField('to')}
              onBlur={() => handleBlur('to')}
              disabled={readOnly || sending}
              placeholder="supplier@example.com"
              className={INPUT_CLASS}
            />
            {!readOnly &&
              (form.to.trim() === '' ? (
                <p className="text-xs text-amber-700 mt-1">
                  No email address found for {supplier} — enter one. Clarivo will remember it.
                </p>
              ) : (
                toCaption && <p className="text-xs text-gray-400 mt-1">{toCaption}</p>
              ))}
          </div>

          <div>
            <label htmlFor={`followup-subject-${docId}`} className="block text-xs font-medium text-gray-500 uppercase tracking-wider mb-1">
              Subject
            </label>
            <input
              id={`followup-subject-${docId}`}
              type="text"
              value={form.subject}
              onChange={updateField('subject')}
              onBlur={() => handleBlur('subject')}
              disabled={readOnly || sending}
              className={INPUT_CLASS}
            />
          </div>

          <div>
            <label htmlFor={`followup-body-${docId}`} className="block text-xs font-medium text-gray-500 uppercase tracking-wider mb-1">
              Message
            </label>
            <textarea
              id={`followup-body-${docId}`}
              rows={16}
              value={form.body}
              onChange={updateField('body')}
              onBlur={() => handleBlur('body')}
              disabled={readOnly || sending}
              className={`${INPUT_CLASS} leading-relaxed resize-y min-h-[18rem]`}
            />
          </div>
        </div>
      </div>

      {/* Bottom row */}
      {!readOnly && (
        <div className="flex-shrink-0 flex items-center justify-between gap-4 px-6 py-4 border-t border-gray-200 bg-white">
          <button onClick={handleRegenerate} disabled={sending} className={SECONDARY_BUTTON_CLASS}>
            Regenerate
          </button>
          <button
            onClick={handleSend}
            disabled={sending || !toValid}
            title={toValid ? undefined : 'Enter a valid email address to send'}
            className="bg-blue-800 hover:bg-blue-900 text-white text-sm font-medium px-5 py-2 rounded shadow-sm disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
          >
            {sending ? 'Sending…' : 'Send'}
          </button>
        </div>
      )}
    </div>
  );
}
