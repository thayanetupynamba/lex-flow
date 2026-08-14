"""Tests for Google Slides opcodes."""

import base64
import importlib.util
import pytest
from unittest.mock import Mock, patch

from lexflow import default_registry
from lexflow.opcodes.opcodes_gslides import (
    _replace_image_request,
    _replace_text_requests,
)

GSLIDES_AVAILABLE = importlib.util.find_spec("googleapiclient") is not None

if GSLIDES_AVAILABLE:
    from googleapiclient.errors import HttpError


async def fake_to_thread(func, *args, **kwargs):
    """Convert asyncio.to_thread to sync call for tests."""
    return func(*args, **kwargs)


def create_mock_client():
    """Build a Mock mimicking SlidesClient interface."""
    client = Mock()
    client.presentations = Mock()
    client.files = Mock()
    client.permissions = Mock()
    return client


def make_http_error(status: int, message: str):
    """Build a real googleapiclient HttpError with a JSON error body."""
    resp = Mock(status=status, reason="")
    content = ('{"error": {"message": "%s"}}' % message).encode("utf-8")
    return HttpError(resp, content)


class TestReplaceTextRequests:
    def test_one_request_per_entry_in_order(self):
        requests = _replace_text_requests(
            {"{{QTD}}": "80", "{{VALOR}}": "R$19.840"}, match_case=True
        )
        assert len(requests) == 2
        first = requests[0]["replaceAllText"]
        assert first["containsText"] == {"text": "{{QTD}}", "matchCase": True}
        assert first["replaceText"] == "80"

    def test_values_coerced_to_str(self):
        requests = _replace_text_requests({"{{QTD}}": 80}, match_case=False)
        assert requests[0]["replaceAllText"]["replaceText"] == "80"
        assert requests[0]["replaceAllText"]["containsText"]["matchCase"] is False

    def test_empty_mapping_raises(self):
        with pytest.raises(ValueError, match="at least one entry"):
            _replace_text_requests({}, match_case=True)


class TestReplaceImageRequest:
    def test_by_object_id_uses_replace_image(self):
        request = _replace_image_request("https://img/logo.png", "img-1", "")
        assert request["replaceImage"]["imageObjectId"] == "img-1"
        assert request["replaceImage"]["url"] == "https://img/logo.png"

    def test_by_placeholder_uses_replace_all_shapes(self):
        request = _replace_image_request(
            "https://img/logo.png", "", "{{LOGO_CLIENTE}}"
        )
        shapes = request["replaceAllShapesWithImage"]
        assert shapes["containsText"]["text"] == "{{LOGO_CLIENTE}}"
        assert shapes["imageUrl"] == "https://img/logo.png"

    def test_neither_mode_raises(self):
        with pytest.raises(ValueError, match="exactly one"):
            _replace_image_request("https://img/logo.png", "", "")

    def test_both_modes_raises(self):
        with pytest.raises(ValueError, match="exactly one"):
            _replace_image_request("https://img/logo.png", "img-1", "{{X}}")


@pytest.mark.skipif(
    not GSLIDES_AVAILABLE, reason="google-api-python-client not installed"
)
class TestGSlidesCreateClient:
    pytestmark = pytest.mark.asyncio

    async def test_create_client_with_adc_builds_slides_and_drive(self):
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
            result = await default_registry.call("gslides_create_client", [])
            built = [call.args[:2] for call in mock_build.call_args_list]
            assert ("slides", "v1") in built
            assert ("drive", "v3") in built
            assert hasattr(result, "presentations")
            assert hasattr(result, "files")

    async def test_create_client_rejects_non_json_path(self):
        with pytest.raises(ValueError, match="credentials_path must be a .json file"):
            await default_registry.call("gslides_create_client", ["/etc/passwd"])

    async def test_subject_without_sa_raises(self):
        with pytest.raises(ValueError, match="subject impersonation requires"):
            await default_registry.call("gslides_create_client", [None, "user@x.com"])


@pytest.mark.skipif(
    not GSLIDES_AVAILABLE, reason="google-api-python-client not installed"
)
class TestGSlidesCopyPresentation:
    pytestmark = pytest.mark.asyncio

    async def test_copy_sends_title(self):
        client = create_mock_client()
        client.files.copy.return_value.execute.return_value = {"id": "new-pres"}
        with patch("asyncio.to_thread", side_effect=fake_to_thread):
            result = await default_registry.call(
                "gslides_copy_presentation",
                [client, "tpl-123", "Proposta Inspira - Acme_Ago_2026"],
            )
        assert result["id"] == "new-pres"
        client.files.copy.assert_called_once_with(
            fileId="tpl-123",
            body={"name": "Proposta Inspira - Acme_Ago_2026"},
            supportsAllDrives=True,
        )

    async def test_copy_into_shared_folder_sets_parents(self):
        client = create_mock_client()
        client.files.copy.return_value.execute.return_value = {"id": "new-pres"}
        with patch("asyncio.to_thread", side_effect=fake_to_thread):
            await default_registry.call(
                "gslides_copy_presentation",
                [client, "tpl-123", "Proposta", "folder-abc"],
            )
        body = client.files.copy.call_args.kwargs["body"]
        assert body["parents"] == ["folder-abc"]

    async def test_api_error_is_sanitized(self):
        client = create_mock_client()
        client.files.copy.return_value.execute.side_effect = make_http_error(
            403, "insufficient scopes"
        )
        with patch("asyncio.to_thread", side_effect=fake_to_thread):
            with pytest.raises(RuntimeError, match="Google Slides API error \\(403\\)"):
                await default_registry.call(
                    "gslides_copy_presentation", [client, "tpl-123", "Proposta"]
                )


