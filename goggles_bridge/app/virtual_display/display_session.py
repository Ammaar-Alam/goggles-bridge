"""Lease an owned virtual display for exactly the duration of a stream."""
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import threading

from . import displays, virtual_driver


_virtual_lease = threading.Lock()
LEASE_MUTEX = r'Global\GogglesBridge.VirtualDisplay.Lease'


def _lease_api():
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.ReleaseMutex.argtypes = [wintypes.HANDLE]
    kernel.ReleaseMutex.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    return kernel


@contextmanager
def _process_lease():
    """Exclude streams and driver maintenance across Windows sessions."""
    kernel = _lease_api()
    handle = kernel.CreateMutexW(None, False, LEASE_MUTEX)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    owned = False
    try:
        result = kernel.WaitForSingleObject(handle, 0)
        if result == 258:  # WAIT_TIMEOUT: another stream owns the display.
            raise displays.DisplayError('Another app is using the virtual display.')
        if result not in (0, 0x80):  # WAIT_OBJECT_0 or WAIT_ABANDONED.
            raise ctypes.WinError(ctypes.get_last_error())
        # An abandoned mutex transfers ownership after a crashed owner.
        # Reconfigure and verify the existing owned display before using it.
        owned = True
        yield
    finally:
        try:
            if owned:
                if not kernel.ReleaseMutex(handle):
                    raise ctypes.WinError(ctypes.get_last_error())
        finally:
            kernel.CloseHandle(handle)


@contextmanager
def lease(width=1920, height=1080, fps=60, emit=None):
    """Yield a selected display and release an owned extension on every exit.

    enable_extended owns rollback until it returns successfully. After that,
    even event callbacks and source validation are inside this cleanup scope.
    Use from the stream thread so display setup never blocks the UI thread.
    """
    if not _virtual_lease.acquire(blocking=False):
        raise displays.DisplayError('A stream already owns the virtual display.')
    try:
        with _process_lease():
            state = virtual_driver.status()
            if not state['owned']:
                raise displays.DisplayError('Install the virtual display driver in the setup guide before streaming.')
            if not state.get('config_current'):
                raise displays.DisplayError('Repair the virtual display driver in the setup guide.')
            target = displays.enable_extended(width, height, fps)
            failure = None
            try:
                yield target
            except BaseException as exc:
                failure = exc
                raise
            finally:
                try:
                    displays.disable_extended(expected=target)
                except Exception as exc:
                    if failure is None:
                        raise
                    if hasattr(failure, 'add_note'):
                        failure.add_note(f'Virtual display cleanup failed: {exc}')
                    if emit:
                        try:
                            emit('display_cleanup_error', message=str(exc))
                        except Exception:
                            pass
    finally:
        _virtual_lease.release()
