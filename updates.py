"""Check GitHub Releases for a newer Claudometer version.

No UI or third-party deps (stdlib urllib), so it's importable by every adapter
and unit-testable headlessly. The UI layers call ``check()`` off the main thread
and present the result with a native dialog.
"""

import json
import re
import urllib.request

import config

LATEST_API = "https://api.github.com/repos/ali-dev178/claudometer/releases/latest"
RELEASES_PAGE = config.REPO_URL + "/releases/latest"


def _parts(v: str):
    """Numeric version tuple from a string like 'v1.2.3' -> (1, 2, 3)."""
    nums = re.findall(r"\d+", v or "")
    return tuple(int(n) for n in nums[:3]) or (0,)


def is_newer(latest: str, current: str) -> bool:
    """True if `latest` is a strictly higher version than `current`."""
    return _parts(latest) > _parts(current)


def latest_version(timeout: float = 6.0):
    """Return the newest published version (no leading 'v'), or None on error."""
    req = urllib.request.Request(
        LATEST_API,
        headers={"User-Agent": "claudometer-update-check",
                 "Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.load(resp)
    tag = (data.get("tag_name") or "").lstrip("v")
    return tag or None


def check(timeout: float = 6.0) -> dict:
    """Compare the installed version against the latest GitHub release.

    Returns {status: 'update'|'current'|'error', current, latest?, url, error?}.
    Never raises — network/parse failures come back as status 'error'.
    """
    current = config.APP_VERSION
    try:
        latest = latest_version(timeout)
    except Exception as exc:  # network down, rate-limited, bad JSON, …
        return {"status": "error", "current": current,
                "url": RELEASES_PAGE, "error": str(exc)}
    if not latest:
        return {"status": "error", "current": current,
                "url": RELEASES_PAGE, "error": "no published release found"}
    status = "update" if is_newer(latest, current) else "current"
    return {"status": status, "current": current, "latest": latest,
            "url": RELEASES_PAGE}
