; Machine-wide driver component; the GUI remains a per-user installation.
#ifndef Version
  #define Version "0.0.0"
#endif

[Setup]
AppId=GogglesBridgeDisplay
AppName=Goggles Bridge Display
AppVersion={#Version}
PrivilegesRequired=admin
DefaultDirName={commonpf64}\Goggles Bridge Display
UsePreviousAppDir=no
DisableDirPage=yes
DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\maintenance\GogglesBridgeDisplaySetup.exe
OutputDir=..\dist
OutputBaseFilename=GogglesBridgeDisplay-Setup
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=no
Compression=lzma2
SolidCompression=yes

[Files]
Source: "..\dist\GogglesBridgeDisplaySetup\*"; DestDir: "{app}\maintenance"; Flags: recursesubdirs ignoreversion
Source: "..\vendor\vdd\*"; DestDir: "{app}"; Flags: ignoreversion

[UninstallDelete]
; PyInstaller's dependency set changes between builds. Remove retired modules
; too, including files not recorded by the currently installed uninstaller.
Type: filesandordirs; Name: "{app}\maintenance"
Type: files; Name: "{app}\vdd_settings.xml"
Type: files; Name: "{app}\vdd_settings.tmp"
Type: files; Name: "{app}\display-install.json"

[Code]
#include "protected_runtime.iss"

var
  MaintenanceLock: THandle;
  WorkerExitCode: Integer;

function CreateMutex(Security: Integer; InitialOwner: Boolean; Name: String): THandle;
  external 'CreateMutexW@kernel32.dll stdcall';
function WaitForSingleObject(Handle: THandle; Milliseconds: Cardinal): Cardinal;
  external 'WaitForSingleObject@kernel32.dll stdcall';
function ReleaseMutex(Handle: THandle): Boolean;
  external 'ReleaseMutex@kernel32.dll stdcall';
function CloseHandle(Handle: THandle): Boolean;
  external 'CloseHandle@kernel32.dll stdcall';

procedure LockMaintenance;
var
  Status: Cardinal;
begin
  if MaintenanceLock <> 0 then Exit;
  MaintenanceLock := CreateMutex(0, False, 'Global\GogglesBridge.DisplaySetup');
  if MaintenanceLock = 0 then RaiseException('Another driver setup is running or cannot be accessed.');
  Status := WaitForSingleObject(MaintenanceLock, 0);
  if (Status <> 0) and (Status <> $80) then begin
    CloseHandle(MaintenanceLock);
    MaintenanceLock := 0;
    RaiseException('Another driver setup is running. Close it and try again.');
  end;
end;

procedure UnlockMaintenance;
begin
  if MaintenanceLock <> 0 then begin
    ReleaseMutex(MaintenanceLock);
    CloseHandle(MaintenanceLock);
    MaintenanceLock := 0;
  end;
end;

function RunWorker(Operation: String): Integer;
begin
  CheckProtectedRuntime;
  if not Exec(DriverComponentDirectory + '\maintenance\GogglesBridgeDisplaySetup.exe',
              Operation, DriverComponentDirectory, SW_HIDE, ewWaitUntilTerminated, Result) then
    Result := 1;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  try
    if CompareText(ExpandFileName(WizardDirValue), DriverComponentDirectory) <> 0 then
      RaiseException('The driver component must be installed in its fixed Program Files directory.');
    LockMaintenance;
    CheckProtectedRuntime;
    if PreviousDevicePresent then
      RaiseException('Remove the previous virtual display with the currently installed Goggles Bridge app or its display component in Windows Installed apps, then retry. The existing removal tools have been retained.');
  except
    Result := GetExceptionMessage;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then begin
    try
      WorkerExitCode := RunWorker('install-display-driver');
      if (WorkerExitCode <> 0) and (WorkerExitCode <> 3010) then
        SuppressibleMsgBox('Driver setup failed. See ' + DriverComponentDirectory + '\display-install.json. You can repair or uninstall the component.', mbError, MB_OK, IDOK);
    except
      WorkerExitCode := 1;
      SuppressibleMsgBox(GetExceptionMessage, mbError, MB_OK, IDOK);
    end;
  end;
end;

function GetCustomSetupExitCode: Integer;
begin
  Result := WorkerExitCode;
end;

function NeedRestart: Boolean;
begin
  Result := WorkerExitCode = 3010;
end;

procedure DeinitializeSetup;
begin
  UnlockMaintenance;
end;

procedure CurUninstallStepChanged(Step: TUninstallStep);
begin
  if Step = usUninstall then begin
    try
      if CompareText(ExpandConstant('{app}'), DriverComponentDirectory) <> 0 then
        RaiseException('The driver component is outside its expected directory.');
      LockMaintenance;
      if RunWorker('remove-display-driver') <> 0 then
        RaiseException('Driver removal failed. Close streams and check ' + DriverComponentDirectory + '\display-install.json. Setup files have been retained.');
    except
      SuppressibleMsgBox(GetExceptionMessage, mbError, MB_OK, IDOK);
      Abort;
    end;
  end;
end;

procedure DeinitializeUninstall;
begin
  UnlockMaintenance;
end;
