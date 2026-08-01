"""The README's reference sheets.

The README claims every image is a real render through the app's own code, so
a state that stops working stops generating rather than quietly shipping a
picture of something that no longer exists. That is only true if the
generators are actually exercised, which is what this does.

It also pins the handful of strings the sheets would otherwise be tempted to
hand-write. Three of them were wrong before this existed: the stuck toast puts
its dwell in the title, a subtitle names the session rather than its project,
and a mixed batch reads "1 need you · 2 finished". A screenshot of a string
the app never emits is worse than no screenshot at all.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "assets"))

import sessions_core as sc          # noqa: E402

gallery = pytest.importorskip("make_gallery", reason="needs Pillow")


def _session(title="Ship the release pipeline", project="claude-widget",
             status=sc.WAITING, age=240, reason="input needed"):
    return sc.Session(session_id="s", pid=4242, cwd=f"/work/{project}",
                      name=f"{project}-1", title=title, status=status,
                      waiting_for=reason,
                      status_updated_at=sc.now_ms() - age * 1000)


# --------------------------------------------------------------------------- #
# Every sheet still generates
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name", ["strip_gallery", "contrast_gallery",
                                  "toast_gallery", "popover_gallery",
                                  "rows_gallery", "tray_gallery"])
def test_the_sheet_still_generates(name, tmp_path, monkeypatch):
    monkeypatch.setattr(gallery, "OUT", str(tmp_path))
    getattr(gallery, name)()
    written = list(tmp_path.glob("*.png"))
    if name == "tray_gallery" and not written:
        pytest.skip("pystray unavailable, so there is no tray to draw")
    assert written, f"{name} produced nothing"
    assert all(p.stat().st_size > 1000 for p in written), "a blank sheet"


# --------------------------------------------------------------------------- #
# The strings on those sheets are the app's, not the author's
# --------------------------------------------------------------------------- #
def test_the_stuck_alert_carries_its_dwell_in_the_title():
    payload = sc.alert_for_stuck(_session(age=12 * 60))
    assert payload["title"].startswith("Still waiting · "), (
        "the sheet said 'Still waiting' with the dwell in the subtitle; the "
        "app puts it in the title")
    assert payload["subtitle"] == "Ship the release pipeline · input needed"


def test_an_alert_subtitle_names_the_session_not_the_project():
    payload = sc.alert_for(sc.Transition(
        "status", _session("Refactor the payment retries", "checkout-api",
                           reason="permission to run Bash"),
        sc.BUSY, sc.WAITING))
    assert payload["subtitle"].startswith("Refactor the payment retries · "), (
        "the sheet led with the project, which is not what the app sends")


def test_a_mixed_batch_reads_the_way_the_app_writes_it():
    blocked = sc.alert_for(sc.Transition("status", _session(), sc.BUSY, sc.WAITING))
    done = [sc.alert_for(sc.Transition(
        "status", _session(f"Done {n}", "p", sc.IDLE, 30, ""), sc.BUSY, sc.IDLE))
        for n in range(2)]
    merged = sc.coalesce_alerts([blocked] + done)
    assert merged["title"] == "3 session updates"
    assert merged["subtitle"] == "1 need you · 2 finished", (
        "the sheet invented a tidier wording than the app's")


def test_a_batch_of_one_kind_counts_that_kind():
    alerts = [sc.alert_for(sc.Transition("status", _session(f"S{n}"),
                                         sc.BUSY, sc.WAITING))
              for n in range(3)]
    assert sc.coalesce_alerts(alerts)["title"] == "3 sessions need you"


def test_the_ended_alert_is_grey_and_says_so():
    payload = sc.alert_for(sc.Transition(
        "gone", _session("Explore the caching idea", "proxy-passer",
                         sc.IDLE, 1560, "")))
    assert payload["title"] == "Session ended" and payload["color"] == "grey"
