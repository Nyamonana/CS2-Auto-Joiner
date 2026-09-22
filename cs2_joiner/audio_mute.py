from __future__ import annotations

from dataclasses import dataclass
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
from typing import Callable, Iterable


TARGET_PROCESS_NAME = "cs2.exe"
CLSCTX_ALL = 0x17
COINIT_MULTITHREADED = 0x0
RPC_E_CHANGED_MODE = 0x80010106
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


@dataclass(frozen=True)
class ProcessMuteResult:
    found: bool
    muted: bool
    session_count: int


@dataclass
class AudioSession:
    pid: int
    process_name: str
    muted: bool
    set_mute: Callable[[bool], None]
    release: Callable[[], None] = lambda: None

    def set_muted(self, muted: bool) -> None:
        self.set_mute(muted)
        self.muted = muted


class AudioMuteError(RuntimeError):
    pass


def is_target_process(process_name_or_path: str, target_process_name: str = TARGET_PROCESS_NAME) -> bool:
    if not process_name_or_path:
        return False
    candidate = Path(process_name_or_path).name.lower()
    return candidate == target_process_name.lower()


def toggle_process_sessions(
    sessions: Iterable[AudioSession],
    target_process_name: str = TARGET_PROCESS_NAME,
) -> ProcessMuteResult:
    session_list = list(sessions)
    matched = [session for session in session_list if is_target_process(session.process_name, target_process_name)]
    try:
        if not matched:
            return ProcessMuteResult(found=False, muted=False, session_count=0)

        should_mute = not all(session.muted for session in matched)
        for session in matched:
            session.set_muted(should_mute)

        return ProcessMuteResult(found=True, muted=should_mute, session_count=len(matched))
    finally:
        for session in session_list:
            release = getattr(session, "release", None)
            if callable(release):
                release()


def toggle_process_mute(target_process_name: str = TARGET_PROCESS_NAME) -> ProcessMuteResult:
    if os.name != "nt":
        raise AudioMuteError("Per-application mute is only supported on Windows.")
    ole32 = _ole32()
    initialized = _co_initialize(ole32)
    try:
        controller = WindowsCoreAudioSessionController()
        return toggle_process_sessions(controller.list_sessions(), target_process_name)
    finally:
        if initialized:
            ole32.CoUninitialize()


class WindowsCoreAudioSessionController:
    def list_sessions(self) -> list[AudioSession]:
        """Return owned sessions; the caller must keep COM initialized until release."""
        if os.name != "nt":
            raise AudioMuteError("Per-application mute is only supported on Windows.")

        ole32 = _ole32()
        pointers: list[ctypes.c_void_p] = []
        sessions: list[AudioSession] = []
        try:
            enumerator = _co_create_instance(ole32, _guid("{BCDE0395-E52F-467C-8E3D-C4579291692E}"), _guid("{A95664D2-9614-4F35-A746-DE8DB63617E6}"))
            pointers.append(enumerator)
            devices = _get_active_audio_endpoints(enumerator)
            pointers.extend(devices)
            for device in devices:
                manager = _activate_audio_session_manager(device)
                pointers.append(manager)
                session_enum = _get_session_enumerator(manager)
                pointers.append(session_enum)
                sessions.extend(_collect_audio_sessions(session_enum))
            return sessions
        except Exception:
            for session in sessions:
                session.release()
            raise
        finally:
            for pointer in reversed(pointers):
                _release(pointer)


def _collect_audio_sessions(session_enum: ctypes.c_void_p) -> list[AudioSession]:
    count = ctypes.c_int()
    _check_hresult(_method(session_enum, 3, ctypes.c_long, ctypes.POINTER(ctypes.c_int))(session_enum, ctypes.byref(count)), "IAudioSessionEnumerator.GetCount")

    sessions: list[AudioSession] = []
    for index in range(count.value):
        control = ctypes.c_void_p()
        control2 = ctypes.c_void_p()
        volume = ctypes.c_void_p()
        try:
            _check_hresult(
                _method(session_enum, 4, ctypes.c_long, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p))(
                    session_enum,
                    index,
                    ctypes.byref(control),
                ),
                "IAudioSessionEnumerator.GetSession",
            )
            control2 = _query_interface(control, _guid("{BFB7FF88-7239-4FC9-8FA2-07C950BE9C6D}"))
            volume = _query_interface(control, _guid("{87CE5498-68D6-44E5-9215-6DA47EF883D8}"))

            pid = ctypes.c_ulong()
            _check_hresult(
                _method(control2, 14, ctypes.c_long, ctypes.POINTER(ctypes.c_ulong))(control2, ctypes.byref(pid)),
                "IAudioSessionControl2.GetProcessId",
            )
            if pid.value == 0:
                continue

            muted = ctypes.c_int()
            _check_hresult(
                _method(volume, 6, ctypes.c_long, ctypes.POINTER(ctypes.c_int))(volume, ctypes.byref(muted)),
                "ISimpleAudioVolume.GetMute",
            )

            process_name = _get_process_name(pid.value)
            volume_for_setter = volume
            volume = ctypes.c_void_p()
            sessions.append(
                AudioSession(
                    pid=int(pid.value),
                    process_name=process_name,
                    muted=bool(muted.value),
                    set_mute=lambda target, volume_pointer=volume_for_setter: _set_session_mute(volume_pointer, target),
                    release=lambda volume_pointer=volume_for_setter: _release(volume_pointer),
                )
            )
        except Exception:
            for session in sessions:
                session.release()
            raise
        finally:
            _release(control2)
            _release(volume)
            _release(control)

    return sessions


