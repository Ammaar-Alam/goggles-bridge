"""Install the unmodified, pinned MIT-licensed VDD with normal Windows elevation.

No certificate stores, test-signing settings, or driver signature policies are
changed. Existing VDD installations are deliberately left under their owner's
control. Runtime layout uses user32, never the driver's unsafe reload pipe.
"""
import ctypes as C
from ctypes import wintypes as W
import json
import os
from pathlib import Path
import subprocess
import sys
import re
import uuid
import xml.etree.ElementTree as ET
try:
    import winreg
except ImportError:  # Other platforms can import the app without Windows services.
    winreg = None

from .paths import install_folder, maintenance_command
from .system import powershell_json, sha256, system_directory, NO_WINDOW
from .elevation import InstallerProcess

REGISTRY = r'SOFTWARE\MikeTheTech\VirtualDisplayDriver'
OWNER = r'SOFTWARE\G2Bridge\GogglesBridgeDisplay'
UMDF_SERVICE = r'SOFTWARE\Microsoft\Windows NT\CurrentVersion\WUDF\Services\MttVDD'
PREVIOUS_OWNER = r'SOFTWARE\G2Bridge\DJIGogglesStreamerDisplay'
INSTANCE_ID = re.compile(r'ROOT\\GOGGLESBRIDGE\\[0-9a-f]{32}', re.IGNORECASE)
PREVIOUS_NOTICE = 'This older display installation cannot be verified. Uninstall it with the original app or its Windows uninstall entry, then install again.'
HASHES = {
    'mttvdd.cat': '08a0093fc9b2e32b287a6f8a77ca4de0a31830d29fc33d2b13a918dc859468f6',
    'MttVDD.dll': 'c9ca837f57a98fbd43bc416a7f535a95843626e7759eaf85cf0cd7ce334dbb05',
    'MttVDD.inf': '550d211fe481e74dfe3f9d724ed78be48b3a9113405965d683d9373e8d672f5d',
}
CONFIG = '''<?xml version="1.0" encoding="utf-8"?>
<vdd_settings><monitors><count>1</count></monitors>
<gpu><friendlyname>default</friendlyname></gpu>
<global><g_refresh_rate>60</g_refresh_rate><g_refresh_rate>75</g_refresh_rate>
<g_refresh_rate>90</g_refresh_rate><g_refresh_rate>100</g_refresh_rate></global>
<resolutions><resolution><width>1920</width><height>1080</height>
<refresh_rate>60</refresh_rate></resolution><resolution><width>1280</width><height>720</height>
<refresh_rate>60</refresh_rate></resolution></resolutions>
<options><logging>false</logging><debuglogging>false</debuglogging>
<SDR10bit>false</SDR10bit><HDRPlus>false</HDRPlus><HardwareCursor>false</HardwareCursor>
<CustomEdid>false</CustomEdid></options></vdd_settings>
'''


def owner_value(name, registry=OWNER):
    if winreg is None:
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, registry) as key:
            value = winreg.QueryValueEx(key, name)[0]
            return value if isinstance(value, str) else None
    except FileNotFoundError:
        return None


def owned_instance():
    instance = owner_value('DeviceInstanceId')
    return instance if instance and INSTANCE_ID.fullmatch(instance) else None


def _save_owner(name, value):
    with winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, OWNER, 0, winreg.KEY_WRITE) as key:
        winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)


def _owns_configuration():
    if winreg is None:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, REGISTRY) as key:
            path = winreg.QueryValueEx(key, 'VDDPATH')[0]
        return Path(path).resolve() == install_folder().resolve()
    except (FileNotFoundError, OSError, TypeError, ValueError):
        return False


def previous_installation():
    """Old sequential IDs cannot prove which app created the current device."""
    if owned_instance():
        return False
    return any(_instance_exists(instance) for instance in
               (owner_value('DeviceInstanceId'), owner_value('DeviceInstanceId', PREVIOUS_OWNER)))


def _instance_exists(instance):
    if not instance or winreg is None:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, 'SYSTEM\\CurrentControlSet\\Enum\\'+instance):
            return True
    except FileNotFoundError:
        return False


def _instance_registered(instance):
    if not instance or not INSTANCE_ID.fullmatch(instance) or winreg is None:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            'SYSTEM\\CurrentControlSet\\Enum\\' + instance) as key:
            identifiers = winreg.QueryValueEx(key, 'HardwareID')[0]
        if isinstance(identifiers, str):
            identifiers = [identifiers]
        return any(value.lower() in ('root\\mttvdd', 'mttvdd') for value in identifiers)
    except (FileNotFoundError, OSError, TypeError, ValueError):
        return False


