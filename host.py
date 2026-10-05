#!/usr/bin/env python3
"""LiteView host - run this on the computer you want to control.

The controlling computer just opens http://<this-computer-ip>:<port> in a browser.
"""
import argparse
import asyncio
import hashlib
import hmac
import io
import ipaddress
import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from aiohttp import WSMsgType, web
from PIL import Image
from pynput.keyboard import Controller as KeyboardController
from pynput.keyboard import Key, KeyCode
from pynput.mouse import Button
from pynput.mouse import Controller as MouseController

# When bundled by PyInstaller, data files are extracted to sys._MEIPASS.
# At dev time, they sit next to host.py.
if getattr(sys, 'frozen', False):
    HERE = Path(sys._MEIPASS)
else:
    HERE = Path(__file__).resolve().parent

# Optional: thirdeye captures protected windows (WDA_EXCLUDEFROMCAPTURE).
# Try the installed wheel first; fall back to the local module_2 copy.
try:
    import eye3 as _eye3
except ImportError:
    try:
        sys.path.insert(0, str(HERE / "module_2" / "thirdeye" / "python"))
        import eye3 as _eye3
    except ImportError:
        _eye3 = None

# dxcam uses DXGI Desktop Duplication, which captures GPU-composited frames
# (including hardware-decoded video like Netflix) — something BitBlt cannot do.
# We use it alongside thirdeye: thirdeye clears WDA_EXCLUDEFROMCAPTURE, dxcam
# captures during that window.
try:
    import dxcam as _dxcam
except ImportError:
    _dxcam = None
PASSWORD_FILE = Path.home() / ".liteview_password"
LOG_FILE      = Path.home() / ".liteview.log"

# ---------------------------------------------------------------- DWM hook reader
# When liteview_dwm_hook.dll is injected into dwm.exe it writes every composited
# frame to a named shared-memory mapping BEFORE the GPU driver applies the
# WDA_EXCLUDEFROMCAPTURE black-out.  We read those frames here.

_SHM_NAME       = "Global\\LiteViewFrame"
_SHM_TOTAL_SIZE = 64 + 3840 * 2160 * 4   # header (64 B) + worst-case 4K BGRA
_PIXFMT_BGRA8   = 0
_PIXFMT_RGBA8   = 1
_PIXFMT_RGB10A2 = 2

class _DWMReader:
    """Reads frames from the shared memory written by liteview_dwm_hook.dll."""
    def __init__(self):
        import ctypes, ctypes.wintypes
        k32 = ctypes.windll.kernel32
        self._k32 = k32
        FILE_MAP_READ = 0x0004

        # Fix 64-bit pointer truncation
        k32.OpenFileMappingW.restype = ctypes.wintypes.HANDLE
        k32.MapViewOfFile.restype = ctypes.c_void_p
        k32.MapViewOfFile.argtypes = [
            ctypes.wintypes.HANDLE, ctypes.wintypes.DWORD,
            ctypes.wintypes.DWORD, ctypes.wintypes.DWORD, ctypes.c_size_t,
        ]

        self._hMap = k32.OpenFileMappingW(FILE_MAP_READ, False, _SHM_NAME)
        if not self._hMap:
            raise OSError("DWM hook shared memory not found")
        self._ptr = k32.MapViewOfFile(self._hMap, FILE_MAP_READ, 0, 0, _SHM_TOTAL_SIZE)
        if not self._ptr:
            k32.CloseHandle(self._hMap)
            raise OSError("Failed to map DWM shared memory")
        self._last_frame = 0

    def grab(self):
        """Return (width, height, format_code, bytes) or None if no new frame."""
        import ctypes
        base = self._ptr
        # Read header fields (offsets match FrameHeader in shared.h)
        magic    = ctypes.c_uint32.from_address(base +  0).value
        width    = ctypes.c_uint32.from_address(base +  4).value
        height   = ctypes.c_uint32.from_address(base +  8).value
        fmt      = ctypes.c_uint32.from_address(base + 12).value
        frameNum = ctypes.c_uint64.from_address(base + 16).value
        ready    = ctypes.c_uint32.from_address(base + 24).value

        if magic != 0x4C564448 or not ready or frameNum == self._last_frame:
            return None
        if width == 0 or height == 0:
            return None
        pixel_bytes = width * height * 4
        data = (ctypes.c_uint8 * pixel_bytes).from_address(base + 64)
        result = (width, height, fmt, bytes(data))
        self._last_frame = frameNum
        return result

    def close(self):
        if self._ptr:
            self._k32.UnmapViewOfFile(self._ptr)
            self._ptr = 0
        if self._hMap:
            self._k32.CloseHandle(self._hMap)
            self._hMap = None


_dwm_reader: "_DWMReader | None" = None


def _enable_debug_privilege() -> bool:
    """Enable SeDebugPrivilege so OpenProcess can open system processes like dwm.exe.
    The privilege is present in admin tokens but disabled by default."""
    try:
        import ctypes, ctypes.wintypes
        advapi32 = ctypes.windll.advapi32
        k32 = ctypes.windll.kernel32
        TOKEN_ADJUST_PRIVILEGES = 0x0020
        TOKEN_QUERY = 0x0008
        SE_PRIVILEGE_ENABLED = 0x00000002

        class _LUID(ctypes.Structure):
            _fields_ = [("LowPart", ctypes.wintypes.DWORD), ("HighPart", ctypes.c_long)]

        class _LUID_ATTR(ctypes.Structure):
            _fields_ = [("Luid", _LUID), ("Attributes", ctypes.wintypes.DWORD)]

        class _TOKEN_PRIVS(ctypes.Structure):
            _fields_ = [("PrivilegeCount", ctypes.wintypes.DWORD),
                        ("Privileges", _LUID_ATTR * 1)]

        hTok = ctypes.wintypes.HANDLE()
        if not advapi32.OpenProcessToken(k32.GetCurrentProcess(),
                                         TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY,
                                         ctypes.byref(hTok)):
            return False
        luid = _LUID()
        advapi32.LookupPrivilegeValueW(None, "SeDebugPrivilege", ctypes.byref(luid))
        tp = _TOKEN_PRIVS()
        tp.PrivilegeCount = 1
        tp.Privileges[0].Luid = luid
        tp.Privileges[0].Attributes = SE_PRIVILEGE_ENABLED
        advapi32.AdjustTokenPrivileges(hTok, False, ctypes.byref(tp),
                                       ctypes.sizeof(tp), None, None)
        k32.CloseHandle(hTok)
        return True
    except Exception:
        return False


