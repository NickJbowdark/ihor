import dataclasses
from datetime import datetime, timezone

import pytest

from gim import demo
from gim.analysis.bundle import build_request
from gim.analysis.contracts import AnalysisResult, Claim, validate_result
from gim.analysis.providers import ExtractiveBaselineProvider
from gim.storage import db

SINCE = datetime(2000, 1, 1, tzinfo=timezone.utc)


@pytest.fixture
def request_(tmp_path):
    demo.build_demo(tmp_path / "demo.db", rich=False)
    conn = db.connect(tmp_path / "demo.db")
    return build_request(conn, SINCE, live_only=False, max_events=100), conn


def test_request_structure_is_immutable_and_traceable(request_):
    req, conn = request_
    assert req.events and all(e.ref.startswith("EVT-") for e in req.events)
    doc = req.events[0].documents[0]
    assert doc.ref.startswith("DOC-") and doc.url.startswith("https://") and doc.content_hash
    with pytest.raises(dataclasses.FrozenInstanceError):
        doc.title = "tampered"
    assert all(e.geography.event_country is None for e in req.events)     # unknown stays unknown
    assert '"independent_origins"' in req.events[0].corroboration
    assert '"ref": "DOC-' in req.to_json()


def test_live_only_excludes_synthetic_data(request_):
    _, conn = request_
    assert build_request(conn, SINCE).events == ()


def test_baseline_provider_output_validates(request_):
    req, _ = request_
    result = ExtractiveBaselineProvider().analyse(req)
    assert result.claims and validate_result(req, result) == []


def test_validator_rejects_invented_evidence(request_):
    req, _ = request_
    real = req.events[0].documents[0]
    bad = AnalysisResult(req.request_id, "x", "y", [
        Claim("Invented", "REPORTED_FACT", ["DOC-999999"]),
        Claim("Fake quote", "REPORTED_FACT", [real.ref], quote="words that are not in the source"),
        Claim("Someone says", "ATTRIBUTED_CLAIM", [real.ref]),
        Claim("Likely escalation", "ASSESSMENT", [real.ref]),
        Claim("Background", "CONTEXT", [real.ref]),
        Claim("x", "RUMOUR", []),
    ])
    problems = validate_result(req, bad)
    assert len(problems) == 6
    assert any("not supplied" in p for p in problems) and any("verbatim" in p for p in problems)
