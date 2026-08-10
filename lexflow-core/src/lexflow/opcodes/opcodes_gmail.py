"""Gmail opcodes for LexFlow.

This module provides opcodes for interacting with the Gmail API, focused on
composing messages: creating drafts (for human review before sending) and
sending messages directly.

Installation:
    pip install lexflow[gmail]
    or:
    pip install google-auth google-auth-oauthlib google-api-python-client

Authentication:
    Option 1 - Service Account (recommended for production):
        Pass the path to a service account JSON file to gmail_create_client().
        To act as a specific mailbox, enable domain-wide delegation on the
        service account and pass that user's address as ``subject`` (the service
        account then impersonates that user).

    Option 2 - Application Default Credentials (recommended for development):
        Run: gcloud auth application-default login
        Then call gmail_create_client() without arguments.

Required scope:
    https://www.googleapis.com/auth/gmail.compose
    (Covers creating/updating drafts AND sending. ``gmail.send`` alone cannot
    create drafts.)

API reference: https://developers.google.com/gmail/api/reference/rest
"""

from __future__ import annotations

import asyncio
import base64
import os
import re
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any, Dict, List, Optional

try:
    from google.oauth2.service_account import Credentials
    from google.auth import default as google_auth_default
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError

    GMAIL_AVAILABLE = True
except ImportError:
    GMAIL_AVAILABLE = False

SCOPES = ("https://www.googleapis.com/auth/gmail.compose",)

_HEADER_INJECTION_RE = re.compile(r"[\r\n]")


class GmailClient:
    """Reusable Gmail client scoped to the authenticated user ("me")."""

    def __init__(self, service):
        self.service = service
        self.users = service.users()


async def _execute(request) -> Any:
    """Run a googleapiclient request off-thread, sanitizing API errors.

    Raises:
        RuntimeError: If the Gmail API returns an error. The raw ``HttpError``
            (whose ``str()`` includes the request URI and response body) is
            not propagated; only its status and reason are.
    """
    try:
        return await asyncio.to_thread(request.execute)
    except HttpError as e:
        raise RuntimeError(f"Gmail API error ({e.status_code}): {e.reason}") from e


def _build_raw(
    to: str,
    subject: str,
    body: str,
    cc: str = "",
    bcc: str = "",
    html: str = "",
) -> str:
    """Build a base64url-encoded RFC 2822 message for the Gmail API.

    Args:
        to: Primary recipient address(es), comma-separated.
        subject: Subject line.
        body: Plain-text body.
        cc: Optional comma-separated CC list.
        bcc: Optional comma-separated BCC list.
        html: Optional HTML body (sent as multipart/alternative alongside text).

    Returns:
        The URL-safe base64 string for the Gmail ``raw`` field.

    Raises:
        ValueError: If ``to`` is empty, or if ``to``/``cc``/``bcc``/``subject``
            contain a carriage return or newline (header injection).
    """
    if not to or not isinstance(to, str):
        raise ValueError("to must be a non-empty string")
    for field_name, value in (
        ("to", to),
        ("cc", cc),
        ("bcc", bcc),
        ("subject", subject),
    ):
        if value and _HEADER_INJECTION_RE.search(value):
            raise ValueError(f"{field_name} must not contain line breaks")

    if html:
        message: Any = MIMEMultipart("alternative")
        message.attach(MIMEText(body or "", "plain"))
        message.attach(MIMEText(html, "html"))
    else:
        message = MIMEText(body or "", "plain")
    message["To"] = to
    if cc:
        message["Cc"] = cc
    if bcc:
        message["Bcc"] = bcc
    message["Subject"] = subject
    return base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")


