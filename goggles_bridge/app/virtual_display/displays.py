"""Windows display inventory and targeted layout changes, independent of the SDK."""
import ctypes as C
from ctypes import wintypes as W
import time

from . import display_config


class DisplayError(RuntimeError):
    pass


class DISPLAY_DEVICE(C.Structure):
    _fields_ = [('cb', W.DWORD), ('DeviceName', W.WCHAR*32), ('DeviceString', W.WCHAR*128),
                ('StateFlags', W.DWORD), ('DeviceID', W.WCHAR*128), ('DeviceKey', W.WCHAR*128)]


class DEVMODE(C.Structure):
    _fields_ = [('dmDeviceName', W.WCHAR*32), ('dmSpecVersion', W.WORD), ('dmDriverVersion', W.WORD),
                ('dmSize', W.WORD), ('dmDriverExtra', W.WORD), ('dmFields', W.DWORD),
                ('x', W.LONG), ('y', W.LONG), ('orientation', W.DWORD), ('fixedOutput', W.DWORD),
                ('dmColor', W.SHORT), ('dmDuplex', W.SHORT), ('dmYResolution', W.SHORT),
                ('dmTTOption', W.SHORT), ('dmCollate', W.SHORT), ('dmFormName', W.WCHAR*32),
                ('dmLogPixels', W.WORD), ('dmBitsPerPel', W.DWORD), ('dmPelsWidth', W.DWORD),
                ('dmPelsHeight', W.DWORD), ('dmDisplayFlags', W.DWORD), ('dmDisplayFrequency', W.DWORD),
                ('dmICMMethod', W.DWORD), ('dmICMIntent', W.DWORD), ('dmMediaType', W.DWORD),
                ('dmDitherType', W.DWORD), ('dmReserved1', W.DWORD), ('dmReserved2', W.DWORD),
                ('dmPanningWidth', W.DWORD), ('dmPanningHeight', W.DWORD)]


class MONITORINFO(C.Structure):
    _fields_ = [('cbSize', W.DWORD), ('rcMonitor', W.RECT), ('rcWork', W.RECT),
                ('dwFlags', W.DWORD), ('szDevice', W.WCHAR*32)]


def api():
    user = C.WinDLL('user32', use_last_error=True)
    user.EnumDisplayDevicesW.argtypes = [W.LPCWSTR, W.DWORD, C.POINTER(DISPLAY_DEVICE), W.DWORD]
    user.EnumDisplaySettingsExW.argtypes = [W.LPCWSTR, W.DWORD, C.POINTER(DEVMODE), W.DWORD]
    user.ChangeDisplaySettingsExW.argtypes = [W.LPCWSTR, C.POINTER(DEVMODE), W.HWND, W.DWORD, C.c_void_p]
    user.GetMonitorInfoW.argtypes = [W.HMONITOR, C.POINTER(MONITORINFO)]
    return user


def mode(device, number=0xFFFFFFFF):
    value = DEVMODE(dmSize=C.sizeof(DEVMODE))
    return value if api().EnumDisplaySettingsExW(device, number, C.byref(value), 0) else None


def inventory():
    user = api()
    handles = {}
    callback = C.WINFUNCTYPE(W.BOOL, W.HMONITOR, W.HDC, C.POINTER(W.RECT), W.LPARAM)

    @callback
    def monitor(handle, _dc, _rect, _data):
        info = MONITORINFO(cbSize=C.sizeof(MONITORINFO))
        if user.GetMonitorInfoW(handle, C.byref(info)):
            handles[info.szDevice] = int(handle)
        return True

    user.EnumDisplayMonitors.argtypes = [W.HDC, C.c_void_p, callback, W.LPARAM]
    user.EnumDisplayMonitors(None, None, monitor, 0)
    result = []
    index = 0
    while True:
        device = DISPLAY_DEVICE(cb=C.sizeof(DISPLAY_DEVICE))
        if not user.EnumDisplayDevicesW(None, index, C.byref(device), 0):
            break
        index += 1
        if device.StateFlags & 8:  # Legacy mirroring drivers are not displays.
            continue
        current = mode(device.DeviceName)
        monitor_device = DISPLAY_DEVICE(cb=C.sizeof(DISPLAY_DEVICE))
        user.EnumDisplayDevicesW(device.DeviceName, 0, C.byref(monitor_device), 0)
        result.append(dict(device=device.DeviceName, hardware_id=device.DeviceID,
            monitor_id=monitor_device.DeviceID, active=bool(device.StateFlags & 1),
            primary=bool(device.StateFlags & 4), hmonitor=handles.get(device.DeviceName),
            width=current.dmPelsWidth if current else 0, height=current.dmPelsHeight if current else 0,
            x=current.x if current else 0, y=current.y if current else 0,
            hz=current.dmDisplayFrequency if current else 0))
    return result


def virtual_display(rows=None):
    from .virtual_driver import owned_instance, _owns_configuration, _instance_registered
    instance = owned_instance()
    if not instance or not _owns_configuration() or not _instance_registered(instance):
        raise DisplayError('This app does not own the virtual display. Check the setup guide.')
    candidates = []
    for row in inventory() if rows is None else rows:
        if row['hardware_id'].lower() in ('root\\mttvdd', 'mttvdd') and row['monitor_id']:
            identity = display_config.identity(row['device'])
            if identity[0] == instance.casefold():
                candidates.append(dict(row, identity=identity))
    if len(candidates) != 1:
        raise DisplayError('Cannot find exactly one display owned by this app. Check the setup guide.')
    return candidates[0]