def _init_dwm_hook(dll_path: Path) -> bool:
    """Inject liteview_dwm_hook.dll into dwm.exe, then patch the DXGI vtable
    from Python (D3D11CreateDevice deadlocks inside dwm.exe)."""
    global _dwm_reader
    if not dll_path.exists():
        print(f"[!] DWM hook: DLL not found at {dll_path}", flush=True)
        return False

    _enable_debug_privilege()

    # Find dwm.exe PID.
    try:
        out = subprocess.check_output(
            ["tasklist", "/FI", "IMAGENAME eq dwm.exe", "/FO", "CSV", "/NH"],
            text=True, timeout=5, stderr=subprocess.DEVNULL,
        )
    except Exception as exc:
        print(f"[!] DWM hook: tasklist failed: {exc}", flush=True)
        return False
    dwm_pid = None
    for line in out.splitlines():
        parts = line.split(",")
        if len(parts) >= 2 and "dwm" in parts[0].lower():
            try:
                dwm_pid = int(parts[1].strip('"'))
                break
            except ValueError:
                pass
    if not dwm_pid:
        print("[!] DWM hook: dwm.exe not found in tasklist", flush=True)
        return False

    print(f"[*] DWM hook: injecting into dwm.exe (PID {dwm_pid}) ...", flush=True)
    hmod = _inject_dll(dwm_pid, dll_path)
    if not hmod:
        print("[!] DWM hook: injection failed", flush=True)
        return False

    # Give DllMain time to set up shared memory.
    time.sleep(0.5)

    # Patch the DXGI vtable from Python.
    if not _patch_dxgi_vtable(dwm_pid, dll_path, hmod):
        print("[!] DWM hook: vtable patching failed", flush=True)
        return False

    time.sleep(0.5)
    try:
        _dwm_reader = _DWMReader()
        print("[+] DWM hook: active — all windows capturable (WDA bypassed at compositor level)", flush=True)
        return True
    except OSError as exc:
        print(f"[!] DWM hook: shared memory not ready: {exc}", flush=True)
        return False


def _get_dll_export_rva(dll_path: Path, export_name: str) -> int:
    """Parse PE exports to find the RVA of an exported symbol."""
    import struct
    with open(dll_path, "rb") as f:
        data = f.read()
    # PE header
    pe_off = struct.unpack_from("<I", data, 0x3C)[0]
    # Optional header offset
    opt_off = pe_off + 24
    # Number of sections
    num_sections = struct.unpack_from("<H", data, pe_off + 6)[0]
    opt_size = struct.unpack_from("<H", data, pe_off + 20)[0]
    sections_off = opt_off + opt_size
    # Export directory RVA (data directory[0])
    export_rva = struct.unpack_from("<I", data, opt_off + 112)[0]
    export_size = struct.unpack_from("<I", data, opt_off + 116)[0]
    if export_rva == 0:
        return 0
    # RVA to file offset using section table
    def rva_to_offset(rva):
        for i in range(num_sections):
            s = sections_off + i * 40
            vaddr = struct.unpack_from("<I", data, s + 12)[0]
            vsize = struct.unpack_from("<I", data, s + 8)[0]
            raw = struct.unpack_from("<I", data, s + 20)[0]
            if vaddr <= rva < vaddr + max(vsize, struct.unpack_from("<I", data, s + 16)[0]):
                return rva - vaddr + raw
        return rva
    eo = rva_to_offset(export_rva)
    num_names = struct.unpack_from("<I", data, eo + 24)[0]
    names_rva = struct.unpack_from("<I", data, eo + 32)[0]
    ordinals_rva = struct.unpack_from("<I", data, eo + 36)[0]
    funcs_rva = struct.unpack_from("<I", data, eo + 28)[0]
    names_off = rva_to_offset(names_rva)
    ordinals_off = rva_to_offset(ordinals_rva)
    funcs_off = rva_to_offset(funcs_rva)
    target = export_name.encode("ascii")
    for i in range(num_names):
        name_rva = struct.unpack_from("<I", data, names_off + i * 4)[0]
        name_off = rva_to_offset(name_rva)
        # Read null-terminated string
        end = data.index(b"\x00", name_off)
        name = data[name_off:end]
        if name == target:
            ordinal = struct.unpack_from("<H", data, ordinals_off + i * 2)[0]
            func_rva = struct.unpack_from("<I", data, funcs_off + ordinal * 4)[0]
            return func_rva
    return 0