def _instance_ready(instance):
    manager = C.WinDLL('cfgmgr32')
    manager.CM_Locate_DevNodeW.argtypes = [C.POINTER(W.DWORD), W.LPWSTR, W.ULONG]
    manager.CM_Get_DevNode_Status.argtypes = [C.POINTER(W.ULONG), C.POINTER(W.ULONG), W.DWORD, W.ULONG]
    node, flags, problem = W.DWORD(), W.ULONG(), W.ULONG()
    if manager.CM_Locate_DevNodeW(C.byref(node), instance, 0):
        return False
    if manager.CM_Get_DevNode_Status(C.byref(flags), C.byref(problem), node, 0):
        return False
    return bool(flags.value & 0x8) and not flags.value & 0x400  # DN_STARTED, DN_HAS_PROBLEM


def configuration_details(text):
    """Check modes while preserving an explicitly configured render-GPU preference."""
    try:
        current, expected = ET.fromstring(text), ET.fromstring(CONFIG)
        gpu = current.find('gpu/friendlyname')
        if gpu is None or not (gpu.text or '').strip():
            return False, None
        preference = gpu.text.strip()
        gpu.text = 'default'
        for root in (current, expected):
            for node in root.iter():
                node.text = (node.text or '').strip() or None
                node.tail = None
        return ET.tostring(current) == ET.tostring(expected), preference
    except ET.ParseError:
        return False, None


def status():
    if os.name != 'nt':
        return dict(owned=False, installed=False, config_current=False)
    instance = owned_instance()
    previous = previous_installation()
    owned = bool(instance or (install_folder()/'unins000.exe').is_file())
    current = False
    try:
        current, _ = configuration_details((install_folder()/'vdd_settings.xml').read_text(encoding='utf-8'))
    except (OSError, UnicodeError):
        pass
    from .displays import inventory
    installed = owned or previous or any(r['hardware_id'].lower() in ('root\\mttvdd','mttvdd') for r in inventory())
    return dict(owned=owned, installed=installed, previous=previous,
                config_current=bool(instance and current and _owns_configuration() and
                                    _instance_registered(instance) and _instance_ready(instance)))


def verify_package(folder=None):
    folder = Path(folder) if folder else install_folder()
    for name, digest in HASHES.items():
        if sha256(folder/name) != digest:
            raise ValueError('Virtual display driver checksum mismatch. Reinstall the app.')
    literal = str(folder/'mttvdd.cat').replace("'", "''")
    signature = powershell_json(f"Get-AuthenticodeSignature -LiteralPath '{literal}' | "
        "Select-Object @{n='Status';e={$_.Status.ToString()}},"
        "@{n='Signer';e={$_.SignerCertificate.Subject}} | ConvertTo-Json -Compress")
    if not signature or signature['Status'] != 'Valid' or 'CN=SignPath Foundation,' not in signature['Signer']:
        raise ValueError('Windows could not verify the virtual display driver signature.')
    return folder


class VirtualInstaller(InstallerProcess):
    def __init__(self, remove=False):
        from .security import lock_program
        program, arguments = maintenance_command(remove)
        digest = None
        if not remove:
            try:
                from _goggles_driver_build import INSTALLER_SHA256
            except ImportError as exc:
                raise RuntimeError('Use a packaged Goggles Bridge build to install the display driver.') from exc
            digest = INSTALLER_SHA256
        self._locks = lock_program(program, digest=digest, protected_folder=install_folder() if remove else None)
        try:
            self._launch(program, subprocess.list2cmdline(arguments))
        except BaseException:
            self._locks.close()
            raise

    def poll(self):
        code = super().poll()
        if code is not None:
            self._locks.close()
        return code


class GUID(C.Structure):
    _fields_ = [('Data1', W.DWORD), ('Data2', W.WORD), ('Data3', W.WORD), ('Data4', W.BYTE*8)]


class DEVINFO(C.Structure):
    _fields_ = [('cbSize', W.DWORD), ('ClassGuid', GUID), ('DevInst', W.DWORD), ('Reserved', C.c_size_t)]


class INSTALL_PARAMS(C.Structure):
    _fields_ = [('cbSize', W.DWORD), ('Flags', W.DWORD), ('FlagsEx', W.DWORD),
                ('hwndParent', W.HWND), ('InstallMsgHandler', C.c_void_p),
                ('InstallMsgHandlerContext', C.c_void_p), ('FileQueue', W.HANDLE),
                ('ClassInstallReserved', C.c_size_t), ('Reserved', W.DWORD), ('DriverPath', W.WCHAR*260)]


