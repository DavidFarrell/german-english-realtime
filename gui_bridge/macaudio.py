"""macOS audio helpers (CoreAudio via ctypes) + a one-shot Bluetooth-earbud un-stick recipe.

Why this exists: BT earbuds that advertise a mic (HFP/HSP) get auto-selected by macOS as the
*default input* on connect, and the moment anything reads the default mic (the Sound settings pane,
a screen/audio recorder, a call app) the buds collapse from A2DP stereo (out 2) to HFP mono call
mode (out 1). DebbieDavidApp needs them in stereo to play German->left / English->right. This module
(a) names the process actually holding a mic, so the toast can say WHO, and (b) un-sticks the buds
by parking both default routes off them so the SCO link drops and they renegotiate A2DP, then hands
output back. Pure ctypes - no compiled helper, no extra Python deps. Every call is defensive: any
CoreAudio failure degrades to a neutral result so the bridge never crashes on it.

macOS only (the process-list selectors are 14.4+). Callers guard with `is_supported()`.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import os
import platform
import shutil
import subprocess
import time

# -- ctypes bindings ------------------------------------------------------------------------------

_IS_MAC = platform.system() == "Darwin"


def _fourcc(code: str) -> int:
    return int.from_bytes(code.encode("ascii"), "big")


# Selectors / scopes (CoreAudio constants).
kAudioObjectSystemObject = 1
kAudioObjectPropertyElementMain = 0
SCOPE_GLOBAL = _fourcc("glob")
SCOPE_INPUT = _fourcc("inpt")
SCOPE_OUTPUT = _fourcc("outp")
PROP_DEVICES = _fourcc("dev#")
PROP_DEFAULT_INPUT = _fourcc("dIn ")
PROP_DEFAULT_OUTPUT = _fourcc("dOut")
PROP_PROCESS_LIST = _fourcc("prs#")
PROP_PROC_RUNNING_INPUT = _fourcc("piri")
PROP_PROC_PID = _fourcc("ppid")
PROP_NAME = _fourcc("lnam")
PROP_STREAM_CONFIG = _fourcc("slay")


class _Addr(ctypes.Structure):
    _fields_ = [("mSelector", ctypes.c_uint32),
                ("mScope", ctypes.c_uint32),
                ("mElement", ctypes.c_uint32)]


class _AudioBuffer(ctypes.Structure):
    _fields_ = [("mNumberChannels", ctypes.c_uint32),
                ("mDataByteSize", ctypes.c_uint32),
                ("mData", ctypes.c_void_p)]


class _AudioBufferList(ctypes.Structure):
    # mBuffers is 8-byte aligned (it holds a pointer), so ctypes places it at offset 8,
    # matching the C layout - the manual-offset version got this wrong.
    _fields_ = [("mNumberBuffers", ctypes.c_uint32),
                ("mBuffers", _AudioBuffer * 1)]


def _addr(selector: int, scope: int = SCOPE_GLOBAL) -> _Addr:
    return _Addr(selector, scope, kAudioObjectPropertyElementMain)


if _IS_MAC:
    _ca = ctypes.CDLL(ctypes.util.find_library("CoreAudio"))
    _cf = ctypes.CDLL(ctypes.util.find_library("CoreFoundation"))
    _libc = ctypes.CDLL(None)

    _ca.AudioObjectGetPropertyDataSize.restype = ctypes.c_int32
    _ca.AudioObjectGetPropertyDataSize.argtypes = [
        ctypes.c_uint32, ctypes.POINTER(_Addr), ctypes.c_uint32,
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    _ca.AudioObjectGetPropertyData.restype = ctypes.c_int32
    _ca.AudioObjectGetPropertyData.argtypes = [
        ctypes.c_uint32, ctypes.POINTER(_Addr), ctypes.c_uint32,
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]
    _ca.AudioObjectSetPropertyData.restype = ctypes.c_int32
    _ca.AudioObjectSetPropertyData.argtypes = [
        ctypes.c_uint32, ctypes.POINTER(_Addr), ctypes.c_uint32,
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p]

    _cf.CFStringGetCString.restype = ctypes.c_bool
    _cf.CFStringGetCString.argtypes = [
        ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
    _cf.CFRelease.argtypes = [ctypes.c_void_p]

    _libc.proc_pidpath.restype = ctypes.c_int
    _libc.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]


def is_supported() -> bool:
    return _IS_MAC


# -- low-level property reads ---------------------------------------------------------------------

def _get_array(obj: int, selector: int, ctype) -> list:
    """Read a variable-length array property into a list of `ctype`."""
    size = ctypes.c_uint32(0)
    a = _addr(selector)
    if _ca.AudioObjectGetPropertyDataSize(obj, ctypes.byref(a), 0, None, ctypes.byref(size)) != 0:
        return []
    n = size.value // ctypes.sizeof(ctype)
    if n == 0:
        return []
    buf = (ctype * n)()
    if _ca.AudioObjectGetPropertyData(obj, ctypes.byref(a), 0, None,
                                      ctypes.byref(size), buf) != 0:
        return []
    return list(buf)


def _get_scalar(obj: int, selector: int, ctype):
    val = ctype()
    size = ctypes.c_uint32(ctypes.sizeof(ctype))
    a = _addr(selector)
    if _ca.AudioObjectGetPropertyData(obj, ctypes.byref(a), 0, None,
                                      ctypes.byref(size), ctypes.byref(val)) != 0:
        return None
    return val.value


def _device_name(dev: int) -> str:
    cfstr = ctypes.c_void_p()
    size = ctypes.c_uint32(ctypes.sizeof(ctypes.c_void_p))
    a = _addr(PROP_NAME)
    if _ca.AudioObjectGetPropertyData(dev, ctypes.byref(a), 0, None,
                                      ctypes.byref(size), ctypes.byref(cfstr)) != 0 or not cfstr:
        return ""
    buf = ctypes.create_string_buffer(512)
    ok = _cf.CFStringGetCString(cfstr, buf, 512, 0x08000100)  # kCFStringEncodingUTF8
    _cf.CFRelease(cfstr)
    return buf.value.decode("utf-8", "replace") if ok else ""


def _channels(dev: int, scope: int) -> int:
    """Sum the channels of a device's stream config in `scope` (input or output)."""
    size = ctypes.c_uint32(0)
    a = _addr(PROP_STREAM_CONFIG, scope)
    if _ca.AudioObjectGetPropertyDataSize(dev, ctypes.byref(a), 0, None, ctypes.byref(size)) != 0:
        return 0
    if size.value < ctypes.sizeof(_AudioBufferList):
        return 0
    buf = (ctypes.c_byte * size.value)()
    if _ca.AudioObjectGetPropertyData(dev, ctypes.byref(a), 0, None,
                                      ctypes.byref(size), buf) != 0:
        return 0
    abl = ctypes.cast(buf, ctypes.POINTER(_AudioBufferList)).contents
    n = abl.mNumberBuffers
    if n == 0:
        return 0
    base = ctypes.addressof(abl) + _AudioBufferList.mBuffers.offset
    buffers = (_AudioBuffer * n).from_address(base)
    return sum(int(b.mNumberChannels) for b in buffers)


