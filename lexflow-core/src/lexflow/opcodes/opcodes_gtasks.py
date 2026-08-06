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
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from google.auth.credentials import Credentials

try:
    from google.oauth2.service_account import Credentials
    from google.auth import default as google_auth_default
    from googleapiclient.discovery import build

    GTASKS_AVAILABLE = True
except ImportError:
    GTASKS_AVAILABLE = False

SCOPES = ["https://www.googleapis.com/auth/tasks"]


def _normalize_due(due: str) -> Optional[str]:
    """Normalize a due date for the Tasks API.

    Args:
        due: Empty string (no due date), a ``YYYY-MM-DD`` date, or a full
            RFC 3339 timestamp.

    Returns:
        ``None`` if ``due`` is empty, an RFC 3339 timestamp for a bare date,
        otherwise the value unchanged. Google Tasks only stores the date part
        of ``due`` and always renders it at midnight UTC.
    """
    if not due:
        return None
    if len(due) == 10 and due[4] == "-" and due[7] == "-":
        return f"{due}T00:00:00.000Z"
    return due


def register_gtasks_opcodes():
    """Register Google Tasks opcodes to the default registry."""
    if not GTASKS_AVAILABLE:
        return

    from .opcodes import opcode, register_category

    class TasksClient:
        """Reusable Google Tasks client."""

        def __init__(self, service):
            self.service = service
            self.tasks = service.tasks()
            self.tasklists = service.tasklists()

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
    ) -> TasksClient:
        """Create a Google Tasks client for API operations.

        Args:
            credentials_path: Path to a service account JSON file. If None,
                uses Application Default Credentials (ADC).
            subject: User email to impersonate via domain-wide delegation.
                Only valid together with a service account ``credentials_path``.

        Returns:
            TasksClient object to use with the other gtasks_* opcodes.

        Example with Service Account impersonating a user:
            credentials_path: "/path/to/service-account.json"
            subject: "vendedora@empresa.com"

        Example with ADC (after 'gcloud auth application-default login'):
            # No arguments needed
        """
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
                Credentials.from_service_account_file, resolved, scopes=SCOPES
            )
            if subject:
                credentials = credentials.with_subject(subject)
        else:
            if subject:
                raise ValueError(
                    "subject impersonation requires a service account credentials_path"
                )
            credentials, _ = await asyncio.to_thread(google_auth_default, scopes=SCOPES)

        service = await asyncio.to_thread(build, "tasks", "v1", credentials=credentials)
        return TasksClient(service)

    # ========================================================================
    # Task-list Operations
    # ========================================================================

    @opcode(category="gtasks")
    async def gtasks_list_tasklists(client: TasksClient) -> List[Dict[str, Any]]:
        """List the authenticated user's task lists.

        Args:
            client: TasksClient from gtasks_create_client.

        Returns:
            List of dicts with id, title for each task list.

        Example:
            client: { node: create_client }
        """
        request = client.tasklists.list()
        result = await asyncio.to_thread(request.execute)
        return [
            {"id": item["id"], "title": item.get("title", "")}
            for item in result.get("items", [])
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
            show_completed: Include completed tasks (default: True).

        Returns:
            List of Task resources (id, title, status, due, notes, ...).

        Example:
            client: { node: create_client }
            tasklist: "@default"
        """
        request = client.tasks.list(
            tasklist=tasklist,
            showCompleted=show_completed,
            showHidden=show_completed,
        )
        result = await asyncio.to_thread(request.execute)
        return result.get("items", [])

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
        return await asyncio.to_thread(request.execute)

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
            title: Task title.
            notes: Optional free-text notes.
            due: Optional due date. Accepts "YYYY-MM-DD" (normalized to RFC 3339)
                or a full RFC 3339 timestamp. Google Tasks stores only the date.
            tasklist: Task list id (default: "@default").

        Returns:
            The created Task resource (including its "id").

        Example:
            client: { node: create_client }
            title: "Enviar proposta ao cliente"
            due: "2026-08-12"
        """
        body: Dict[str, Any] = {"title": title, "status": "needsAction"}
        if notes:
            body["notes"] = notes
        due_rfc = _normalize_due(due)
        if due_rfc:
            body["due"] = due_rfc
        request = client.tasks.insert(tasklist=tasklist, body=body)
        return await asyncio.to_thread(request.execute)

    @opcode(category="gtasks")
    async def gtasks_update_task(
        client: TasksClient,
        task_id: str,
        title: str = "",
        notes: str = "",
        due: str = "",
        status: str = "",
        tasklist: str = "@default",
    ) -> Dict[str, Any]:
        """Update fields of an existing task (partial update).

        Only non-empty fields are sent, so passing "" leaves a field unchanged.

        Args:
            client: TasksClient from gtasks_create_client.
            task_id: The task id.
            title: New title (unchanged if "").
            notes: New notes (unchanged if "").
            due: New due date, "YYYY-MM-DD" or RFC 3339 (unchanged if "").
            status: New status: "needsAction" or "completed" (unchanged if "").
            tasklist: Task list id (default: "@default").

        Returns:
            The updated Task resource.

        Example:
            client: { node: create_client }
            task_id: "MTIzNDU2Nzg5"
            status: "completed"
        """
        body: Dict[str, Any] = {}
        if title:
            body["title"] = title
        if notes:
            body["notes"] = notes
        due_rfc = _normalize_due(due)
        if due_rfc:
            body["due"] = due_rfc
        if status:
            body["status"] = status
        request = client.tasks.patch(tasklist=tasklist, task=task_id, body=body)
        return await asyncio.to_thread(request.execute)

    @opcode(category="gtasks")
    async def gtasks_complete_task(
        client: TasksClient,
        task_id: str,
        completed: bool = True,
        tasklist: str = "@default",
    ) -> Dict[str, Any]:
        """Mark a task completed (or reopen it).

        Convenience wrapper over gtasks_update_task for the checkbox use case.

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
        return await asyncio.to_thread(request.execute)

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
        await asyncio.to_thread(request.execute)
        return {"deleted": True, "id": task_id}
