"""Keep verified native installers immutable until their elevated process exits."""
from contextlib import ExitStack
import ctypes as C
from ctypes import wintypes as W
import hashlib
from pathlib import Path


TRUSTED = {'S-1-5-18', 'S-1-5-32-544',
           'S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464'}
WRITE_ACCESS = 0x500D0156  # generic write/all, delete, change owner/DACL, file/directory writes


def check_permissions(path):
    """Reject writable or user-owned runtime objects before executing any of them."""
    advapi, kernel = C.WinDLL('advapi32'), C.WinDLL('kernel32')
    advapi.GetNamedSecurityInfoW.argtypes = [W.LPWSTR, W.DWORD, W.DWORD] + [C.POINTER(C.c_void_p)]*5
    advapi.ConvertSidToStringSidW.argtypes = [C.c_void_p, C.POINTER(W.LPWSTR)]
    advapi.GetAce.argtypes = [C.c_void_p, W.DWORD, C.POINTER(C.c_void_p)]
    kernel.LocalFree.argtypes = [C.c_void_p]
    owner, group, dacl, sacl, descriptor = (C.c_void_p() for _ in range(5))

    def sid_text(sid):
        value = W.LPWSTR()
        if not sid or not advapi.ConvertSidToStringSidW(sid, C.byref(value)):
            raise RuntimeError(f'Cannot verify driver setup permissions: {path}')
        try:
            return value.value
        finally:
            kernel.LocalFree(value)

    result = advapi.GetNamedSecurityInfoW(str(path), 1, 5, C.byref(owner), C.byref(group),
                                         C.byref(dacl), C.byref(sacl), C.byref(descriptor))
    if result:
        raise C.WinError(result)
    try:
        if sid_text(owner) not in TRUSTED or not dacl:
            raise RuntimeError(f'Driver setup location is not protected: {path}')
        # ACL header: revision, reserved, size, ACE count, reserved (8 bytes).
        count = C.c_ushort.from_address(dacl.value + 4).value
        for index in range(count):
            ace = C.c_void_p()
            if not advapi.GetAce(dacl, index, C.byref(ace)):
                raise RuntimeError(f'Cannot read driver setup permissions: {path}')
            kind, flags = C.string_at(ace, 2)
            if kind == 1:  # Deny ACEs cannot grant write access.
                continue
            if kind != 0:
                raise RuntimeError(f'Unsupported driver setup permissions: {path}')
            mask = W.DWORD.from_address(ace.value + 4).value
            sid = sid_text(ace.value + 8)
            if mask & WRITE_ACCESS and sid not in TRUSTED:
                if sid == 'S-1-3-0' and flags & 8:  # Inherit-only CREATOR OWNER; parent is protected.
                    continue
                raise RuntimeError(f'Driver setup location allows untrusted writes: {path}')
    finally:
        kernel.LocalFree(descriptor)


def lock_program(program, digest=None, protected_folder=None):
    """Return locks held by the caller; hash bytes from the locked EXE handle."""
    if protected_folder is not None:
        folder = Path(protected_folder)
        if folder.parent.lstat().st_file_attributes & 0x400:
            raise RuntimeError(f'Driver setup cannot use a redirected path: {folder.parent}')
        check_permissions(folder.parent)
        def check_tree(path):
            if path.lstat().st_file_attributes & 0x400:
                raise RuntimeError(f'Driver setup cannot use a redirected path: {path}')
            check_permissions(path)
            if path.is_dir():
                for child in path.iterdir():
                    check_tree(child)
        check_tree(folder)
        # Protected paths cannot be replaced by an ordinary process. Do not lock
        # the uninstaller or its directory: it must be able to delete itself.
        return ExitStack()
    if not digest:
        raise RuntimeError('The driver installer has no embedded checksum. Rebuild the app.')
    program = Path(program).absolute()
    kernel = C.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [W.LPCWSTR, W.DWORD, W.DWORD, C.c_void_p, W.DWORD, W.DWORD, W.HANDLE]
    kernel.CreateFileW.restype = W.HANDLE
    kernel.CloseHandle.argtypes = [W.HANDLE]
    kernel.GetFileInformationByHandle.argtypes = [W.HANDLE, C.c_void_p]
    kernel.ReadFile.argtypes = [W.HANDLE, C.c_void_p, W.DWORD, C.POINTER(W.DWORD), C.c_void_p]
    locks = ExitStack()
    try:
        # Lock ancestors too: ShellExecute must resolve the same path after hashing.
        for path in (*reversed(program.parents), program):
            handle = kernel.CreateFileW(str(path), 0x80000000, 1, None, 3, 0x02200000, None)
            if handle == C.c_void_p(-1).value:
                raise C.WinError(C.get_last_error())
            locks.callback(kernel.CloseHandle, handle)
            info = C.create_string_buffer(52)
            if not kernel.GetFileInformationByHandle(handle, info):
                raise C.WinError(C.get_last_error())
            if int.from_bytes(info.raw[:4], 'little') & 0x400:
                raise RuntimeError(f'Driver setup cannot use a redirected path: {path}')
        actual = hashlib.sha256()
        block, length = C.create_string_buffer(1024*1024), W.DWORD()
        while True:
            if not kernel.ReadFile(handle, block, len(block), C.byref(length), None):
                raise C.WinError(C.get_last_error())
            if not length.value:
                break
            actual.update(block.raw[:length.value])
        if actual.hexdigest() != digest:
            raise RuntimeError('The driver installer changed. Reinstall Goggles Bridge before continuing.')
        return locks
    except BaseException:
        locks.close()
        raise
