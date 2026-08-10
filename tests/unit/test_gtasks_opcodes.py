"""Tests for Google Tasks opcodes."""

import importlib.util
import pytest
from unittest.mock import Mock, patch

from lexflow import default_registry
from lexflow.opcodes.opcodes_gtasks import _normalize_due

GTASKS_AVAILABLE = importlib.util.find_spec("googleapiclient") is not None

if GTASKS_AVAILABLE:
    from googleapiclient.errors import HttpError


async def fake_to_thread(func, *args, **kwargs):
    """Convert asyncio.to_thread to sync call for tests."""
    return func(*args, **kwargs)


def create_mock_client():
    """Build a Mock mimicking TasksClient interface."""
    client = Mock()
    client.tasks = Mock()
    client.tasklists = Mock()
    return client


def make_http_error(status: int, message: str):
    """Build a real googleapiclient HttpError with a JSON error body."""
    resp = Mock(status=status, reason="")
    content = ('{"error": {"message": "%s"}}' % message).encode("utf-8")
    return HttpError(resp, content)


class TestNormalizeDue:
    def test_empty_is_none(self):
        assert _normalize_due("") is None

    def test_none_is_none(self):
        assert _normalize_due(None) is None

    def test_bare_date_becomes_rfc3339(self):
        assert _normalize_due("2026-08-12") == "2026-08-12T00:00:00.000Z"

    def test_full_timestamp_passthrough(self):
        assert _normalize_due("2026-08-12T09:30:00.000Z") == "2026-08-12T09:30:00.000Z"

    def test_invalid_bare_date_raises(self):
        with pytest.raises(ValueError, match="due must be"):
            _normalize_due("2026-13-45")

    def test_br_date_format_raises(self):
        with pytest.raises(ValueError, match="due must be"):
            _normalize_due("12/08/2026")

    def test_garbage_timestamp_raises(self):
        with pytest.raises(ValueError, match="due must be"):
            _normalize_due("not-a-date-at-all")

    def test_non_string_raises_type_error(self):
        with pytest.raises(TypeError, match="due must be a string"):
            _normalize_due(12345)


@pytest.mark.skipif(
    not GTASKS_AVAILABLE, reason="google-api-python-client not installed"
)
class TestGTasksCreateClient:
    pytestmark = pytest.mark.asyncio

    async def test_create_client_with_adc(self):
        mock_creds = Mock()
        with (
            patch("asyncio.to_thread", side_effect=fake_to_thread),
            patch(
                "lexflow.opcodes._google_auth.google_auth_default",
                return_value=(mock_creds, "project-id"),
            ),
            patch(
                "lexflow.opcodes._google_auth.build",
                return_value=Mock(),
            ) as mock_build,
        ):
            result = await default_registry.call("gtasks_create_client", [])
            mock_build.assert_called_once_with("tasks", "v1", credentials=mock_creds)
            assert hasattr(result, "service")
            assert hasattr(result, "tasks")

    async def test_create_client_rejects_non_json_path(self):
        with pytest.raises(ValueError, match="credentials_path must be a .json file"):
            await default_registry.call("gtasks_create_client", ["/etc/passwd"])

    async def test_create_client_rejects_path_traversal(self):
        with pytest.raises(ValueError, match="must not contain '..'"):
            await default_registry.call(
                "gtasks_create_client", ["../../etc/secrets/creds.json"]
            )

    async def test_create_client_rejects_nonexistent_file(self):
        with pytest.raises(ValueError, match="credentials file not found"):
            await default_registry.call(
                "gtasks_create_client", ["/nonexistent/creds.json"]
            )

    async def test_subject_without_sa_raises(self):
        with pytest.raises(ValueError, match="subject impersonation requires"):
            await default_registry.call("gtasks_create_client", [None, "user@x.com"])

    async def test_subject_impersonation_with_sa(self):
        base_creds = Mock()
        impersonated = Mock()
        base_creds.with_subject.return_value = impersonated
        with (
            patch("asyncio.to_thread", side_effect=fake_to_thread),
            patch("os.path.isfile", return_value=True),
            patch(
                "lexflow.opcodes._google_auth.Credentials.from_service_account_file",
                return_value=base_creds,
            ),
            patch(
                "lexflow.opcodes._google_auth.build",
                return_value=Mock(),
            ) as mock_build,
        ):
            await default_registry.call(
                "gtasks_create_client", ["/path/sa.json", "vendedora@x.com"]
            )
            base_creds.with_subject.assert_called_once_with("vendedora@x.com")
            mock_build.assert_called_once_with("tasks", "v1", credentials=impersonated)

    async def test_create_client_uses_custom_scopes(self):
        mock_creds = Mock()
        with (
            patch("asyncio.to_thread", side_effect=fake_to_thread),
            patch(
                "lexflow.opcodes._google_auth.google_auth_default",
                return_value=(mock_creds, "project-id"),
            ) as mock_default,
            patch("lexflow.opcodes._google_auth.build", return_value=Mock()),
        ):
            await default_registry.call(
                "gtasks_create_client",
                [None, None, ["https://www.googleapis.com/auth/tasks.readonly"]],
            )
            mock_default.assert_called_once_with(
                scopes=["https://www.googleapis.com/auth/tasks.readonly"]
            )

    async def test_close_client(self):
        client = create_mock_client()
        with patch("asyncio.to_thread", side_effect=fake_to_thread):
            result = await default_registry.call("gtasks_close_client", [client])
        assert result is True
        client.service.close.assert_called_once()


