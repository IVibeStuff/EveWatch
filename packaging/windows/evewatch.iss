; Inno Setup script - builds EveWatch-Setup-<version>.exe
; Build:  ISCC.exe /DAppVersion=1.2.0 packaging\windows\evewatch.iss
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{F80E7E7F-1144-4F13-BAE4-3AD4306C1E6F}
AppName=EveWatch
AppVersion={#AppVersion}
AppPublisher=EveWatch
DefaultDirName={autopf}\EveWatch
DefaultGroupName=EveWatch
UninstallDisplayIcon={app}\EveWatch.exe
OutputDir=..\..\dist\installer
OutputBaseFilename=EveWatch-Setup-{#AppVersion}
SetupIconFile=..\..\assets\icon.ico
Compression=lzma2
SolidCompression=yes
; per-user install: no administrator prompt, and updates just install over it
PrivilegesRequired=lowest
CloseApplications=yes
RestartApplications=no
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked
Name: "autostart"; Description: "Start EveWatch when I sign in to Windows"; Flags: unchecked

[Files]
Source: "..\..\dist\EveWatch\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{group}\EveWatch"; Filename: "{app}\EveWatch.exe"
Name: "{group}\Uninstall EveWatch"; Filename: "{uninstallexe}"
Name: "{autodesktop}\EveWatch"; Filename: "{app}\EveWatch.exe"; Tasks: desktopicon
Name: "{userstartup}\EveWatch"; Filename: "{app}\EveWatch.exe"; Parameters: "--no-browser"; Tasks: autostart

[Run]
Filename: "{app}\EveWatch.exe"; Description: "Start EveWatch now"; Flags: nowait postinstall skipifsilent

; Your data (logins, plans, caches) lives in %LOCALAPPDATA%\EveWatch and is
; deliberately NOT removed on uninstall.