def select_source(width, height, hz, *, expected):
    target = virtual_display()
    if target['identity'] != expected['identity'] or target['monitor_id'] != expected['monitor_id']:
        raise DisplayError('The leased display was replaced. Start the stream again.')
    if not target['active'] or not target['hmonitor']:
        raise DisplayError('The virtual display is not active. Check the driver and try again.')
    if (target['width'], target['height'], target['hz']) != (width, height, hz) or target['primary']:
        raise DisplayError(f'The virtual display must be a secondary {width} × {height} / {hz} Hz display.')
    return target


def enable_extended(width=1920, height=1080, hz=60):
    if (width, height) not in ((1280, 720), (1920, 1080)) or hz not in (60, 75, 90, 100):
        raise DisplayError('Unsupported virtual display mode')
    before = inventory()
    target = virtual_display(before)
    if target['primary']:
        raise DisplayError('The virtual display is primary. Restore a physical primary display in Windows settings first.')
    active = [r for r in before if r['active'] and r['device'] != target['device']]
    original_modes = {r['device']: mode(r['device']) for r in before if r['active']}
    if not active:
        raise DisplayError('At least one physical display must remain active.')
    desired = None
    for index in range(1000):
        candidate = mode(target['device'], index)
        if candidate is None:
            break
        if (candidate.dmPelsWidth, candidate.dmPelsHeight, candidate.dmDisplayFrequency) == (width,height,hz):
            desired = candidate
            break
    if desired is None:
        raise DisplayError(f'The virtual driver does not provide {width} × {height} / {hz} Hz. Update the virtual display configuration.')
    desired.x = max(r['x']+r['width'] for r in active)
    primary = next((r for r in active if r['primary']), None)
    if primary is None:
        raise DisplayError('No physical primary display is available. The layout was not changed.')
    desired.y = primary['y']
    desired.dmFields |= 0x20 | 0x80000 | 0x100000 | 0x400000  # position, width, height, frequency
    rollback = None
    legacy_changed = False
    try:
        if target['active']:
            if display_config.identity(target['device']) != target['identity']:
                raise DisplayError('The virtual display was replaced. Its configuration was not changed.')
            status = api().ChangeDisplaySettingsExW(target['device'], C.byref(desired), None, 3, None)
            if status != 0:
                raise DisplayError(f'Windows rejected the virtual display mode ({status}).')
            legacy_changed = True
            status = api().ChangeDisplaySettingsExW(target['device'], C.byref(desired), None, 1, None)
            if status != 0:
                raise DisplayError(f'Windows could not enable the virtual display ({status}).')
        else:
            rollback = display_config.attach(target['device'], width, height, hz, desired.x, desired.y,
                                             target['identity'])
        deadline = time.monotonic()+8
        while time.monotonic() < deadline:
            after = inventory()
            for original in active:
                now = next((r for r in after if r['device'] == original['device']), {})
                if any(now.get(k) != original[k] for k in ('width','height','hz','x','y','primary','active')):
                    raise DisplayError('Windows changed the existing layout. A rollback was attempted; check display settings.')
            current = next((r for r in after if r['device'] == target['device']), {})
            if not current.get('active') or not current.get('hmonitor'):
                time.sleep(.2)
                continue
            result = select_source(width, height, hz, expected=target)
            return result
        raise DisplayError('The virtual display did not activate. Check Windows display settings.')
    except Exception as exc:
        if rollback is not None:
            try:
                rollback()
            except Exception as rollback_error:
                if hasattr(exc, 'add_note'):
                    exc.add_note(f'Display layout rollback failed: {rollback_error}')
            raise
        if not legacy_changed:
            raise
        if display_config.identity(target['device']) != target['identity']:
            raise DisplayError('The virtual display was replaced. Its layout was not restored.') from exc
        # Stage a rollback of the exact pre-change modes, then apply together.
        for device, original in original_modes.items():
            if original:
                api().ChangeDisplaySettingsExW(device, C.byref(original), None, 0x10000001, None)
        api().ChangeDisplaySettingsExW(None,None,None,0,None)
        raise


def disable_extended(*, expected):
    rows = inventory()
    if not any(r['monitor_id'] == expected['monitor_id'] and
               r['hardware_id'] == expected['hardware_id'] for r in rows):
        for row in rows:
            if (row['hardware_id'] == expected['hardware_id'] and row['monitor_id'] and
                    display_config.identity(row['device']) == expected['identity']):
                raise DisplayError('The leased monitor identity changed. Its replacement was not detached.')
        return  # The leased monitor was already removed; there is nothing to detach.
    target = virtual_display(rows)
    if target['identity'] != expected['identity'] or target['monitor_id'] != expected['monitor_id']:
        raise DisplayError('The leased display identity changed. The replacement display was not detached.')
    if target['primary'] or not any(r['active'] and r['device'] != target['device'] for r in rows):
        raise DisplayError('Cannot detach the primary or only active display.')
    if not target['active']:
        return
    if display_config.identity(target['device']) != expected['identity']:
        raise DisplayError('The leased display identity changed. The replacement display was not detached.')
    value = DEVMODE(dmSize=C.sizeof(DEVMODE), dmFields=0x20 | 0x80000 | 0x100000)
    status = api().ChangeDisplaySettingsExW(target['device'], C.byref(value), None, 1, None)
    if status != 0:
        raise DisplayError(f'Windows could not detach the virtual display ({status}).')