@pytest.mark.skipif(
    not GTASKS_AVAILABLE, reason="google-api-python-client not installed"
)
class TestGTasksReadOpcodes:
    pytestmark = pytest.mark.asyncio

    async def test_list_tasklists(self):
        client = create_mock_client()
        client.tasklists.list.return_value.execute.return_value = {
            "items": [
                {"id": "@default", "title": "Minhas tarefas"},
                {"id": "L2", "title": "Trabalho", "extra": "ignored"},
            ]
        }
        with patch("asyncio.to_thread", side_effect=fake_to_thread):
            result = await default_registry.call("gtasks_list_tasklists", [client])
        assert result == [
            {"id": "@default", "title": "Minhas tarefas"},
            {"id": "L2", "title": "Trabalho"},
        ]

    async def test_list_tasklists_follows_pagination(self):
        client = create_mock_client()
        client.tasklists.list.return_value.execute.side_effect = [
            {"items": [{"id": "1", "title": "A"}], "nextPageToken": "tok-2"},
            {"items": [{"id": "2", "title": "B"}]},
        ]
        with patch("asyncio.to_thread", side_effect=fake_to_thread):
            result = await default_registry.call("gtasks_list_tasklists", [client])
        assert result == [{"id": "1", "title": "A"}, {"id": "2", "title": "B"}]
        assert client.tasklists.list.return_value.execute.call_count == 2

    async def test_list_tasks_returns_items(self):
        client = create_mock_client()
        expected = [{"id": "t1", "status": "needsAction"}]
        client.tasks.list.return_value.execute.return_value = {"items": expected}
        with patch("asyncio.to_thread", side_effect=fake_to_thread):
            result = await default_registry.call("gtasks_list_tasks", [client])
        assert result == expected
        client.tasks.list.assert_called_with(
            tasklist="@default", showCompleted=True, showHidden=True, pageToken=None
        )

    async def test_list_tasks_empty(self):
        client = create_mock_client()
        client.tasks.list.return_value.execute.return_value = {}
        with patch("asyncio.to_thread", side_effect=fake_to_thread):
            result = await default_registry.call("gtasks_list_tasks", [client])
        assert result == []

    async def test_list_tasks_follows_pagination_past_first_page(self):
        client = create_mock_client()
        client.tasks.list.return_value.execute.side_effect = [
            {"items": [{"id": f"t{i}"} for i in range(20)], "nextPageToken": "tok-2"},
            {"items": [{"id": "t20"}]},
        ]
        with patch("asyncio.to_thread", side_effect=fake_to_thread):
            result = await default_registry.call("gtasks_list_tasks", [client])
        assert len(result) == 21
        assert result[-1] == {"id": "t20"}

    async def test_get_task(self):
        client = create_mock_client()
        client.tasks.get.return_value.execute.return_value = {
            "id": "t1",
            "title": "X",
        }
        with patch("asyncio.to_thread", side_effect=fake_to_thread):
            result = await default_registry.call("gtasks_get_task", [client, "t1"])
        assert result["id"] == "t1"
        client.tasks.get.assert_called_with(tasklist="@default", task="t1")

    async def test_get_task_wraps_api_error(self):
        client = create_mock_client()
        client.tasks.get.return_value.execute.side_effect = make_http_error(
            404, "Task not found"
        )
        with patch("asyncio.to_thread", side_effect=fake_to_thread):
            with pytest.raises(RuntimeError, match="Task not found"):
                await default_registry.call("gtasks_get_task", [client, "missing"])


