"""Fill version + SHA256 into the Scoop / Homebrew / winget manifests from a
published GitHub Release.

    python packaging/update-manifests.py v1.0.0

Downloads the release assets (Claudometer.exe, Claudometer-macos.zip), computes
their SHA256, and rewrites the three manifest files in place. Run it once after
each release; commit the updated manifests to your tap / bucket / winget-pkgs PR.
"""
import hashlib
import pathlib
import re
import sys
import urllib.request

REPO = "ali-dev178/claudometer"
HERE = pathlib.Path(__file__).resolve().parent


def sha256_url(url: str) -> str:
    print("  hashing", url)
    h = hashlib.sha256()
    with urllib.request.urlopen(url) as resp:  # noqa: S310 (trusted GitHub URL)
        for chunk in iter(lambda: resp.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _sub(path: pathlib.Path, replacements) -> None:
    text = path.read_text(encoding="utf-8")
    for pattern, repl in replacements:
        text = re.sub(pattern, repl, text, count=1)
    path.write_text(text, encoding="utf-8")
    print("  updated", path.relative_to(HERE.parent))


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit("usage: update-manifests.py vX.Y.Z")
    tag = sys.argv[1]
    ver = tag.lstrip("v")
    base = f"https://github.com/{REPO}/releases/download/{tag}"
    exe_sha = sha256_url(f"{base}/Claudometer.exe")            # scoop (portable)
    setup_sha = sha256_url(f"{base}/ClaudometerSetup.exe")     # winget (inno installer)
    zip_sha = sha256_url(f"{base}/Claudometer-macos.zip")      # homebrew (.app zip)

    _sub(HERE / "scoop" / "claudometer.json", [
        (r'"version":\s*"[^"]*"', f'"version": "{ver}"'),
        (r'/download/v[^/]+/Claudometer\.exe', f'/download/{tag}/Claudometer.exe'),
        (r'"hash":\s*"[0-9a-fA-F]{64}"', f'"hash": "{exe_sha}"'),
    ])
    _sub(HERE / "homebrew" / "claudometer.rb", [
        (r'version "[^"]*"', f'version "{ver}"'),
        (r'sha256 "[^"]*"', f'sha256 "{zip_sha}"'),
    ])
    _sub(HERE / "winget" / "Claudometer.installer.yaml", [
        (r'PackageVersion:.*', f'PackageVersion: {ver}'),
        (r'InstallerUrl:.*', f'InstallerUrl: {base}/ClaudometerSetup.exe'),
        (r'InstallerSha256:.*', f'InstallerSha256: {setup_sha.upper()}'),
    ])
    print(f"\nDone — manifests point at {tag}.")


if __name__ == "__main__":
    main()
