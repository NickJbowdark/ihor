from datetime import timezone

import pytest

from gim.config import get_settings
from gim.regions import REGIONS, build_query
from gim.sources import gdelt
from gim.sources.gdelt import GdeltClient, GdeltError, parse_artlist


def test_parse_drops_bad_records_and_cleans(gdelt_payload):
    arts = parse_artlist(gdelt_payload)
    assert len(arts) == 3
    first = arts[0]
    assert first.canonical_url == "https://example.com/world/ukraine-talks"
    assert first.seen_at.tzinfo == timezone.utc and first.seen_at.hour == 10
    assert arts[0].title_fingerprint == arts[1].title_fingerprint  # syndicated copy
    assert arts[2].title == "Sudan: UN warns of famine as refugees flee & aid stalls"
    assert "humanitarian" in arts[2].categories


def test_parse_empty_response():
    assert parse_artlist({}) == []


def test_build_query_format():
    q = build_query(REGIONS["europe"], topic="ceasefire,peace talks", language="english")
    assert q.startswith('(\"European Union\" OR NATO OR')
    assert '(ceasefire OR "peace talks")' in q
    assert q.endswith("sourcelang:english")


class FakeResponse:
    def __init__(self, status=200, text="", json_data=None):
        self.status_code, self.text, self._json = status, text, json_data

    def json(self):
        if self._json is None:
            raise ValueError("not json")
        return self._json


class FakeSession:
    def __init__(self, responses):
        self.responses, self.headers, self.calls = list(responses), {}, []

    def get(self, url, params=None, timeout=None):
        self.calls.append(params)
        return self.responses.pop(0)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(gdelt.time, "sleep", lambda s: None)


def test_client_success(gdelt_payload):
    session = FakeSession([FakeResponse(text="{...}", json_data=gdelt_payload)])
    arts = GdeltClient(get_settings(), session).search_articles("(NATO OR Ukraine)", "24h", 999)
    assert len(arts) == 3
    assert session.calls[0]["maxrecords"] == 250  # capped
    assert "GIM" in session.headers["User-Agent"]


def test_client_plain_text_error_becomes_gdelt_error():
    session = FakeSession([FakeResponse(text="Queries containing OR'd terms must be surrounded by ().")])
    with pytest.raises(GdeltError, match="surrounded"):
        GdeltClient(get_settings(), session).search_articles("a OR b")


def test_client_retries_on_429(gdelt_payload):
    session = FakeSession([FakeResponse(429), FakeResponse(text="{..}", json_data=gdelt_payload)])
    assert len(GdeltClient(get_settings(), session).search_articles("NATO")) == 3
    assert len(session.calls) == 2


def test_invalid_timespan():
    with pytest.raises(ValueError):
        GdeltClient(get_settings(), FakeSession([])).search_articles("NATO", timespan="24 hours; DROP")
