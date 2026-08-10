"""Shared Google API authentication for LexFlow opcodes.

Centralizes the credentials_path validation, service-account loading, domain-wide
delegation (subject impersonation), and Application Default Credentials fallback
used by opcodes_gmail.py and opcodes_gtasks.py, so the two stay in sync as this
logic evolves instead of drifting apart.
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
            together with a service account ``credentials_path``.
            Note: with a service account authorized in the Admin Console,
            ``subject`` can impersonate ANY user in the domain — restrict
            which subjects a workflow may pass here at the caller/app layer.

    Returns:
        The built googleapiclient service (Resource) object.

    Raises:
        ValueError: If ``credentials_path`` contains '..' components, doesn't
            end in ``.json``, or doesn't exist; or if ``subject`` is given
            without a service account ``credentials_path``.
    """
    scopes = list(scopes)
    if credentials_path:
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
