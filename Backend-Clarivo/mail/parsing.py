"""
Turn a raw Gmail API message into the handful of fields Clarivo needs.

Gmail's ``format="full"`` response is a nested MIME tree, not a flat
record: headers live in a list of ``{name, value}`` pairs whose casing is
whatever the sending mail server chose, bodies are base64url-encoded
somewhere inside ``payload.parts``, and the same message may carry the
text twice (once as ``text/plain``, once as ``text/html``).
``parse_message`` flattens all of that into one dict.

``clean_body`` then strips the quoted reply history.  That matters more
than it looks: each reply in a thread repeats the entire conversation
below it, so storing bodies verbatim would embed the same paragraphs into
Weaviate once per reply — inflating the project context with duplicates
and letting an old, superseded quantity outvote the current one at
retrieval time.
"""

import base64
import re
from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.utils import parseaddr

from bs4 import BeautifulSoup

# Body text is capped before it is stored.  A long thread is mostly
# quoted history anyway, and the useful content of a supplier email is
# near the top.
MAX_BODY_CHARS = 8000


# ---------------------------------------------------------------------------
# Header helpers
# ---------------------------------------------------------------------------

def _header_map(part):
    """Flatten a part's header list into a lowercase-keyed dict.

    Header names arrive with whatever capitalisation the sending server
    used — ``Message-ID``, ``Message-Id`` and ``MESSAGE-ID`` are all the
    same header per RFC 5322 — so every lookup goes through this.  The
    first occurrence wins, which is the right choice for the headers read
    here (a second ``Subject`` is malformed mail, not extra information).
    """
    headers = {}
    for header in (part or {}).get("headers", []) or []:
        name = (header.get("name") or "").lower()
        if name and name not in headers:
            headers[name] = header.get("value") or ""
    return headers


def _decode_mime_header(value):
    """Decode an RFC 2047 encoded header into plain text.

    Gmail hands back header values exactly as they arrived, so a subject
    with a non-ASCII character comes through as
    ``=?UTF-8?B?UmVjaHVuZyBmw7xy?=`` rather than as readable text.  This
    is not cosmetic: the subject line is one of the things email routing
    searches for a project name in, and an undecoded subject can never
    match.
    """
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        # Malformed encoded-words are not worth losing the header over.
        return value


def _charset_of(content_type):
    """Pull the ``charset`` parameter out of a Content-Type header value."""
    for parameter in (content_type or "").split(";")[1:]:
        if "=" not in parameter:
            continue
        key, _, raw_value = parameter.partition("=")
        if key.strip().lower() == "charset":
            return raw_value.strip().strip('"').strip("'")
    return None


# ---------------------------------------------------------------------------
# Body helpers
# ---------------------------------------------------------------------------

def _decode_body_data(data, charset):
    """base64url-decode *data* and decode the bytes using *charset*.

    Gmail strips the ``=`` padding from its base64url output, which
    ``base64.urlsafe_b64decode`` rejects outright, so the padding is put
    back first.  ``errors="replace"`` because a mislabelled charset should
    cost a few characters, not the whole email.
    """
    if not data:
        return ""

    padded = data + "=" * (-len(data) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded)
    except Exception:
        return ""

    try:
        return raw.decode(charset or "utf-8", errors="replace")
    except LookupError:
        # An unknown charset name, e.g. a typo'd "utf8-"; fall back.
        return raw.decode("utf-8", errors="replace")


def _is_attachment_part(part, headers):
    """True if this part is a file travelling with the message.

    Checked before a part's text is treated as body content: an attached
    ``notes.txt`` is a ``text/plain`` part too, and without this test its
    contents would be spliced into the email body.
    """
    if part.get("filename"):
        return True
    if (part.get("body") or {}).get("attachmentId"):
        return True
    return (headers.get("content-disposition", "").strip().lower()
            .startswith("attachment"))


def _walk_parts(part, plain_chunks, html_chunks, attachments):
    """Recurse through the MIME tree, sorting parts into the three buckets."""
    if not part:
        return

    headers = _header_map(part)
    mime_type = (part.get("mimeType") or "").lower()
    body = part.get("body") or {}

    if _is_attachment_part(part, headers):
        attachments.append(_describe_attachment(part, headers, mime_type, body))
    elif mime_type == "text/plain":
        plain_chunks.append(
            _decode_body_data(
                body.get("data"),
                _charset_of(headers.get("content-type")),
            )
        )
    elif mime_type == "text/html":
        html_chunks.append(
            _decode_body_data(
                body.get("data"),
                _charset_of(headers.get("content-type")),
            )
        )

    # Container parts (multipart/*) hold the real content in their
    # children.  Recursing unconditionally also handles a forwarded email
    # arriving as a nested message/rfc822 part.
    for child in part.get("parts") or []:
        _walk_parts(child, plain_chunks, html_chunks, attachments)


def _describe_attachment(part, headers, mime_type, body):
    """Build the attachment record for one MIME part."""
    disposition = headers.get("content-disposition", "").strip().lower()

    return {
        "filename": part.get("filename") or "",
        "mime_type": mime_type,
        "attachment_id": body.get("attachmentId") or "",
        "size": body.get("size") or 0,
        # An inline part is referenced from the HTML body by its
        # Content-ID rather than being offered to the reader as a file —
        # in practice almost always a logo in a signature block.  Flagged
        # rather than dropped here so the caller decides.
        "is_inline": bool(headers.get("content-id"))
        or disposition.startswith("inline"),
    }