def _patch_dxgi_vtable(dwm_pid: int, dll_path: Path, dll_base: int) -> bool:
    """Inline-hook DXGI Present/Present1 inside dwm.exe.

    Instead of patching the vtable (which DWM may not use), we overwrite the
    first bytes of the actual Present function code with a JMP to our hook.
    A trampoline (saved original bytes + JMP back) lets the hook call the
    original function.
    """
    import ctypes
    import ctypes.wintypes
    import struct

    k32 = ctypes.windll.kernel32
    HANDLE = ctypes.wintypes.HANDLE
    LPVOID = ctypes.c_void_p
    DWORD  = ctypes.wintypes.DWORD
    SIZE_T = ctypes.c_size_t

    k32.OpenProcess.argtypes = [DWORD, ctypes.wintypes.BOOL, DWORD]
    k32.OpenProcess.restype = HANDLE
    k32.ReadProcessMemory.argtypes = [HANDLE, LPVOID, LPVOID, SIZE_T, ctypes.POINTER(SIZE_T)]
    k32.ReadProcessMemory.restype = ctypes.wintypes.BOOL
    k32.WriteProcessMemory.argtypes = [HANDLE, LPVOID, ctypes.c_char_p, SIZE_T, ctypes.POINTER(SIZE_T)]
    k32.WriteProcessMemory.restype = ctypes.wintypes.BOOL
    k32.VirtualProtectEx.argtypes = [HANDLE, LPVOID, SIZE_T, DWORD, ctypes.POINTER(DWORD)]
    k32.VirtualProtectEx.restype = ctypes.wintypes.BOOL
    k32.CloseHandle.argtypes = [HANDLE]

    # --- Step 1: Run vtable_finder.exe to get Present function addresses ---
    finder_exe = dll_path.parent / "vtable_finder.exe"
    if not finder_exe.exists():
        finder_exe = HERE / "module_4" / "dwm-hook" / "build" / "vtable_finder.exe"
    if not finder_exe.exists():
        print(f"[!] DWM hook: vtable_finder.exe not found", flush=True)
        return False

    print("[*] DWM hook: running vtable_finder.exe to discover Present addresses...", flush=True)
    try:
        out = subprocess.check_output([str(finder_exe)], text=True, timeout=10,
                                      stderr=subprocess.PIPE)
    except subprocess.CalledProcessError as exc:
        print(f"[!] DWM hook: vtable_finder failed: {exc.stderr}", flush=True)
        return False

    present_addr = 0
    present1_addr = 0
    for line in out.strip().splitlines():
        key, _, val = line.partition("=")
        if key in ("PRESENT", "PRESENT1") and val:
            try:
                addr = int(val, 16)
            except ValueError:
                continue
            if key == "PRESENT":
                present_addr = addr
            elif key == "PRESENT1":
                present1_addr = addr

    if not present_addr or not present1_addr:
        print(f"[!] DWM hook: vtable_finder parse failed: {out}", flush=True)
        return False

    print(f"[*] DWM hook: Present  @ 0x{present_addr:016X}", flush=True)
    print(f"[*] DWM hook: Present1 @ 0x{present1_addr:016X}", flush=True)

    # --- Step 2: Find hook & trampoline addresses in DWM's address space ---
    rva_hooked_present  = _get_dll_export_rva(dll_path, "HookedPresent")
    rva_hooked_present1 = _get_dll_export_rva(dll_path, "HookedPresent1")
    rva_trampoline0     = _get_dll_export_rva(dll_path, "g_trampoline0")
    rva_trampoline1     = _get_dll_export_rva(dll_path, "g_trampoline1")

    if not all([rva_hooked_present, rva_hooked_present1, rva_trampoline0, rva_trampoline1]):
        print(f"[!] DWM hook: failed to find exports in DLL", flush=True)
        return False

    addr_hooked_present  = dll_base + rva_hooked_present
    addr_hooked_present1 = dll_base + rva_hooked_present1
    addr_trampoline0     = dll_base + rva_trampoline0
    addr_trampoline1     = dll_base + rva_trampoline1

    print(f"[*] DWM hook: HookedPresent  @ 0x{addr_hooked_present:016X}", flush=True)
    print(f"[*] DWM hook: HookedPresent1 @ 0x{addr_hooked_present1:016X}", flush=True)

    # --- Step 3: Build trampolines and install inline hooks ---
    # x64 absolute JMP: FF 25 00 00 00 00 [8-byte address] = 14 bytes
    STOLEN_BYTES = 14  # we overwrite 14 bytes at the start of Present

    def build_jmp(target_addr):
        """Build a 14-byte absolute JMP to target_addr."""
        return b"\xFF\x25\x00\x00\x00\x00" + struct.pack("<Q", target_addr)

    h = k32.OpenProcess(0x001FFFFF, False, dwm_pid)
    if not h:
        print(f"[!] DWM hook: OpenProcess failed: {ctypes.GetLastError()}", flush=True)
        return False
    try:
        written = SIZE_T(0)
        old_prot = DWORD(0)

        for i, (fn_addr, hook_addr, tramp_addr, name) in enumerate([
            (present_addr,  addr_hooked_present,  addr_trampoline0, "Present"),
            (present1_addr, addr_hooked_present1, addr_trampoline1, "Present1"),
        ]):
            # 1. Read original bytes from the function
            orig_bytes = (ctypes.c_byte * STOLEN_BYTES)()
            k32.ReadProcessMemory(h, fn_addr, ctypes.byref(orig_bytes), STOLEN_BYTES,
                                  ctypes.byref(written))
            saved = bytes(orig_bytes)
            print(f"[*] DWM hook: {name} original bytes: {saved.hex()}", flush=True)

            # Detect stale hook from a previous run
            if saved[:2] == b"\xFF\x25":
                print(f"[*] DWM hook: {name} already hooked (stale JMP), skipping", flush=True)
                continue

            # 2. Build trampoline: saved bytes + JMP back to (fn_addr + STOLEN_BYTES)
            jmp_back = build_jmp(fn_addr + STOLEN_BYTES)
            trampoline = saved + jmp_back
            # Make trampoline memory executable
            k32.VirtualProtectEx(h, tramp_addr, len(trampoline), 0x40, ctypes.byref(old_prot))
            k32.WriteProcessMemory(h, tramp_addr, trampoline, len(trampoline),
                                   ctypes.byref(written))
            print(f"[*] DWM hook: {name} trampoline written @ 0x{tramp_addr:016X}", flush=True)

            # 3. Overwrite function start with JMP to our hook
            jmp_to_hook = build_jmp(hook_addr)
            k32.VirtualProtectEx(h, fn_addr, STOLEN_BYTES, 0x40, ctypes.byref(old_prot))
            ok = k32.WriteProcessMemory(h, fn_addr, jmp_to_hook, STOLEN_BYTES,
                                        ctypes.byref(written))
            k32.VirtualProtectEx(h, fn_addr, STOLEN_BYTES, old_prot.value,
                                 ctypes.byref(old_prot))
            if not ok:
                print(f"[!] DWM hook: WriteProcessMemory FAILED for {name}: err={ctypes.GetLastError()}", flush=True)
                return False

            # Flush instruction cache
            k32.FlushInstructionCache.argtypes = [HANDLE, LPVOID, SIZE_T]
            k32.FlushInstructionCache(h, fn_addr, STOLEN_BYTES)

            # 4. Verify: read back the patched bytes
            verify = (ctypes.c_byte * STOLEN_BYTES)()
            k32.ReadProcessMemory(h, fn_addr, ctypes.byref(verify), STOLEN_BYTES,
                                  ctypes.byref(written))
            patched = bytes(verify)
            expected = jmp_to_hook
            if patched == expected:
                print(f"[+] DWM hook: {name} inline hook VERIFIED ✓", flush=True)
            else:
                print(f"[!] DWM hook: {name} VERIFY FAILED! expected={expected.hex()} got={patched.hex()}", flush=True)
                return False

        print(f"[+] DWM hook: all hooks active!", flush=True)
    finally:
        k32.CloseHandle(h)

    return True

try:
    from module_2.thirdeye import ThirdEyeModule
except Exception:  # pragma: no cover - optional integration module
    ThirdEyeModule = None

# ---------------------------------------------------------------- capture-bypass integration
# capture-bypass (module_3) injects a persistent DLL into browser processes that
# calls SetWindowDisplayAffinity(WDA_NONE) every 500 ms, permanently clearing
# WDA_EXCLUDEFROMCAPTURE so DXGI can see protected windows (e.g. Netflix in Chrome).
# It handles Chrome's multi-process architecture by injecting all child PIDs.

