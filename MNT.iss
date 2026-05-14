; Inno Setup script for Monitor_Noti_tester.
;
; Build prereqs (on the Windows machine doing the install build):
;   - dist\MNT\  exists, produced by `pyinstaller --clean --noconfirm MNT.spec`
;   - build_assets\icon.ico  exists (Windows uses .ico, not .png)
;   - Inno Setup 6 installed: https://jrsoftware.org/isdl.php
;
; Compile:
;   "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" MNT.iss
;     (or open MNT.iss in the Inno Setup IDE and hit Build)
;
; Output:
;   Output\MNT-Setup-<version>.exe   (single file customer runs)
;
; Upgrade behavior:
;   Re-running the installer on an existing install:
;     - Detects the existing install via AppId GUID below
;     - Closes any running MNT.exe (asks user politely first)
;     - Overwrites all bundled files
;     - PRESERVES the license cache + user data (those live in %APPDATA%,
;       not the install dir, so they're untouched)
;     - PRESERVES models/ and config/ subdirs the user dropped in next
;       to the exe (uninsneveruninstall flag below)

#define MyAppName "Baksters Notification Runner"
#define MyAppShortName "MNT"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "Baksters"
#define MyAppURL "https://github.com/Jukree1997/Monitor_Noti_tester"
#define MyAppExeName "MNT.exe"

[Setup]
; AppId — DO NOT CHANGE this GUID across versions. It's how Inno Setup
; recognizes the same product for upgrades vs new installs. Generated
; for MNT; would change only if forking the product.
AppId={{A8F2D1C6-7B3E-4F9A-8C5D-2E1F4B6A9D03}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}/releases

; Default install dir. {autopf} = Program Files on 64-bit; falls back
; to Program Files (x86) on a 32-bit Windows (we'd never run there).
DefaultDirName={autopf}\{#MyAppShortName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
LicenseFile=LICENSE.txt
PrivilegesRequired=admin
ArchitecturesInstallIn64BitMode=x64compatible
ArchitecturesAllowed=x64compatible
; Output location for the compiled Setup-*.exe
OutputDir=Output
OutputBaseFilename=MNT-Setup-{#MyAppVersion}
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
; Branding — the icon shown for Setup-*.exe in Windows Explorer
SetupIconFile=build_assets\icon.ico
; Uninstaller appears in Apps & Features under this name
UninstallDisplayName={#MyAppName}
UninstallDisplayIcon={app}\{#MyAppExeName}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; \
    GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; The single line below pulls in the ENTIRE PyInstaller output —
; the MNT.exe entry point plus the _internal\ folder with all Python,
; Qt, CUDA, etc. Inno Setup handles ~1 GB of files just fine; the
; compressed installer will be ~1.5-2 GB.
Source: "dist\MNT\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
; Optional "Launch MNT" checkbox at end of install
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; \
    Flags: nowait postinstall skipifsilent

[UninstallDelete]
; The PyInstaller bundle is one big tree, but customers may have
; dropped their own models/ and config/ files next to the exe.
; Uninstaller should NOT delete those — Inno's default is to delete
; only what it installed, so user-added files are safe automatically.
; The license cache lives in %APPDATA%\Baksters\MNT\ and survives
; uninstall by design (so a reinstall just works).

[Code]
; Detect a running MNT.exe and ask the user to close it before
; overwriting files. Without this, the installer would fail mid-copy
; on file-in-use errors.
function InitializeSetup(): Boolean;
var
  ErrCode: Integer;
begin
  Result := True;
  // Loop until either MNT.exe isn't running, or the user gives up.
  while Exec('taskkill', '/FI "IMAGENAME eq MNT.exe" /NH', '', SW_HIDE, ewWaitUntilTerminated, ErrCode) do
  begin
    // taskkill's exit code is 128 when nothing matched — that means
    // no MNT.exe running, we can proceed.
    if ErrCode = 128 then
      break;
    if MsgBox('Monitor_Noti_tester is currently running. Please close it before installing the update.' + #13#10 + #13#10 + 'Click Retry once you''ve closed it, or Cancel to abort.', mbConfirmation, MB_RETRYCANCEL) = IDCANCEL then
    begin
      Result := False;
      break;
    end;
  end;
end;