class DRIVER_INFO(C.Structure):
    _fields_ = [('cbSize', W.DWORD), ('DriverType', W.DWORD), ('Reserved', C.c_size_t),
                ('Description', W.WCHAR*256), ('MfgName', W.WCHAR*256), ('ProviderName', W.WCHAR*256),
                ('DriverDate', W.FILETIME), ('DriverVersion', C.c_ulonglong)]


class DeviceRollbackError(RuntimeError):
    pass


def _install_device(inf, instance=None):
    if instance and not INSTANCE_ID.fullmatch(instance):
        raise RuntimeError(PREVIOUS_NOTICE)
    setup = C.WinDLL('setupapi', use_last_error=True)
    setup.SetupDiCreateDeviceInfoList.argtypes = [C.POINTER(GUID), W.HWND]
    setup.SetupDiCreateDeviceInfoList.restype = W.HANDLE
    setup.SetupDiCreateDeviceInfoW.argtypes = [W.HANDLE,W.LPCWSTR,C.POINTER(GUID),W.LPCWSTR,W.HWND,W.DWORD,C.POINTER(DEVINFO)]
    setup.SetupDiOpenDeviceInfoW.argtypes = [W.HANDLE, W.LPCWSTR, W.HWND, W.DWORD, C.POINTER(DEVINFO)]
    setup.SetupDiSetDeviceRegistryPropertyW.argtypes = [W.HANDLE,C.POINTER(DEVINFO),W.DWORD,C.c_void_p,W.DWORD]
    setup.SetupDiCallClassInstaller.argtypes = [W.DWORD,W.HANDLE,C.POINTER(DEVINFO)]
    setup.SetupDiDestroyDeviceInfoList.argtypes = [W.HANDLE]
    setup.SetupDiGetDeviceInstallParamsW.argtypes = [W.HANDLE, C.POINTER(DEVINFO), C.POINTER(INSTALL_PARAMS)]
    setup.SetupDiSetDeviceInstallParamsW.argtypes = setup.SetupDiGetDeviceInstallParamsW.argtypes
    setup.SetupDiBuildDriverInfoList.argtypes = [W.HANDLE, C.POINTER(DEVINFO), W.DWORD]
    setup.SetupDiDestroyDriverInfoList.argtypes = setup.SetupDiBuildDriverInfoList.argtypes
    setup.SetupDiEnumDriverInfoW.argtypes = [W.HANDLE, C.POINTER(DEVINFO), W.DWORD, W.DWORD, C.POINTER(DRIVER_INFO)]
    guid = GUID(0x4d36e968,0xe325,0x11ce,(W.BYTE*8)(0xbf,0xc1,0x08,0x00,0x2b,0xe1,0x03,0x18))
    handle = setup.SetupDiCreateDeviceInfoList(C.byref(guid),None)
    if handle in (None,C.c_void_p(-1).value):
        raise C.WinError(C.get_last_error())
    info = DEVINFO(cbSize=C.sizeof(DEVINFO))
    registered = False
    try:
        if instance:
            if not setup.SetupDiOpenDeviceInfoW(handle, instance, None, 0, C.byref(info)):
                raise C.WinError(C.get_last_error())
        else:
            instance = 'ROOT\\GOGGLESBRIDGE\\'+uuid.uuid4().hex
            # A full random ID avoids Windows reusing an old sequential ROOT\DISPLAY ID.
            if not setup.SetupDiCreateDeviceInfoW(handle,instance,C.byref(guid),
                    'Goggles Bridge Virtual Display',None,0,C.byref(info)):
                raise C.WinError(C.get_last_error())
            _save_owner('DeviceInstanceId', instance)
            hardware = C.create_unicode_buffer('Root\\MttVDD\0')
            if not setup.SetupDiSetDeviceRegistryPropertyW(handle,C.byref(info),1,hardware,C.sizeof(hardware)):
                raise C.WinError(C.get_last_error())
            registered = True
            if not setup.SetupDiCallClassInstaller(0x19,handle,C.byref(info)):
                raise C.WinError(C.get_last_error())
        # Bind only this device to the pinned INF, never all hardware-ID matches.
        params = INSTALL_PARAMS(cbSize=C.sizeof(INSTALL_PARAMS))
        if not setup.SetupDiGetDeviceInstallParamsW(handle, C.byref(info), C.byref(params)):
            raise C.WinError(C.get_last_error())
        params.Flags |= 0x10000  # DI_ENUMSINGLEINF
        params.DriverPath = str(inf)
        if not setup.SetupDiSetDeviceInstallParamsW(handle, C.byref(info), C.byref(params)):
            raise C.WinError(C.get_last_error())
        if not setup.SetupDiBuildDriverInfoList(handle, C.byref(info), 2):
            raise C.WinError(C.get_last_error())
        driver = DRIVER_INFO(cbSize=C.sizeof(DRIVER_INFO))
        if not setup.SetupDiEnumDriverInfoW(handle, C.byref(info), 2, 0, C.byref(driver)):
            raise C.WinError(C.get_last_error())
        update = C.WinDLL('newdev',use_last_error=True).DiInstallDevice
        update.argtypes = [W.HWND, W.HANDLE, C.POINTER(DEVINFO), C.POINTER(DRIVER_INFO), W.DWORD, C.POINTER(W.BOOL)]
        reboot = W.BOOL()
        if not update(None,handle,C.byref(info),C.byref(driver),0,C.byref(reboot)):
            raise C.WinError(C.get_last_error())
        return bool(reboot.value)
    except Exception:
        if registered:
            if not setup.SetupDiCallClassInstaller(5,handle,C.byref(info)):
                raise DeviceRollbackError('Device installation and rollback failed. Configuration was retained; run driver repair or removal again.')
        raise
    finally:
        setup.SetupDiDestroyDriverInfoList(handle, C.byref(info), 2)
        setup.SetupDiDestroyDeviceInfoList(handle)


