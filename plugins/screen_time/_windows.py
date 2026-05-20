"""Windows foreground application reader via Win32 APIs.

Pure ``ctypes`` implementation so the plugin has zero extra dependencies on
Windows. Returns the executable path as the stable identifier and the EXE's
``FileDescription`` (falling back to the basename without ``.exe``) as the
display name.
"""
from __future__ import annotations

import logging
import os
import sys
from typing import Optional

logger = logging.getLogger(__name__)

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_DEFAULT_PATH_BUFFER = 1024


def read_foreground() -> Optional[tuple[str, str]]:
    """Return ``(exe_path, app_name)`` for the foreground Win32 window.

    Returns ``None`` if no foreground window is currently active (e.g. when
    the secure desktop owns input during UAC prompts).
    """
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return None

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD

    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL

    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None

    pid = wintypes.DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return None

    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not handle:
        return None

    try:
        buffer = ctypes.create_unicode_buffer(_DEFAULT_PATH_BUFFER)
        size = wintypes.DWORD(len(buffer))
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return None
        exe_path = buffer.value
    finally:
        kernel32.CloseHandle(handle)

    if not exe_path:
        return None

    display = _read_file_description(exe_path) or _basename_without_extension(exe_path)
    return exe_path, display


def _basename_without_extension(exe_path: str) -> str:
    basename = os.path.basename(exe_path)
    if basename.lower().endswith(".exe"):
        return basename[:-4]
    return basename


def _read_file_description(exe_path: str) -> Optional[str]:
    """Read ``FileDescription`` (with fallback to ``ProductName``) from the EXE."""
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return None

    try:
        version_dll = ctypes.WinDLL("version", use_last_error=True)
    except OSError:
        return None

    version_dll.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
    version_dll.GetFileVersionInfoSizeW.restype = wintypes.DWORD
    version_dll.GetFileVersionInfoW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
    ]
    version_dll.GetFileVersionInfoW.restype = wintypes.BOOL
    version_dll.VerQueryValueW.argtypes = [
        ctypes.c_void_p,
        wintypes.LPCWSTR,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_uint),
    ]
    version_dll.VerQueryValueW.restype = wintypes.BOOL

    dummy = wintypes.DWORD(0)
    size = version_dll.GetFileVersionInfoSizeW(exe_path, ctypes.byref(dummy))
    if not size:
        return None

    buffer = ctypes.create_string_buffer(size)
    if not version_dll.GetFileVersionInfoW(exe_path, 0, size, buffer):
        return None

    translations_ptr = ctypes.c_void_p()
    translations_len = ctypes.c_uint(0)
    if not version_dll.VerQueryValueW(
        buffer,
        "\\VarFileInfo\\Translation",
        ctypes.byref(translations_ptr),
        ctypes.byref(translations_len),
    ):
        return None
    if not translations_ptr.value or translations_len.value < 4:
        return None

    pair_count = translations_len.value // 4
    pair_array = (ctypes.c_uint16 * (pair_count * 2)).from_address(translations_ptr.value)
    translations: list[tuple[int, int]] = []
    for index in range(pair_count):
        language = pair_array[index * 2]
        codepage = pair_array[index * 2 + 1]
        translations.append((language, codepage))
    translations.sort(key=lambda pair: 0 if pair == (0x0409, 0x04B0) else 1)

    for key in ("FileDescription", "ProductName"):
        for language, codepage in translations:
            sub_block = f"\\StringFileInfo\\{language:04x}{codepage:04x}\\{key}"
            value_ptr = ctypes.c_void_p()
            value_len = ctypes.c_uint(0)
            if not version_dll.VerQueryValueW(
                buffer,
                sub_block,
                ctypes.byref(value_ptr),
                ctypes.byref(value_len),
            ):
                continue
            if not value_ptr.value or value_len.value <= 1:
                continue
            value = ctypes.wstring_at(value_ptr.value, value_len.value - 1).strip()
            if value:
                return value
    return None
