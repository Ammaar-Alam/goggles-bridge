"""Targeted CCD attachment for a detached indirect display on Windows 10+.

Legacy ChangeDisplaySettingsEx can reject an IDD after its path was detached.
Keep every active CCD path and mode, adding only the requested GDI source.
Never use SDC_TOPOLOGY_EXTEND or SDC_ALLOW_CHANGES: either could change other
monitors. Windows validates the exact supplied configuration before applying.
"""
import ctypes as C
from ctypes import wintypes as W


class LUID(C.Structure):
    _fields_ = [('LowPart', W.DWORD), ('HighPart', W.LONG)]


class RATIO(C.Structure):
    _fields_ = [('Numerator', W.UINT), ('Denominator', W.UINT)]


class SOURCE(C.Structure):
    _fields_ = [('adapterId', LUID), ('id', W.UINT), ('modeInfoIdx', W.UINT), ('statusFlags', W.UINT)]


class TARGET(C.Structure):
    _fields_ = [('adapterId', LUID), ('id', W.UINT), ('modeInfoIdx', W.UINT),
                ('outputTechnology', W.UINT), ('rotation', W.UINT), ('scaling', W.UINT),
                ('refreshRate', RATIO), ('scanLineOrdering', W.UINT),
                ('targetAvailable', W.BOOL), ('statusFlags', W.UINT)]


class PATH(C.Structure):
    _fields_ = [('sourceInfo', SOURCE), ('targetInfo', TARGET), ('flags', W.UINT)]


class SOURCE_MODE(C.Structure):
    _fields_ = [('width', W.UINT), ('height', W.UINT), ('pixelFormat', W.UINT), ('position', W.POINT)]


class MODES(C.Union):
    # Preserve opaque target signal timings byte-for-byte, including uint64 alignment.
    _fields_ = [('sourceMode', SOURCE_MODE), ('raw', C.c_ulonglong * 6)]


class MODE(C.Structure):
    _anonymous_ = ['value']
    _fields_ = [('infoType', W.UINT), ('id', W.UINT), ('adapterId', LUID), ('value', MODES)]


class HEADER(C.Structure):
    _fields_ = [('type', W.UINT), ('size', W.UINT), ('adapterId', LUID), ('id', W.UINT)]


class NAME(C.Structure):
    _fields_ = [('header', HEADER), ('name', W.WCHAR * 32)]


class ADAPTER_NAME(C.Structure):
    _fields_ = [('header', HEADER), ('path', W.WCHAR * 128)]


def api():
    user = C.WinDLL('user32', use_last_error=True)
    user.GetDisplayConfigBufferSizes.argtypes = [W.UINT, C.POINTER(W.UINT), C.POINTER(W.UINT)]
    user.QueryDisplayConfig.argtypes = [W.UINT, C.POINTER(W.UINT), C.POINTER(PATH),
                                       C.POINTER(W.UINT), C.POINTER(MODE), C.c_void_p]
    user.DisplayConfigGetDeviceInfo.argtypes = [C.POINTER(HEADER)]
    user.SetDisplayConfig.argtypes = [W.UINT, C.POINTER(PATH), W.UINT, C.POINTER(MODE), W.UINT]
    return user


def query():
    user = api()
    for _ in range(3):  # Hotplug can invalidate the sizes between these calls.
        count, mode_count = W.UINT(), W.UINT()
        code = user.GetDisplayConfigBufferSizes(1, C.byref(count), C.byref(mode_count))
        if code:
            raise RuntimeError(f'Windows could not read display paths ({code}).')
        paths, modes = (PATH * count.value)(), (MODE * mode_count.value)()
        code = user.QueryDisplayConfig(1, C.byref(count), paths, C.byref(mode_count), modes, None)
        if code == 122:  # ERROR_INSUFFICIENT_BUFFER
            continue
        if code:
            raise RuntimeError(f'Windows could not read display configuration ({code}).')
        # QDC_ALL_PATHS without QDC_VIRTUAL_MODE_AWARE uses full modeInfoIdx fields.
        return list(paths)[:count.value], list(modes)[:mode_count.value]
    raise RuntimeError('The display configuration is changing. Try again.')


def source_name(path):
    value = NAME(header=HEADER(type=1, size=C.sizeof(NAME),
        adapterId=path.sourceInfo.adapterId, id=path.sourceInfo.id))
    code = api().DisplayConfigGetDeviceInfo(C.byref(value.header))
    return value.name if code == 0 else None


def configure(paths, modes, flags):
    code = api().SetDisplayConfig(len(paths), (PATH * len(paths))(*paths),
                                 len(modes), (MODE * len(modes))(*modes), flags)
    if code:
        raise RuntimeError(f'Windows could not apply the virtual display configuration ({code}).')


