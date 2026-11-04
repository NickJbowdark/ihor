"""Parses every REAL response you recorded with `python -m gim.collect --record`.

Skipped when tests/fixtures/recorded/ is empty (e.g. in a sandbox without network).
"""
import json
from pathlib import Path

import pytest

from gim.sources.gdelt import parse_artlist
from gim.sources.registry import get_registry
from gim.sources.reliefweb import parse_reports
from gim.sources.rss import parse_feed

RECORDED = sorted(p for p in (Path(__file__).parent / "fixtures" / "recorded").glob("*__*") if p.is_file())


@pytest.mark.skipif(not RECORDED, reason="no recorded real responses yet (see tests/fixtures/README.md)")
@pytest.mark.parametrize("path", RECORDED, ids=lambda p: p.name)
def test_recorded_response_parses(path):
    source_id = path.name.split("__")[0]
    spec = get_registry()[source_id]
    body = path.read_bytes()
    if spec.connector == "rss":
        docs = parse_feed(body, spec)
    elif spec.connector == "reliefweb":
        docs = parse_reports(json.loads(body), spec.id)
    elif spec.connector == "gdelt":
        docs = parse_artlist(json.loads(body))
    else:
        pytest.skip(f"no parser for {spec.connector}")
    assert docs, f"{path.name}: parsed zero documents -- format changed or empty response?"
    for d in docs:
        assert d.url.startswith(("http://", "https://")) and d.title
