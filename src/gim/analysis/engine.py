"""Analysis engine: event analysis and report synthesis with validation and caching.

Pipeline for one event
  1. build an immutable EventBundle (documents, excerpts, corroboration, geography)
  2. retrieve related EARLIER events from the archive (RAG)
  3. ask the analyst (LLM or deterministic baseline) for structured claims
  4. validate every claim deterministically; ask the model ONCE to repair violations;
     anything still invalid is REJECTED (kept, shown to the analyst, never used)
  5. cache the result keyed by evidence content + provider + model + prompt version

Two analysts implement the same interface:
  * LLMAnalyst      -- DeepSeek / Ollama / any OpenAI-compatible model (gim.analysis.llm)
  * BaselineAnalyst -- no AI: attributed headlines only. Always available; also the
                       evaluation baseline every model has to beat.
"""
from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import asdict
from typing import Protocol

from gim.analysis import prompts
from gim.analysis.bundle import build_event_bundles
from gim.analysis.contracts import AnalysisRequest, AnalysisResult, Claim, split_valid
from gim.analysis.llm import BudgetExceeded, LLMClient, LLMError, NotConfigured, load_config
from gim.analysis.retrieval import related_history
from gim.storage import db, intel

log = logging.getLogger(__name__)


class Analyst(Protocol):
    name: str
    model: str

    def analyse_event(self, request: AnalysisRequest) -> AnalysisResult: ...

    def synthesise(self, request: AnalysisRequest, spec_text: str, event_claims: dict[str, list[dict]],
                   trends: list[str] | None = None) -> AnalysisResult: ...


# ---------------------------------------------------------------------------
# deterministic baseline (no AI)
# ---------------------------------------------------------------------------
class BaselineAnalyst:
    name = "baseline"
    model = "deterministic"

    def analyse_event(self, request: AnalysisRequest) -> AnalysisResult:
        ev = request.events[0]
        claims = []
        docs = {d.ref: d for d in ev.documents}
        # one statement per ORIGIN (copies of a wire story are not repeated)
        for origin in json.loads(ev.corroboration)["origins"][:6]:
            d = docs.get(f"DOC-{origin['document_ids'][0]}")
            if not d:
                continue
            claims.append(Claim(text=f"{origin['label']} reported: {d.title}", claim_type="ATTRIBUTED_CLAIM",
                                source_refs=[f"{d.ref}#title"], event_ref=ev.ref, attributed_to=origin["label"],
                                quote=d.title, section="key_developments"))
        for a in request.archive[:3]:
            if a.documents:
                d = a.documents[0]
                claims.append(Claim(text=f"Earlier related report ({a.first_published_at[:10]}, {d.publisher}): {d.title}",
                                    claim_type="ATTRIBUTED_CLAIM", source_refs=[f"{d.ref}#title"], event_ref=a.ref,
                                    attributed_to=d.original_reporter or d.publisher, quote=d.title,
                                    section="background"))
        return AnalysisResult(request.request_id, self.name, self.model, claims,
                              open_questions=["No AI provider configured: significance, background, implications "
                                              "and source comparison require analyst judgement."])

    def synthesise(self, request, spec_text, event_claims, trends: list[str] | None = None) -> AnalysisResult:
        claims = []
        for ev in request.events[:6]:
            if ev.documents:
                d = ev.documents[0]
                claims.append(Claim(text=f"{d.original_reporter or d.publisher} reported: {d.title}",
                                    claim_type="ATTRIBUTED_CLAIM", source_refs=[f"{d.ref}#title"], event_ref=ev.ref,
                                    attributed_to=d.original_reporter or d.publisher, quote=d.title,
                                    section="executive_summary"))
        return AnalysisResult(request.request_id, self.name, self.model, claims, title="")