def _html_to_text(html):
    """Convert an HTML body to plain text."""
    return BeautifulSoup(html, "html.parser").get_text("\n")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_message(gmail_message):
    """Flatten a ``format="full"`` Gmail message into a plain dict.

    Returns a dict with ``gmail_message_id``, ``thread_id``,
    ``internal_date`` (an aware datetime), ``headers``, ``body_text`` and
    ``attachments``.  The body is the raw extracted text — pass it
    through ``clean_body`` before storing it.
    """
    message = gmail_message or {}
    payload = message.get("payload") or {}
    top_headers = _header_map(payload)

    plain_chunks, html_chunks, attachments = [], [], []
    _walk_parts(payload, plain_chunks, html_chunks, attachments)

    # text/plain wins when the sender provided both: it is what they
    # actually typed, where the HTML alternative is wrapped in markup that
    # survives conversion only approximately.
    plain = "\n".join(chunk for chunk in plain_chunks if chunk).strip()
    if plain:
        body_text = plain
    else:
        html = "\n".join(chunk for chunk in html_chunks if chunk)
        body_text = _html_to_text(html).strip() if html else ""

    internal_date = message.get("internalDate")
    from_name, from_email = parseaddr(top_headers.get("from", ""))

    return {
        "gmail_message_id": message.get("id") or "",
        "thread_id": message.get("threadId") or "",
        # Sent as a string holding epoch milliseconds (int64).
        "internal_date": (
            datetime.fromtimestamp(int(internal_date) / 1000, tz=timezone.utc)
            if internal_date
            else None
        ),
        "headers": {
            "from_name": _decode_mime_header(from_name),
            # Addresses are ASCII and case-insensitive; normalising here
            # means every later comparison against a supplier contact can
            # be a plain equality test.
            "from_email": from_email.strip().lower(),
            "to": _decode_mime_header(top_headers.get("to", "")),
            "subject": _decode_mime_header(top_headers.get("subject", "")),
            "date": top_headers.get("date", ""),
            # The RFC 5322 Message-ID, which is *not* the Gmail message
            # id: this is the globally unique value other mail servers
            # quote in In-Reply-To and References, and so the one that
            # stitches a thread together across providers.
            "rfc_message_id": top_headers.get("message-id", ""),
            "in_reply_to": top_headers.get("in-reply-to", ""),
            "references": top_headers.get("references", ""),
        },
        "body_text": body_text,
        "attachments": attachments,
    }


# ---------------------------------------------------------------------------
# Quoted-history removal
# ---------------------------------------------------------------------------

# Gmail's attribution line, e.g.
#     On Mon, 3 Mar 2025 at 09:14, Jane Patel <jane@acme.com> wrote:
# Long addresses push it onto a second line, so the gap between "On" and
# "wrote:" is allowed to contain newlines — but is bounded, so that an
# "On" in a sentence cannot swallow the rest of the email by pairing with
# some distant "wrote:".
_GMAIL_ATTRIBUTION_RE = re.compile(
    r"^On\s[\s\S]{0,300}?\swrote:\s*$",
    re.MULTILINE,
)

# Outlook's separator, in its various dash counts and localised forms.
_ORIGINAL_MESSAGE_RE = re.compile(
    r"^\s*-{2,}\s*Original Message\s*-{2,}\s*$",
    re.MULTILINE | re.IGNORECASE,
)

# The header block Outlook writes above quoted text:
#     From: Jane Patel <jane@acme.com>
#     Sent: Monday 3 March 2025 09:14
_FROM_LINE_RE = re.compile(r"^\s*From:\s*\S", re.IGNORECASE)
_SENT_LINE_RE = re.compile(r"^\s*(?:Sent|Date):\s*\S", re.IGNORECASE)


def _find_outlook_header_block(text):
    """Offset of an Outlook-style ``From:``/``Sent:`` quote header, if any.

    A bare ``From:`` line is not enough on its own — it appears in
    legitimate prose and in pasted invoice details — so it only counts as
    a quote marker when a ``Sent:`` or ``Date:`` line follows within the
    next few lines, as Outlook always writes it.
    """
    offset = 0
    lines = text.splitlines(keepends=True)

    for index, line in enumerate(lines):
        if _FROM_LINE_RE.match(line):
            for following in lines[index + 1:index + 4]:
                if _SENT_LINE_RE.match(following):
                    return offset
        offset += len(line)

    return None


def _strip_quoted_history(text):
    """Cut *text* at the first sign of quoted reply history."""
    cuts = []

    for pattern in (_GMAIL_ATTRIBUTION_RE, _ORIGINAL_MESSAGE_RE):
        match = pattern.search(text)
        if match:
            cuts.append(match.start())

    outlook_cut = _find_outlook_header_block(text)
    if outlook_cut is not None:
        cuts.append(outlook_cut)

    if not cuts:
        return text
    return text[:min(cuts)]


def clean_body(text):
    """Reduce an email body to just the part the sender actually wrote.

    Removes quoted reply history, ``>`` quote lines and excess blank
    lines, then truncates.  Safe to call on an empty string.
    """
    if not text:
        return ""

    # Normalise line endings first: a CRLF body would otherwise defeat
    # the `$`-anchored patterns below, which match before the \n but
    # after the \r.
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    text = _strip_quoted_history(text)

    # Drop quote lines.  Leading whitespace is stripped before the test
    # because nested quotes are often indented ("  > > as discussed").
    text = "\n".join(
        line for line in text.split("\n") if not line.lstrip().startswith(">")
    )

    # Collapse runs of 3 or more blank lines down to a single blank line.
    # Counted in newlines: one blank line is "\n\n", so three blank lines
    # are four consecutive newlines.
    text = re.sub(r"\n{4,}", "\n\n", text)

    return text.strip()[:MAX_BODY_CHARS]