@pytest.mark.skipif(
    not GTASKS_AVAILABLE, reason="google-api-python-client not installed"
)
class TestGTasksWriteOpcodes:
    pytestmark = pytest.mark.asyncio

    async def test_create_task_normalizes_due(self):
        client = create_mock_client()
        client.tasks.insert.return_value.execute.return_value = {"id": "new"}
        with patch("asyncio.to_thread", side_effect=fake_to_thread):
            result = await default_registry.call(
                "gtasks_create_task",
                [client, "Enviar proposta", "nota", "2026-08-12"],
            )
        assert result == {"id": "new"}
        client.tasks.insert.assert_called_with(
            tasklist="@default",
            body={
                "title": "Enviar proposta",
                "status": "needsAction",
                "notes": "nota",
                "due": "2026-08-12T00:00:00.000Z",
            },
        )

    async def test_create_task_minimal_no_due_no_notes(self):
        client = create_mock_client()
        client.tasks.insert.return_value.execute.return_value = {"id": "new"}
        with patch("asyncio.to_thread", side_effect=fake_to_thread):
            await default_registry.call("gtasks_create_task", [client, "Só título"])
        client.tasks.insert.assert_called_with(
            tasklist="@default",
            body={"title": "Só título", "status": "needsAction"},
        )

    async def test_create_task_rejects_empty_title(self):
        client = create_mock_client()
        with pytest.raises(ValueError, match="title must not be empty"):
            await default_registry.call("gtasks_create_task", [client, ""])

    async def test_create_task_rejects_invalid_due(self):
        client = create_mock_client()
        with pytest.raises(ValueError, match="due must be"):
            await default_registry.call(
                "gtasks_create_task", [client, "Título", "", "12/08/2026"]
            )

    async def test_update_task_only_sends_provided_fields(self):
        client = create_mock_client()
        client.tasks.patch.return_value.execute.return_value = {"id": "t1"}
        with patch("asyncio.to_thread", side_effect=fake_to_thread):
            await default_registry.call(
                "gtasks_update_task",
                [client, "t1", None, None, None, "completed"],
            )
        client.tasks.patch.assert_called_with(
            tasklist="@default", task="t1", body={"status": "completed"}
        )

    async def test_update_task_clears_field_with_empty_string(self):
        client = create_mock_client()
        client.tasks.patch.return_value.execute.return_value = {"id": "t1"}
        with patch("asyncio.to_thread", side_effect=fake_to_thread):
            await default_registry.call(
                "gtasks_update_task",
                [client, "t1", None, "", None, None],
            )
        client.tasks.patch.assert_called_with(
            tasklist="@default", task="t1", body={"notes": ""}
        )

    async def test_update_task_clears_due_with_empty_string(self):
        client = create_mock_client()
        client.tasks.patch.return_value.execute.return_value = {"id": "t1"}
        with patch("asyncio.to_thread", side_effect=fake_to_thread):
            await default_registry.call(
                "gtasks_update_task",
                [client, "t1", None, None, ""],
            )
        client.tasks.patch.assert_called_with(
            tasklist="@default", task="t1", body={"due": None}
        )

    async def test_update_task_no_fields_raises(self):
        client = create_mock_client()
        with pytest.raises(ValueError, match="at least one of"):
            await default_registry.call("gtasks_update_task", [client, "t1"])

    async def test_update_task_rejects_invalid_status(self):
        client = create_mock_client()
        with pytest.raises(ValueError, match="status must be one of"):
            await default_registry.call(
                "gtasks_update_task", [client, "t1", None, None, None, "done"]
            )

    async def test_complete_task(self):
        client = create_mock_client()
        client.tasks.patch.return_value.execute.return_value = {
            "id": "t1",
            "status": "completed",
        }
        with patch("asyncio.to_thread", side_effect=fake_to_thread):
            result = await default_registry.call("gtasks_complete_task", [client, "t1"])
        assert result["status"] == "completed"
        client.tasks.patch.assert_called_with(
            tasklist="@default", task="t1", body={"status": "completed"}
        )

    async def test_uncomplete_task(self):
        client = create_mock_client()
        client.tasks.patch.return_value.execute.return_value = {"id": "t1"}
        with patch("asyncio.to_thread", side_effect=fake_to_thread):
            await default_registry.call("gtasks_complete_task", [client, "t1", False])
        client.tasks.patch.assert_called_with(
            tasklist="@default", task="t1", body={"status": "needsAction"}
        )

    async def test_delete_task(self):
        client = create_mock_client()
        client.tasks.delete.return_value.execute.return_value = {}
        with patch("asyncio.to_thread", side_effect=fake_to_thread):
            result = await default_registry.call("gtasks_delete_task", [client, "t1"])
        assert result == {"deleted": True, "id": "t1"}
        client.tasks.delete.assert_called_with(tasklist="@default", task="t1")

    async def test_delete_task_wraps_api_error(self):
        client = create_mock_client()
        client.tasks.delete.return_value.execute.side_effect = make_http_error(
            403, "Insufficient permissions"
        )
        with patch("asyncio.to_thread", side_effect=fake_to_thread):
            with pytest.raises(RuntimeError, match="Insufficient permissions"):
                await default_registry.call("gtasks_delete_task", [client, "t1"])


@pytest.mark.skipif(GTASKS_AVAILABLE, reason="google-api-python-client IS installed")
async def test_gtasks_opcodes_not_registered_when_not_installed():
    """Graceful degradation when google-api-python-client is not installed."""
    opcodes = default_registry.list_opcodes()
    gtasks_opcodes = [op for op in opcodes if op.startswith("gtasks_")]
    assert gtasks_opcodes == []
