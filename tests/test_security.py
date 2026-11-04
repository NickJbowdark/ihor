import logging

from gim import collect
from gim.logging_setup import JsonFormatter, RedactingFormatter
from gim.security import REDACTED, redact, redact_bytes
from gim.sources.base import Connector
from gim.sources.registry import get_registry
from gim.storage import db

SECRET = "my-approved-appname-1234"


def test_redacts_env_values_and_patterns(monkeypatch):
    monkeypatch.setenv("GIM_RELIEFWEB_APPNAME", SECRET)
    msg = f"HTTPSConnectionPool: Max retries exceeded with url: /v2/reports?appname={SECRET}&x=1"
    out = redact(msg)
    assert SECRET not in out and REDACTED in out and "x=1" in out
    assert "abcdef123456" not in redact("Authorization: Bearer abcdef123456")
    assert "tok123456" not in redact("token=tok123456&page=2")
    assert SECRET.encode() not in redact_bytes(f"<x>{SECRET}</x>".encode())


def test_log_formatters_redact(monkeypatch):
    monkeypatch.setenv("GIM_UCDP_TOKEN", "ucdp-secret-token")
    rec = logging.makeLogRecord({"msg": "failed with header x-ucdp-access-token: ucdp-secret-token",
                                 "levelname": "ERROR", "name": "t"})
    assert "ucdp-secret-token" not in JsonFormatter().format(rec)
    assert "ucdp-secret-token" not in RedactingFormatter("%(message)s").format(rec)


class LeakyConnector(Connector):
    def fetch(self, state):
        raise RuntimeError(f"connection failed for https://api.example.test/v2?appname={SECRET}")


def test_errors_are_redacted_before_db(tmp_path, monkeypatch):
    monkeypatch.setenv("GIM_RELIEFWEB_APPNAME", SECRET)
    conn = db.connect(tmp_path / "t.db")
    spec = get_registry()["reliefweb_reports"]
    r = collect.run_source(spec, conn, None, connector=LeakyConnector(spec, None))
    assert SECRET not in r.message
    assert SECRET not in db.get_source_status(conn)["reliefweb_reports"]["last_error"]
    assert SECRET not in (db.recent_runs(conn)[0]["error"] or "")


def test_recorded_fixtures_are_redacted(tmp_path, monkeypatch):
    monkeypatch.setenv("GIM_RELIEFWEB_APPNAME", SECRET)
    paths = collect._record(get_registry()["reliefweb_reports"], [("r.json", f'{{"appname":"{SECRET}"}}'.encode())],
                            tmp_path)
    assert SECRET.encode() not in paths[0].read_bytes()