def path_identity(path):
    """Resolve the display adapter interface to its actual PnP device instance."""
    from .virtual_driver import GUID, DEVINFO
    name = ADAPTER_NAME(header=HEADER(type=4, size=C.sizeof(ADAPTER_NAME),
                                     adapterId=path.targetInfo.adapterId))
    code = api().DisplayConfigGetDeviceInfo(C.byref(name.header))
    if code:
        raise RuntimeError(f'Windows could not identify the display adapter ({code}).')
    setup = C.WinDLL('setupapi', use_last_error=True)
    setup.SetupDiCreateDeviceInfoList.argtypes = [C.POINTER(GUID), W.HWND]
    setup.SetupDiCreateDeviceInfoList.restype = W.HANDLE
    # SP_DEVICE_INTERFACE_DATA and SP_DEVINFO_DATA share the same ABI layout.
    setup.SetupDiOpenDeviceInterfaceW.argtypes = [W.HANDLE, W.LPCWSTR, W.DWORD, C.POINTER(DEVINFO)]
    setup.SetupDiGetDeviceInterfaceDetailW.argtypes = [W.HANDLE, C.POINTER(DEVINFO), C.c_void_p,
                                                    W.DWORD, C.POINTER(W.DWORD), C.POINTER(DEVINFO)]
    setup.SetupDiGetDeviceInstanceIdW.argtypes = [W.HANDLE, C.POINTER(DEVINFO), W.LPWSTR,
                                               W.DWORD, C.POINTER(W.DWORD)]
    setup.SetupDiDestroyDeviceInfoList.argtypes = [W.HANDLE]
    handle = setup.SetupDiCreateDeviceInfoList(None, None)
    if handle in (None, C.c_void_p(-1).value):
        raise C.WinError(C.get_last_error())
    try:
        interface = DEVINFO(cbSize=C.sizeof(DEVINFO))
        info = DEVINFO(cbSize=C.sizeof(DEVINFO))
        if not setup.SetupDiOpenDeviceInterfaceW(handle, name.path, 0, C.byref(interface)):
            raise C.WinError(C.get_last_error())
        # This documented size-query fills info even though the detail buffer is absent.
        if not setup.SetupDiGetDeviceInterfaceDetailW(handle, C.byref(interface), None, 0, None, C.byref(info)):
            if C.get_last_error() != 122:
                raise C.WinError(C.get_last_error())
        instance = C.create_unicode_buffer(512)
        if not setup.SetupDiGetDeviceInstanceIdW(handle, C.byref(info), instance, len(instance), None):
            raise C.WinError(C.get_last_error())
        return instance.value.casefold(), path.targetInfo.id
    finally:
        setup.SetupDiDestroyDeviceInfoList(handle)


def identity(device):
    paths, _ = query()
    matches = {path_identity(p) for p in paths if p.targetInfo.targetAvailable and source_name(p) == device}
    if len(matches) != 1:
        raise RuntimeError('Cannot uniquely identify the virtual display device.')
    return matches.pop()


def attach(device, width, height, hz, x, y, expected):
    """Attach only device; return a rollback callable for post-change verification."""
    paths, modes = query()
    active = [p for p in paths if p.flags & 1]
    candidates = [p for p in paths if p.targetInfo.targetAvailable and source_name(p) == device]
    if not active or len(candidates) != 1 or candidates[0].flags & 1:
        raise RuntimeError('Cannot uniquely identify the detached virtual display. Refresh display settings and try again.')
    target = PATH.from_buffer_copy(candidates[0])
    if path_identity(target) != expected:
        raise RuntimeError('The virtual display was replaced. Its configuration was not changed.')
    target.flags |= 1
    target.sourceInfo.modeInfoIdx = len(modes)
    target.targetInfo.modeInfoIdx = 0xFFFFFFFF
    target.targetInfo.refreshRate = RATIO(hz, 1)
    target.targetInfo.scanLineOrdering = 1  # Progressive.
    new_mode = MODE(infoType=1, id=target.sourceInfo.id, adapterId=target.sourceInfo.adapterId)
    new_mode.sourceMode = SOURCE_MODE(width, height, 4, W.POINT(x, y))  # 32 bpp.
    proposed_paths, proposed_modes = [*active, target], [*modes, new_mode]
    configure(proposed_paths, proposed_modes, 0x20 | 0x40)  # USE_SUPPLIED | VALIDATE

    def rollback():
        if identity(device) != expected:
            raise RuntimeError('The virtual display was replaced. Its layout was not restored.')
        configure(active, modes, 0x20 | 0x80 | 0x200)  # USE_SUPPLIED | APPLY | SAVE_TO_DATABASE

    try:
        configure(proposed_paths, proposed_modes, 0x20 | 0x80 | 0x200)
    except Exception as exc:
        try:
            rollback()
        except Exception as rollback_error:
            if hasattr(exc, 'add_note'):
                exc.add_note(f'Display layout rollback failed: {rollback_error}')
        raise
    return rollback
