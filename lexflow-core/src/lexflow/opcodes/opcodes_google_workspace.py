"""Google Workspace opcodes for LexFlow (Polaris ingestion).

Provides Calendar / Drive / Gmail operations used by the Polaris pipeline to
discover external sales meetings, fetch their transcripts, and create the
follow-up draft email to the champion.

Installation:
    pip install lexflow[google_workspace]

Authentication:
    Uses a Google service account with domain-wide delegation. The service
    account impersonates a Workspace user (the subject) to read their Calendar
    and Drive (Meet recordings/transcripts) and to create Gmail drafts.

Required scopes:
    - https://www.googleapis.com/auth/calendar.events.readonly
    - https://www.googleapis.com/auth/calendar.events
    - https://www.googleapis.com/auth/drive.readonly
    - https://www.googleapis.com/auth/gmail.compose
"""

from __future__ import annotations

import asyncio
import base64
import json
from email.mime.text import MIMEText
from typing import Any, Dict, List, Optional

try:
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError

    GOOGLE_WORKSPACE_AVAILABLE = True
except ImportError:
    GOOGLE_WORKSPACE_AVAILABLE = False
    HttpError = Exception


def register_google_workspace_opcodes() -> None:
    """Register Google Workspace opcodes to the default registry."""
    if not GOOGLE_WORKSPACE_AVAILABLE:
        return

    from .opcodes import opcode, register_category

    register_category(
        id="google_workspace",
        label="Google Workspace",
        prefix="gworkspace_",
        description="Calendar, Drive e Gmail via service account (domain-wide delegation).",
        color="#4285F4",
        icon="🗂️",
        requires="google_workspace",
        order=270,
    )

    @opcode(category="google_workspace")
    async def gworkspace_create_service(
        service_account_json: str,
        api: str,
        version: str,
        scopes: List[str],
        subject: Optional[str] = None,
    ) -> Any:
        """Create a Google API service client via service account.

        Args:
            service_account_json: Service account credentials as a JSON string.
            api: API name - "calendar", "drive" or "gmail".
            version: API version - "v3", "v3" or "v1" respectively.
            scopes: OAuth2 scopes to request.
            subject: Workspace user e-mail to impersonate (domain-wide delegation).

        Returns:
            An authenticated Google API service resource.
        """
        def _build():
            info = json.loads(service_account_json)
            creds = service_account.Credentials.from_service_account_info(
                info, scopes=scopes
            )
            if subject:
                creds = creds.with_subject(subject)
            return build(api, version, credentials=creds, cache_discovery=False)

        try:
            return await asyncio.to_thread(_build)
        except Exception as error:
            raise RuntimeError(f"Failed to create Google service: {error}") from error

    @opcode(category="google_workspace")
    async def gworkspace_close_service(service: Any) -> bool:
        """Close a Google API service and release resources."""
        await asyncio.to_thread(service.close)
        return True

    @opcode(category="google_workspace")
    async def gcalendar_list_events(
        service: Any,
        time_min: str = "",
        time_max: str = "",
        days_back: int = 2,
        calendar_id: str = "primary",
        max_results: int = 100,
    ) -> List[Dict[str, Any]]:
        """List calendar events within a time window.

        When time_min/time_max are empty the window is (now - days_back) to now,
        so the daily scheduler works without explicit inputs.

        Args:
            service: Calendar service from gworkspace_create_service.
            time_min: RFC3339 lower bound. Empty means now minus days_back.
            time_max: RFC3339 upper bound. Empty means now.
            days_back: Days to look back when time_min is empty (default: 2).
            calendar_id: Calendar to read (default: primary).
            max_results: Maximum number of events to return.

        Returns:
            List of event resources (summary, start, end, attendees...).
        """
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)
        if not time_max:
            time_max = now.isoformat()
        if not time_min:
            time_min = (now - timedelta(days=days_back)).isoformat()

        def _call():
            return (
                service.events()
                .list(
                    calendarId=calendar_id,
                    timeMin=time_min,
                    timeMax=time_max,
                    maxResults=max_results,
                    singleEvents=True,
                    orderBy="startTime",
                )
                .execute()
            )

        try:
            response = await asyncio.to_thread(_call)
            return response.get("items", [])
        except HttpError as error:
            raise RuntimeError(f"Failed to list calendar events: {error}") from error

    @opcode(category="google_workspace")
    async def gcalendar_create_event(
        service: Any,
        summary: str,
        date: str,
        description: str = "",
        calendar_id: str = "primary",
    ) -> Dict[str, Any]:
        """Create an all-day Calendar event (used as a task with a due date).

        Args:
            service: Calendar service from gworkspace_create_service.
            summary: Event title (e.g. "[Polaris] tarefa").
            date: Due date in YYYY-MM-DD.
            description: Optional event description.
            calendar_id: Target calendar (default: primary).

        Returns:
            The created event resource (with id, htmlLink).
        """
        body = {
            "summary": summary,
            "description": description,
            "start": {"date": date},
            "end": {"date": date},
        }

        def _call():
            return service.events().insert(calendarId=calendar_id, body=body).execute()

        try:
            return await asyncio.to_thread(_call)
        except HttpError as error:
            raise RuntimeError(f"Failed to create calendar event: {error}") from error

    @opcode(category="google_workspace")
    async def gdrive_search_files(
        service: Any,
        query: str,
        page_size: int = 20,
    ) -> List[Dict[str, Any]]:
        """Search Drive files using Drive query syntax.

        Args:
            service: Drive service from gworkspace_create_service.
            query: Drive q-query string.
            page_size: Maximum number of files to return.

        Returns:
            List of file metadata dicts (id, name, mimeType, createdTime, modifiedTime).
        """
        def _call():
            return (
                service.files()
                .list(
                    q=query,
                    pageSize=page_size,
                    fields="files(id, name, mimeType, createdTime, modifiedTime)",
                    orderBy="modifiedTime desc",
                )
                .execute()
            )

        try:
            response = await asyncio.to_thread(_call)
            return response.get("files", [])
        except HttpError as error:
            raise RuntimeError(f"Failed to search Drive files: {error}") from error

    @opcode(category="google_workspace")
    async def gdrive_export_text(
        service: Any,
        file_id: str,
    ) -> str:
        """Export a Google Doc as plain text (includes all tabs).

        Useful for Meet transcript docs where the verbatim transcript is in a
        separate tab - the text export concatenates all tab contents.

        Args:
            service: Drive service from gworkspace_create_service.
            file_id: The Google Doc file ID.

        Returns:
            The document plain-text content.
        """
        def _call():
            return (
                service.files()
                .export(fileId=file_id, mimeType="text/plain")
                .execute()
            )

        try:
            data = await asyncio.to_thread(_call)
            if isinstance(data, bytes):
                return data.decode("utf-8", errors="replace")
            return str(data)
        except HttpError as error:
            raise RuntimeError(f"Failed to export Drive file {file_id}: {error}") from error

    @opcode(category="google_workspace")
    async def gmail_create_draft(
        service: Any,
        to: str,
        subject: str,
        body: str,
        body_html: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create a Gmail draft (not sent).

        Args:
            service: Gmail service from gworkspace_create_service.
            to: Recipient e-mail address.
            subject: Subject line.
            body: Plain-text body.
            body_html: Optional HTML body (used instead of plain text when provided).

        Returns:
            The created draft resource (with id).
        """
        message = MIMEText(body_html, "html") if body_html else MIMEText(body, "plain")
        message["To"] = to
        message["Subject"] = subject
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")

        def _call():
            return (
                service.users()
                .drafts()
                .create(userId="me", body={"message": {"raw": raw}})
                .execute()
            )

        try:
            return await asyncio.to_thread(_call)
        except HttpError as error:
            raise RuntimeError(f"Failed to create Gmail draft: {error}") from error
