import json
from pathlib import Path

import pytest

from gim.pipeline.attribution import classify_reporting, detect_original_reporter
from gim.pipeline.geo import tag_places
from gim.sources.base import FetchState, NeedsCredentials, ParseError
from gim.sources.http import HttpResponse
from gim.sources.registry import get_registry
from gim.sources.reliefweb import ReliefWebConnector, parse_reports
from gim.sources.rss import RssConnector, parse_feed

FIX = Path(__file__).parent / "fixtures" / "synthetic"
REG = get_registry()


def test_un_news_feed():
    docs = parse_feed((FIX / "un_news.xml").read_bytes(), REG["un_news"])
    assert len(docs) == 3                                  # javascript: link dropped
    sudan, taiwan, nodate = docs
    assert sudan.doc_type == "official_statement" and sudan.source_category == "official_un"
    assert sudan.publisher.startswith("United Nations") and sudan.original_reporter == sudan.publisher
    assert "<b>" not in sudan.summary and "Security Council" in sudan.summary
    assert sudan.countries == ["Sudan"] and sudan.regions == ["africa"]
    assert sudan.published_at.isoformat() == "2026-11-03T14:30:00+00:00"
    assert taiwan.canonical_url.endswith("/0000002")       # utm stripped
    assert set(taiwan.countries) == {"China", "Taiwan"} and taiwan.regions == ["asia_pacific"]
    assert nodate.published_at is None and nodate.provenance["published_missing"]
    assert nodate.regions == ["global"]                    # default region for UN News
    assert sudan.language == "en"


def test_council_and_commission_feeds():
    council = parse_feed((FIX / "eu_council_press.xml").read_bytes(), REG["eu_council_press"])
    assert len(council) == 3
    assert "economic" in council[0].categories            # sanctions
    assert set(council[0].countries) == {"Russia", "Ukraine"}
    assert council[2].regions == ["europe"]               # no place -> registry default
    commission = parse_feed((FIX / "eu_commission_press.xml").read_bytes(), REG["eu_commission_press"])
    assert len(commission) == 2                           # Atom works too
    assert commission[0].updated_at is not None           # updated != published is kept
    assert "Moldova" in commission[1].countries


def test_media_feed_detects_wire_copy():
    spec = REG["bbc_world"]  # any international_media spec
    docs = parse_feed((FIX / "media_feed.xml").read_bytes(), spec)
    wire, own = docs
    assert wire.doc_type == "syndicated" and wire.original_reporter == "Reuters"
    assert own.doc_type == "original_reporting" and own.original_reporter == ""
    assert "Democratic Republic of the Congo" in own.countries   # via 'Goma' / 'DRC'


def test_invalid_feed_raises():
    with pytest.raises(ParseError):
        parse_feed(b"<html><body>Access denied</body></html>", REG["un_news"])


def test_reliefweb_parse():
    docs = parse_reports(json.loads((FIX / "reliefweb_reports.json").read_text("utf-8")))
    assert len(docs) == 2                                  # empty-title record dropped
    sitrep, analysis = docs
    assert sitrep.publisher.startswith("ReliefWeb")
    assert sitrep.original_reporter == "UN Office for the Coordination of Humanitarian Affairs"
    assert sitrep.doc_type == "official_statement" and analysis.doc_type == "analysis_opinion"
    assert set(sitrep.countries) == {"Sudan", "Chad"}
    assert sitrep.published_at.date().isoformat() == "2026-11-03"   # date.original, not posting date
    assert "humanitarian" in analysis.categories
    assert analysis.original_reporter.count(";") == 1


class FakeHttp:
    def __init__(self, response):
        self.response, self.calls = response, []

    def get(self, url, **kw):
        self.calls.append(("GET", url, kw))
        return self.response

    def post(self, url, **kw):
        self.calls.append(("POST", url, kw))
        return self.response


def test_reliefweb_needs_credentials(monkeypatch):
    monkeypatch.delenv("GIM_RELIEFWEB_APPNAME", raising=False)
    conn = ReliefWebConnector(REG["reliefweb_reports"], FakeHttp(None))
    with pytest.raises(NeedsCredentials):
        conn.fetch(FetchState())


def test_reliefweb_request_shape(monkeypatch):
    monkeypatch.setenv("GIM_RELIEFWEB_APPNAME", "approved-app")
    http = FakeHttp(HttpResponse(200, (FIX / "reliefweb_reports.json").read_bytes()))
    result = ReliefWebConnector(REG["reliefweb_reports"], http).fetch(FetchState())
    method, url, kw = http.calls[0]
    assert method == "POST" and kw["params"] == {"appname": "approved-app"}
    assert kw["json"]["filter"]["field"] == "date.created"
    assert len(result.documents) == 2 and result.raw


def test_rss_conditional_get():
    http = FakeHttp(HttpResponse(304, b"", {}))
    result = RssConnector(REG["un_news"], http).fetch(FetchState(etag='"abc"'))
    assert result.not_modified and result.etag == '"abc"'
    assert http.calls[0][2]["etag"] == '"abc"'


def test_geo_and_attribution_edge_cases():
    assert tag_places("South Sudan peace deal")[0] == ["South Sudan"]      # not also "Sudan"
    assert tag_places("Let us talk about the economy")[0] == []           # 'us' is not the USA
    assert tag_places("US envoy visits Seoul")[0] == ["United States", "South Korea"]
    assert detect_original_reporter("KYIV (Reuters) - text") == "Reuters"
    assert classify_reporting("apnews.com", "anything", "unknown") == ("original_reporting", "Associated Press")
    assert classify_reporting("blog.test", "no marker", "unknown") == ("unknown", "")
