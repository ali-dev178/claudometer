"""Shared fixtures.

The only one here is a Tk root, and it exists because there must be exactly
one for the whole run. Creating a root, destroying it, and creating another
leaves Tcl unable to find its own library — "couldn't read file tk.tcl" —
after which every later test that wants a display SKIPS instead of running.

That failure mode is worse than a red test: a guard that quietly stops running
is how the missing py-modules entry reached PyPI in the first place. So the
root is made once, shared, and never torn down mid-run.
"""

import pytest


@pytest.fixture(scope="session")
def tk_root():
    """One Tk root for the entire test run. Windows are Toplevels off it."""
    try:
        import tkinter as tk
        root = tk.Tk()
    except Exception as exc:                    # headless CI has no display
        pytest.skip(f"no Tk display: {exc}")
    root.withdraw()
    yield root
    try:
        root.destroy()
    except Exception:
        pass
