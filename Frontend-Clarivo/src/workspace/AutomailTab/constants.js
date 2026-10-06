// Shared vocabulary for the Automail tab.  Both sub-views and the tab
// badge read from here, so a state only has to be described once.

// The follow-up states that are a person's turn to act.  The badge on
// the Automail tab counts exactly these.
export const ATTENTION_STATES = ['needs_draft', 'reply_received', 'draft_stale'];

// How each follow-up state is presented.  Same bordered-pill vocabulary
// as the Files and Workspace tabs, so a colour means the same thing
// everywhere in the workspace.
export const FOLLOWUP_STATE_DISPLAY = {
  needs_draft: {
    label: 'Needs follow-up',
    className: 'bg-amber-50 text-amber-700 border-amber-200',
  },
  reply_received: {
    label: 'Supplier replied',
    className: 'bg-blue-50 text-blue-800 border-blue-200',
  },
  draft_stale: {
    label: 'Draft out of date',
    className: 'bg-amber-50 text-amber-700 border-amber-200',
  },
  draft_ready: {
    label: 'Draft ready',
    className: 'bg-teal-50 text-teal-800 border-teal-200',
  },
  awaiting_reply: {
    label: 'Awaiting reply',
    className: 'bg-gray-50 text-gray-600 border-gray-200',
  },
  resolved: {
    label: 'Resolved',
    className: 'bg-emerald-50 text-emerald-700 border-emerald-200',
  },
  not_needed: {
    label: 'No longer needed',
    className: 'bg-gray-50 text-gray-400 border-gray-200',
  },
};

export const describeFollowupState = (state) =>
  FOLLOWUP_STATE_DISPLAY[state] || {
    label: (state || 'unknown').replace(/_/g, ' '),
    className: 'bg-gray-50 text-gray-600 border-gray-200',
  };

// Why an email ended up in this project.  Shown so a thread that looks
// unrelated can be explained rather than just doubted.
export const ROUTE_REASON_LABELS = {
  thread: 'Reply to follow-up',
  project_name: 'Project named',
  known_supplier: 'Known supplier',
  sent_by_clarivo: 'Sent by Clarivo',
};

export const routeReasonLabel = (reason) =>
  ROUTE_REASON_LABELS[reason] || (reason ? reason.replace(/_/g, ' ') : '');

// Where a filled-in recipient address came from.  "none" has no caption:
// an empty To field gets its own helper text instead.
export const TO_SOURCE_CAPTIONS = {
  email_sender: "Learned from the supplier's own email",
  document: "Found on the supplier's documents",
  manual: 'Entered by you',
};

// An attachment's pipeline status, in the Files tab's colours: amber
// while it is being worked on, green once it has been read, red when it
// could not be.
const IN_PROGRESS_STATUSES = [
  'pending_extraction',
  'extracted',
  'embedding',
  'embedded',
  'classifying',
  'checking',
];
const DONE_STATUSES = ['classified', 'checked'];

export const describeAttachmentStatus = (status) => {
  const label = (status || 'unknown').replace(/_/g, ' ');
  if (IN_PROGRESS_STATUSES.includes(status)) {
    return { label: 'Processing', className: 'bg-amber-50 text-amber-700 border-amber-200' };
  }
  if (DONE_STATUSES.includes(status)) {
    return { label: status === 'checked' ? 'Checked' : 'Ready', className: 'bg-emerald-50 text-emerald-700 border-emerald-200' };
  }
  if ((status || '').startsWith('failed')) {
    return { label: 'Failed', className: 'bg-red-50 text-red-700 border-red-200' };
  }
  return { label, className: 'bg-gray-50 text-gray-600 border-gray-200' };
};

// Deliberately permissive: the backend and Gmail are the real judges of
// an address.  This only stops Send being pressed on obvious nonsense.
const EMAIL_PATTERN = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
export const isValidEmail = (value) => EMAIL_PATTERN.test((value || '').trim());

/** A timestamp as "6 Oct 2026, 14:32", or '' when there isn't one. */
export const formatDateTime = (value) => {
  if (!value) return '';
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleString(undefined, {
    day: 'numeric',
    month: 'short',
    year: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  });
};

/** A timestamp as "just now", "5m ago", "3h ago", "2d ago" or a date. */
export const formatRelative = (value) => {
  if (!value) return '';
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;

  const seconds = Math.round((Date.now() - parsed.getTime()) / 1000);
  if (seconds < 60) return 'just now';
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.round(hours / 24);
  if (days < 7) return `${days}d ago`;
  return parsed.toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' });
};
