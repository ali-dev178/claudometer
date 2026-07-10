# Packaging & distribution

How Claudometer reaches users without them cloning a repo. Five channels, one
release drives them all.

## The release flow (one command)

Tag a version and GitHub Actions does the rest:

```bash
git tag v1.0.0 && git push origin v1.0.0
```

[`.github/workflows/release.yml`](../.github/workflows/release.yml) then:

1. **Builds binaries** — Windows `Claudometer.exe` (PyInstaller) + `ClaudometerSetup.exe`
   (Inno Setup installer), macOS `Claudometer.app` zip + `Claudometer.dmg`.
2. **Creates the GitHub Release** and attaches all four assets.
3. **Publishes to PyPI** via Trusted Publishing (OIDC — no token stored).
4. Prints each asset's **SHA256** to the run summary.

One-time prerequisites (yours to do):
- Enable the workflow to run: `gh auth refresh -h github.com -s workflow`, then commit/push it.
- **PyPI Trusted Publisher:** on PyPI, add this repo + `release.yml` as a trusted
  publisher for the `claudometer` project — https://docs.pypi.org/trusted-publishers/

## After a release — refresh the package-manager manifests

```bash
python packaging/update-manifests.py v1.0.0
```

Downloads the release assets, computes SHA256, and fills version + hash into the
Scoop / Homebrew / winget manifests below. Then publish each:

| Channel | Manifest | How users install |
|---|---|---|
| **pipx / PyPI** | `pyproject.toml` (auto) | `pipx install claudometer` |
| **Scoop** (Windows) | `scoop/claudometer.json` | `scoop install <raw-url-or-bucket>` |
| **Homebrew** (macOS) | `homebrew/claudometer.rb` | `brew install --cask <tap>/claudometer` |
| **winget** (Windows) | `winget/Claudometer.installer.yaml` | `winget install MuhammadAli.Claudometer` |
| **Installer / binary** | (built by CI) | download from Releases, double-click |

- **Homebrew** wants its own tap repo `homebrew-claudometer` with the cask under
  `Casks/`. Then: `brew install --cask ali-dev178/claudometer/claudometer`.
- **Scoop** installs straight from the raw manifest URL, or add a bucket repo.
- **winget** requires a PR to `microsoft/winget-pkgs` (review takes days–weeks).

## Building locally (no CI)

```powershell
py -m pip install -r requirements-dev.txt
powershell -ExecutionPolicy Bypass -File packaging\build-windows.ps1   # dist\Claudometer.exe
iscc packaging\windows-installer\claudometer.iss                       # dist\ClaudometerSetup.exe (needs Inno Setup)
```
```bash
python3 -m pip install -r requirements-dev.txt
bash packaging/build-macos.sh                                          # dist/Claudometer.app
```

## Code signing (not done yet)

The binaries are **unsigned**, so users get a one-time SmartScreen/Gatekeeper
prompt (documented in the README). To remove it: a Windows code-signing cert
(~$100–400/yr) and Apple notarization ($99/yr). `pipx`/`scoop`/`brew` sidestep it.