# -- public queries -------------------------------------------------------------------------------

def _proc_name(pid: int) -> str:
    buf = ctypes.create_string_buffer(4096)
    if _libc.proc_pidpath(pid, buf, 4096) > 0:
        return buf.value.decode("utf-8", "replace").rsplit("/", 1)[-1]
    return f"pid {pid}"


def running_input_process_names() -> list[str]:
    """Names of processes currently holding an input (mic) stream open. macOS 14.4+; [] otherwise."""
    if not _IS_MAC:
        return []
    try:
        names = []
        for proc in _get_array(kAudioObjectSystemObject, PROP_PROCESS_LIST, ctypes.c_uint32):
            running = _get_scalar(proc, PROP_PROC_RUNNING_INPUT, ctypes.c_uint32)
            if running:
                pid = _get_scalar(proc, PROP_PROC_PID, ctypes.c_int32)
                if pid:
                    names.append(_proc_name(pid))
        return names
    except Exception:
        return []


def _all_devices() -> list[tuple[int, str, int, int]]:
    out = []
    for dev in _get_array(kAudioObjectSystemObject, PROP_DEVICES, ctypes.c_uint32):
        out.append((dev, _device_name(dev),
                    _channels(dev, SCOPE_INPUT), _channels(dev, SCOPE_OUTPUT)))
    return out


