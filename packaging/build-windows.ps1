# Build a standalone Claudometer.exe (no Python needed to run).
# Prereq:  py -m pip install -r requirements-dev.txt
# Output:  dist\Claudometer.exe

$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
Push-Location $root
try {
    py -m PyInstaller --onefile --noconsole --name Claudometer `
        --icon assets/icon.ico `
        --collect-submodules PIL `
        --hidden-import PIL._tkinter_finder `
        app.py
    Write-Host "`nBuilt: $root\dist\Claudometer.exe"
} finally {
    Pop-Location
}
