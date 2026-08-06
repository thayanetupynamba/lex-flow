"""Tests for Gmail opcodes."""

import base64
import importlib.util
from email import message_from_bytes
from email.header import decode_header, make_header
from unittest.mock import Mock, patch

import pytest

from lexflow import default_registry
from lexflow.opcodes.opcodes_gmail import _build_raw

GMAIL_AVAILABLE = importlib.util.find_spec("googleapiclient") is not None


async def fake_to_thread(func, *args, **kwargs):
    """Convert asyncio.to_thread to sync call for tests."""
    return func(*args, **kwargs)


def create_mock_client():
    """Build a Mock mimicking GmailClient interface."""
    client = Mock()
    client.users = Mock()
    return client


def decode_raw(raw: str):
    """Decode a base64url raw message back into an email.message.Message."""
    return message_from_bytes(base64.urlsafe_b64decode(raw))


class TestBuildRaw:
    def test_plain_text_headers_and_body(self):
        raw = _build_raw(
            "champion@cliente.com",
            "Próximos passos",
            "Olá, obrigado pela reunião.",
            cc="cc@cliente.com",
        )
        mime = decode_raw(raw)
        assert mime["To"] == "champion@cliente.com"
        assert mime["Cc"] == "cc@cliente.com"
        # Non-ASCII subject is RFC 2047 encoded on serialization.
        assert str(make_header(decode_header(mime["Subject"]))) == "Próximos passos"
        assert "obrigado pela reunião" in mime.get_payload(decode=True).decode("utf-8")

    def test_html_is_multipart_alternative(self):
        raw = _build_raw("a@b.com", "Sub", "plain", html="<p>rich</p>")
        mime = decode_raw(raw)
        assert mime.is_multipart()
        subtypes = [p.get_content_subtype() for p in mime.get_payload()]
        assert "plain" in subtypes and "html" in subtypes


@pytest.mark.asyncio
@pytest.mark.skipif(
    not GMAIL_AVAILABLE, reason="google-api-python-client not installed"
)
class TestGmailCreateClient:
    async def test_create_client_with_adc(self):
        mock_creds = Mock()
        with (
            patch("asyncio.to_thread", side_effect=fake_to_thread),
            patch(
                "lexflow.opcodes.opcodes_gmail.google_auth_default",
                return_value=(mock_creds, "project-id"),
            ),
            patch(
                "lexflow.opcodes.opcodes_gmail.build",
                return_value=Mock(),
            ) as mock_build,
        ):
            result = await default_registry.call("gmail_create_client", [])
            mock_build.assert_called_once_with("gmail", "v1", credentials=mock_creds)
            assert hasattr(result, "service")
            assert hasattr(result, "users")

    async def test_create_client_rejects_non_json_path(self):
        with pytest.raises(ValueError, match="credentials_path must be a .json file"):
            await default_registry.call("gmail_create_client", ["/etc/passwd"])

    async def test_subject_without_sa_raises(self):
        with pytest.raises(ValueError, match="subject impersonation requires"):
            await default_registry.call("gmail_create_client", [None, "user@x.com"])

    async def test_subject_impersonation_with_sa(self):
        base_creds = Mock()
        impersonated = Mock()
        base_creds.with_subject.return_value = impersonated
        with (
            patch("asyncio.to_thread", side_effect=fake_to_thread),
            patch("os.path.isfile", return_value=True),
            patch(
                "lexflow.opcodes.opcodes_gmail.Credentials.from_service_account_file",
                return_value=base_creds,
            ),
            patch(
                "lexflow.opcodes.opcodes_gmail.build",
                return_value=Mock(),
            ) as mock_build,
        ):
            await default_registry.call(
                "gmail_create_client", ["/path/sa.json", "vendedora@x.com"]
            )
            base_creds.with_subject.assert_called_once_with("vendedora@x.com")
            mock_build.assert_called_once_with("gmail", "v1", credentials=impersonated)


@pytest.mark.asyncio
@pytest.mark.skipif(
    not GMAIL_AVAILABLE, reason="google-api-python-client not installed"
)
class TestGmailCompose:
    async def test_create_draft_posts_raw(self):
        client = create_mock_client()
        client.users.drafts().create.return_value.execute.return_value = {
            "id": "draft-1",
            "message": {"id": "m1", "threadId": "t1"},
        }
        with patch("asyncio.to_thread", side_effect=fake_to_thread):
            result = await default_registry.call(
                "gmail_create_draft",
                [client, "champion@cliente.com", "Próximos passos", "Olá."],
            )
        assert result["id"] == "draft-1"
        _, kwargs = client.users.drafts().create.call_args
        assert kwargs["userId"] == "me"
        raw = kwargs["body"]["message"]["raw"]
        mime = decode_raw(raw)
        assert mime["To"] == "champion@cliente.com"

    async def test_send_message_posts_raw(self):
        client = create_mock_client()
        client.users.messages().send.return_value.execute.return_value = {
            "id": "m1",
            "threadId": "t1",
        }
        with patch("asyncio.to_thread", side_effect=fake_to_thread):
            result = await default_registry.call(
                "gmail_send_message",
                [client, "a@b.com", "Sub", "Body"],
            )
        assert result["id"] == "m1"
        _, kwargs = client.users.messages().send.call_args
        assert kwargs["userId"] == "me"
        assert "raw" in kwargs["body"]


@pytest.mark.asyncio
@pytest.mark.skipif(GMAIL_AVAILABLE, reason="google-api-python-client IS installed")
async def test_gmail_opcodes_not_registered_when_not_installed():
    """Graceful degradation when google-api-python-client is not installed."""
    opcodes = default_registry.list_opcodes()
    gmail_opcodes = [op for op in opcodes if op.startswith("gmail_")]
    assert gmail_opcodes == []