def _check_consumers(instance):
    devices = vdd_devices()
    if any(row['PNPDeviceID'].casefold() != (instance or '').casefold() for row in devices):
        raise RuntimeError('Another virtual display uses the shared driver. Remove it with its own app before continuing.')
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, REGISTRY):
            if not _owns_configuration():
                raise RuntimeError('Another application owns the display configuration. It was not changed.')
    except FileNotFoundError:
        pass


def _write_config(config, content):
    temporary = config.with_suffix('.tmp')
    try:
        temporary.write_bytes(content)
        temporary.replace(config)
    finally:
        temporary.unlink(missing_ok=True)


def _pnputil(*arguments):
    result = subprocess.run([str(system_directory()/'pnputil.exe'), *arguments],
                            capture_output=True, timeout=90, creationflags=NO_WINDOW,
                            cwd=install_folder())
    if result.returncode not in (0, 3010):
        raise RuntimeError(f'Windows driver maintenance failed ({result.returncode}).')
    return result.returncode


def _stage_package(inf):
    setup = C.WinDLL('setupapi', use_last_error=True)
    copy = setup.SetupCopyOEMInfW
    copy.argtypes = [W.LPCWSTR, W.LPCWSTR, W.DWORD, W.DWORD, W.LPWSTR, W.DWORD,
                     C.POINTER(W.DWORD), C.POINTER(W.LPWSTR)]
    published = C.create_unicode_buffer(32768)
    if copy(str(inf), None, 1, 8, published, len(published), None, None):
        name = Path(published.value).name
        try:
            _save_owner('PublishedInf', name)
        except Exception:
            _pnputil('/delete-driver', name)
            raise
    elif C.get_last_error() != 80:  # Existing packages belong to their original installer.
        raise C.WinError(C.get_last_error())


def install():
    if os.name != 'nt':
        raise RuntimeError('Virtual display installation is only available on Windows.')
    if previous_installation():
        raise RuntimeError(PREVIOUS_NOTICE)
    instance = owned_instance()
    _check_consumers(instance)
    target = verify_package()
    config = target/'vdd_settings.xml'
    try:
        original = config.read_bytes()
    except FileNotFoundError:
        original = None
    try:
        current, preference = configuration_details((original or b'').decode('utf-8'))
    except UnicodeError:
        current, preference = False, None
    updated = ET.fromstring(CONFIG)
    if preference:
        updated.find('gpu/friendlyname').text = preference
    configured = _owns_configuration()
    registered = bool(instance and _instance_registered(instance))
    ready = registered and _instance_ready(instance)
    if current and configured and ready:
        return 0
    _write_config(config, ET.tostring(updated, encoding='utf-8', xml_declaration=True))
    try:
        with winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, REGISTRY, 0, winreg.KEY_WRITE) as key:
            winreg.SetValueEx(key, 'VDDPATH', 0, winreg.REG_SZ, str(target))
        if ready:
            return _pnputil('/restart-device', instance)
        # A missing device can be recreated, but a reused ID must never be adopted.
        if instance and not registered and _instance_exists(instance):
            raise RuntimeError('The recorded device ID now belongs to another device.')
        _stage_package(target/'MttVDD.inf')
        reboot = _install_device(target/'MttVDD.inf', instance if registered else None)
        return 3010 if reboot else 0
    except DeviceRollbackError:
        raise  # The remaining device still needs its configuration and ownership record.
    except Exception:
        if original is None:
            config.unlink(missing_ok=True)
        else:
            _write_config(config, original)
        if not configured and _owns_configuration():
            winreg.DeleteKey(winreg.HKEY_LOCAL_MACHINE, REGISTRY)
        raise


