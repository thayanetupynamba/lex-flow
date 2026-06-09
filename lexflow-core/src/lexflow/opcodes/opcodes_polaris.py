"""Polaris business opcodes for LexFlow.

Pure helpers used by the Polaris ingestion pipeline. No external dependencies.
"""

from __future__ import annotations

from typing import Any, Dict, List


def register_polaris_opcodes() -> None:
    """Register Polaris helper opcodes to the default registry."""
    from .opcodes import opcode, register_category

    register_category(
        id="polaris",
        label="Polaris",
        prefix="polaris_",
        description="Helpers para o pipeline de analise de reunioes de vendas Polaris.",
        color="#1D9E75",
        icon="🧭",
        order=280,
    )

    @opcode(category="polaris")
    async def polaris_extract_context(
        attendees: List[Dict[str, Any]],
        internal_domain: str = "inspira.legal",
        organizer_email: str = "",
    ) -> Dict[str, Any]:
        """Derive sales-meeting context from a calendar event's attendees.

        An event is "external" when it has at least one internal attendee
        (@internal_domain) and at least one external attendee.

        Args:
            attendees: List of attendee dicts (each with at least "email";
                optionally "displayName" and "organizer").
            internal_domain: Domain that identifies Inspira participants.
            organizer_email: Event organizer e-mail, used to prefer the seller.

        Returns:
            A dict with:
                - is_external (bool): whether this is an external meeting.
                - vendedor_email (str): the Inspira seller ("" if none).
                - empresa_dominio (str): most frequent external domain ("" if none).
                - empresa_nome (str): a readable guess from the domain.
                - internos (list[str]): internal attendee e-mails.
                - externos (list[str]): external attendee e-mails.
        """
        suffix = "@" + internal_domain.lower().lstrip("@")
        internos: List[str] = []
        externos: List[str] = []

        for attendee in attendees or []:
            email = (attendee.get("email") or "").strip().lower()
            if not email or "@" not in email:
                continue
            if email.endswith(suffix):
                internos.append(email)
            else:
                externos.append(email)

        is_external = bool(internos) and bool(externos)

        vendedor_email = ""
        organizer = (organizer_email or "").strip().lower()
        if organizer and organizer.endswith(suffix):
            vendedor_email = organizer
        elif internos:
            vendedor_email = internos[0]

        empresa_dominio = ""
        if externos:
            counts: Dict[str, int] = {}
            for email in externos:
                domain = email.split("@", 1)[1]
                counts[domain] = counts.get(domain, 0) + 1
            empresa_dominio = max(counts, key=lambda k: counts[k])

        empresa_nome = ""
        if empresa_dominio:
            empresa_nome = empresa_dominio.split(".", 1)[0].capitalize()

        return {
            "is_external": is_external,
            "vendedor_email": vendedor_email,
            "empresa_dominio": empresa_dominio,
            "empresa_nome": empresa_nome,
            "internos": internos,
            "externos": externos,
        }
