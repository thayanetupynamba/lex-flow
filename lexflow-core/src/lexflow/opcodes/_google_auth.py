"""Shared Google API authentication for LexFlow opcodes.

Centralizes the credentials_path validation, service-account loading, domain-wide
delegation (subject impersonation), and Application Default Credentials fallback
used by opcodes_gmail.py and opcodes_gtasks.py, so the two stay in sync as this
logic evolves instead of drifting apart.

Domain-wide delegation allowlist:
    A service account authorized for domain-wide delegation in the Admin
    Console can impersonate ANY user in the domain. Since ``subject`` is a
    workflow-supplied argument, an unrestricted workflow (or one built from
    untrusted/LLM-influenced input) could otherwise impersonate any mailbox.
    ``GOOGLE_DWD_SUBJECT_ALLOWLIST`` restricts which ``subject`` values
    ``build_google_service`` will accept:

    - Unset or empty: impersonation is disabled entirely (``subject`` always
      rejected). This is the fail-closed default.
    - A comma-separated list of entries, each either a full email address
      (exact match, case-insensitive) or ``@domain.com`` (matches any address
      at that domain, case-insensitive). Example:
      ``GOOGLE_DWD_SUBJECT_ALLOWLIST=vendas@empresa.com,@suporte.empresa.com``
"""

from __future__ import annotations

import asyncio
import os
from typing import Optional, Sequence

try:
    from google.auth import default as google_auth_default
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build

    GOOGLE_AUTH_AVAILABLE = True
except ImportError:
    GOOGLE_AUTH_AVAILABLE = False

DWD_SUBJECT_ALLOWLIST_ENV_VAR = "GOOGLE_DWD_SUBJECT_ALLOWLIST"


def _subject_allowed(subject: str) -> bool:
    """Check ``subject`` against GOOGLE_DWD_SUBJECT_ALLOWLIST.

    Args:
        subject: The email address requested for impersonation.

    Returns:
        True if the allowlist is set and contains an exact match for
        ``subject`` or a ``@domain`` entry matching its domain. False
        (including when the env var is unset/empty) otherwise.
    """
    raw = os.environ.get(DWD_SUBJECT_ALLOWLIST_ENV_VAR, "")
    entries = [entry.strip().lower() for entry in raw.split(",") if entry.strip()]
    subject_lower = subject.lower()
    return any(
        subject_lower.endswith(entry)
        if entry.startswith("@")
        else subject_lower == entry
        for entry in entries
    )


async def build_google_service(
    service_name: str,
    version: str,
    scopes: Sequence[str],
    credentials_path: Optional[str] = None,
    subject: Optional[str] = None,
):
    """Build an authenticated googleapiclient service.

    Args:
        service_name: googleapiclient discovery service name (e.g. "gmail").
        version: API version (e.g. "v1").
        scopes: OAuth scopes to request.
        credentials_path: Path to a service account JSON file. If None, uses
            Application Default Credentials (ADC).
        subject: Email to impersonate via domain-wide delegation. Only valid
            together with a service account ``credentials_path``, and only if
            ``subject`` is present in ``GOOGLE_DWD_SUBJECT_ALLOWLIST`` (see
            module docstring) — impersonation is denied by default.

    Returns:
        The built googleapiclient service (Resource) object.

    Raises:
        ValueError: If ``credentials_path`` contains '..' components, doesn't
            end in ``.json``, or doesn't exist; if ``subject`` is given
            without a service account ``credentials_path``; or if ``subject``
            is not allowed by ``GOOGLE_DWD_SUBJECT_ALLOWLIST``.
    """
    scopes = list(scopes)
    if credentials_path:
        if subject and not _subject_allowed(subject):
            raise ValueError(
                f"subject {subject!r} is not allowed for domain-wide delegation "
                f"impersonation. Add it (or its @domain) to the "
                f"{DWD_SUBJECT_ALLOWLIST_ENV_VAR} environment variable."
            )
        if ".." in os.path.normpath(credentials_path).split(os.sep):
            raise ValueError("credentials_path must not contain '..' path components")
        resolved = os.path.realpath(credentials_path)
        if not resolved.endswith(".json"):
            raise ValueError("credentials_path must be a .json file")
        if not os.path.isfile(resolved):
            raise ValueError(f"credentials file not found: {credentials_path}")
        credentials = await asyncio.to_thread(
            Credentials.from_service_account_file, resolved, scopes=scopes
        )
        if subject:
            credentials = credentials.with_subject(subject)
    else:
        if subject:
            raise ValueError(
                "subject impersonation requires a service account credentials_path"
            )
        credentials, _ = await asyncio.to_thread(google_auth_default, scopes=scopes)

    return await asyncio.to_thread(
        build, service_name, version, credentials=credentials
    )
