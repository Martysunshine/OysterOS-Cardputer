"""The real `OsLayer` of the Windows executor: Win32 calls through ctypes, psutil and one UDP socket.

This is the only module of the Control Deck that touches the operating system. It runs with the rights of the
account that started the backend and never elevates (no UAC prompt, no `runas`). Nothing here is reachable
except through `WindowsExecutor`, which has already checked host, allowlists and elevation.

Not verified on a real interactive session: the media-key presses, the lock and the service restart are
covered by unit tests against a fake layer only (see docs/CONTROL_DECK.md).
"""

from __future__ import annotations

import ctypes
import os
import socket
import sys
import time

from oyster.deck.actions import ActionOutcomeUnknown
from oyster.deck.executor_windows import (
    DEFAULT_WOL_BROADCAST,
    ExecutorConfigError,
    HostHealth,
    OsFailure,
)

# Virtual-key codes of the media keys (winuser.h).
VK_BY_KEY = {
    "mute": 0xAD,  # VK_VOLUME_MUTE
    "vol_down": 0xAE,  # VK_VOLUME_DOWN
    "vol_up": 0xAF,  # VK_VOLUME_UP
    "next": 0xB0,  # VK_MEDIA_NEXT_TRACK
    "prev": 0xB1,  # VK_MEDIA_PREV_TRACK
    "play_pause": 0xB3,  # VK_MEDIA_PLAY_PAUSE
}

INPUT_KEYBOARD = 1
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002

SC_MANAGER_CONNECT = 0x0001
SERVICE_QUERY_STATUS = 0x0004
SERVICE_START = 0x0010
SERVICE_STOP = 0x0020
SERVICE_CONTROL_STOP = 0x00000001
SERVICE_STOPPED = 0x00000001
SERVICE_RUNNING = 0x00000004
ERROR_ACCESS_DENIED = 5
ERROR_SERVICE_DOES_NOT_EXIST = 1060
ERROR_SERVICE_NOT_ACTIVE = 1062
SERVICE_WAIT_S = 20.0  # per phase; two phases stay inside the 60 s action ceiling

if sys.platform == "win32":
    from ctypes import wintypes

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [
            ("dx", wintypes.LONG),
            ("dy", wintypes.LONG),
            ("mouseData", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", wintypes.WPARAM),
        ]

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", wintypes.WPARAM),
        ]

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]

    class _InputUnion(ctypes.Union):
        # all three members: the union (and so INPUT) must have the size SendInput expects
        _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]

    class INPUT(ctypes.Structure):
        _anonymous_ = ("u",)
        _fields_ = [("type", wintypes.DWORD), ("u", _InputUnion)]

    class SERVICE_STATUS(ctypes.Structure):
        _fields_ = [
            ("dwServiceType", wintypes.DWORD),
            ("dwCurrentState", wintypes.DWORD),
            ("dwControlsAccepted", wintypes.DWORD),
            ("dwWin32ExitCode", wintypes.DWORD),
            ("dwServiceSpecificExitCode", wintypes.DWORD),
            ("dwCheckPoint", wintypes.DWORD),
            ("dwWaitHint", wintypes.DWORD),
        ]


def _key_input(vk: int, flags: int) -> INPUT:
    item = INPUT()
    item.type = INPUT_KEYBOARD
    item.ki.wVk = vk
    item.ki.dwFlags = flags
    return item


