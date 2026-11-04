import pytest

from gim.config import get_settings
from gim.sources import http as http_mod
from gim.sources.http import FetchError, PoliteHttp


class Resp:
    def __init__(self, status, body=b"", headers=None):
        self.status_code, self._body, self.headers = status, body, headers or {}

    def iter_content(self, n):
        for i in range(0, len(self._body), n):
            yield self._body[i:i + n]

    def close(self):
        pass


class Session:
    def __init__(self, responses):
        self.responses, self.headers, self.calls = list(responses), {}, []

    def request(self, method, url, **kw):
        self.calls.append(kw)
        return self.responses.pop(0)


def make(responses):
    s = Session(responses)
    return PoliteHttp(get_settings(), s, sleep=lambda x: None), s


def test_retry_then_success_and_conditional_headers():
    client, s = make([Resp(503), Resp(200, b"ok", {"ETag": '"v1"'})])
    r = client.get("https://example.test/feed", etag='"v0"')
    assert r.body == b"ok" and r.headers["etag"] == '"v1"'
    assert s.calls[0]["headers"]["If-None-Match"] == '"v0"' and len(s.calls) == 2


def test_304_and_4xx():
    client, _ = make([Resp(304)])
    assert client.get("https://example.test/feed").not_modified
    client, _ = make([Resp(403, b"Forbidden")])
    with pytest.raises(FetchError) as exc:
        client.get("https://example.test/feed")
    assert exc.value.status == 403


def test_size_cap(monkeypatch):
    monkeypatch.setattr(http_mod, "MAX_BYTES", 10)
    client, _ = make([Resp(200, b"x" * 100)])
    with pytest.raises(FetchError, match="larger"):
        client.get("https://example.test/big")
