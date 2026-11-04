"""Provider interface. Phase 3 adds e.g. an OpenAI-compatible provider (DeepSeek, Ollama).

Only this protocol is visible to the rest of GIM; changing provider = changing config.
"""
from __future__ import annotations

from typing import Protocol

from gim.analysis.contracts import AnalysisRequest, AnalysisResult, Claim


class AnalysisProvider(Protocol):
    name: str
    model: str

    def analyse(self, request: AnalysisRequest) -> AnalysisResult: ...


class ExtractiveBaselineProvider:
    """Deterministic, non-AI provider: one ATTRIBUTED_CLAIM per event, quoting its earliest headline.

    Useful (1) to test the whole analysis pipeline without any API, and (2) as the
    baseline every AI provider must beat in evaluations.
    """

    name = "extractive-baseline"
    model = "none"

    def analyse(self, request: AnalysisRequest) -> AnalysisResult:
        claims = []
        for ev in request.events:
            if not ev.documents:
                continue
            first = ev.documents[0]
            claims.append(Claim(
                text=f"{first.publisher} reported: {first.title}",
                claim_type="ATTRIBUTED_CLAIM", source_refs=[f"{first.ref}#title"], event_ref=ev.ref,
                attributed_to=first.original_reporter or first.publisher, quote=first.title,
            ))
        return AnalysisResult(request.request_id, self.name, self.model, claims)
