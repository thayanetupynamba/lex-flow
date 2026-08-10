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

if GMAIL_AVAILABLE:
    from googleapiclient.errors import HttpError


async def fake_to_thread(func, *args, **kwargs):
    """Convert asyncio.to_thread to sync call for tests."""
    return func(*args, **kwargs)


def create_mock_client():
    """Build a Mock mimicking GmailClient interface."""
    client = Mock()
    client.users = Mock()
    return client


def make_http_error(status: int, message: str):
    """Build a real googleapiclient HttpError with a JSON error body."""
    resp = Mock(status=status, reason="")
    content = ('{"error": {"message": "%s"}}' % message).encode("utf-8")
    return HttpError(resp, content)


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

    def test_rejects_empty_to(self):
        with pytest.raises(ValueError, match="to must be a non-empty string"):
            _build_raw("", "Sub", "Body")

    def test_rejects_header_injection_in_to(self):
        with pytest.raises(ValueError, match="to must not contain line breaks"):
            _build_raw("a@b.com\nBcc: evil@x.com", "Sub", "Body")

    def test_rejects_header_injection_variant_without_colon(self):
        with pytest.raises(ValueError, match="to must not contain line breaks"):
            _build_raw("a@b.com\nBcc : evil@x.com", "Sub", "Body")

    def test_rejects_header_injection_in_subject(self):
        with pytest.raises(ValueError, match="subject must not contain line breaks"):
            _build_raw("a@b.com", "Sub\r\nX-Evil: 1", "Body")

    def test_rejects_header_injection_in_cc(self):
        with pytest.raises(ValueError, match="cc must not contain line breaks"):
            _build_raw("a@b.com", "Sub", "Body", cc="cc@x.com\nBcc: evil@x.com")

    def test_rejects_header_injection_in_bcc(self):
        with pytest.raises(ValueError, match="bcc must not contain line breaks"):
            _build_raw("a@b.com", "Sub", "Body", bcc="cc@x.com\nBcc: evil@x.com")

    def test_body_may_contain_newlines(self):
        # Only headers are validated; a multi-line body is legitimate.
        raw = _build_raw("a@b.com", "Sub", "line1\nline2")
        mime = decode_raw(raw)
        assert "line1" in mime.get_payload(decode=True).decode("utf-8")


@pytest.mark.skipif(
    not GMAIL_AVAILABLE, reason="google-api-python-client not installed"
)
class TestGmailCreateClient:
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
            result = await default_registry.call("gmail_create_client", [])
            mock_build.assert_called_once_with("gmail", "v1", credentials=mock_creds)
            assert hasattr(result, "service")
            assert hasattr(result, "users")

    async def test_create_client_rejects_non_json_path(self):
        with pytest.raises(ValueError, match="credentials_path must be a .json file"):
            await default_registry.call("gmail_create_client", ["/etc/passwd"])

    async def test_create_client_rejects_path_traversal(self):
        with pytest.raises(ValueError, match="must not contain '..'"):
            await default_registry.call(
                "gmail_create_client", ["../../etc/secrets/creds.json"]
            )

    async def test_create_client_rejects_nonexistent_file(self):
        with pytest.raises(ValueError, match="credentials file not found"):
            await default_registry.call(
                "gmail_create_client", ["/nonexistent/creds.json"]
            )

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
                "lexflow.opcodes._google_auth.Credentials.from_service_account_file",
                return_value=base_creds,
            ),
            patch(
                "lexflow.opcodes._google_auth.build",
                return_value=Mock(),
            ) as mock_build,
        ):
            await default_registry.call(
                "gmail_create_client", ["/path/sa.json", "vendedora@x.com"]
            )
            base_creds.with_subject.assert_called_once_with("vendedora@x.com")
            mock_build.assert_called_once_with("gmail", "v1", credentials=impersonated)

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
                "gmail_create_client",
                [None, None, ["https://www.googleapis.com/auth/gmail.readonly"]],
            )
            mock_default.assert_called_once_with(
                scopes=["https://www.googleapis.com/auth/gmail.readonly"]
            )

    async def test_close_client(self):
        client = create_mock_client()
        with patch("asyncio.to_thread", side_effect=fake_to_thread):
            result = await default_registry.call("gmail_close_client", [client])
        assert result is True
        client.service.close.assert_called_once()


@pytest.mark.skipif(
    not GMAIL_AVAILABLE, reason="google-api-python-client not installed"
)
class TestGmailCompose:
    pytestmark = pytest.mark.asyncio

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

    async def test_create_draft_rejects_header_injection(self):
        client = create_mock_client()
        with pytest.raises(ValueError, match="must not contain line breaks"):
            await default_registry.call(
                "gmail_create_draft",
                [client, "a@b.com\nBcc: evil@x.com", "Sub", "Body"],
            )

    async def test_create_draft_wraps_api_error(self):
        client = create_mock_client()
        client.users.drafts().create.return_value.execute.side_effect = make_http_error(
            403, "Insufficient Permission"
        )
        with patch("asyncio.to_thread", side_effect=fake_to_thread):
            with pytest.raises(RuntimeError, match="Insufficient Permission"):
                await default_registry.call(
                    "gmail_create_draft",
                    [client, "champion@cliente.com", "Sub", "Body"],
                )

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

    async def test_send_message_wraps_api_error(self):
        client = create_mock_client()
        client.users.messages().send.return_value.execute.side_effect = make_http_error(
            500, "Backend Error"
        )
        with patch("asyncio.to_thread", side_effect=fake_to_thread):
            with pytest.raises(RuntimeError, match="Backend Error"):
                await default_registry.call(
                    "gmail_send_message",
                    [client, "a@b.com", "Sub", "Body"],
                )


@pytest.mark.skipif(GMAIL_AVAILABLE, reason="google-api-python-client IS installed")
async def test_gmail_opcodes_not_registered_when_not_installed():
    """Graceful degradation when google-api-python-client is not installed."""
    opcodes = default_registry.list_opcodes()
    gmail_opcodes = [op for op in opcodes if op.startswith("gmail_")]
    assert gmail_opcodes == []