def remove():
    instance = owned_instance()
    published = owner_value('PublishedInf')
    if not owner_value('DeviceInstanceId') and not published and not _owns_configuration():
        return 0  # A failed setup may have installed only the maintenance component.
    # Inventory and shared-configuration checks precede any destructive operation.
    _check_consumers(instance)
    if instance and _instance_registered(instance):
        # Remove the owned monitor children as well as their display adapter.
        if _pnputil('/remove-device', instance, '/subtree') == 3010:
            raise RuntimeError('Restart Windows, then remove the virtual display driver again.')
    if vdd_devices():
        raise RuntimeError('The virtual display is still in use. Driver setup files were retained.')
    if published:
        if not re.fullmatch(r'oem[0-9]+\.inf', published, re.IGNORECASE):
            raise RuntimeError('Invalid recorded driver package. Driver setup files were retained.')
        # No /force or /uninstall: Windows must refuse deletion if another device uses it.
        inf = system_directory().parent/'INF'/published
        if inf.exists():
            if sha256(inf) != HASHES['MttVDD.inf']:
                raise RuntimeError('The recorded driver package changed. It was not removed.')
            if _pnputil('/delete-driver', published) == 3010:
                raise RuntimeError('Restart Windows, then remove the virtual display driver again.')
    if _owns_configuration():
        winreg.DeleteKey(winreg.HKEY_LOCAL_MACHINE, REGISTRY)
    try:
        winreg.DeleteKey(winreg.HKEY_LOCAL_MACHINE, OWNER)
    except FileNotFoundError:
        pass
    # Windows removes the UMDF registration values but can leave empty keys.
    # Never delete values or unknown children belonging to another installation.
    empty_keys = [OWNER.rsplit('\\', 1)[0], REGISTRY.rsplit('\\', 1)[0]]
    if published:
        empty_keys += [UMDF_SERVICE + '\\Parameters', UMDF_SERVICE + '\\State', UMDF_SERVICE]
    for parent in empty_keys:
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, parent) as key:
                if winreg.QueryInfoKey(key)[:2] != (0, 0):
                    continue
            winreg.DeleteKey(winreg.HKEY_LOCAL_MACHINE, parent)
        except FileNotFoundError:
            pass
    return 0


def vdd_devices():
    rows = powershell_json("Get-CimInstance Win32_PnPEntity | Where-Object { "
        "$_.HardwareID -contains 'Root\\MttVDD' -or $_.HardwareID -contains 'MttVDD' } | "
        "Select-Object PNPDeviceID | ConvertTo-Json -Compress")
    return rows if isinstance(rows, list) else [rows] if rows else []


def elevated_main(remove_driver=False):
    if os.name != 'nt' or not C.windll.shell32.IsUserAnAdmin():
        return 5
    # Only the native installer launches the protected, machine-installed runtime.
    expected = install_folder()/'maintenance'/'GogglesBridgeDisplaySetup.exe'
    if not getattr(sys, 'frozen', False) or Path(sys.executable).resolve() != expected:
        return 5
    C.windll.kernel32.SetDllDirectoryW(None)
    os.chdir(install_folder())
    try:
        from .display_session import _process_lease
        with _process_lease():
            code = remove() if remove_driver else install()
        report = dict(success=True, code=code)
    except Exception as exc:
        code = 1
        report = dict(success=False, error=str(exc))
    (install_folder()/'display-install.json').write_text(json.dumps(report,ensure_ascii=False),encoding='utf-8')
    return code


def maintenance_main(argv):
    """Dispatch only fixed maintenance operations in the dedicated maintenance helper."""
    operations = {'install-display-driver': False, 'remove-display-driver': True}
    if len(argv) != 1 or argv[0] not in operations:
        return 64
    return elevated_main(remove_driver=operations[argv[0]])
