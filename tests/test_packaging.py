"""Guards for the things that break silently rather than loudly.

Every check here exists because the corresponding mistake actually shipped or
nearly shipped. None of them are caught by ordinary unit tests: the code all
works, it just never reaches the user, or reaches them unconfigurable.

  * a new top-level module not added to pyproject's py-modules is absent from
    the wheel, so `pipx install claudometer` dies on import — this is how
    autostart.py and updates.py went missing
  * a new key in settings.DEFAULTS that to_toml never writes is silently
    dropped on every save
  * a new key the Settings panel never writes can only be changed by
    hand-editing TOML, which the example config explicitly says you shouldn't
    have to do
  * a Settings window taller than the screen puts Save out of reach
"""

import ast
import pathlib
import re
import sys

import pytest

import settings

ROOT = pathlib.Path(__file__).resolve().parent.parent

#: Keys the panel deliberately doesn't expose. Empty on purpose — every knob is
#: reachable from the UI today, and anything added here needs a stated reason.
PANEL_EXEMPT = set()


def _declared_modules():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    block = re.search(r"py-modules\s*=\s*\[(.*?)\]", text, re.S)
    assert block, "py-modules list not found in pyproject.toml"
    return set(re.findall(r'"([^"]+)"', block.group(1)))


def _top_level_modules():
    return {p.stem for p in ROOT.glob("*.py") if not p.stem.startswith("_")}


# --------------------------------------------------------------------------- #
# Wheel contents
# --------------------------------------------------------------------------- #
def test_every_top_level_module_ships_in_the_wheel():
    missing = _top_level_modules() - _declared_modules()
    assert not missing, (
        f"{sorted(missing)} are not in pyproject's py-modules, so they will be "
        f"absent from the wheel and `pipx install claudometer` will fail on "
        f"import. Add them to py-modules.")


def test_py_modules_lists_nothing_that_does_not_exist():
    stale = _declared_modules() - _top_level_modules()
    assert not stale, f"py-modules names modules that no longer exist: {sorted(stale)}"


def test_every_imported_top_level_module_is_shipped():
    """Anything the app imports must be in the wheel, transitively."""
    declared = _declared_modules()
    local = _top_level_modules()
    for name in sorted(local):
        tree = ast.parse((ROOT / f"{name}.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                targets = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                targets = [(node.module or "").split(".")[0]]
            else:
                continue
            for target in targets:
                if target in local and name in declared:
                    assert target in declared, (
                        f"{name}.py imports {target}, but {target} is not in "
                        f"py-modules — the wheel would ship a broken {name}")


# --------------------------------------------------------------------------- #
# Config coverage
# --------------------------------------------------------------------------- #
def test_to_toml_writes_every_default_key():
    out = settings.to_toml(dict(settings.DEFAULTS))
    for key in settings.DEFAULTS:
        if key == "accent":          # written commented-out when unset
            continue
        assert re.search(rf"^{key} = ", out, re.M), (
            f"settings.to_toml never writes {key!r}, so it is silently dropped "
            f"every time the config is saved")


def test_load_returns_every_default_key(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDOMETER_CONFIG", str(tmp_path / "none.toml"))
    assert set(settings.load()) == set(settings.DEFAULTS)


def _panel_saved_keys():
    """Keys SettingsWindow._save() writes, read statically so this needs no Tk."""
    tree = ast.parse((ROOT / "widget_bar.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not (isinstance(node, ast.FunctionDef) and node.name == "_save"):
            continue
        keys = set()
        for sub in ast.walk(node):
            if isinstance(sub, ast.Dict):
                for k in sub.keys:
                    if isinstance(k, ast.Constant) and isinstance(k.value, str):
                        keys.add(k.value)
        return keys
    raise AssertionError("SettingsWindow._save not found in widget_bar.py")


def test_settings_panel_writes_every_configurable_key():
    saved = _panel_saved_keys()
    missing = set(settings.DEFAULTS) - saved - PANEL_EXEMPT
    assert not missing, (
        f"{sorted(missing)} can only be changed by hand-editing TOML, but "
        f"claudometer.example.toml tells users the in-app panel edits "
        f"everything. Add controls, or add them to PANEL_EXEMPT with a reason.")


def test_example_config_only_uses_real_keys():
    text = (ROOT / "claudometer.example.toml").read_text(encoding="utf-8")
    unknown = set(settings._parse(text)) - set(settings.DEFAULTS)
    assert not unknown, f"example config sets keys that don't exist: {sorted(unknown)}"


def test_example_config_documents_every_key():
    text = (ROOT / "claudometer.example.toml").read_text(encoding="utf-8")
    for key in settings.DEFAULTS:
        assert re.search(rf"^#?\s*{key}\s*=", text, re.M), (
            f"{key!r} is undocumented in claudometer.example.toml")


# --------------------------------------------------------------------------- #
# The Settings window has to fit the screen it configures
# --------------------------------------------------------------------------- #
def _tk_or_skip():
    try:
        import tkinter as tk
        root = tk.Tk()
    except Exception as exc:                       # headless CI has no display
        pytest.skip(f"no Tk display: {exc}")
    root.withdraw()
    return tk, root


@pytest.mark.skipif(sys.platform not in ("win32", "darwin"),
                    reason="needs a real display")
def test_settings_window_fits_the_screen(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDOMETER_CONFIG", str(tmp_path / "c.toml"))
    tk, root = _tk_or_skip()
    try:
        import widget_bar
        win = widget_bar.SettingsWindow(root, "light", settings.load(),
                                        on_apply=lambda c: None)
        screen_h = root.winfo_screenheight()
        for note, toggle in (("collapsed", None),
                             ("sessions expanded", win._toggle_sessions),
                             ("all expanded", win._toggle_advanced)):
            if toggle:
                toggle()
            root.update_idletasks()
            win._resize_scroll()
            root.update_idletasks()
            h = win.top.winfo_reqheight()
            assert h + 64 <= screen_h, (
                f"Settings window is {h}px tall ({note}) on a {screen_h}px "
                f"screen — the Save button would be off-screen")
    finally:
        root.destroy()