def _get_protected_pids():
    """Return PIDs of all processes that own a WDA-protected window."""
    try:
        import ctypes
        import ctypes.wintypes
        user32 = ctypes.windll.user32
    except Exception:
        return set()

    protected_pids = set()

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
    def _cb(hwnd, _):
        affinity = ctypes.c_uint(0)
        user32.GetWindowDisplayAffinity(hwnd, ctypes.byref(affinity))
        if affinity.value != 0:
            pid = ctypes.c_uint(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value:
                protected_pids.add(pid.value)
        return True

    user32.EnumWindows(_cb, 0)
    return protected_pids


def _inject_dll(pid: int, dll_path: Path) -> int:
    """Inject dll_path into process pid via LoadLibrary remote thread.
    Returns the HMODULE (base address) of the loaded DLL, or 0 on failure."""
    try:
        import ctypes
        import ctypes.wintypes
        import struct
        k32 = ctypes.windll.kernel32

        HANDLE = ctypes.wintypes.HANDLE
        LPVOID = ctypes.c_void_p
        DWORD  = ctypes.wintypes.DWORD
        SIZE_T = ctypes.c_size_t
        BOOL   = ctypes.wintypes.BOOL

        k32.OpenProcess.argtypes = [DWORD, BOOL, DWORD]
        k32.OpenProcess.restype = HANDLE
        k32.VirtualAllocEx.argtypes = [HANDLE, LPVOID, SIZE_T, DWORD, DWORD]
        k32.VirtualAllocEx.restype = LPVOID
        k32.WriteProcessMemory.argtypes = [HANDLE, LPVOID, ctypes.c_char_p, SIZE_T, ctypes.POINTER(SIZE_T)]
        k32.WriteProcessMemory.restype = BOOL
        k32.ReadProcessMemory.argtypes = [HANDLE, LPVOID, LPVOID, SIZE_T, ctypes.POINTER(SIZE_T)]
        k32.ReadProcessMemory.restype = BOOL
        k32.GetModuleHandleA.argtypes = [ctypes.c_char_p]
        k32.GetModuleHandleA.restype = ctypes.wintypes.HMODULE
        k32.GetProcAddress.argtypes = [ctypes.wintypes.HMODULE, ctypes.c_char_p]
        k32.GetProcAddress.restype = LPVOID
        k32.CreateRemoteThread.argtypes = [HANDLE, LPVOID, SIZE_T, LPVOID, LPVOID, DWORD, ctypes.POINTER(DWORD)]
        k32.CreateRemoteThread.restype = HANDLE
        k32.WaitForSingleObject.argtypes = [HANDLE, DWORD]
        k32.GetExitCodeThread.argtypes = [HANDLE, ctypes.POINTER(DWORD)]
        k32.CloseHandle.argtypes = [HANDLE]
        k32.VirtualFreeEx.argtypes = [HANDLE, LPVOID, SIZE_T, DWORD]

        dll_bytes = str(dll_path).encode("mbcs") + b"\x00"
        h = k32.OpenProcess(0x001FFFFF, False, pid)  # PROCESS_ALL_ACCESS
        if not h:
            print(f"[!] DWM hook: OpenProcess failed (err={ctypes.GetLastError()})", flush=True)
            return 0
        try:
            # Allocate space for DLL path
            path_addr = k32.VirtualAllocEx(h, None, len(dll_bytes), 0x3000, 0x04)
            if not path_addr:
                print(f"[!] DWM hook: VirtualAllocEx failed (err={ctypes.GetLastError()})", flush=True)
                return 0
            written = SIZE_T(0)
            k32.WriteProcessMemory(h, path_addr, dll_bytes, len(dll_bytes), ctypes.byref(written))

            # Allocate result buffer (8 bytes for HMODULE)
            result_addr = k32.VirtualAllocEx(h, None, 16, 0x3000, 0x04)

            hk = k32.GetModuleHandleA(b"kernel32.dll")
            load_lib = k32.GetProcAddress(hk, b"LoadLibraryA")
            get_last_err = k32.GetProcAddress(hk, b"GetLastError")
            if not load_lib:
                print(f"[!] DWM hook: GetProcAddress(LoadLibraryA) failed", flush=True)
                return 0

            # Build shellcode that calls LoadLibraryA, stores full 64-bit HMODULE
            # and GetLastError result
            sc = bytearray()
            sc += b"\x48\x83\xEC\x28"        # sub rsp, 40
            sc += b"\x48\x89\xCB"             # mov rbx, rcx (result_addr)
            sc += b"\x48\xB9" + struct.pack("<Q", path_addr)   # mov rcx, path_addr
            sc += b"\x48\xB8" + struct.pack("<Q", load_lib)    # mov rax, LoadLibraryA
            sc += b"\xFF\xD0"                 # call rax
            sc += b"\x48\x89\x03"             # mov [rbx], rax (store full 64-bit HMODULE)
            sc += b"\x48\xB8" + struct.pack("<Q", get_last_err) # mov rax, GetLastError
            sc += b"\xFF\xD0"                 # call rax
            sc += b"\x89\x43\x08"             # mov [rbx+8], eax
            sc += b"\x31\xC0"                 # xor eax, eax
            sc += b"\x48\x83\xC4\x28"         # add rsp, 40
            sc += b"\xC3"                     # ret

            code_addr = k32.VirtualAllocEx(h, None, len(sc), 0x3000, 0x40)
            k32.WriteProcessMemory(h, code_addr, bytes(sc), len(sc), ctypes.byref(written))

            print(f"[*] DWM hook: LoadLibraryA @ 0x{load_lib:016X}, DLL path @ 0x{path_addr:016X}", flush=True)
            tid = DWORD(0)
            ht = k32.CreateRemoteThread(h, None, 0, code_addr, result_addr, 0, ctypes.byref(tid))
            if not ht:
                print(f"[!] DWM hook: CreateRemoteThread failed (err={ctypes.GetLastError()})", flush=True)
                return 0
            k32.WaitForSingleObject(ht, 15000)
            k32.CloseHandle(ht)

            # Read results
            buf = (ctypes.c_byte * 16)()
            k32.ReadProcessMemory(h, result_addr, ctypes.byref(buf), 16, ctypes.byref(written))
            hmodule = struct.unpack_from("<Q", bytes(buf), 0)[0]
            last_err = struct.unpack_from("<I", bytes(buf), 8)[0]

            # Cleanup remote allocations
            k32.VirtualFreeEx(h, path_addr, 0, 0x8000)
            k32.VirtualFreeEx(h, result_addr, 0, 0x8000)
            k32.VirtualFreeEx(h, code_addr, 0, 0x8000)

            print(f"[*] DWM hook: HMODULE = 0x{hmodule:016X}, GetLastError = {last_err}", flush=True)
            if not hmodule:
                print(f"[!] DWM hook: LoadLibraryA failed in dwm.exe (err={last_err})", flush=True)
            return hmodule
        finally:
            k32.CloseHandle(h)
    except Exception as exc:
        print(f"[!] DWM hook: injection exception: {exc}", flush=True)
        return 0


def _get_process_name(pid: int) -> str:
    """Return the lowercase exe name for a PID, or '' on failure."""
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return ''
        buf = ctypes.create_unicode_buffer(260)
        size = ctypes.c_ulong(260)
        k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
        k32.CloseHandle(h)
        return buf.value.lower()
    except Exception:
        return ''


# ---------------------------------------------------------------- WDA hook (Option C)
# Patches NtUserSetWindowDisplayAffinity in target processes via inline hooking
# using WriteProcessMemory.  No DLL injection — invisible to anti-injection
# detection (HackerRank, proctoring apps, etc.).  Once patched, ANY call to
# SetWindowDisplayAffinity in the target process is forced to WDA_NONE, so
# normal screen capture (dxcam / BitBlt / thirdeye) sees the window content.

_wda_patched_pids: set = set()


def _find_wda_function_addr():
    """Return (NtUserSetWindowDisplayAffinity, SetWindowDisplayAffinity) addresses.
    Since win32u.dll and user32.dll are system DLLs loaded at the same base in
    every process, the addresses found here are valid in target processes too."""
    import ctypes, ctypes.wintypes
    k32 = ctypes.windll.kernel32
    HMODULE = ctypes.wintypes.HMODULE

    k32.GetModuleHandleW.restype = HMODULE
    k32.LoadLibraryW.restype = HMODULE
    k32.GetProcAddress.restype = ctypes.c_void_p
    k32.GetProcAddress.argtypes = [HMODULE, ctypes.c_char_p]

    win32u = k32.GetModuleHandleW("win32u.dll")
    if not win32u:
        win32u = k32.LoadLibraryW("win32u.dll")
    if not win32u:
        return 0, 0

    fn = k32.GetProcAddress(win32u, b"NtUserSetWindowDisplayAffinity")

    user32_mod = k32.GetModuleHandleW("user32.dll")
    set_wda = k32.GetProcAddress(user32_mod, b"SetWindowDisplayAffinity") if user32_mod else 0

    return fn or 0, set_wda or 0


def _patch_wda_in_process(pid, hwnds, fn_addr, set_wda_addr):
    """Inline-hook NtUserSetWindowDisplayAffinity in *pid* so every call forces
    the affinity parameter to 0 (WDA_NONE).  Then clear existing WDA on *hwnds*
    by executing SetWindowDisplayAffinity(hwnd, 0) via a remote thread."""
    import ctypes
    import ctypes.wintypes
    import struct

    k32 = ctypes.windll.kernel32
    HANDLE = ctypes.wintypes.HANDLE
    LPVOID = ctypes.c_void_p
    DWORD  = ctypes.wintypes.DWORD
    SIZE_T = ctypes.c_size_t

    k32.OpenProcess.argtypes  = [DWORD, ctypes.wintypes.BOOL, DWORD]
    k32.OpenProcess.restype   = HANDLE
    k32.VirtualAllocEx.argtypes = [HANDLE, LPVOID, SIZE_T, DWORD, DWORD]
    k32.VirtualAllocEx.restype  = LPVOID
    k32.WriteProcessMemory.argtypes = [HANDLE, LPVOID, ctypes.c_char_p, SIZE_T, ctypes.POINTER(SIZE_T)]
    k32.WriteProcessMemory.restype  = ctypes.wintypes.BOOL
    k32.ReadProcessMemory.argtypes  = [HANDLE, LPVOID, LPVOID, SIZE_T, ctypes.POINTER(SIZE_T)]
    k32.ReadProcessMemory.restype   = ctypes.wintypes.BOOL
    k32.VirtualProtectEx.argtypes   = [HANDLE, LPVOID, SIZE_T, DWORD, ctypes.POINTER(DWORD)]
    k32.VirtualProtectEx.restype    = ctypes.wintypes.BOOL
    k32.CreateRemoteThread.argtypes = [HANDLE, LPVOID, SIZE_T, LPVOID, LPVOID, DWORD, ctypes.POINTER(DWORD)]
    k32.CreateRemoteThread.restype  = HANDLE
    k32.WaitForSingleObject.argtypes = [HANDLE, DWORD]
    k32.FlushInstructionCache.argtypes = [HANDLE, LPVOID, SIZE_T]
    k32.CloseHandle.argtypes = [HANDLE]
    k32.VirtualFreeEx.argtypes = [HANDLE, LPVOID, SIZE_T, DWORD]

    h = k32.OpenProcess(0x001FFFFF, False, pid)  # PROCESS_ALL_ACCESS
    if not h:
        print(f"[!] WDA hook: OpenProcess({pid}) failed err={ctypes.GetLastError()}", flush=True)
        return False
    try:
        written  = SIZE_T(0)
        old_prot = DWORD(0)

        # --- Step 1: Read original bytes of NtUserSetWindowDisplayAffinity ---
        # The win32u.dll syscall stub is typically:
        #   4C 8B D1           mov r10, rcx                (3 bytes)
        #   B8 XX XX XX XX     mov eax, <syscall_number>   (5 bytes)
        #   F6 04 25 ...       test byte [...], 1          (8 bytes)
        # Total first 3 instructions = 16 bytes.  We steal 16 to fit our
        # 14-byte absolute JMP with 2 bytes of NOP padding.
        STEAL = 16
        orig = (ctypes.c_byte * 32)()
        if not k32.ReadProcessMemory(h, fn_addr, ctypes.byref(orig), 32, ctypes.byref(written)):
            print(f"[!] WDA hook: ReadProcessMemory failed err={ctypes.GetLastError()}", flush=True)
            return False
        orig_bytes = bytes(orig)
        print(f"[*] WDA hook: PID {pid} NtUserSetWindowDisplayAffinity bytes: {orig_bytes[:16].hex()}", flush=True)

        # Already patched from a previous run?
        if orig_bytes[:2] == b"\xFF\x25":
            print(f"[*] WDA hook: PID {pid} already patched", flush=True)
            # Still need to clear existing WDA flags below
        else:
            stolen = orig_bytes[:STEAL]

            # --- Step 2: Build code cave ---
            # Layout:  xor edx,edx  |  stolen bytes  |  jmp back
            cave_code = bytearray()
            cave_code += b"\x31\xD2"                        # xor edx, edx  (force affinity=0)
            cave_code += stolen                              # original stolen bytes
            cave_code += b"\xFF\x25\x00\x00\x00\x00"        # jmp [rip+0]
            cave_code += struct.pack("<Q", fn_addr + STEAL)  # jump-back target

            cave_addr = k32.VirtualAllocEx(h, None, len(cave_code), 0x3000, 0x40)
            if not cave_addr:
                print(f"[!] WDA hook: VirtualAllocEx (cave) failed", flush=True)
                return False
            k32.WriteProcessMemory(h, cave_addr, bytes(cave_code), len(cave_code),
                                   ctypes.byref(written))

            # --- Step 3: Overwrite function start with JMP to cave ---
            jmp = b"\xFF\x25\x00\x00\x00\x00" + struct.pack("<Q", cave_addr)
            jmp += b"\x90" * (STEAL - 14)  # NOP padding to fill stolen region

            k32.VirtualProtectEx(h, fn_addr, STEAL, 0x40, ctypes.byref(old_prot))
            ok = k32.WriteProcessMemory(h, fn_addr, jmp, len(jmp), ctypes.byref(written))
            k32.VirtualProtectEx(h, fn_addr, STEAL, old_prot.value, ctypes.byref(old_prot))
            if not ok:
                print(f"[!] WDA hook: WriteProcessMemory failed err={ctypes.GetLastError()}", flush=True)
                return False
            k32.FlushInstructionCache(h, fn_addr, STEAL)

            # Verify patch
            verify = (ctypes.c_byte * STEAL)()
            k32.ReadProcessMemory(h, fn_addr, ctypes.byref(verify), STEAL, ctypes.byref(written))
            if bytes(verify) != jmp:
                print(f"[!] WDA hook: verify failed", flush=True)
                return False
            print(f"[+] WDA hook: PID {pid} patched (cave @ 0x{cave_addr:016X})", flush=True)

        # --- Step 4: Clear existing WDA on protected windows ---
        if set_wda_addr and hwnds:
            for hwnd in hwnds:
                hwnd_val = hwnd if isinstance(hwnd, int) else int(hwnd)
                # Shellcode: sub rsp,40; mov rcx,hwnd; xor edx,edx; mov rax,SetWindowDisplayAffinity; call rax; add rsp,40; ret
                sc = bytearray()
                sc += b"\x48\x83\xEC\x28"                          # sub rsp, 40
                sc += b"\x48\xB9" + struct.pack("<Q", hwnd_val)    # mov rcx, hwnd
                sc += b"\x31\xD2"                                  # xor edx, edx
                sc += b"\x48\xB8" + struct.pack("<Q", set_wda_addr)# mov rax, SetWindowDisplayAffinity
                sc += b"\xFF\xD0"                                  # call rax
                sc += b"\x48\x83\xC4\x28"                          # add rsp, 40
                sc += b"\xC3"                                      # ret

                sc_addr = k32.VirtualAllocEx(h, None, len(sc), 0x3000, 0x40)
                if not sc_addr:
                    continue
                k32.WriteProcessMemory(h, sc_addr, bytes(sc), len(sc), ctypes.byref(written))

                tid = DWORD(0)
                ht = k32.CreateRemoteThread(h, None, 0, sc_addr, None, 0, ctypes.byref(tid))
                if ht:
                    k32.WaitForSingleObject(ht, 5000)
                    k32.CloseHandle(ht)
                k32.VirtualFreeEx(h, sc_addr, 0, 0x8000)
                print(f"[+] WDA hook: cleared WDA on HWND 0x{hwnd_val:X} in PID {pid}", flush=True)

        return True
    finally:
        k32.CloseHandle(h)


def _wda_hook_monitor():
    """Background thread: watch for WDA-protected windows and patch their
    owning processes so SetWindowDisplayAffinity is forced to WDA_NONE."""
    fn_addr, set_wda_addr = _find_wda_function_addr()
    if not fn_addr:
        print("[!] WDA hook: NtUserSetWindowDisplayAffinity not found in win32u.dll", flush=True)
        return
    print(f"[*] WDA hook: monitor active (NtUserSetWindowDisplayAffinity @ 0x{fn_addr:016X})", flush=True)

    _enable_debug_privilege()

    while True:
        try:
            import ctypes
            import ctypes.wintypes
            user32 = ctypes.windll.user32

            pid_hwnds: dict[int, list] = {}

            @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
            def _cb(hwnd, _):
                affinity = ctypes.c_uint(0)
                user32.GetWindowDisplayAffinity(hwnd, ctypes.byref(affinity))
                if affinity.value != 0:
                    pid = ctypes.c_uint(0)
                    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                    if pid.value and pid.value not in _wda_patched_pids:
                        pid_hwnds.setdefault(pid.value, []).append(hwnd)
                return True

            user32.EnumWindows(_cb, 0)

            for pid, hwnds in pid_hwnds.items():
                name = _get_process_name(pid)
                print(f"[*] WDA hook: found protected PID {pid} ({name or 'unknown'}), patching...", flush=True)
                _patch_wda_in_process(pid, hwnds, fn_addr, set_wda_addr)
                _wda_patched_pids.add(pid)
        except Exception as exc:
            print(f"[!] WDA hook: monitor error: {exc}", flush=True)

        time.sleep(2)


# Process names (substrings, lowercase) that self-terminate on DLL injection.
# We skip these entirely so they don't close themselves when LiteView starts.
_INJECTION_RESISTANT = ("hackerrank", "proctorio", "respondus", "examity", "honorlock")


def _capture_bypass_monitor(cb_dir: Path):
    """Background thread: watch for WDA-protected windows and inject into them.
    Skips processes that actively resist injection (e.g. HackerRank desktop app)."""
    dll = cb_dir / "payload_dll_persistent.dll"
    if not dll.exists():
        print(f"[!] capture-bypass: {dll} not found — run the install script to download it", flush=True)
        return

    print("[*] capture-bypass: monitor started (watching for protected windows)", flush=True)
    injected: set[int] = set()
    skip: set[int] = set()
    while True:
        for pid in _get_protected_pids():
            if pid not in injected and pid not in skip:
                name = _get_process_name(pid)
                if any(r in name for r in _INJECTION_RESISTANT):
                    skip.add(pid)  # known anti-injection app; never touch it
                    continue
                if _inject_dll(pid, dll):
                    print(f"[+] capture-bypass: cleared WDA on PID {pid}", flush=True)
                    injected.add(pid)
                else:
                    skip.add(pid)  # injection-resistant process; don't retry
        time.sleep(2)


def _grab_print_window(hwnd: int, max_width: int, quality: int):
    """Capture hwnd using PrintWindow(PW_RENDERFULLCONTENT) — no injection needed.
    Works on some WDA-protected apps (e.g. Electron apps with anti-injection).
    Returns JPEG bytes or None if the window can't be captured this way."""
    try:
        import ctypes
        import ctypes.wintypes
        user32  = ctypes.windll.user32
        gdi32   = ctypes.windll.gdi32
        PW_RENDERFULLCONTENT = 0x00000002

        rect = ctypes.wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        w = rect.right  - rect.left
        h = rect.bottom - rect.top
        if w <= 0 or h <= 0:
            return None

        hdc_screen = user32.GetDC(None)
        hdc_mem    = gdi32.CreateCompatibleDC(hdc_screen)
        hbmp       = gdi32.CreateCompatibleBitmap(hdc_screen, w, h)
        gdi32.SelectObject(hdc_mem, hbmp)

        ok = user32.PrintWindow(hwnd, hdc_mem, PW_RENDERFULLCONTENT)

        if ok:
            # Convert HBITMAP → PIL Image via BITMAPINFOHEADER
            class BITMAPINFOHEADER(ctypes.Structure):
                _fields_ = [
                    ("biSize",          ctypes.wintypes.DWORD),
                    ("biWidth",         ctypes.wintypes.LONG),
                    ("biHeight",        ctypes.wintypes.LONG),
                    ("biPlanes",        ctypes.wintypes.WORD),
                    ("biBitCount",      ctypes.wintypes.WORD),
                    ("biCompression",   ctypes.wintypes.DWORD),
                    ("biSizeImage",     ctypes.wintypes.DWORD),
                    ("biXPelsPerMeter", ctypes.wintypes.LONG),
                    ("biYPelsPerMeter", ctypes.wintypes.LONG),
                    ("biClrUsed",       ctypes.wintypes.DWORD),
                    ("biClrImportant",  ctypes.wintypes.DWORD),
                ]
            bih = BITMAPINFOHEADER()
            bih.biSize      = ctypes.sizeof(BITMAPINFOHEADER)
            bih.biWidth     = w
            bih.biHeight    = -h  # top-down
            bih.biPlanes    = 1
            bih.biBitCount  = 32
            bih.biCompression = 0  # BI_RGB
            buf = (ctypes.c_char * (w * h * 4))()
            gdi32.GetDIBits(hdc_mem, hbmp, 0, h, buf, ctypes.byref(bih), 0)
            img = Image.frombuffer("RGBA", (w, h), bytes(buf), "raw", "BGRA", 0, 1)
            img = img.convert("RGB")
            if img.width > max_width:
                img = img.resize(
                    (max_width, round(img.height * max_width / img.width)),
                    Image.BILINEAR,
                )
            out = io.BytesIO()
            img.save(out, "JPEG", quality=quality)
            result = out.getvalue()
        else:
            result = None

        gdi32.DeleteObject(hbmp)
        gdi32.DeleteDC(hdc_mem)
        user32.ReleaseDC(None, hdc_screen)
        return result
    except Exception:
        return None


def _find_protected_hwnds():
    """Return list of HWNDs that have WDA protection set."""
    try:
        import ctypes
        import ctypes.wintypes
        user32 = ctypes.windll.user32
    except Exception:
        return []

    hwnds = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
    def _cb(hwnd, _):
        affinity = ctypes.c_uint(0)
        user32.GetWindowDisplayAffinity(hwnd, ctypes.byref(affinity))
        if affinity.value != 0:
            hwnds.append(hwnd)
        return True

    user32.EnumWindows(_cb, 0)
    return hwnds


# ---------------------------------------------------------------- screen capture

_capture = threading.local()  # thirdeye sessions are not thread-safe; keep one per thread

# Shared dxcam camera instance (DXGI Desktop Duplication).
_dxcam_camera = None
_dxcam_lock = threading.Lock()

# ThirdEye capture option: bypass_protection keeps thirdeye as a fallback even
# when capture-bypass hasn't injected yet (e.g. no Admin rights).
_TE_OPTS_BYPASS = None


def _init_capture(quality):
    global _dxcam_camera, _TE_OPTS_BYPASS
    if _TE_OPTS_BYPASS is None:
        _TE_OPTS_BYPASS = _eye3.ThirdEyeOptions(
            format=_eye3.ThirdeyeFormat.JPEG,
            quality=quality,
            bypass_protection=True,
        )
    if _dxcam is not None and _dxcam_camera is None:
        with _dxcam_lock:
            if _dxcam_camera is None:
                _dxcam_camera = _dxcam.create(output_color="RGB")


def grab_jpeg(max_width, quality, force):
    """Return the screen as JPEG bytes, or None if nothing changed since last grab."""
    global _dxcam_camera

    if not hasattr(_capture, "session"):
        _capture.session = _eye3.ThirdEyeSession()
        _capture.last_digest = None
        _init_capture(quality)

    jpeg = None

    # Tier 1: dxcam (DXGI Desktop Duplication) — captures GPU-composited frames.
    # Works for all windows once the WDA hook has cleared the protection flag.
    if jpeg is None and _dxcam_camera is not None:
        try:
            frame = _dxcam_camera.grab()
            if frame is not None:
                img = Image.fromarray(frame)
                if img.width > max_width:
                    img = img.resize(
                        (max_width, round(img.height * max_width / img.width)),
                        Image.BILINEAR,
                    )
                buf = io.BytesIO()
                img.save(buf, "JPEG", quality=quality)
                jpeg = buf.getvalue()
        except Exception as exc:
            print(f"[dxcam] capture error, disabling: {exc}", flush=True)
            _dxcam_camera = None

    # Tier 2: PrintWindow(PW_RENDERFULLCONTENT) on any still-protected window.
    # No injection — asks the window to render itself via DWM.
    if jpeg is None:
        for hwnd in _find_protected_hwnds():
            pw_jpeg = _grab_print_window(hwnd, max_width, quality)
            if pw_jpeg:
                jpeg = pw_jpeg
                break

    # Tier 3: thirdeye BitBlt with per-frame WDA bypass — universal fallback.
    if jpeg is None:
        jpeg = _capture.session.capture_to_buffer(_TE_OPTS_BYPASS)

    digest = hashlib.blake2b(jpeg, digest_size=16).digest()
    if digest == _capture.last_digest and not force:
        return None
    _capture.last_digest = digest
    return jpeg


# ---------------------------------------------------------------- input injection

# Browser KeyboardEvent.code -> pynput Key name. Looked up with getattr because
# some keys (insert, menu, print_screen) don't exist on every OS.
SPECIAL_CODES = {
    "Enter": "enter", "NumpadEnter": "enter", "Backspace": "backspace", "Tab": "tab",
    "Escape": "esc", "Space": "space", "CapsLock": "caps_lock",
    "ArrowUp": "up", "ArrowDown": "down", "ArrowLeft": "left", "ArrowRight": "right",
    "Delete": "delete", "Insert": "insert", "Home": "home", "End": "end",
    "PageUp": "page_up", "PageDown": "page_down",
    "ShiftLeft": "shift_l", "ShiftRight": "shift_r",
    "ControlLeft": "ctrl_l", "ControlRight": "ctrl_r",
    "AltLeft": "alt_l", "AltRight": "alt_r",
    "MetaLeft": "cmd_l", "MetaRight": "cmd_r",
    "ContextMenu": "menu", "PrintScreen": "print_screen",
    **{f"F{i}": f"f{i}" for i in range(1, 13)},
}
CHAR_CODES = {
    "Minus": "-", "Equal": "=", "BracketLeft": "[", "BracketRight": "]",
    "Backslash": "\\", "Semicolon": ";", "Quote": "'", "Backquote": "`",
    "Comma": ",", "Period": ".", "Slash": "/",
    "NumpadAdd": "+", "NumpadSubtract": "-", "NumpadMultiply": "*",
    "NumpadDivide": "/", "NumpadDecimal": ".",
}
BUTTONS = {0: Button.left, 1: Button.middle, 2: Button.right}


def code_to_key(code):
    # Using physical key codes (not typed characters) means held modifiers like
    # Shift/Ctrl are applied by the host OS, so presses and releases always match.
    if code in SPECIAL_CODES:
        return getattr(Key, SPECIAL_CODES[code], None)
    if code in CHAR_CODES:
        return KeyCode.from_char(CHAR_CODES[code])
    if code.startswith("Key") and len(code) == 4:
        return KeyCode.from_char(code[3].lower())
    if code.startswith("Digit"):
        return KeyCode.from_char(code[5:])
    if code.startswith("Numpad") and code[6:].isdigit():
        return KeyCode.from_char(code[6:])
    return None


class InputInjector:
    def __init__(self, monitor):
        self.monitor = monitor
        self.mouse = MouseController()
        self.keyboard = KeyboardController()
        self.held_keys = set()
        self.held_buttons = set()

    def _move(self, ev):
        m = self.monitor
        x = min(max(float(ev["x"]), 0.0), 1.0)
        y = min(max(float(ev["y"]), 0.0), 1.0)
        self.mouse.position = (m["left"] + round(x * (m["width"] - 1)),
                               m["top"] + round(y * (m["height"] - 1)))

    def handle(self, ev):
        t = ev.get("t")
        if t == "move":
            self._move(ev)
        elif t in ("down", "up"):
            button = BUTTONS.get(ev.get("b"))
            if button is None:
                return
            self._move(ev)
            if t == "down":
                self.mouse.press(button)
                self.held_buttons.add(button)
            else:
                self.mouse.release(button)
                self.held_buttons.discard(button)
        elif t == "wheel":
            self.mouse.scroll(int(ev.get("dx", 0)), int(ev.get("dy", 0)))
        elif t in ("kd", "ku"):
            key = code_to_key(str(ev.get("code", "")))
            if key is None:
                return
            if t == "kd":
                self.keyboard.press(key)
                self.held_keys.add(key)
            else:
                self.keyboard.release(key)
                self.held_keys.discard(key)
        elif t == "releaseall":
            self.release_all()

    def release_all(self):
        """Avoid stuck keys/buttons when the viewer loses focus or disconnects."""
        for key in list(self.held_keys):
            try:
                self.keyboard.release(key)
            except Exception:
                pass
        for button in list(self.held_buttons):
            try:
                self.mouse.release(button)
            except Exception:
                pass
        self.held_keys.clear()
        self.held_buttons.clear()


# ---------------------------------------------------------------- web server

async def index(request):
    return web.FileResponse(HERE / "viewer.html")


async def stream_frames(ws, app, acked):
    loop = asyncio.get_running_loop()
    interval = 1 / app["fps"]
    force = True
    third_eye = app.get("third_eye")
    while not ws.closed:
        started = loop.time()
        jpeg = await loop.run_in_executor(
            app["capture_pool"], grab_jpeg, app["max_width"], app["quality"], force)
        if jpeg:
            if third_eye is not None:
                third_eye.observe(jpeg)
                if third_eye.last_frame:
                    jpeg = third_eye.last_frame
            force = False
            acked.clear()
            await ws.send_bytes(jpeg)
            try:
                await asyncio.wait_for(acked.wait(), timeout=5)
            except asyncio.TimeoutError:
                force = True
        await asyncio.sleep(max(0.0, interval - (loop.time() - started)))


async def ws_handler(request):
    app = request.app
    peer = request.remote
    ws = web.WebSocketResponse(heartbeat=20, max_msg_size=64 * 1024)
    await ws.prepare(request)

    try:
        first = await ws.receive(timeout=30)
        auth = json.loads(first.data) if first.type == WSMsgType.TEXT else {}
    except (asyncio.TimeoutError, ValueError):
        auth = {}
    if not hmac.compare_digest(str(auth.get("pw", "")).encode(), app["password"].encode()):
        print(f"[!] Rejected {peer}: wrong password")
        await asyncio.sleep(1)
        await ws.send_str(json.dumps({"t": "error", "msg": "Wrong password"}))
        await ws.close(code=4001)
        return ws
    session = app["session"]
    old = session["ws"]
    if old is not None and not old.closed:
        print("[*] New viewer is taking over the existing session")
        try:
            await old.send_str(json.dumps({"t": "error", "msg": "Another viewer took over this session"}))
        except Exception:
            pass
        asyncio.create_task(old.close(code=4002))
    session["ws"] = ws
    print(f"[+] {peer} connected")
    third_eye = app.get("third_eye")
    if third_eye is not None:
        await ws.send_str(json.dumps({"t": "module", "module": "thirdeye", "enabled": True, "status": "tracking"}))
    await ws.send_str(json.dumps({"t": "ok", "capture": app["capture_backend"]}))
    acked = asyncio.Event()
    injector = app["injector"]
    streamer = asyncio.create_task(stream_frames(ws, app, acked))
    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                ev = json.loads(msg.data)
                if ev.get("t") == "ack":
                    acked.set()
                elif not app["view_only"]:
                    injector.handle(ev)
            except Exception as exc:
                print(f"[!] Bad input event {msg.data[:80]!r}: {exc}")
    finally:
        streamer.cancel()
        injector.release_all()
        if session["ws"] is ws:
            session["ws"] = None
        print(f"[-] {peer} disconnected")
    return ws


def load_password(cli_password):
    if cli_password:
        return cli_password
    if os.environ.get("LITEVIEW_PASSWORD"):
        return os.environ["LITEVIEW_PASSWORD"]
    if PASSWORD_FILE.exists():
        return PASSWORD_FILE.read_text().strip()
    password = secrets.token_urlsafe(9)
    PASSWORD_FILE.write_text(password + "\n")
    try:
        PASSWORD_FILE.chmod(0o600)
    except OSError:
        pass
    return password


def lan_ip():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
        except OSError:
            return "127.0.0.1"


TAILSCALE_NET = ipaddress.ip_network("100.64.0.0/10")


def tailscale_ip():
    """This computer's Tailscale IPv4 address, or None if Tailscale isn't connected."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("100.100.100.100", 1))
            ip = s.getsockname()[0]
        except OSError:
            return None
    return ip if ipaddress.ip_address(ip) in TAILSCALE_NET else None


def wait_for_tailscale():
    ip = tailscale_ip()
    if ip is None:
        print("Waiting for Tailscale to connect...", flush=True)
    while ip is None:
        time.sleep(5)
        ip = tailscale_ip()
    return ip


def main():
    if sys.stdout is None:
        sys.stdout = sys.stderr = open(LOG_FILE, "a", buffering=1, encoding="utf-8")

    parser = argparse.ArgumentParser(description="LiteView host: share this screen and allow remote control.")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--password", help=f"access password (default: $LITEVIEW_PASSWORD, else saved in {PASSWORD_FILE})")
    parser.add_argument("--fps", type=float, default=15)
    parser.add_argument("--quality", type=int, default=60, help="JPEG quality 1-95 (lower = less bandwidth)")
    parser.add_argument("--max-width", type=int, default=1600, help="downscale frames wider than this")
    parser.add_argument("--view-only", action="store_true", help="share the screen but ignore mouse/keyboard")
    parser.add_argument("--tailscale-only", action="store_true",
                        help="only accept connections through Tailscale (waits for Tailscale if it isn't up yet)")
    parser.add_argument("--show-address", action="store_true",
                        help="print the addresses and password to connect with, then exit")
    parser.add_argument("--module", "--module_2", "--third-eye", "--thirdeye",
                        action="append", default=[],
                        help="enable optional LiteView modules, e.g. 'thirdeye'")
    args = parser.parse_args()

    password = load_password(args.password)

    if args.show_address:
        ts_ip = tailscale_ip()
        print(f"  From anywhere (Tailscale):   http://{ts_ip}:{args.port}" if ts_ip
              else "  Tailscale not connected - only reachable on this local network.")
        if not args.tailscale_only:
            print(f"  From the same network:       http://{lan_ip()}:{args.port}")
        print(f"  Password:                    {password}")
        return

    if _eye3 is None:
        sys.exit("thirdeye (eye3) is not installed. Run: pip install eye3")
    try:
        with _eye3.ThirdEyeSession() as probe:
            # Use JPEG for the probe too — PIL reads dimensions without full decode.
            test_jpeg = probe.capture_to_buffer(
                _eye3.ThirdEyeOptions(format=_eye3.ThirdeyeFormat.JPEG, quality=50)
            )
            img = Image.open(io.BytesIO(test_jpeg))
            monitor = {"left": 0, "top": 0, "width": img.width, "height": img.height}
    except Exception as exc:
        sys.exit(f"thirdeye failed to capture the screen: {exc}")

    # WDA hook: background thread patches NtUserSetWindowDisplayAffinity in any
    # process that sets WDA_EXCLUDEFROMCAPTURE, forcing the affinity to WDA_NONE.
    # Uses WriteProcessMemory (no DLL injection) so it works even on apps like
    # HackerRank that detect and resist DLL injection.
    threading.Thread(target=_wda_hook_monitor, daemon=True).start()

    # Also start the DLL-based capture-bypass monitor as a fallback for browsers
    # where the WDA hook alone might not clear already-set flags quickly enough.
    threading.Thread(
        target=_capture_bypass_monitor,
        args=(HERE / "capture-bypass",),
        daemon=True,
    ).start()

    app = web.Application()
    app.update(
        password=password,
        fps=args.fps,
        quality=args.quality,
        max_width=args.max_width,
        view_only=args.view_only,
        session={"ws": None},
        injector=InputInjector(monitor),
        capture_pool=ThreadPoolExecutor(max_workers=1, thread_name_prefix="capture"),
        capture_backend="thirdeye",
    )
    app.router.add_get("/", index)
    app.router.add_get("/ws", ws_handler)

    for requested in args.module:
        requested_name = requested.strip().lower().replace("_", "-")
        if requested_name in {"thirdeye", "third-eye", "module-2", "module_2"}:
            if ThirdEyeModule is None:
                print("[!] ThirdEye module is unavailable in this checkout.")
                continue
            module = ThirdEyeModule()
            module.register(app, frame_provider=grab_jpeg)
            app["third_eye"] = module
            app.setdefault("modules", []).append(module.name)
            print("[+] ThirdEye module enabled")
        else:
            print(f"[!] Unknown module '{requested}' (supported: thirdeye)")

    if args.tailscale_only:
        ts_ip = wait_for_tailscale()
        bind_host = ts_ip
    else:
        ts_ip = tailscale_ip()
        bind_host = "0.0.0.0"

    print("LiteView host is running.")
    if ts_ip:
        print(f"  From anywhere (Tailscale):   http://{ts_ip}:{args.port}")
    else:
        print("  Tailscale not connected - only reachable on this local network.")
    if not args.tailscale_only:
        print(f"  From the same network:       http://{lan_ip()}:{args.port}")
    print(f"  Password:                    {app['password']}")
    print(f"  Screen:                      {monitor['width']}x{monitor['height']}"
          + ("  (view only)" if args.view_only else ""))
    if app.get("modules"):
        print(f"  Modules:                     {', '.join(app['modules'])}")
    web.run_app(app, host=bind_host, port=args.port, print=None)


if __name__ == "__main__":
    main()
