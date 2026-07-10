; Inno Setup script for Claudometer — builds dist\ClaudometerSetup.exe
; Build locally:  iscc packaging\windows-installer\claudometer.iss
; (CI passes the version:  ISCC /DAppVersion=1.0.0 ... )
; Requires dist\Claudometer.exe to already exist (PyInstaller build).

#define AppName "Claudometer"
#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif

[Setup]
AppId={{B3F1B8C2-4B1E-4C7A-9E2B-CLAUDOMETER01}}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=Muhammad Ali
AppPublisherURL=https://github.com/ali-dev178/claudometer
DefaultDirName={autopf}\Claudometer
DefaultGroupName=Claudometer
DisableProgramGroupPage=yes
OutputDir=..\..\dist
OutputBaseFilename=ClaudometerSetup
Compression=lzma2
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern
SetupIconFile=..\..\assets\icon.ico
UninstallDisplayIcon={app}\Claudometer.exe
; Per-user install — no administrator prompt (friendlier for non-technical users).
PrivilegesRequired=lowest

[Tasks]
Name: "startup"; Description: "Start Claudometer automatically when I sign in"; GroupDescription: "Startup:"
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked

[Files]
Source: "..\..\dist\Claudometer.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\Claudometer"; Filename: "{app}\Claudometer.exe"
Name: "{userdesktop}\Claudometer"; Filename: "{app}\Claudometer.exe"; Tasks: desktopicon
Name: "{userstartup}\Claudometer"; Filename: "{app}\Claudometer.exe"; Tasks: startup

[Run]
Filename: "{app}\Claudometer.exe"; Description: "Launch Claudometer now"; Flags: nowait postinstall skipifsilent
