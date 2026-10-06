"""
Decide which project an incoming email belongs to — or that none does.

This is the gate in front of everything the sync stores.  An email that
no rule claims is skipped and *nothing* about it is persisted: not an
EMAIL# record, not a correspondence document, not its attachments.  A
user's inbox is mostly not about Clarivo, and the cost of guessing wrong
is not a tidy-up job but a privacy problem — unrelated personal mail
embedded into a project's searchable context.

The three rules are tried in descending order of confidence:

1. **thread** — the email continues a Gmail thread already linked to a
   project, e.g. a supplier replying to a follow-up Clarivo sent.  This
   is the only rule that is certain rather than inferred, and the only
   one that can also identify *which invoice* the conversation is about.
2. **project_name** — a project's name appears in the subject or body.
3. **known_supplier** — the sender is a recorded contact of exactly one
   project.

Rule 3 deliberately gives up when more than one project matches.  A
supplier who works on two sites is the normal case, not an edge case,
and filing their email into whichever project happened to be listed
first would quietly attach evidence to the wrong job.  Ambiguous is a
real answer here, and the answer is "don't store it".

Every rule draws only on projects owned by ``account.user``.  A Gmail
account is connected by one user, and the mail it receives can never be
routed into somebody else's project.
"""

import logging
import re

from projects.models import Project

from .parsing import clean_body
from .storage import get_thread_link, list_contacts

logger = logging.getLogger(__name__)

# Project names shorter than this are not searched for in message text.
# A name like "Ash" or "Site" would hit ordinary prose constantly, and a
# false match here routes an unrelated email into a real project.
MIN_NAME_MATCH_LENGTH = 5

_WHITESPACE_RE = re.compile(r"\s+")


def _normalise(text):
    """Lowercase *text* and collapse every whitespace run to one space.

    Applied to both the project name and the message text so that a
    subject which wrapped a name across a line break, or wrote it with
    different capitalisation, still matches.
    """
    if not text:
        return ""
    return _WHITESPACE_RE.sub(" ", text).strip().lower()


def _candidate_projects(account):
    """Projects this email could possibly belong to."""
    return list(Project.objects.filter(owner=account.user))


def _cleaned_body(parsed):
    """The body text with quoted history removed.

    Prefers a ``cleaned_body`` the caller has already computed — the sync
    needs it anyway, to store as the correspondence document — and falls
    back to cleaning the raw body here so this function is usable on its
    own.
    """
    if parsed.get("cleaned_body"):
        return parsed["cleaned_body"]
    return clean_body(parsed.get("body_text", ""))


def route_email(account, parsed):
    """Choose the project an email belongs to.

    Parameters
    ----------
    account : GmailAccount
        The connected mailbox the email arrived in.
    parsed : dict
        The output of ``mail.parsing.parse_message``.  If it carries a
        ``cleaned_body`` key that is used as-is; otherwise the body is
        cleaned here.

    Returns
    -------
    tuple[int, str | None, str] | None
        ``(project_id, invoice_doc_id_or_None, route_reason)``, or
        ``None`` if no rule claims the email — in which case the caller
        must store nothing at all.
    """
    projects = _candidate_projects(account)
    if not projects:
        return None

    # Keyed by string, because DynamoDB holds project ids as strings
    # while Django holds them as ints.
    by_id = {str(project.pk): project for project in projects}

    headers = parsed.get("headers") or {}

    # -- Rule 1: the thread is already linked --------------------------
    thread_id = parsed.get("thread_id")
    if thread_id:
        link = get_thread_link(account.email, thread_id)
        if link:
            linked_id = str(link.get("project_id") or "")
            project = by_id.get(linked_id)
            if project is not None:
                # invoice_doc_id is optional on a link, and "" is how an
                # absent one can come back — normalise both to None.
                invoice_doc_id = link.get("invoice_doc_id") or None
                logger.info(
                    "Routed email %s to project %s by thread",
                    parsed.get("gmail_message_id"),
                    project.pk,
                )
                return (project.pk, invoice_doc_id, "thread")

            # A link pointing at a project this user does not own, or
            # one that has since been deleted.  Not an error: thread
            # links outlive the projects they name, so fall through and
            # let the remaining rules try.
            logger.info(
                "Thread %s is linked to project %r, which is not an "
                "available project for %s — ignoring the link",
                thread_id,
                linked_id,
                account.email,
            )

    # -- Rule 2: a project name appears in the text --------------------
    haystack = _normalise(
        f"{headers.get('subject', '')}\n{_cleaned_body(parsed)}"
    )

    if haystack:
        matches = []
        for project in projects:
            name = _normalise(project.name)
            if len(name) < MIN_NAME_MATCH_LENGTH:
                continue
            if name in haystack:
                matches.append((len(name), project))

        if matches:
            # Longest name wins: if a project is called "Riverside" and
            # another "Riverside Tower Phase 2", text naming the second
            # contains the first as a substring, and the more specific
            # name is the better answer.  The project id breaks ties so
            # the choice is deterministic rather than dependent on query
            # order.
            matches.sort(key=lambda pair: (-pair[0], pair[1].pk))
            project = matches[0][1]
            logger.info(
                "Routed email %s to project %s by project name",
                parsed.get("gmail_message_id"),
                project.pk,
            )
            return (project.pk, None, "project_name")

    # -- Rule 3: the sender is a known supplier contact ----------------
    from_email = (headers.get("from_email") or "").strip().lower()

    if from_email:
        matched_projects = []
        for project in projects:
            # Contact emails are stored lowercased by upsert_contact, and
            # from_email is lowercased by parse_message, so this is a
            # plain equality test.
            contact_emails = {
                (contact.get("email") or "").strip().lower()
                for contact in list_contacts(project.pk)
            }
            if from_email in contact_emails:
                matched_projects.append(project)

        if len(matched_projects) == 1:
            project = matched_projects[0]
            logger.info(
                "Routed email %s to project %s by known supplier %s",
                parsed.get("gmail_message_id"),
                project.pk,
                from_email,
            )
            return (project.pk, None, "known_supplier")

        if len(matched_projects) > 1:
            # This supplier works on several of the user's projects, so
            # the sender alone cannot say which one this email is about.
            logger.info(
                "Sender %s is a contact of %d projects — too ambiguous to "
                "route, skipping email %s",
                from_email,
                len(matched_projects),
                parsed.get("gmail_message_id"),
            )
            return None

    logger.info(
        "No routing rule matched email %s from %s — skipping",
        parsed.get("gmail_message_id"),
        from_email or "unknown sender",
    )
    return None