def output_channels(name_substr: str) -> int:
    """Max output channels of the (highest-channel) device whose name contains `name_substr`."""
    if not _IS_MAC:
        return 2
    best = 0
    for _id, name, _inc, outc in _all_devices():
        if name_substr.lower() in name.lower():
            best = max(best, outc)
    return best


def _find_device_id(name_substr: str, want_output: bool) -> int | None:
    needle = name_substr.lower()
    match = None
    for dev, name, inc, outc in _all_devices():
        if needle in name.lower() and (outc if want_output else inc) > 0:
            if match is None or (outc if want_output else inc) > match[1]:
                match = (dev, outc if want_output else inc)
    return match[0] if match else None


def set_default(kind: str, name_substr: str) -> bool:
    """Set the system default input/output device by name substring. True on success."""
    if not _IS_MAC:
        return False
    want_output = kind == "output"
    dev = _find_device_id(name_substr, want_output)
    if dev is None:
        return False
    selector = PROP_DEFAULT_OUTPUT if want_output else PROP_DEFAULT_INPUT
    a = _addr(selector)
    val = ctypes.c_uint32(dev)
    return _ca.AudioObjectSetPropertyData(
        kAudioObjectSystemObject, ctypes.byref(a), 0, None,
        ctypes.sizeof(val), ctypes.byref(val)) == 0


# -- the un-stick recipe --------------------------------------------------------------------------

def _find_blueutil() -> str | None:
    """Locate blueutil. shutil.which alone misses it when the app is launched from Finder (launchd
    PATH excludes Homebrew), so also probe the usual install locations."""
    found = shutil.which("blueutil")
    if found:
        return found
    for p in ("/opt/homebrew/bin/blueutil", "/usr/local/bin/blueutil"):
        if os.path.exists(p):
            return p
    return None


def _bt_address(name_substr: str) -> str | None:
    try:
        out = subprocess.run(["system_profiler", "SPBluetoothDataType"],
                             capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return None
    lines = out.splitlines()
    for i, line in enumerate(lines):
        if name_substr.lower() in line.lower() and line.strip().endswith(":"):
            for j in range(i + 1, min(i + 12, len(lines))):
                s = lines[j].strip()
                if s.lower().startswith("address:"):
                    return s.split(":", 1)[1].strip()
    return None


def force_earbuds_stereo(output_substr: str, input_substr: str,
                         park_output_substr: str = "MacBook Pro Speakers") -> dict:
    """Un-stick BT earbuds from HFP mono back to A2DP stereo.

    Recipe (proven 18 Jun 2026): park BOTH default routes off the earbuds (output -> built-in
    speakers, input -> the real mic) so the earbuds go fully idle and the SCO link drops; wait for
    the A2DP renegotiation; if still mono, disconnect/reconnect over Bluetooth; then hand the
    default output back to the earbuds (input stays on the real mic so nothing re-grabs their mic).
    Returns {ok, channels, steps}.
    """
    steps: list[str] = []
    if not _IS_MAC:
        return {"ok": False, "channels": 0, "steps": ["not macOS"]}

    # 1. steer both defaults off the earbuds
    set_default("input", input_substr)
    set_default("output", park_output_substr)
    steps.append(f"parked output->{park_output_substr}, input->{input_substr}")
    time.sleep(4.0)

    # 2. if still mono, do a Bluetooth reconnect (needs blueutil)
    if output_channels(output_substr) < 2:
        addr = _bt_address(output_substr)
        blueutil = _find_blueutil()
        if addr and blueutil:
            try:
                subprocess.run([blueutil, "--disconnect", addr], timeout=10)
                time.sleep(3.0)
                subprocess.run([blueutil, "--connect", addr], timeout=10)
                steps.append("bluetooth reconnect")
                # hold the default input on the real mic through the reconnect window
                for _ in range(8):
                    set_default("input", input_substr)
                    time.sleep(0.6)
            except Exception as exc:
                steps.append(f"blueutil failed: {exc}")
        else:
            steps.append("still mono; blueutil/address unavailable")

    # 3. hand output back to the earbuds (only worth it if they're stereo now)
    chans = output_channels(output_substr)
    if chans >= 2:
        set_default("output", output_substr)
        steps.append(f"output->{output_substr}")
    chans = output_channels(output_substr)
    return {"ok": chans >= 2, "channels": chans, "steps": steps}
