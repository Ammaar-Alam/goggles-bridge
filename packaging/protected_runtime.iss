{ Check security before starting any privileged Python code. Inno Setup 6's
  Pascal Script process is 32-bit even with ArchitecturesInstallIn64BitMode. }
type
  TAclSize = record
    AceCount, BytesUsed, BytesFree: Cardinal;
  end;
  TAceHeaderMask = record
    AceType, Flags: Byte;
    Size: Word;
    Mask: Cardinal;
  end;

function SecurityInfo(Name: String; ObjectType, Information: Cardinal;
  var Owner, Group, Dacl, Sacl, Descriptor: Cardinal): Cardinal;
  external 'GetNamedSecurityInfoW@advapi32.dll stdcall';
function AclInformation(Acl: Cardinal; var Information: TAclSize;
  Size, InformationClass: Cardinal): Boolean;
  external 'GetAclInformation@advapi32.dll stdcall';
function GetAce(Acl, Index: Cardinal; var Ace: Cardinal): Boolean;
  external 'GetAce@advapi32.dll stdcall';
procedure ReadAce(var Header: TAceHeaderMask; Source, Size: Cardinal);
  external 'RtlMoveMemory@kernel32.dll stdcall';
function KnownSid(Sid: Cardinal; Kind: Integer): Boolean;
  external 'IsWellKnownSid@advapi32.dll stdcall';
function StringSid(Text: String; var Sid: Cardinal): Boolean;
  external 'ConvertStringSidToSidW@advapi32.dll stdcall';
function EqualSid(First, Second: Cardinal): Boolean;
  external 'EqualSid@advapi32.dll stdcall';
function FreeSecurity(Memory: Cardinal): Cardinal;
  external 'LocalFree@kernel32.dll stdcall';
function SecurityAttributes(Name: String): Cardinal;
  external 'GetFileAttributesW@kernel32.dll stdcall';
function SecurityLastError: Cardinal;
  external 'GetLastError@kernel32.dll stdcall';

function TrustedOwner(Sid: Cardinal): Boolean;
var
  InstallerSid: Cardinal;
begin
  if Sid = 0 then begin
    Result := False;
    Exit;
  end;
  Result := KnownSid(Sid, 22) or KnownSid(Sid, 26); { SYSTEM, Administrators }
  if Result then Exit;
  InstallerSid := 0;
  if not StringSid('S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464', InstallerSid) then
    RaiseException('Cannot verify the driver runtime owner.');
  try
    Result := EqualSid(Sid, InstallerSid);
  finally
    FreeSecurity(InstallerSid);
  end;
end;

procedure CheckProtectedPermissions(Owner, Dacl: Cardinal; Path: String);
var
  Ace, Index: Cardinal;
  Info: TAclSize;
  Header: TAceHeaderMask;
begin
  if (Owner = 0) or not TrustedOwner(Owner) or (Dacl = 0) then
    RaiseException('Driver runtime ownership or permissions are unsafe: ' + Path);
  if not AclInformation(Dacl, Info, 12, 2) then
    RaiseException('Cannot inspect driver runtime permissions: ' + Path);
  if Info.AceCount = 0 then Exit;
  for Index := 0 to Info.AceCount - 1 do begin
    if not GetAce(Dacl, Index, Ace) then
      RaiseException('Cannot inspect a driver runtime permission: ' + Path);
    ReadAce(Header, Ace, 8);
    if ((Header.AceType <> 0) and (Header.AceType <> 1)) or (Header.Size < 16) then
      RaiseException('Unsupported driver runtime permission: ' + Path);
    { Reject writable allow ACEs even if another ACE denies access. Include
      delete-child, DELETE, WRITE_DAC/OWNER, and generic WRITE/ALL rights. }
    if (Header.AceType = 0) and ((Header.Mask and $500D0156) <> 0) and
       not TrustedOwner(Ace + 8) then begin
      { Inherited CREATOR OWNER permissions resolve to the trusted installer. }
      if ((Header.Flags and $08) = 0) or not KnownSid(Ace + 8, 3) then
        RaiseException('A non-administrator can change the driver runtime: ' + Path);
    end;
  end;
end;

procedure CheckProtectedObject(Path: String);
var
  Attributes, Owner, Group, Dacl, Sacl, Descriptor: Cardinal;
begin
  Attributes := SecurityAttributes(Path);
  if Attributes = $FFFFFFFF then
    RaiseException('Cannot inspect the driver runtime: ' + Path);
  if (Attributes and $400) <> 0 then
    RaiseException('Driver setup cannot use a redirected file or directory: ' + Path);
  Descriptor := 0;
  if SecurityInfo(Path, 1, 5, Owner, Group, Dacl, Sacl, Descriptor) <> 0 then
    RaiseException('Cannot inspect driver runtime permissions: ' + Path);
  try
    CheckProtectedPermissions(Owner, Dacl, Path);
  finally
    FreeSecurity(Descriptor);
  end;
end;

procedure CheckProtectedTree(Path: String);
var
  Find: TFindRec;
begin
  CheckProtectedObject(Path);
  if DirExists(Path) then begin
    if not FindFirst(Path + '\*', Find) then
      RaiseException('Cannot enumerate the driver runtime: ' + Path);
    try
      repeat
        if (Find.Name <> '.') and (Find.Name <> '..') then
          CheckProtectedTree(Path + '\' + Find.Name);
      until not FindNext(Find);
      if SecurityLastError <> 18 then { ERROR_NO_MORE_FILES }
        RaiseException('Cannot finish inspecting the driver runtime: ' + Path);
    finally
      FindClose(Find);
    end;
  end;
end;

function DriverComponentDirectory: String;
begin
  Result := ExpandConstant('{commonpf64}\Goggles Bridge Display');
end;

function PreviousDevicePresent: Boolean;
var
  Instance, Prefix: String;
  Index: Integer;
begin
  Result := False;
  if not RegQueryStringValue(HKLM64, 'SOFTWARE\G2Bridge\GogglesBridgeDisplay',
                            'DeviceInstanceId', Instance) then Exit;
  if not RegKeyExists(HKLM64, 'SYSTEM\CurrentControlSet\Enum\' + Instance) then Exit;
  Instance := Uppercase(Instance);
  Prefix := 'ROOT\GOGGLESBRIDGE\';
  Result := True;
  if (Length(Instance) <> Length(Prefix) + 32) or
     (Copy(Instance, 1, Length(Prefix)) <> Prefix) then Exit;
  for Index := Length(Prefix) + 1 to Length(Instance) do
    if Pos(Copy(Instance, Index, 1), '0123456789ABCDEF') = 0 then Exit;
  Result := False;
end;

procedure CheckProtectedRuntime;
var
  Attributes, ErrorCode: Cardinal;
begin
  CheckProtectedObject(ExpandConstant('{commonpf64}'));
  Attributes := SecurityAttributes(DriverComponentDirectory);
  if Attributes = $FFFFFFFF then begin
    ErrorCode := SecurityLastError;
    if (ErrorCode <> 2) and (ErrorCode <> 3) then
      RaiseException('Cannot inspect the driver runtime directory.');
  end else
    CheckProtectedTree(DriverComponentDirectory);
end;