def register_gmail_opcodes():
    """Register Gmail opcodes to the default registry."""
    if not GMAIL_AVAILABLE:
        return

    from .opcodes import opcode, register_category

    register_category(
        id="gmail",
        label="Gmail Operations",
        prefix="gmail_",
        color="#EA4335",
        icon="✉️",
        requires="gmail",
        order=216,
        description="Cria rascunhos e envia e-mails via Gmail.",
    )

    # ========================================================================
    # Authentication
    # ========================================================================

    @opcode(category="gmail")
    async def gmail_create_client(
        credentials_path: Optional[str] = None,
        subject: Optional[str] = None,
        scopes: Optional[List[str]] = None,
    ) -> GmailClient:
        """Create a Gmail client for API operations.

        Args:
            credentials_path: Path to a service account JSON file. If None,
                uses Application Default Credentials (ADC).
            subject: Mailbox email to impersonate via domain-wide delegation.
                Only valid together with a service account ``credentials_path``.
                Note: with a service account authorized in the Admin Console,
                ``subject`` can impersonate ANY user in the domain — restrict
                which subjects a workflow may pass here at the caller/app layer.
            scopes: OAuth scopes to request (default: ``gmail.compose``, which
                covers creating/updating drafts and sending). Narrow this to
                least privilege if a workflow only needs to create drafts.

        Returns:
            GmailClient object to use with the other gmail_* opcodes.

        Example with Service Account impersonating a mailbox:
            credentials_path: "/path/to/service-account.json"
            subject: "vendedora@empresa.com"

        Example with ADC (after 'gcloud auth application-default login'):
            # No arguments needed
        """
        request_scopes = scopes or SCOPES
        if credentials_path:
            if ".." in os.path.normpath(credentials_path).split(os.sep):
                raise ValueError(
                    "credentials_path must not contain '..' path components"
                )
            resolved = os.path.realpath(credentials_path)
            if not resolved.endswith(".json"):
                raise ValueError("credentials_path must be a .json file")
            if not os.path.isfile(resolved):
                raise ValueError(f"credentials file not found: {credentials_path}")
            credentials = await asyncio.to_thread(
                Credentials.from_service_account_file, resolved, scopes=request_scopes
            )
            if subject:
                credentials = credentials.with_subject(subject)
        else:
            if subject:
                raise ValueError(
                    "subject impersonation requires a service account credentials_path"
                )
            credentials, _ = await asyncio.to_thread(
                google_auth_default, scopes=request_scopes
            )

        service = await asyncio.to_thread(build, "gmail", "v1", credentials=credentials)
        return GmailClient(service)

    @opcode(category="gmail")
    async def gmail_close_client(client: GmailClient) -> bool:
        """Close the Gmail client and release the underlying HTTP connection.

        Args:
            client: GmailClient from gmail_create_client.

        Returns:
            True when closed successfully.

        Example:
            client: { node: create_client }
        """
        await asyncio.to_thread(client.service.close)
        return True

    # ========================================================================
    # Compose Operations
    # ========================================================================

    @opcode(category="gmail")
    async def gmail_create_draft(
        client: GmailClient,
        to: str,
        subject: str,
        body: str,
        cc: str = "",
        bcc: str = "",
        html: str = "",
    ) -> Dict[str, Any]:
        """Create a Gmail draft (not sent), for human review before sending.

        Args:
            client: GmailClient from gmail_create_client.
            to: Primary recipient address(es), comma-separated.
            subject: Subject line.
            body: Plain-text body.
            cc: Optional comma-separated CC list.
            bcc: Optional comma-separated BCC list.
            html: Optional HTML body (sent alongside the plain text).

        Returns:
            The Gmail Draft resource (id, message with id/threadId).

        Example:
            client: { node: create_client }
            to: "champion@cliente.com"
            subject: "Próximos passos"
            body: "Olá, obrigado pela reunião de hoje..."
        """
        raw = _build_raw(to, subject, body, cc, bcc, html)
        request = client.users.drafts().create(
            userId="me", body={"message": {"raw": raw}}
        )
        return await _execute(request)

    @opcode(category="gmail")
    async def gmail_send_message(
        client: GmailClient,
        to: str,
        subject: str,
        body: str,
        cc: str = "",
        bcc: str = "",
        html: str = "",
    ) -> Dict[str, Any]:
        """Send an email immediately as the authenticated user.

        Warning:
            Not idempotent and not cancellable: a timeout followed by a
            workflow retry/fallback can send the message twice. There is no
            dedupe key on this opcode.

            If exposed as an agent tool (e.g. via ``opcodes_pydantic_ai``),
            this is an arbitrary-send primitive reachable by prompt injection
            in any content the agent processes. Prefer allowlisting only
            ``gmail_create_draft`` for agent tool use, reserving this opcode
            for direct workflow calls.

        Args:
            client: GmailClient from gmail_create_client.
            to: Primary recipient address(es), comma-separated.
            subject: Subject line.
            body: Plain-text body.
            cc: Optional comma-separated CC list.
            bcc: Optional comma-separated BCC list.
            html: Optional HTML body (sent alongside the plain text).

        Returns:
            The sent Message resource (id, threadId, labelIds).

        Example:
            client: { node: create_client }
            to: "champion@cliente.com"
            subject: "Próximos passos"
            body: "Olá, obrigado pela reunião de hoje..."
        """
        raw = _build_raw(to, subject, body, cc, bcc, html)
        request = client.users.messages().send(userId="me", body={"raw": raw})
        return await _execute(request)
