# Packaging

Standalone builds so end‑users don't need Python installed.

## Windows → `Claudometer.exe`
```powershell
py -m pip install -r requirements-dev.txt
powershell -ExecutionPolicy Bypass -File packaging\build-windows.ps1
```
Produces `dist\Claudometer.exe`. Double‑click to run; drag the readout where you
want it. Add it to `shell:startup` to launch on login.

## macOS → `Claudometer.app`
```bash
python3 -m pip install -r requirements-dev.txt
bash packaging/build-macos.sh
```
Produces `dist/Claudometer.app`, a menu‑bar agent (no Dock icon).

## Automated releases (CI)
Push a version tag and GitHub Actions builds both and attaches them to the
Release:
```bash
git tag v0.1.0 && git push origin v0.1.0
```
See `.github/workflows/release.yml`.

## Distribution channels (templates)
After your first release, fill the URLs + SHA256 in:
- `winget/Claudometer.installer.yaml` — submit to `microsoft/winget-pkgs`
- `homebrew/claudometer.rb` — publish via your own `brew tap`
