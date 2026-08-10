"""Google Tasks opcodes for LexFlow.

This module provides opcodes for interacting with the Google Tasks API,
enabling task-list automation workflows (create, read, update, complete and
delete tasks) similar to n8n/Zapier.

Installation:
    pip install lexflow[gtasks]
    or:
    pip install google-auth google-auth-oauthlib google-api-python-client

Authentication:
    Option 1 - Service Account (recommended for production):
        Pass the path to a service account JSON file to gtasks_create_client().
        To act on a specific user's task list, enable domain-wide delegation on
        the service account and pass that user's address as ``subject`` (the
        service account then impersonates that user).

    Option 2 - Application Default Credentials (recommended for development):
        Run: gcloud auth application-default login
        Then call gtasks_create_client() without arguments.

Required scope:
    https://www.googleapis.com/auth/tasks
    (``tasks.readonly`` only covers get/list; create/patch/delete need ``tasks``.)

API reference: https://developers.google.com/tasks/reference/rest
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

try:
    from google.oauth2.service_account import Credentials
    from google.auth import default as google_auth_default
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError

    GTASKS_AVAILABLE = True
except ImportError:
    GTASKS_AVAILABLE = False

SCOPES = ("https://www.googleapis.com/auth/tasks",)

_VALID_STATUSES = ("needsAction", "completed")


class TasksClient:
    """Reusable Google Tasks client."""

    def __init__(self, service):
        self.service = service
        self.tasks = service.tasks()
        self.tasklists = service.tasklists()


async def _execute(request) -> Any:
    """Run a googleapiclient request off-thread, sanitizing API errors.

    Raises:
        RuntimeError: If the Tasks API returns an error. The raw ``HttpError``
            (whose ``str()`` includes the request URI and response body) is
            not propagated; only its status and reason are.
    """
    try:
        return await asyncio.to_thread(request.execute)
    except HttpError as e:
        raise RuntimeError(
            f"Google Tasks API error ({e.status_code}): {e.reason}"
        ) from e


async def _list_all_pages(list_request_factory, item_key: str) -> List[Dict[str, Any]]:
    """Follow ``nextPageToken`` to collect every page of a Tasks list response.

    Args:
        list_request_factory: Callable taking an optional page token and
            returning a googleapiclient request for that page.
        item_key: The response key holding the page's items (always
            ``"items"`` for this API, kept explicit for clarity).

    Returns:
        The concatenated ``items`` across all pages (``[]`` if none).
    """
    items: List[Dict[str, Any]] = []
    page_token: Optional[str] = None
    while True:
        result = await _execute(list_request_factory(page_token))
        items.extend(result.get(item_key, []))
        page_token = result.get("nextPageToken")
        if not page_token:
            return items


def _normalize_due(due: Optional[str]) -> Optional[str]:
    """Normalize and validate a due date for the Tasks API.

    Args:
        due: ``None``/empty string (no due date), a ``YYYY-MM-DD`` date, or a
            full RFC 3339 timestamp.

    Returns:
        ``None`` if ``due`` is empty, an RFC 3339 timestamp for a bare date,
        otherwise the value unchanged. Google Tasks only stores the date part
        of ``due`` and always renders it at midnight UTC.

    Raises:
        ValueError: If ``due`` is non-empty but is not a valid ``YYYY-MM-DD``
            date or ISO 8601/RFC 3339 timestamp.
    """
    if not due:
        return None
    if not isinstance(due, str):
        raise TypeError(f"due must be a string, got {type(due).__name__}")
    if len(due) == 10:
        try:
            datetime.strptime(due, "%Y-%m-%d")
        except ValueError as e:
            raise ValueError(
                f"due must be 'YYYY-MM-DD' or a full RFC 3339 timestamp, got: {due!r}"
            ) from e
        return f"{due}T00:00:00.000Z"
    try:
        datetime.fromisoformat(due.replace("Z", "+00:00"))
    except ValueError as e:
        raise ValueError(
            f"due must be 'YYYY-MM-DD' or a full RFC 3339 timestamp, got: {due!r}"
        ) from e
    return due


def register_gtasks_opcodes():
    """Register Google Tasks opcodes to the default registry."""
    if not GTASKS_AVAILABLE:
        return

    from .opcodes import opcode, register_category

    register_category(
        id="gtasks",
        label="Google Tasks Operations",
        prefix="gtasks_",
        color="#1A73E8",
        icon="✅",
        requires="gtasks",
        order=215,
        description="Cria e sincroniza itens na Lista de Tarefas do Google.",
    )

    # ========================================================================
    # Authentication
    # ========================================================================

    @opcode(category="gtasks")
    async def gtasks_create_client(
        credentials_path: Optional[str] = None,
        subject: Optional[str] = None,
        scopes: Optional[List[str]] = None,
    ) -> TasksClient:
        """Create a Google Tasks client for API operations.

        Args:
            credentials_path: Path to a service account JSON file. If None,
                uses Application Default Credentials (ADC).
            subject: User email to impersonate via domain-wide delegation.
                Only valid together with a service account ``credentials_path``.
                Note: with a service account authorized in the Admin Console,
                ``subject`` can impersonate ANY user in the domain — restrict
                which subjects a workflow may pass here at the caller/app layer.
            scopes: OAuth scopes to request (default: ``.../auth/tasks``).

        Returns:
            TasksClient object to use with the other gtasks_* opcodes.

        Example with Service Account impersonating a user:
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

        service = await asyncio.to_thread(build, "tasks", "v1", credentials=credentials)
        return TasksClient(service)

    @opcode(category="gtasks")
    async def gtasks_close_client(client: TasksClient) -> bool:
        """Close the Tasks client and release the underlying HTTP connection.

        Args:
            client: TasksClient from gtasks_create_client.

        Returns:
            True when closed successfully.

        Example:
            client: { node: create_client }
        """
        await asyncio.to_thread(client.service.close)
        return True

    # ========================================================================
    # Task-list Operations
    # ========================================================================

    @opcode(category="gtasks")
    async def gtasks_list_tasklists(client: TasksClient) -> List[Dict[str, Any]]:
        """List the authenticated user's task lists.

        Args:
            client: TasksClient from gtasks_create_client.

        Returns:
            List of dicts with id, title for each task list. Follows
            pagination internally, so this returns every task list, not just
            the first page.

        Example:
            client: { node: create_client }
        """
        raw_items = await _list_all_pages(
            lambda page_token: client.tasklists.list(pageToken=page_token),
            "items",
        )
        return [
            {"id": item["id"], "title": item.get("title", "")} for item in raw_items
        ]

    # ========================================================================
    # Read Operations
    # ========================================================================

    @opcode(category="gtasks")
    async def gtasks_list_tasks(
        client: TasksClient,
        tasklist: str = "@default",
        show_completed: bool = True,
    ) -> List[Dict[str, Any]]:
        """List tasks in a task list.

        Args:
            client: TasksClient from gtasks_create_client.
            tasklist: Task list id (default: "@default", the user's main list).
            show_completed: Include completed tasks. Also controls
                ``showHidden``, since Google Tasks hides completed tasks by
                default independent of ``showCompleted`` (default: True).

        Returns:
            List of Task resources (id, title, status, due, notes, ...).
            Follows pagination internally, so this returns every matching
            task, not just the first page.

        Example:
            client: { node: create_client }
            tasklist: "@default"
        """
        return await _list_all_pages(
            lambda page_token: client.tasks.list(
                tasklist=tasklist,
                showCompleted=show_completed,
                showHidden=show_completed,
                pageToken=page_token,
            ),
            "items",
        )

    @opcode(category="gtasks")
    async def gtasks_get_task(
        client: TasksClient,
        task_id: str,
        tasklist: str = "@default",
    ) -> Dict[str, Any]:
        """Read a single task.

        Args:
            client: TasksClient from gtasks_create_client.
            task_id: The task id.
            tasklist: Task list id (default: "@default").

        Returns:
            The Task resource (id, title, status, due, notes, ...).

        Example:
            client: { node: create_client }
            task_id: "MTIzNDU2Nzg5"
        """
        request = client.tasks.get(tasklist=tasklist, task=task_id)
        return await _execute(request)

    # ========================================================================
    # Write Operations
    # ========================================================================

    @opcode(category="gtasks")
    async def gtasks_create_task(
        client: TasksClient,
        title: str,
        notes: str = "",
        due: str = "",
        tasklist: str = "@default",
    ) -> Dict[str, Any]:
        """Create a task in a task list.

        Args:
            client: TasksClient from gtasks_create_client.
            title: Task title. Must not be empty.
            notes: Optional free-text notes.
            due: Optional due date. Accepts "YYYY-MM-DD" (normalized to RFC 3339)
                or a full RFC 3339 timestamp. Google Tasks stores only the date.
            tasklist: Task list id (default: "@default").

        Returns:
            The created Task resource (including its "id").

        Raises:
            ValueError: If ``title`` is empty or ``due`` is not a valid date.

        Example:
            client: { node: create_client }
            title: "Enviar proposta ao cliente"
            due: "2026-08-12"
        """
        if not title:
            raise ValueError("title must not be empty")
        body: Dict[str, Any] = {"title": title, "status": "needsAction"}
        if notes:
            body["notes"] = notes
        due_rfc = _normalize_due(due)
        if due_rfc:
            body["due"] = due_rfc
        request = client.tasks.insert(tasklist=tasklist, body=body)
        return await _execute(request)

    @opcode(category="gtasks")
    async def gtasks_update_task(
        client: TasksClient,
        task_id: str,
        title: Optional[str] = None,
        notes: Optional[str] = None,
        due: Optional[str] = None,
        status: Optional[str] = None,
        tasklist: str = "@default",
    ) -> Dict[str, Any]:
        """Update fields of an existing task (partial update).

        ``None`` (the default) leaves a field unchanged. Pass ``""``
        explicitly for ``title``/``notes``/``due`` to clear that field.

        Args:
            client: TasksClient from gtasks_create_client.
            task_id: The task id.
            title: New title, or "" to clear. None (default) leaves unchanged.
            notes: New notes, or "" to clear. None (default) leaves unchanged.
            due: New due date ("YYYY-MM-DD" or RFC 3339), or "" to clear.
                None (default) leaves unchanged.
            status: New status, "needsAction" or "completed". None (default)
                leaves unchanged.
            tasklist: Task list id (default: "@default").

        Returns:
            The updated Task resource.

        Raises:
            ValueError: If every field is None (nothing to update), or if
                ``status`` is provided but isn't "needsAction"/"completed".

        Example:
            client: { node: create_client }
            task_id: "MTIzNDU2Nzg5"
            status: "completed"
        """
        body: Dict[str, Any] = {}
        if title is not None:
            body["title"] = title
        if notes is not None:
            body["notes"] = notes
        if due is not None:
            body["due"] = _normalize_due(due) if due else None
        if status is not None:
            if status not in _VALID_STATUSES:
                raise ValueError(
                    f"status must be one of {_VALID_STATUSES}, got: {status!r}"
                )
            body["status"] = status
        if not body:
            raise ValueError(
                "at least one of title, notes, due, status must be provided"
            )
        request = client.tasks.patch(tasklist=tasklist, task=task_id, body=body)
        return await _execute(request)

    @opcode(category="gtasks")
    async def gtasks_complete_task(
        client: TasksClient,
        task_id: str,
        completed: bool = True,
        tasklist: str = "@default",
    ) -> Dict[str, Any]:
        """Mark a task completed (or reopen it).

        Sets only the status field via a patch request — equivalent to
        calling gtasks_update_task with just its status argument set.

        Args:
            client: TasksClient from gtasks_create_client.
            task_id: The task id.
            completed: True to complete, False to reopen (default: True).
            tasklist: Task list id (default: "@default").

        Returns:
            The updated Task resource.

        Example:
            client: { node: create_client }
            task_id: "MTIzNDU2Nzg5"
            completed: true
        """
        status = "completed" if completed else "needsAction"
        request = client.tasks.patch(
            tasklist=tasklist, task=task_id, body={"status": status}
        )
        return await _execute(request)

    @opcode(category="gtasks")
    async def gtasks_delete_task(
        client: TasksClient,
        task_id: str,
        tasklist: str = "@default",
    ) -> Dict[str, Any]:
        """Delete a task from a task list.

        Args:
            client: TasksClient from gtasks_create_client.
            task_id: The task id.
            tasklist: Task list id (default: "@default").

        Returns:
            Dict with deleted status and the task id.

        Example:
            client: { node: create_client }
            task_id: "MTIzNDU2Nzg5"
        """
        request = client.tasks.delete(tasklist=tasklist, task=task_id)
        await _execute(request)
        return {"deleted": True, "id": task_id}
