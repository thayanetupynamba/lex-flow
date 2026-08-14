"""Google Slides opcodes for LexFlow.

This module provides opcodes for generating documents from Google Slides
templates (Apresentações Google): copy a template deck, replace placeholder
texts and images, share the result, and export it as PDF — the
copy-and-fill flow behind proposal/report generation workflows.

Installation:
    pip install lexflow[gslides]
    or:
    pip install google-auth google-auth-oauthlib google-api-python-client

Authentication:
    Option 1 - Service Account (recommended for production):
        Pass the path to a service account JSON file to gslides_create_client().
        To act as a specific user (whose Drive holds the template), enable
        domain-wide delegation on the service account and pass that user's
        address as ``subject`` (the service account then impersonates that
        user; see the allowlist notes in ``_google_auth``).

    Option 2 - Application Default Credentials (recommended for development):
        Run: gcloud auth application-default login
        Then call gslides_create_client() without arguments.

Required scopes:
    https://www.googleapis.com/auth/presentations (read/batchUpdate)
    https://www.googleapis.com/auth/drive (copy a template the client did not
    create, create permissions, export PDF — ``drive.file`` is NOT enough to
    copy an arbitrary template)

Concurrency:
    SlidesClient is not thread-safe (see its docstring) — don't share one
    client across concurrent fork/spawn branches.

API reference: https://developers.google.com/slides/api/reference/rest
"""

from __future__ import annotations

import asyncio
import base64
from typing import Any, Dict, List, Optional

try:
    from googleapiclient.errors import HttpError

    from ._google_auth import GOOGLE_AUTH_AVAILABLE, build_google_service

    GSLIDES_AVAILABLE = GOOGLE_AUTH_AVAILABLE
except ImportError:
    GSLIDES_AVAILABLE = False

SCOPES = (
    "https://www.googleapis.com/auth/presentations",
    "https://www.googleapis.com/auth/drive",
)

_VALID_ROLES = ("reader", "commenter", "writer")


class SlidesClient:
    """Reusable Google Slides + Drive client pair.

    Slides edits go through the Slides API while copy/share/export are Drive
    API operations, so the client wraps one service of each.

    Not thread-safe: each service wraps a single httplib2.Http connection
    underneath, which is not safe for concurrent requests. If a workflow's
    same ``{ node: create_client }`` output is reused across concurrent
    fork/spawn branches, create one client per branch instead of sharing
    one (identical restriction to TasksClient in opcodes_gtasks.py).
    """

    def __init__(self, slides_service, drive_service):
        self.slides_service = slides_service
        self.drive_service = drive_service
        self.presentations = slides_service.presentations()
        self.files = drive_service.files()
        self.permissions = drive_service.permissions()


async def _execute(request) -> Any:
    """Run a googleapiclient request off-thread, sanitizing API errors.

    Raises:
        RuntimeError: If the Slides/Drive API returns an error. The raw
            ``HttpError`` (whose ``str()`` includes the request URI and
            response body) is not propagated; only its status and reason are.
    """
    try:
        return await asyncio.to_thread(request.execute)
    except HttpError as e:
        raise RuntimeError(
            f"Google Slides API error ({e.status_code}): {e.reason}"
        ) from e


def _replace_text_requests(
    replacements: Dict[str, Any], match_case: bool
) -> List[Dict[str, Any]]:
    """Build one ``replaceAllText`` request per replacement entry.

    Args:
        replacements: Mapping of ``find text -> replace text`` (values are
            coerced to ``str``).
        match_case: Whether the find is case-sensitive.

    Returns:
        The ``batchUpdate`` request list, in the mapping's iteration order.

    Raises:
        ValueError: If ``replacements`` is empty.
    """
    if not replacements:
        raise ValueError("replacements must have at least one entry")
    return [
        {
            "replaceAllText": {
                "containsText": {"text": find, "matchCase": match_case},
                "replaceText": str(replace),
            }
        }
        for find, replace in replacements.items()
    ]