@pytest.mark.skipif(
    not GSLIDES_AVAILABLE, reason="google-api-python-client not installed"
)
class TestGSlidesGetPresentation:
    pytestmark = pytest.mark.asyncio

    async def test_get_passes_fields_only_when_set(self):
        client = create_mock_client()
        client.presentations.get.return_value.execute.return_value = {
            "presentationId": "p1"
        }
        with patch("asyncio.to_thread", side_effect=fake_to_thread):
            await default_registry.call("gslides_get_presentation", [client, "p1"])
            client.presentations.get.assert_called_with(presentationId="p1")
            await default_registry.call(
                "gslides_get_presentation", [client, "p1", "slides(objectId)"]
            )
            client.presentations.get.assert_called_with(
                presentationId="p1", fields="slides(objectId)"
            )


@pytest.mark.skipif(
    not GSLIDES_AVAILABLE, reason="google-api-python-client not installed"
)
class TestGSlidesReplaceText:
    pytestmark = pytest.mark.asyncio

    async def test_atomic_batch_and_occurrence_counts(self):
        client = create_mock_client()
        client.presentations.batchUpdate.return_value.execute.return_value = {
            "replies": [
                {"replaceAllText": {"occurrencesChanged": 2}},
                {"replaceAllText": {}},
            ]
        }
        with patch("asyncio.to_thread", side_effect=fake_to_thread):
            result = await default_registry.call(
                "gslides_replace_text",
                [client, "p1", {"{{QTD}}": "80", "{{VALOR}}": "R$19.840"}],
            )
        body = client.presentations.batchUpdate.call_args.kwargs["body"]
        assert len(body["requests"]) == 2
        assert result["occurrences"] == {"{{QTD}}": 2, "{{VALOR}}": 0}

    async def test_empty_mapping_raises(self):
        client = create_mock_client()
        with pytest.raises(ValueError, match="at least one entry"):
            await default_registry.call("gslides_replace_text", [client, "p1", {}])


@pytest.mark.skipif(
    not GSLIDES_AVAILABLE, reason="google-api-python-client not installed"
)
class TestGSlidesReplaceImage:
    pytestmark = pytest.mark.asyncio

    async def test_placeholder_mode_sends_single_request(self):
        client = create_mock_client()
        client.presentations.batchUpdate.return_value.execute.return_value = {
            "replies": [{}]
        }
        with patch("asyncio.to_thread", side_effect=fake_to_thread):
            await default_registry.call(
                "gslides_replace_image",
                [client, "p1", "https://img/logo.png", "", "{{LOGO_CLIENTE}}"],
            )
        body = client.presentations.batchUpdate.call_args.kwargs["body"]
        assert len(body["requests"]) == 1
        assert "replaceAllShapesWithImage" in body["requests"][0]


@pytest.mark.skipif(
    not GSLIDES_AVAILABLE, reason="google-api-python-client not installed"
)
class TestGSlidesShareLink:
    pytestmark = pytest.mark.asyncio

    async def test_creates_anyone_permission_and_returns_link(self):
        client = create_mock_client()
        client.permissions.create.return_value.execute.return_value = {"id": "perm1"}
        with patch("asyncio.to_thread", side_effect=fake_to_thread):
            result = await default_registry.call(
                "gslides_share_link", [client, "p1", "reader"]
            )
        client.permissions.create.assert_called_once_with(
            fileId="p1",
            body={"type": "anyone", "role": "reader"},
            supportsAllDrives=True,
        )
        assert result["web_view_link"].endswith("/presentation/d/p1/edit")

    async def test_invalid_role_raises(self):
        client = create_mock_client()
        with pytest.raises(ValueError, match="role must be one of"):
            await default_registry.call("gslides_share_link", [client, "p1", "owner"])


@pytest.mark.skipif(
    not GSLIDES_AVAILABLE, reason="google-api-python-client not installed"
)
class TestGSlidesExportPdf:
    pytestmark = pytest.mark.asyncio

    async def test_export_returns_base64_pdf(self):
        client = create_mock_client()
        client.files.export.return_value.execute.return_value = b"%PDF-fake"
        with patch("asyncio.to_thread", side_effect=fake_to_thread):
            result = await default_registry.call("gslides_export_pdf", [client, "p1"])
        client.files.export.assert_called_once_with(
            fileId="p1", mimeType="application/pdf"
        )
        assert result["size"] == len(b"%PDF-fake")
        assert base64.b64decode(result["base64"]) == b"%PDF-fake"


@pytest.mark.skipif(
    not GSLIDES_AVAILABLE, reason="google-api-python-client not installed"
)
class TestGSlidesCloseClient:
    pytestmark = pytest.mark.asyncio

    async def test_close_client_closes_both_services(self):
        client = Mock()
        with patch("asyncio.to_thread", side_effect=fake_to_thread):
            result = await default_registry.call("gslides_close_client", [client])
        assert result is True
        client.slides_service.close.assert_called_once()
        client.drive_service.close.assert_called_once()