# ---------------------------------------------------------------------------
# LLM analyst
# ---------------------------------------------------------------------------
class LLMAnalyst:
    def __init__(self, client: LLMClient):
        self.client = client
        self.name = client.config.provider
        self.model = client.config.model

    def _run(self, request: AnalysisRequest, messages: list[dict], purpose: str) -> AnalysisResult:
        try:
            data = self.client.chat_json(messages, purpose)
        except (BudgetExceeded, NotConfigured):
            raise
        except LLMError as exc:
            if "not valid JSON" not in str(exc) and "no JSON object" not in str(exc):
                raise
            log.warning("Model returned invalid JSON, asking for a repair")
            data = self.client.chat_json(messages + [prompts.repair_message([str(exc)])], purpose + ":repair")
        claims = [Claim.from_dict(c) for c in data.get("claims", []) if isinstance(c, dict)]
        accepted, rejected = split_valid(request, claims)
        if rejected:
            problems = [f"claim '{r['claim']['text'][:80]}': {'; '.join(r['problems'])}" for r in rejected]
            log.info("Validator rejected %d claim(s); asking for one repair", len(rejected))
            try:
                repaired = self.client.chat_json(
                    messages + [{"role": "assistant", "content": json.dumps(data, ensure_ascii=False)[:20000]},
                                prompts.repair_message(problems)], purpose + ":repair")
            except LLMError as exc:
                # The first answer's VALID claims are still usable; never lose them to a failed repair.
                log.warning("Repair round failed (%s); keeping the validated first answer", exc)
                repaired = None
            if repaired is not None:
                claims2 = [Claim.from_dict(c) for c in repaired.get("claims", []) if isinstance(c, dict)]
                accepted2, rejected2 = split_valid(request, claims2)
                if len(accepted2) >= len(accepted):   # a "repair" that loses support is not an improvement
                    accepted, rejected, data = accepted2, rejected2, repaired
        questions = [str(q)[:500] for q in (data.get("open_questions") or []) if q][:10]
        return AnalysisResult(request.request_id, self.name, self.model, accepted, questions,
                              rejected=rejected, title=str(data.get("title") or "")[:200])

    def analyse_event(self, request: AnalysisRequest) -> AnalysisResult:
        result = self._run(request, prompts.event_messages(request), "event_analysis")
        for c in result.claims:
            c.event_ref = c.event_ref or request.events[0].ref
        return result

    def synthesise(self, request, spec_text, event_claims, trends: list[str] | None = None) -> AnalysisResult:
        return self._run(request, prompts.report_messages(request, spec_text, event_claims, trends),
                         "report_synthesis")


def get_analyst(conn, prefer_ai: bool = True) -> tuple[Analyst, list[str]]:
    """The configured LLM analyst, or the baseline with the reasons why."""
    config = load_config()
    problems = config.problems()
    if prefer_ai and not problems:
        return LLMAnalyst(LLMClient(config, conn)), []
    return BaselineAnalyst(), problems if prefer_ai else ["AI disabled for this run"]


# ---------------------------------------------------------------------------
# event analysis with cache
# ---------------------------------------------------------------------------
def _cache_key(analyst: Analyst, request: AnalysisRequest) -> str:
    evidence = [(d.ref, d.content_hash) for ev in request.events + request.archive for d in ev.documents]
    raw = json.dumps([prompts.PROMPT_VERSION, analyst.name, analyst.model, evidence], sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def analyse_event(conn, event_id: int, analyst: Analyst, *, use_cache: bool = True,
                  with_history: bool = True) -> dict:
    """Analyse one event. Returns the stored analysis content (dict)."""
    rows = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchall()
    if not rows:
        raise ValueError(f"no event EVT-{event_id}")
    event = dict(rows[0])
    bundle = build_event_bundles(conn, [event])[0]
    archive = related_history(conn, event) if with_history else ()
    request = AnalysisRequest(request_id=f"evt-{event_id}-{uuid.uuid4().hex[:8]}", task="event_analysis",
                              created_at=db.utcnow_iso(), window_start=event["first_published_at"],
                              window_end=event["last_published_at"], events=(bundle,), archive=archive)
    key = _cache_key(analyst, request)
    if use_cache:
        cached = intel.get_event_analysis(conn, key)
        if cached:
            return {**json.loads(cached["content"]), "cached": True, "rejected": json.loads(cached["rejected"])}
    result = analyst.analyse_event(request)
    content = {
        "event_ref": bundle.ref, "provider": analyst.name, "model": analyst.model,
        "prompt_version": prompts.PROMPT_VERSION, "generated_at": db.utcnow_iso(),
        "claims": [asdict(c) for c in result.claims], "open_questions": result.open_questions,
        "archive_refs": [a.ref for a in archive],
        "evidence_refs": [d.ref for d in bundle.documents] + [d.ref for a in archive for d in a.documents],
        "data_origin": event.get("data_origin", "live"),
    }
    if result.claims or analyst.name == "baseline":
        # An AI answer with zero valid claims is not cached: a later run may do better.
        intel.save_event_analysis(conn, event_id=event_id, cache_key=key, provider=analyst.name,
                                  model=analyst.model, prompt_version=prompts.PROMPT_VERSION, content=content,
                                  rejected=result.rejected)
    return {**content, "cached": False, "rejected": result.rejected}
