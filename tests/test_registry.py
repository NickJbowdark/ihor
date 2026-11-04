import pytest

from gim.sources.registry import RegistryError, load_registry, get_registry

PHASE2_FIRST = ["gdelt", "un_news", "eu_council_press", "eu_commission_press", "reliefweb_reports"]


def test_real_registry_is_valid_and_has_phase2_sources():
    reg = get_registry()
    for sid in PHASE2_FIRST:
        assert reg[sid].implemented and reg[sid].enabled
    assert reg["reliefweb_reports"].auth_env == ("GIM_RELIEFWEB_APPNAME",)
    # Every implemented source declares licence + polling interval
    for spec in reg.values():
        assert spec.licence, spec.id
        if spec.implemented:
            assert spec.url.startswith("https://") and spec.poll_minutes >= 5


def test_no_reliability_score_field():
    # Reliability is judged per claim, never as a fixed score per organisation.
    assert all("reliability" not in s.extra for s in get_registry().values())


def _write(tmp_path, body):
    p = tmp_path / "s.toml"
    p.write_text(body, encoding="utf-8")
    return p


def test_registry_rejects_bad_entries(tmp_path):
    base = 'id="x1"\nname="n"\norganization="o"\ncategory="official_un"\ndoc_type="official_statement"\n'
    with pytest.raises(RegistryError, match="https"):
        load_registry(_write(tmp_path, '[[source]]\n' + base + 'connector="rss"\nurl="http://insecure"\n'))
    with pytest.raises(RegistryError, match="category"):
        load_registry(_write(tmp_path, '[[source]]\n' + base.replace("official_un", "blog") + 'connector="planned"\n'))
    with pytest.raises(RegistryError, match="duplicate"):
        entry = '[[source]]\n' + base + 'connector="planned"\n'
        load_registry(_write(tmp_path, entry + entry))


def test_missing_credentials(monkeypatch):
    spec = get_registry()["reliefweb_reports"]
    monkeypatch.delenv("GIM_RELIEFWEB_APPNAME", raising=False)
    assert spec.missing_credentials() == ["GIM_RELIEFWEB_APPNAME"]
    monkeypatch.setenv("GIM_RELIEFWEB_APPNAME", "my-approved-app")
    assert spec.missing_credentials() == []
