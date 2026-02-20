; LLC Scanner — Inno Setup Script
; Build with: iscc installer.iss
; Requires:
;   - installer\redist\python-3.11.9-amd64.exe   (download from python.org)
;   - installer\launcher.exe                       (built by build_installer.py)
;   - installer\dist\app\*                         (staged app files)

#define AppName      "LLC Scanner"
#define AppVersion   "1.0"
#define AppPublisher "kfdez"
#define AppURL       "https://github.com/kfdez/llc-scanner"
#define AppExeName   "launcher.exe"

[Setup]
AppId={{A7B3C9D2-4E6F-4A8B-9C1D-2E3F5A7B8C9D}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}
AppUpdatesURL={#AppURL}
DefaultDirName={localappdata}\{#AppName}
DisableProgramGroupPage=yes
; No admin rights needed — installs to %LocalAppData%
PrivilegesRequired=lowest
OutputDir=dist
OutputBaseFilename=LLC-Scanner-Setup
SetupIconFile=..\gui\assets\logo.ico
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
; Minimum Windows 10
MinVersion=10.0
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; Python 3.11 installer — extracted to temp, deleted after install
Source: "redist\python-3.11.9-amd64.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall

; Launcher EXE (compiled by PyInstaller)
Source: "launcher.exe"; DestDir: "{app}"; Flags: ignoreversion

; App source files
Source: "dist\app\main.py";          DestDir: "{app}"; Flags: ignoreversion
Source: "dist\app\config.py";        DestDir: "{app}"; Flags: ignoreversion
Source: "dist\app\requirements.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "dist\app\cards\*";          DestDir: "{app}\cards";      Flags: ignoreversion recursesubdirs
Source: "dist\app\db\*";             DestDir: "{app}\db";         Flags: ignoreversion recursesubdirs
Source: "dist\app\ebay\*";           DestDir: "{app}\ebay";       Flags: ignoreversion recursesubdirs
Source: "dist\app\gui\*";            DestDir: "{app}\gui";        Flags: ignoreversion recursesubdirs
Source: "dist\app\identifier\*";     DestDir: "{app}\identifier"; Flags: ignoreversion recursesubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExeName}"; IconFilename: "{app}\gui\assets\logo.ico"
Name: "{autodesktop}\{#AppName}";  Filename: "{app}\{#AppExeName}"; IconFilename: "{app}\gui\assets\logo.ico"; Tasks: desktopicon

[Run]
; 1. Install Python 3.11 silently (per-user, no PATH modification)
;    Skipped if the user already has Python 3.11+ of any version (3.11, 3.12, 3.13...)
;    The Check function below handles version detection.
Filename: "{tmp}\python-3.11.9-amd64.exe"; \
    Parameters: "/quiet InstallAllUsers=0 PrependPath=0 Include_launcher=0 Include_test=0"; \
    StatusMsg: "Installing Python 3.11 (this only happens once)..."; \
    Check: NeedsPython311

; 2. Run launcher on finish (optional checkbox)
Filename: "{app}\{#AppExeName}"; \
    Description: "{cm:LaunchProgram,{#StringChange(AppName, '&', '&&')}}"; \
    Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Remove the venv and any generated data the app creates inside install dir
Type: filesandordirs; Name: "{app}\.venv"

[Code]
// Returns True if we need to install Python 3.11 (i.e. no Python 3.11+ found).
// Checks known standard install locations for Python 3.11-3.15:
//   - Per-user:    %LocalAppData%\Programs\Python\Python3XX\python.exe
//   - System-wide: %ProgramFiles%\Python\Python3XX\python.exe
//   - System-wide: %ProgramFiles%\Python3XX\python.exe  (alternate layout)
//   - Launcher:    %LocalAppData%\Programs\Python\Launcher\py.exe
// The runtime launcher (launcher.exe) also checks PATH at first launch,
// so any Python on PATH (winget, Store, scoop, custom) will be used then.
function NeedsPython311(): Boolean;
var
  Minor: Integer;
  PythonExe: String;
  PyLauncher: String;
begin
  // Check for the Python Launcher (py.exe) — present when any Python is
  // installed via the official installer regardless of PATH setting.
  PyLauncher := ExpandConstant('{localappdata}') + '\Programs\Python\Launcher\py.exe';
  if FileExists(PyLauncher) then
  begin
    Result := False;
    Exit;
  end;
  // Also check the system-wide launcher location
  PyLauncher := ExpandConstant('{pf}') + '\Python\Launcher\py.exe';
  if FileExists(PyLauncher) then
  begin
    Result := False;
    Exit;
  end;

  // Check known per-user Python install locations (3.11 through 3.15)
  for Minor := 11 to 15 do
  begin
    PythonExe := ExpandConstant('{localappdata}') + '\Programs\Python\Python3'
                 + IntToStr(Minor) + '\python.exe';
    if FileExists(PythonExe) then
    begin
      Result := False;
      Exit;
    end;
  end;

  // Check system-wide Python install locations — two common layouts
  for Minor := 11 to 15 do
  begin
    PythonExe := ExpandConstant('{pf}') + '\Python\Python3'
                 + IntToStr(Minor) + '\python.exe';
    if FileExists(PythonExe) then
    begin
      Result := False;
      Exit;
    end;
    PythonExe := ExpandConstant('{pf}') + '\Python3'
                 + IntToStr(Minor) + '\python.exe';
    if FileExists(PythonExe) then
    begin
      Result := False;
      Exit;
    end;
  end;

  // No suitable Python found — install bundled Python 3.11
  Result := True;
end;

// Show a friendly note about the first-run dependency download
procedure InitializeWizard();
var
  InfoPage: TOutputMsgWizardPage;
begin
  InfoPage := CreateOutputMsgPage(
    wpSelectDir,
    'First-Run Information',
    'What happens after installation',
    'After installation, LLC Scanner will:' + #13#10 +
    '' + #13#10 +
    '  1. Install Python dependencies (~600 MB from PyPI)' + #13#10 +
    '     This takes 3-8 minutes on the first launch.' + #13#10 +
    '' + #13#10 +
    '  2. Run the Setup Wizard to download card data' + #13#10 +
    '     (~22,000 cards - requires an internet connection).' + #13#10 +
    '' + #13#10 +
    'An internet connection is required for the first launch.' + #13#10 +
    'Subsequent launches will start instantly.'
  );
end;