def _replace_image_request(
    image_url: str, image_object_id: str, contains_text: str
) -> Dict[str, Any]:
    """Build the request that swaps an image (or placeholder shape) for a URL.

    Args:
        image_url: Public URL of the new image (must be fetchable by Google:
            PNG/JPEG/GIF, <50 MB, <25 megapixels; login-gated URLs fail).
        image_object_id: ObjectId of an existing image to replace in place
            (``replaceImage``), keeping its box.
        contains_text: Placeholder text marking the shape(s) to replace with
            the image (``replaceAllShapesWithImage``), keeping the shape box.

    Returns:
        The single ``batchUpdate`` request for the selected mode.

    Raises:
        ValueError: If neither or both of ``image_object_id`` and
            ``contains_text`` are given.
    """
    if bool(image_object_id) == bool(contains_text):
        raise ValueError(
            "pass exactly one of image_object_id or contains_text"
        )
    if image_object_id:
        return {
            "replaceImage": {
                "imageObjectId": image_object_id,
                "imageReplaceMethod": "CENTER_INSIDE",
                "url": image_url,
            }
        }
    return {
        "replaceAllShapesWithImage": {
            "containsText": {"text": contains_text, "matchCase": False},
            "imageReplaceMethod": "CENTER_INSIDE",
            "imageUrl": image_url,
        }
    }


