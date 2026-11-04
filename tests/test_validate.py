from datetime import datetime, timedelta, timezone
from pathlib import Path

from gim import validate
from gim.sources.base import Connector, FetchResult
from gim.sources.http import FetchError
from gim.sources.registry import get_registry
from gim.sources.rss import parse_feed

FIX = Path(__file__).parent / "fixtures" / "synthetic"
REG = get_registry()
NOW = datetime(2026, 11, 5, tzinfo=timezone.utc)


def feed(name="un_news.xml"):
    return (FIX / name).read_bytes()


def test_good_feed_passes():
    body = feed()
    docs = parse_feed(body, REG["un_news"])
    v = validate.evaluate(REG["un_news"], docs, [("feed.xml", body)], now=NOW)
    # 1 of 3 synthetic items has no date: 67 % < 80 % timestamp requirement -> FAIL is correct
    assert v.outcome == "fail" and any("timestamp" in r for r in v.reasons)
    v = validate.evaluate(REG["un_news"], docs[:2], [("feed.xml", body)], now=NOW)
    assert v.outcome == "pass" and v.valid_documents == 2 and v.samples


def test_http_200_with_html_is_not_verified():
    body = b"<html><body>Please accept cookies</body></html>"
    v = validate.evaluate(REG["un_news"], [], [("feed.xml", body)], now=NOW)
    assert v.outcome == "fail" and any("not recognised as RSS" in r for r in v.reasons)


def test_valid_but_empty_feed_is_inconclusive():
    body = b'<?xml version="1.0"?><rss version="2.0"><channel><title>x</title></channel></rss>'
    v = validate.evaluate(REG["un_news"], [], [("feed.xml", body)], now=NOW)
    assert v.outcome == "empty"


def test_bad_documents_fail_quality_checks():
    body = feed()
    docs = parse_feed(body, REG["un_news"])[:2]
    docs[0].published_at = NOW + timedelta(days=3)       # future timestamp
    docs[1].original_reporter = ""                        # official statement without attribution
    v = validate.evaluate(REG["un_news"], docs, [("feed.xml", body)], now=NOW)
    assert v.outcome == "fail"
    assert v.field_failures["timestamp_in_future"] == 1 and v.field_failures["attribution_missing"] == 1


def test_reliefweb_schema_check():
    import json
    body = (FIX / "reliefweb_reports.json").read_bytes()
    assert validate.schema_problems(REG["reliefweb_reports"], [("r.json", body)]) == []
    broken = json.dumps({"results": []}).encode()
    assert validate.schema_problems(REG["reliefweb_reports"], [("r.json", broken)])


class Failing(Connector):
    def fetch(self, state):
        raise FetchError("network error: proxy refused", None)


class Fine(Connector):
    def fetch(self, state):
        assert state.etag is None                          # validation never uses conditional GET
        body = feed()
        return FetchResult(parse_feed(body, self.spec)[:2], raw=[("feed.xml", body)])


def test_validate_source_outcomes(tmp_path, monkeypatch):
    spec = REG["un_news"]
    v = validate.validate_source(spec, None, connector=Failing(spec, None))
    assert v.outcome == "fail" and v.error_kind == "network"
    v = validate.validate_source(spec, None, connector=Fine(spec, None), record=True, record_dir=tmp_path)
    assert v.outcome == "pass" and v.recorded
    monkeypatch.delenv("GIM_RELIEFWEB_APPNAME", raising=False)
    v = validate.validate_source(REG["reliefweb_reports"], None)
    assert v.outcome == "skipped" and v.error_kind == "credentials"
    assert validate.validate_source(REG["acled"], None).outcome == "skipped"


def test_report_and_labels(tmp_path):
    spec = REG["un_news"]
    v = validate.validate_source(spec, None, connector=Fine(spec, None))
    md, js = validate.write_report([v], tmp_path)
    assert "PASS" in md.read_text("utf-8") and js.exists()
    now = datetime.now(timezone.utc)
    assert validate.verification_label(None) == "unverified"
    assert validate.verification_label({"outcome": "pass", "validated_at": now.isoformat()}).startswith("verified")
    old = (now - timedelta(days=40)).isoformat()
    assert validate.verification_label({"outcome": "pass", "validated_at": old}).startswith("verification expired")
    assert validate.verification_label({"outcome": "fail", "validated_at": old}).startswith("FAILED")
