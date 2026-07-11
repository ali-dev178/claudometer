"""Update-check logic (version compare + check() result shape).

`check()` is exercised with the network mocked so it stays headless and offline.
"""

import updates
import config


def test_parts_extracts_numeric_triple():
    assert updates._parts("v1.2.3") == (1, 2, 3)
    assert updates._parts("1.1.10") == (1, 1, 10)
    assert updates._parts("") == (0,)
    assert updates._parts(None) == (0,)


def test_is_newer_semantics():
    assert updates.is_newer("1.1.4", "1.1.3") is True
    assert updates.is_newer("1.2.0", "1.1.9") is True
    assert updates.is_newer("1.1.10", "1.1.9") is True   # numeric, not string
    assert updates.is_newer("1.1.3", "1.1.3") is False
    assert updates.is_newer("1.1.2", "1.1.3") is False


def test_check_reports_update(monkeypatch):
    monkeypatch.setattr(updates, "latest_version", lambda timeout=6.0: "9.9.9")
    res = updates.check()
    assert res["status"] == "update"
    assert res["latest"] == "9.9.9"
    assert res["current"] == config.APP_VERSION
    assert res["url"].endswith("/releases/latest")


def test_check_reports_current(monkeypatch):
    monkeypatch.setattr(updates, "latest_version",
                        lambda timeout=6.0: config.APP_VERSION)
    res = updates.check()
    assert res["status"] == "current"


def test_check_handles_network_error(monkeypatch):
    def boom(timeout=6.0):
        raise OSError("no network")
    monkeypatch.setattr(updates, "latest_version", boom)
    res = updates.check()
    assert res["status"] == "error"
    assert "error" in res
    assert res["url"].endswith("/releases/latest")


def test_check_handles_missing_release(monkeypatch):
    monkeypatch.setattr(updates, "latest_version", lambda timeout=6.0: None)
    res = updates.check()
    assert res["status"] == "error"