def _set_session_mute(volume: ctypes.c_void_p, muted: bool) -> None:
    _check_hresult(
        _method(volume, 5, ctypes.c_long, ctypes.c_int, ctypes.c_void_p)(volume, int(muted), None),
        "ISimpleAudioVolume.SetMute",
    )


def _co_initialize(ole32) -> bool:
    result = ole32.CoInitializeEx(None, COINIT_MULTITHREADED)
    unsigned_result = ctypes.c_ulong(result).value
    if unsigned_result == RPC_E_CHANGED_MODE:
        return False
    _check_hresult(result, "CoInitializeEx")
    return True


def _co_create_instance(ole32, clsid, iid) -> ctypes.c_void_p:
    pointer = ctypes.c_void_p()
    _check_hresult(
        ole32.CoCreateInstance(ctypes.byref(clsid), None, CLSCTX_ALL, ctypes.byref(iid), ctypes.byref(pointer)),
        "CoCreateInstance",
    )
    return pointer


def _get_active_audio_endpoints(enumerator: ctypes.c_void_p) -> list[ctypes.c_void_p]:
    collection = ctypes.c_void_p()
    devices: list[ctypes.c_void_p] = []
    try:
        _check_hresult(
            _method(enumerator, 3, ctypes.c_long, ctypes.c_int, ctypes.c_ulong, ctypes.POINTER(ctypes.c_void_p))(
                enumerator, 0, 1, ctypes.byref(collection)),
            "IMMDeviceEnumerator.EnumAudioEndpoints",
        )
        count = ctypes.c_uint()
        _check_hresult(_method(collection, 3, ctypes.c_long, ctypes.POINTER(ctypes.c_uint))(
            collection, ctypes.byref(count)), "IMMDeviceCollection.GetCount")
        for index in range(count.value):
            device = ctypes.c_void_p()
            _check_hresult(_method(collection, 4, ctypes.c_long, ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p))(
                collection, index, ctypes.byref(device)), "IMMDeviceCollection.Item")
            devices.append(device)
        return devices
    except Exception:
        for device in devices:
            _release(device)
        raise
    finally:
        _release(collection)


def _activate_audio_session_manager(device: ctypes.c_void_p) -> ctypes.c_void_p:
    manager = ctypes.c_void_p()
    iid = _guid("{77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F}")
    _check_hresult(
        _method(device, 3, ctypes.c_long, ctypes.POINTER(GUID), ctypes.c_ulong, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))(
            device,
            ctypes.byref(iid),
            CLSCTX_ALL,
            None,
            ctypes.byref(manager),
        ),
        "IMMDevice.Activate",
    )
    return manager


def _get_session_enumerator(manager: ctypes.c_void_p) -> ctypes.c_void_p:
    session_enum = ctypes.c_void_p()
    _check_hresult(
        _method(manager, 5, ctypes.c_long, ctypes.POINTER(ctypes.c_void_p))(manager, ctypes.byref(session_enum)),
        "IAudioSessionManager2.GetSessionEnumerator",
    )
    return session_enum


def _query_interface(pointer: ctypes.c_void_p, iid) -> ctypes.c_void_p:
    result_pointer = ctypes.c_void_p()
    _check_hresult(
        _method(pointer, 0, ctypes.c_long, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p))(
            pointer,
            ctypes.byref(iid),
            ctypes.byref(result_pointer),
        ),
        "IUnknown.QueryInterface",
    )
    return result_pointer


def _release(pointer: ctypes.c_void_p) -> None:
    if pointer:
        _method(pointer, 2, ctypes.c_ulong)(pointer)


def _method(pointer: ctypes.c_void_p, index: int, restype, *argtypes):
    vtable = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    address = vtable[index]
    return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(address)


def _check_hresult(result: int, operation: str) -> None:
    if ctypes.c_long(result).value < 0:
        raise AudioMuteError(f"{operation} failed: HRESULT 0x{ctypes.c_ulong(result).value:08X}")


def _get_process_name(pid: int) -> str:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return ""
    try:
        size = ctypes.c_ulong(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return ""
        return Path(buffer.value).name
    finally:
        kernel32.CloseHandle(handle)


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]


def _guid(value: str) -> GUID:
    guid = GUID()
    _check_hresult(_ole32().CLSIDFromString(value, ctypes.byref(guid)), "CLSIDFromString")
    return guid


def _ole32():
    library = ctypes.WinDLL("ole32")
    library.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    library.CoInitializeEx.restype = ctypes.c_long
    library.CoUninitialize.argtypes = []
    library.CoUninitialize.restype = None
    library.CoCreateInstance.argtypes = [ctypes.POINTER(GUID), ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)]
    library.CoCreateInstance.restype = ctypes.c_long
    library.CLSIDFromString.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(GUID)]
    library.CLSIDFromString.restype = ctypes.c_long
    return library