def register_gslides_opcodes():
    """Register Google Slides opcodes to the default registry."""
    if not GSLIDES_AVAILABLE:
        return

    from .opcodes import opcode, register_category

    register_category(
        id="gslides",
        label="Google Slides Operations",
        prefix="gslides_",
        color="#FBBC04",
        icon="📽️",
        requires="gslides",
        order=216,
        description="Copia e edita apresentações do Google Slides a partir de templates.",
    )

    # ========================================================================
    # Authentication
    # ========================================================================

    @opcode(category="gslides")
    async def gslides_create_client(
        credentials_path: Optional[str] = None,
        subject: Optional[str] = None,
        scopes: Optional[List[str]] = None,
    ) -> SlidesClient:
        """Create a Google Slides + Drive client for API operations.

        Args:
            credentials_path: Path to a service account JSON file. If None,
                uses Application Default Credentials (ADC).
            subject: User email to impersonate via domain-wide delegation.
                Only valid together with a service account ``credentials_path``,
                and only if ``subject`` is listed in the
                ``GOOGLE_DWD_SUBJECT_ALLOWLIST`` environment variable (comma-
                separated exact addresses and/or ``@domain.com`` entries).
                Impersonation is denied by default (empty/unset allowlist),
                since an authorized service account can otherwise impersonate
                ANY user in the domain.
            scopes: OAuth scopes to request (default: ``presentations`` +
                ``drive``).

        Returns:
            SlidesClient object to use with the other gslides_* opcodes. Not
            thread-safe — see SlidesClient's docstring before reusing it
            across concurrent fork/spawn branches.

        Example with Service Account impersonating a user:
            credentials_path: "/path/to/service-account.json"
            subject: "vendedora@empresa.com"

        Example with ADC (after 'gcloud auth application-default login'):
            # No arguments needed
        """
        effective_scopes = scopes or SCOPES
        slides_service = await build_google_service(
            "slides", "v1", effective_scopes, credentials_path, subject
        )
        drive_service = await build_google_service(
            "drive", "v3", effective_scopes, credentials_path, subject
        )
        return SlidesClient(slides_service, drive_service)

    @opcode(category="gslides")
    async def gslides_close_client(client: SlidesClient) -> bool:
        """Close the client and release the underlying HTTP connections.

        Args:
            client: SlidesClient from gslides_create_client.

        Returns:
            True when closed successfully.

        Example:
            client: { node: create_client }
        """
        await asyncio.to_thread(client.slides_service.close)
        await asyncio.to_thread(client.drive_service.close)
        return True

    # ========================================================================
    # Copy (template → new presentation)
    # ========================================================================

    @opcode(category="gslides")
    async def gslides_copy_presentation(
        client: SlidesClient,
        template_id: str,
        title: str,
        folder_id: str = "",
    ) -> Dict[str, Any]:
        """Copy a presentation (the template) into a new file.

        With ``folder_id`` the copy lands in that folder — including a shared
        folder or a Shared Drive — so all generated decks can live in one
        team folder. Without it, the copy lands in the authenticated user's
        My Drive. The authenticated identity needs write access to the
        destination.

        Args:
            client: SlidesClient from gslides_create_client.
            template_id: Drive file id of the template presentation.
            title: Name of the new file.
            folder_id: Destination folder id ("" = user's My Drive).

        Returns:
            The new Drive file resource — ``id`` is the new presentation id.

        Example:
            client: { node: create_client }
            template_id: "1AbC..."
            title: "Proposta Inspira - Acme_Ago_2026"
            folder_id: "1SUEDNMTQ..."
        """
        body: Dict[str, Any] = {"name": title}
        if folder_id:
            body["parents"] = [folder_id]
        return await _execute(
            client.files.copy(
                fileId=template_id, body=body, supportsAllDrives=True
            )
        )

    # ========================================================================
    # Read
    # ========================================================================

    @opcode(category="gslides")
    async def gslides_get_presentation(
        client: SlidesClient,
        presentation_id: str,
        fields: str = "",
    ) -> Dict[str, Any]:
        """Read a presentation's structure (slides, elements, object ids).

        Use this to discover the ``objectId`` of an image/shape to replace.

        Args:
            client: SlidesClient from gslides_create_client.
            presentation_id: Presentation id.
            fields: Optional partial-response mask (e.g.
                "slides(objectId,pageElements(objectId,shape,image))") to keep
                the payload small; "" returns everything.

        Returns:
            The Presentation resource (possibly trimmed by ``fields``).

        Example:
            client: { node: create_client }
            presentation_id: { node: copy, path: id }
        """
        kwargs: Dict[str, Any] = {"presentationId": presentation_id}
        if fields:
            kwargs["fields"] = fields
        return await _execute(client.presentations.get(**kwargs))

    # ========================================================================
    # Edit
    # ========================================================================

    @opcode(category="gslides")
    async def gslides_batch_update(
        client: SlidesClient,
        presentation_id: str,
        requests: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Apply raw Slides ``batchUpdate`` requests (escape hatch).

        Covers everything the convenience opcodes don't: ``insertText``,
        ``deleteObject``, ``createImage``, ``duplicateObject``, etc. See the
        Slides API reference for request shapes. Requests are applied
        atomically, in order.

        Args:
            client: SlidesClient from gslides_create_client.
            presentation_id: Presentation id.
            requests: List of request objects.

        Returns:
            The batchUpdate response (``replies`` aligned with ``requests``).

        Example:
            client: { node: create_client }
            presentation_id: { node: copy, path: id }
            requests: [{ "deleteObject": { "objectId": "shape1" } }]
        """
        return await _execute(
            client.presentations.batchUpdate(
                presentationId=presentation_id, body={"requests": requests}
            )
        )

    @opcode(category="gslides")
    async def gslides_replace_text(
        client: SlidesClient,
        presentation_id: str,
        replacements: Dict[str, Any],
        match_case: bool = True,
    ) -> Dict[str, Any]:
        """Replace placeholder texts across the whole presentation.

        Builds one ``replaceAllText`` request per mapping entry and applies
        them in a single atomic ``batchUpdate``. Placeholders like
        ``{{QTD_LICENCAS}}`` in the template are the intended targets.

        Args:
            client: SlidesClient from gslides_create_client.
            presentation_id: Presentation id.
            replacements: Mapping of find text -> replace text
                (e.g. {"{{QTD}}": "80", "{{VALOR}}": "R$19.840"}).
            match_case: Case-sensitive find (default: True).

        Returns:
            Dict with ``occurrences`` (mapping of find text -> count changed)
            and the raw ``replies``.

        Raises:
            ValueError: If ``replacements`` is empty.

        Example:
            client: { node: create_client }
            presentation_id: { node: copy, path: id }
            replacements: { "{{QTD}}": "80" }
        """
        requests = _replace_text_requests(replacements, match_case)
        response = await _execute(
            client.presentations.batchUpdate(
                presentationId=presentation_id, body={"requests": requests}
            )
        )
        replies = response.get("replies", [])
        occurrences = {
            find: (replies[i] or {})
            .get("replaceAllText", {})
            .get("occurrencesChanged", 0)
            for i, find in enumerate(replacements)
            if i < len(replies)
        }
        return {"occurrences": occurrences, "replies": replies}

    @opcode(category="gslides")
    async def gslides_replace_image(
        client: SlidesClient,
        presentation_id: str,
        image_url: str,
        image_object_id: str = "",
        contains_text: str = "",
    ) -> Dict[str, Any]:
        """Swap an image (or a placeholder shape) for a new image by URL.

        Two modes — pass exactly one of:
          - ``image_object_id``: replaces that image in place, keeping its
            box (``replaceImage``). Find the id via gslides_get_presentation.
          - ``contains_text``: replaces every shape containing that text with
            the image, keeping the shape's box (``replaceAllShapesWithImage``)
            — e.g. a ``{{LOGO_CLIENTE}}`` text box in the template.

        The URL must be publicly fetchable by Google (PNG/JPEG/GIF, <50 MB,
        <25 megapixels); a login-gated URL does not work.

        Args:
            client: SlidesClient from gslides_create_client.
            presentation_id: Presentation id.
            image_url: Public URL of the new image.
            image_object_id: ObjectId of the image to replace in place.
            contains_text: Placeholder text marking the shape(s) to replace.

        Returns:
            The batchUpdate response (``replies``).

        Raises:
            ValueError: If neither or both of ``image_object_id`` and
                ``contains_text`` are given.

        Example:
            client: { node: create_client }
            presentation_id: { node: copy, path: id }
            image_url: "https://images.weserv.nl/?url=acme.com/logo.png"
            contains_text: "{{LOGO_CLIENTE}}"
        """
        request = _replace_image_request(image_url, image_object_id, contains_text)
        return await _execute(
            client.presentations.batchUpdate(
                presentationId=presentation_id, body={"requests": [request]}
            )
        )

    # ========================================================================
    # Share / Export
    # ========================================================================

    @opcode(category="gslides")
    async def gslides_share_link(
        client: SlidesClient,
        presentation_id: str,
        role: str = "reader",
    ) -> Dict[str, Any]:
        """Create an anyone-with-the-link permission and return the link.

        A copy starts with the template's inherited access; call this only
        when the workflow decides the deck should be link-shareable.

        Args:
            client: SlidesClient from gslides_create_client.
            presentation_id: Presentation (Drive file) id.
            role: "reader", "commenter" or "writer".

        Returns:
            Dict with ``web_view_link`` and the created ``permission``.

        Raises:
            ValueError: If ``role`` is not one of the valid roles.

        Example:
            client: { node: create_client }
            presentation_id: { node: copy, path: id }
            role: "reader"
        """
        if role not in _VALID_ROLES:
            raise ValueError(f"role must be one of {_VALID_ROLES}, got: {role!r}")
        permission = await _execute(
            client.permissions.create(
                fileId=presentation_id,
                body={"type": "anyone", "role": role},
                supportsAllDrives=True,
            )
        )
        return {
            "web_view_link": (
                f"https://docs.google.com/presentation/d/{presentation_id}/edit"
            ),
            "permission": permission,
        }

    @opcode(category="gslides")
    async def gslides_export_pdf(
        client: SlidesClient,
        presentation_id: str,
    ) -> Dict[str, Any]:
        """Export the presentation as PDF, returned as base64.

        The PDF is rendered by Google itself (pixel-faithful to the deck).
        Base64 because opcode results are JSON values; decode client-side
        for download.

        Args:
            client: SlidesClient from gslides_create_client.
            presentation_id: Presentation (Drive file) id.

        Returns:
            Dict with ``base64`` (PDF bytes, base64-encoded) and ``size``
            (byte count).

        Example:
            client: { node: create_client }
            presentation_id: { node: copy, path: id }
        """
        data = await _execute(
            client.files.export(fileId=presentation_id, mimeType="application/pdf")
        )
        return {"base64": base64.b64encode(data).decode("ascii"), "size": len(data)}