class WindowsOsLayer:
    """Windows implementation of `executor_windows.OsLayer`. Constructing it touches nothing."""

    def __init__(self, wol_broadcast: str = DEFAULT_WOL_BROADCAST, wol_port: int = 9):
        if sys.platform != "win32":
            raise ExecutorConfigError("OYSTER_DECK_EXECUTOR=windows needs Windows")
        self._broadcast = wol_broadcast
        self._port = wol_port
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
        user32.SendInput.restype = wintypes.UINT
        user32.LockWorkStation.argtypes = []
        user32.LockWorkStation.restype = wintypes.BOOL
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        shell32.IsUserAnAdmin.argtypes = []
        shell32.IsUserAnAdmin.restype = wintypes.BOOL
        adv = ctypes.WinDLL("advapi32", use_last_error=True)
        adv.OpenSCManagerW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
        adv.OpenSCManagerW.restype = wintypes.HANDLE
        adv.OpenServiceW.argtypes = [wintypes.HANDLE, wintypes.LPCWSTR, wintypes.DWORD]
        adv.OpenServiceW.restype = wintypes.HANDLE
        adv.ControlService.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(SERVICE_STATUS)]
        adv.ControlService.restype = wintypes.BOOL
        adv.StartServiceW.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p]
        adv.StartServiceW.restype = wintypes.BOOL
        adv.QueryServiceStatus.argtypes = [wintypes.HANDLE, ctypes.POINTER(SERVICE_STATUS)]
        adv.QueryServiceStatus.restype = wintypes.BOOL
        adv.CloseServiceHandle.argtypes = [wintypes.HANDLE]
        adv.CloseServiceHandle.restype = wintypes.BOOL
        self._user32, self._shell32, self._adv = user32, shell32, adv

    # --- input and session
    def send_media_key(self, key: str) -> None:
        vk = VK_BY_KEY.get(key)
        if vk is None:
            raise OsFailure("bad_key", "refused: unknown media key")
        down = _key_input(vk, KEYEVENTF_EXTENDEDKEY)
        up = _key_input(vk, KEYEVENTF_EXTENDEDKEY | KEYEVENTF_KEYUP)
        events = (INPUT * 2)(down, up)
        sent = self._user32.SendInput(2, events, ctypes.sizeof(INPUT))
        if sent == 1:  # only the press went through: release it so the key is not left down
            self._user32.SendInput(1, ctypes.byref(events[1]), ctypes.sizeof(INPUT))
        if sent != 2:
            raise OsFailure("input_blocked", "Windows blocked the key press")

    def lock_workstation(self) -> None:
        if not self._user32.LockWorkStation():
            raise OsFailure("lock_failed", "Windows refused the lock request")

    # --- read-only state
    def host_health(self) -> HostHealth:
        import psutil

        drive = (os.environ.get("SystemDrive") or "C:") + "\\"
        return HostHealth(
            cpu_percent=psutil.cpu_percent(interval=0.25),
            memory_percent=psutil.virtual_memory().percent,
            disk_percent=psutil.disk_usage(drive).percent,
        )

    def service_status(self, name: str) -> str | None:
        import psutil

        try:
            return str(psutil.win_service_get(name).status())
        except psutil.NoSuchProcess:
            return None
        except psutil.AccessDenied:
            raise OsFailure("access_denied", "Windows did not allow reading this service") from None

    # --- network
    def wake_on_lan(self, mac: str) -> None:
        packet = bytes([0xFF]) * 6 + bytes.fromhex(mac.replace(":", "")) * 16
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                sock.settimeout(3.0)
                sock.sendto(packet, (self._broadcast, self._port))
        except OSError:
            raise OsFailure("wol_send_failed", "could not send the magic packet") from None

    # --- services (administrator rights)
    def is_elevated(self) -> bool:
        return bool(self._shell32.IsUserAnAdmin())

    def restart_service(self, name: str) -> None:
        adv = self._adv
        manager = adv.OpenSCManagerW(None, None, SC_MANAGER_CONNECT)
        if not manager:
            raise OsFailure("service_manager", "could not open the Windows service manager")
        try:
            handle = adv.OpenServiceW(manager, name, SERVICE_QUERY_STATUS | SERVICE_START | SERVICE_STOP)
            if not handle:
                err = ctypes.get_last_error()
                if err == ERROR_SERVICE_DOES_NOT_EXIST:
                    raise OsFailure("service_not_found", f"service {name} not found")
                if err == ERROR_ACCESS_DENIED:
                    raise OsFailure("needs_admin", "needs administrator rights; nothing was restarted")
                raise OsFailure("service_open_failed", "could not open the service")
            try:
                self._stop_and_start(handle)
            finally:
                adv.CloseServiceHandle(handle)
        finally:
            adv.CloseServiceHandle(manager)

    def _state(self, handle) -> int:
        status = SERVICE_STATUS()
        if not self._adv.QueryServiceStatus(handle, ctypes.byref(status)):
            raise ActionOutcomeUnknown("service state could not be read")
        return int(status.dwCurrentState)

    def _wait_for(self, handle, wanted: int) -> bool:
        deadline = time.monotonic() + SERVICE_WAIT_S
        while time.monotonic() < deadline:
            if self._state(handle) == wanted:
                return True
            time.sleep(0.25)
        return self._state(handle) == wanted

    def _stop_and_start(self, handle) -> None:
        adv = self._adv
        status = SERVICE_STATUS()
        if not adv.ControlService(handle, SERVICE_CONTROL_STOP, ctypes.byref(status)):
            err = ctypes.get_last_error()
            if err == ERROR_ACCESS_DENIED:
                raise OsFailure("needs_admin", "needs administrator rights; nothing was restarted")
            if err != ERROR_SERVICE_NOT_ACTIVE:  # already stopped is fine: only start it
                raise OsFailure("stop_failed", "Windows refused to stop the service; it was not restarted")
        if not self._wait_for(handle, SERVICE_STOPPED):
            raise ActionOutcomeUnknown("service did not report stopped in time")
        if not adv.StartServiceW(handle, 0, None):
            raise OsFailure("restart_incomplete", "service was stopped but could not be started again")
        if not self._wait_for(handle, SERVICE_RUNNING):
            raise ActionOutcomeUnknown("service did not report running in time")
