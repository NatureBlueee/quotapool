"""Native Windows tray monitor for the loopback Quotapool dashboard.

The monitor intentionally has no GUI-toolkit dependency.  It talks only to
the existing loopback dashboard API and uses Shell_NotifyIcon for the small
piece of UI that should remain visible all day.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence

from .desktop import (
    _start_dashboard_background,
    local_port_ready,
    load_runtime,
    start_services,
    stop_services,
)


WINDOW_CLASS = "QuotapoolTrayMonitorWindow"
MUTEX_NAME = "Local\\QuotapoolTrayMonitor"
WM_APP = 0x8000
WM_TRAY = WM_APP + 1
WM_STATUS_READY = WM_APP + 2
WM_ACTION_READY = WM_APP + 3

WM_CLOSE = 0x0010
WM_DESTROY = 0x0002
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
WM_CONTEXTMENU = 0x007B

NIM_ADD = 0x00000000
NIM_MODIFY = 0x00000001
NIM_DELETE = 0x00000002
NIF_MESSAGE = 0x00000001
NIF_ICON = 0x00000002
NIF_TIP = 0x00000004
NIF_INFO = 0x00000010
NIIF_INFO = 0x00000001
NIIF_WARNING = 0x00000002
NIIF_ERROR = 0x00000003

MF_STRING = 0x00000000
MF_SEPARATOR = 0x00000800
MF_GRAYED = 0x00000001
TPM_RETURNCMD = 0x0100
TPM_NONOTIFY = 0x0080
TPM_RIGHTBUTTON = 0x0002
MB_YESNO = 0x00000004
MB_ICONWARNING = 0x00000030
MB_DEFBUTTON2 = 0x00000100
IDYES = 6
ERROR_ALREADY_EXISTS = 183

CMD_OPEN = 1001
CMD_REFRESH = 1002
CMD_REPAIR = 1003
CMD_RESTART = 1004
CMD_LOGS = 1005
CMD_EXIT = 1099


@dataclass(frozen=True)
class TrayStatus:
    level: str
    badge: str
    title: str
    tooltip: str
    detail: str
    available: int = 0
    total: int = 0
    active: int = 0


def _account_available(account: Mapping[str, Any]) -> bool:
    if account.get("disabled") or account.get("retired"):
        return False
    state = str(account.get("status") or "").lower()
    if state in {
        "disabled", "retired", "expired", "exhausted", "unavailable",
        "invalid", "paused", "rate_limited", "ratelimited",
    }:
        return False
    quota = account.get("quota")
    quota_state = (
        str(quota.get("unifiedStatus") or "").lower()
        if isinstance(quota, Mapping) else ""
    )
    return quota_state not in {"exhausted", "blocked", "denied", "expired"}


def status_from_snapshot(
    snapshot: Optional[Mapping[str, Any]], *, error: str = ""
) -> TrayStatus:
    """Reduce the large dashboard payload to stable tray semantics."""
    if error or not snapshot:
        detail = error or "无法读取本地状态"
        return TrayStatus(
            "error", "!", "QuotaPool 无法连接",
            f"QuotaPool 无法连接 | {detail}"[:127], detail,
        )

    services = snapshot.get("services")
    services = services if isinstance(services, Mapping) else {}
    service_ok = bool(services.get("coordinator") and services.get("dataplane"))
    plane = snapshot.get("dataPlane")
    plane = plane if isinstance(plane, Mapping) else {}
    raw_accounts = plane.get("accounts") or snapshot.get("accounts") or []
    accounts = [item for item in raw_accounts if isinstance(item, Mapping)]
    available = sum(1 for item in accounts if _account_available(item))
    total = len(accounts)
    sessions = plane.get("sessions")
    sessions = sessions if isinstance(sessions, Mapping) else {}
    active = int(sessions.get("active") or 0)
    waiting = plane.get("waiting")
    waiting = waiting if isinstance(waiting, Mapping) else {}
    waiting_count = int(waiting.get("count") or 0)
    current = str(plane.get("currentAccount") or "未选择")

    dashboard = snapshot.get("dashboard")
    dashboard = dashboard if isinstance(dashboard, Mapping) else {}
    host = dashboard.get("windowsHost")
    host = host if isinstance(host, Mapping) else {}
    host_warn = bool(
        host.get("isWindows")
        and not host.get("refreshing")
        and not host.get("healthy", True)
    )

    if not service_ok:
        level, badge, title = "error", "!", "QuotaPool 服务异常"
        detail = "协调器或数据面未就绪"
    elif available == 0:
        level, badge, title = "error", "0", "QuotaPool 无可用账号"
        detail = (
            "尚未添加账号" if total == 0 else "所有账号均不可用或额度受限"
        )
    elif host_warn:
        level, badge, title = "warning", str(min(available, 99)), "C 盘空间需检查"
        detail = "Windows 磁盘空间偏低或维护状态异常"
    elif waiting_count:
        level, badge, title = "warning", str(min(available, 99)), "QuotaPool 有任务等待"
        detail = f"{waiting_count} 个会话正在等待额度恢复"
    else:
        level, badge, title = "ok", str(min(available, 99)), "QuotaPool 正常"
        detail = f"可用账号 {available}/{total}，当前 {current}"

    tooltip = (
        f"{title} | 可用 {available}/{total} | 当前 {current} | 活跃会话 {active}"
    )[:127]
    return TrayStatus(
        level, badge, title, tooltip, detail,
        available=available, total=total, active=active,
    )


def _fetch_snapshot(url: str, timeout: float = 8.0) -> Dict[str, Any]:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(
        url, headers={"Accept": "application/json", "Cache-Control": "no-cache"}
    )
    with opener.open(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("状态接口返回了无效数据")
    return payload


def _logs_directory(runtime: Mapping[str, Any]) -> Path:
    return Path(str(runtime["db_path"])).expanduser().parent / "logs"


def _shutdown_existing() -> bool:
    if os.name != "nt":
        return False
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    user32.FindWindowW.restype = wintypes.HWND
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.PostMessageW.restype = wintypes.BOOL
    hwnd = user32.FindWindowW(WINDOW_CLASS, None)
    return bool(hwnd and user32.PostMessageW(hwnd, WM_CLOSE, 0, 0))


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class _NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uTimeoutOrVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", _GUID),
        ("hBalloonIcon", wintypes.HICON),
    ]


class _POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", _BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


class _ICONINFO(ctypes.Structure):
    _fields_ = [
        ("fIcon", wintypes.BOOL),
        ("xHotspot", wintypes.DWORD),
        ("yHotspot", wintypes.DWORD),
        ("hbmMask", wintypes.HBITMAP),
        ("hbmColor", wintypes.HBITMAP),
    ]


class NativeTray:
    def __init__(
        self, runtime_path: Optional[str], host: str, port: int, poll_seconds: float
    ) -> None:
        self.runtime_path = runtime_path
        self.runtime = load_runtime(runtime_path)
        self.host = host
        self.port = port
        self.url = f"http://{host}:{port}/"
        self.snapshot_url = f"{self.url}api/snapshot"
        self.poll_seconds = max(2.0, poll_seconds)
        self.status = status_from_snapshot(None, error="正在连接本地服务")
        self._last_notified_level: Optional[str] = None
        self._status_lock = threading.Lock()
        self._stop = threading.Event()
        self._icon: Optional[int] = None
        self._hwnd: Optional[int] = None
        self._action_message = ""

        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
        self._configure_apis()

        self.WNDPROC = ctypes.WINFUNCTYPE(
            ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
            wintypes.WPARAM, wintypes.LPARAM,
        )
        self._wndproc_ref = self.WNDPROC(self._wndproc)

    def _fetch_with_dashboard_repair(self) -> Dict[str, Any]:
        try:
            return _fetch_snapshot(self.snapshot_url)
        except Exception:
            # The monitor owns the dashboard's availability, but not the
            # coordinator/data-plane lifecycle. Recreating this read-only
            # loopback process cannot interrupt an active Claude request.
            # A busy snapshot can exceed the HTTP timeout while the server is
            # still healthy. Never create a second dashboard when the port is
            # already listening.
            if local_port_ready(self.host, self.port):
                raise
            _start_dashboard_background(self.runtime_path, self.host, self.port, False)
            return _fetch_snapshot(self.snapshot_url)

    def _configure_apis(self) -> None:
        self.shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(_NOTIFYICONDATAW)]
        self.shell32.Shell_NotifyIconW.restype = wintypes.BOOL
        self.user32.DefWindowProcW.argtypes = [
            wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
        ]
        self.user32.DefWindowProcW.restype = ctypes.c_ssize_t
        self.user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        self.user32.PostMessageW.restype = wintypes.BOOL
        self.user32.DestroyWindow.argtypes = [wintypes.HWND]
        self.user32.DestroyWindow.restype = wintypes.BOOL
        self.user32.PostQuitMessage.argtypes = [ctypes.c_int]
        self.user32.DestroyIcon.argtypes = [wintypes.HICON]
        self.user32.DestroyIcon.restype = wintypes.BOOL
        self.user32.CreateIconIndirect.argtypes = [ctypes.POINTER(_ICONINFO)]
        self.user32.CreateIconIndirect.restype = wintypes.HICON
        self.user32.DrawTextW.argtypes = [
            wintypes.HDC, wintypes.LPCWSTR, ctypes.c_int,
            ctypes.POINTER(wintypes.RECT), wintypes.UINT,
        ]
        self.user32.DrawTextW.restype = ctypes.c_int
        self.user32.CreatePopupMenu.restype = wintypes.HMENU
        self.user32.AppendMenuW.argtypes = [
            wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR,
        ]
        self.user32.AppendMenuW.restype = wintypes.BOOL
        self.user32.TrackPopupMenu.restype = wintypes.UINT
        self.user32.TrackPopupMenu.argtypes = [
            wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, wintypes.HWND, ctypes.c_void_p,
        ]
        self.user32.DestroyMenu.argtypes = [wintypes.HMENU]
        self.user32.DestroyMenu.restype = wintypes.BOOL
        self.user32.GetCursorPos.argtypes = [ctypes.POINTER(_POINT)]
        self.user32.GetCursorPos.restype = wintypes.BOOL
        self.user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        self.user32.SetForegroundWindow.restype = wintypes.BOOL
        self.user32.MessageBoxW.argtypes = [
            wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT,
        ]
        self.user32.MessageBoxW.restype = ctypes.c_int
        self.user32.GetMessageW.argtypes = [
            ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT,
        ]
        self.user32.GetMessageW.restype = wintypes.BOOL
        self.user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
        self.user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
        self.user32.DispatchMessageW.restype = ctypes.c_ssize_t
        self.gdi32.CreateDIBSection.argtypes = [
            wintypes.HDC, ctypes.POINTER(_BITMAPINFO), wintypes.UINT,
            ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD,
        ]
        self.gdi32.CreateDIBSection.restype = wintypes.HBITMAP
        self.gdi32.CreateBitmap.argtypes = [
            ctypes.c_int, ctypes.c_int, wintypes.UINT, wintypes.UINT,
            ctypes.c_void_p,
        ]
        self.gdi32.CreateBitmap.restype = wintypes.HBITMAP
        self.gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
        self.gdi32.CreateCompatibleDC.restype = wintypes.HDC
        self.gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
        self.gdi32.SelectObject.restype = wintypes.HGDIOBJ
        self.gdi32.CreateSolidBrush.argtypes = [wintypes.DWORD]
        self.gdi32.CreateSolidBrush.restype = wintypes.HBRUSH
        self.gdi32.CreateFontW.restype = wintypes.HFONT
        self.gdi32.CreateFontW.argtypes = [
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
            wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
            wintypes.LPCWSTR,
        ]
        self.gdi32.SetBkMode.argtypes = [wintypes.HDC, ctypes.c_int]
        self.gdi32.SetTextColor.argtypes = [wintypes.HDC, wintypes.DWORD]
        self.gdi32.Ellipse.argtypes = [
            wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        ]
        self.gdi32.DeleteDC.argtypes = [wintypes.HDC]
        self.gdi32.DeleteDC.restype = wintypes.BOOL
        self.gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
        self.gdi32.DeleteObject.restype = wintypes.BOOL

    def _icon_data(self, flags: int) -> _NOTIFYICONDATAW:
        data = _NOTIFYICONDATAW()
        data.cbSize = ctypes.sizeof(data)
        data.hWnd = self._hwnd
        data.uID = 1
        data.uFlags = flags
        data.uCallbackMessage = WM_TRAY
        data.hIcon = self._icon
        data.szTip = self.status.tooltip
        return data

    def _make_icon(self, level: str, label: str) -> int:
        size = 32
        bmi = _BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = size
        bmi.bmiHeader.biHeight = -size
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bits = ctypes.c_void_p()
        color = self.gdi32.CreateDIBSection(
            0, ctypes.byref(bmi), 0, ctypes.byref(bits), 0, 0
        )
        mask_bytes = (ctypes.c_ubyte * (size * size // 8))()
        mask = self.gdi32.CreateBitmap(
            size, size, 1, 1, ctypes.cast(mask_bytes, ctypes.c_void_p)
        )
        dc = self.gdi32.CreateCompatibleDC(0)
        old_bitmap = self.gdi32.SelectObject(dc, color)

        rgb = {
            "ok": 0x005AC45A,
            "warning": 0x0000A5FF,
            "error": 0x004848E0,
        }.get(level, 0x00808080)
        brush = self.gdi32.CreateSolidBrush(rgb)
        old_brush = self.gdi32.SelectObject(dc, brush)
        self.gdi32.Ellipse(dc, 1, 1, size - 1, size - 1)
        self.gdi32.SelectObject(dc, old_brush)
        self.gdi32.DeleteObject(brush)

        height = 18 if len(label) == 1 else 14
        font = self.gdi32.CreateFontW(
            -height, 0, 0, 0, 700, 0, 0, 0, 1, 0, 0, 4, 0, "Segoe UI"
        )
        old_font = self.gdi32.SelectObject(dc, font)
        self.gdi32.SetBkMode(dc, 1)
        self.gdi32.SetTextColor(dc, 0x00FFFFFF)
        rect = wintypes.RECT(0, 5 if len(label) == 1 else 7, size, size)
        self.user32.DrawTextW(dc, label, -1, ctypes.byref(rect), 0x0001 | 0x0004 | 0x0020)
        self.gdi32.SelectObject(dc, old_font)
        self.gdi32.DeleteObject(font)
        self.gdi32.SelectObject(dc, old_bitmap)
        self.gdi32.DeleteDC(dc)

        pixels = (ctypes.c_ubyte * (size * size * 4)).from_address(bits.value)
        for index in range(0, len(pixels), 4):
            if pixels[index] or pixels[index + 1] or pixels[index + 2]:
                pixels[index + 3] = 255

        info = _ICONINFO(True, 0, 0, mask, color)
        icon = self.user32.CreateIconIndirect(ctypes.byref(info))
        self.gdi32.DeleteObject(mask)
        self.gdi32.DeleteObject(color)
        if not icon:
            raise ctypes.WinError(ctypes.get_last_error())
        return int(icon)

    def _apply_status(self, *, notify: bool = True) -> None:
        new_icon = self._make_icon(self.status.level, self.status.badge)
        old_icon, self._icon = self._icon, new_icon
        data = self._icon_data(NIF_MESSAGE | NIF_ICON | NIF_TIP)
        self.shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(data))
        if old_icon:
            self.user32.DestroyIcon(old_icon)
        if notify and self._last_notified_level is not None:
            if self.status.level != self._last_notified_level:
                self._notify(self.status.title, self.status.detail, self.status.level)
        elif notify and self.status.level == "error":
            self._notify(self.status.title, self.status.detail, self.status.level)
        self._last_notified_level = self.status.level

    def _notify(self, title: str, message: str, level: str = "ok") -> None:
        data = self._icon_data(NIF_INFO)
        data.szInfoTitle = title[:63]
        data.szInfo = message[:255]
        data.dwInfoFlags = {
            "error": NIIF_ERROR, "warning": NIIF_WARNING
        }.get(level, NIIF_INFO)
        self.shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(data))

    def _poll(self) -> None:
        while not self._stop.is_set():
            try:
                snapshot = self._fetch_with_dashboard_repair()
                status = status_from_snapshot(snapshot)
            except (OSError, ValueError, RuntimeError, urllib.error.URLError) as exc:
                status = status_from_snapshot(None, error=str(exc))
            with self._status_lock:
                self.status = status
            if self._hwnd:
                self.user32.PostMessageW(self._hwnd, WM_STATUS_READY, 0, 0)
            self._stop.wait(self.poll_seconds)

    def _run_action(self, action: str) -> None:
        def worker() -> None:
            try:
                if action == "repair":
                    start_services(self.runtime_path)
                    message = "后台服务已确认就绪"
                else:
                    stop_services(self.runtime_path)
                    start_services(self.runtime_path)
                    message = "协调器和数据面已重新启动"
                level = "ok"
            except Exception as exc:
                message, level = str(exc), "error"
            self._action_message = json.dumps({"message": message, "level": level}, ensure_ascii=False)
            if self._hwnd:
                self.user32.PostMessageW(self._hwnd, WM_ACTION_READY, 0, 0)
        threading.Thread(target=worker, name=f"quotapool-tray-{action}", daemon=True).start()

    def _show_menu(self) -> None:
        menu = self.user32.CreatePopupMenu()
        self.user32.AppendMenuW(menu, MF_STRING | MF_GRAYED, 0, self.status.tooltip)
        self.user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        self.user32.AppendMenuW(menu, MF_STRING, CMD_OPEN, "打开完整监控面板")
        self.user32.AppendMenuW(menu, MF_STRING, CMD_REFRESH, "立即刷新")
        self.user32.AppendMenuW(menu, MF_STRING, CMD_REPAIR, "启动 / 修复后台服务")
        self.user32.AppendMenuW(menu, MF_STRING, CMD_RESTART, "重启后台服务…")
        self.user32.AppendMenuW(menu, MF_STRING, CMD_LOGS, "打开日志目录")
        self.user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        self.user32.AppendMenuW(menu, MF_STRING, CMD_EXIT, "退出监控器")
        point = _POINT()
        self.user32.GetCursorPos(ctypes.byref(point))
        self.user32.SetForegroundWindow(self._hwnd)
        command = self.user32.TrackPopupMenu(
            menu, TPM_RETURNCMD | TPM_NONOTIFY | TPM_RIGHTBUTTON,
            point.x, point.y, 0, self._hwnd, None,
        )
        self.user32.DestroyMenu(menu)
        self._handle_command(command)

    def _handle_command(self, command: int) -> None:
        if command == CMD_OPEN:
            webbrowser.open(self.url)
        elif command == CMD_REFRESH:
            threading.Thread(target=self._poll_once, daemon=True).start()
        elif command == CMD_REPAIR:
            self._run_action("repair")
        elif command == CMD_RESTART:
            answer = self.user32.MessageBoxW(
                self._hwnd,
                "重启会短暂中断正在发送的新请求。确认重启 Quotapool 后台服务吗？",
                "QuotaPool",
                MB_YESNO | MB_ICONWARNING | MB_DEFBUTTON2,
            )
            if answer == IDYES:
                self._run_action("restart")
        elif command == CMD_LOGS:
            path = _logs_directory(self.runtime)
            path.mkdir(parents=True, exist_ok=True)
            os.startfile(str(path))
        elif command == CMD_EXIT:
            self.user32.PostMessageW(self._hwnd, WM_CLOSE, 0, 0)

    def _poll_once(self) -> None:
        try:
            status = status_from_snapshot(self._fetch_with_dashboard_repair())
        except Exception as exc:
            status = status_from_snapshot(None, error=str(exc))
        with self._status_lock:
            self.status = status
        if self._hwnd:
            self.user32.PostMessageW(self._hwnd, WM_STATUS_READY, 0, 0)

    def _wndproc(self, hwnd: int, message: int, wparam: int, lparam: int) -> int:
        if message == WM_TRAY:
            event = int(lparam) & 0xFFFF
            if event in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                webbrowser.open(self.url)
            elif event in (WM_RBUTTONUP, WM_CONTEXTMENU):
                self._show_menu()
            return 0
        if message == WM_STATUS_READY:
            with self._status_lock:
                self._apply_status()
            return 0
        if message == WM_ACTION_READY:
            try:
                result = json.loads(self._action_message)
            except (TypeError, json.JSONDecodeError):
                result = {"message": "操作已完成", "level": "ok"}
            self._notify("QuotaPool", result["message"], result["level"])
            threading.Thread(target=self._poll_once, daemon=True).start()
            return 0
        if message == WM_CLOSE:
            self.user32.DestroyWindow(hwnd)
            return 0
        if message == WM_DESTROY:
            self._stop.set()
            if self._icon:
                data = self._icon_data(0)
                self.shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(data))
                self.user32.DestroyIcon(self._icon)
                self._icon = None
            self.user32.PostQuitMessage(0)
            return 0
        return self.user32.DefWindowProcW(hwnd, message, wparam, lparam)

    def run(self) -> int:
        self.kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        self.kernel32.GetModuleHandleW.restype = wintypes.HMODULE
        hinstance = self.kernel32.GetModuleHandleW(None)

        class WNDCLASSEXW(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.UINT), ("style", wintypes.UINT),
                ("lpfnWndProc", self.WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HICON), ("hCursor", wintypes.HANDLE),
                ("hbrBackground", wintypes.HBRUSH), ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR), ("hIconSm", wintypes.HICON),
            ]

        window_class = WNDCLASSEXW()
        window_class.cbSize = ctypes.sizeof(window_class)
        window_class.lpfnWndProc = self._wndproc_ref
        window_class.hInstance = hinstance
        window_class.lpszClassName = WINDOW_CLASS
        self.user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
        self.user32.RegisterClassExW.restype = wintypes.ATOM
        self.user32.CreateWindowExW.argtypes = [
            wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p,
        ]
        self.user32.RegisterClassExW(ctypes.byref(window_class))
        self.user32.CreateWindowExW.restype = wintypes.HWND
        self._hwnd = self.user32.CreateWindowExW(
            0, WINDOW_CLASS, "QuotaPool", 0, 0, 0, 0, 0,
            None, None, hinstance, None,
        )
        if not self._hwnd:
            raise ctypes.WinError(ctypes.get_last_error())

        self._icon = self._make_icon(self.status.level, self.status.badge)
        data = self._icon_data(NIF_MESSAGE | NIF_ICON | NIF_TIP)
        if not self.shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(data)):
            raise ctypes.WinError(ctypes.get_last_error())

        threading.Thread(target=self._poll, name="quotapool-tray-poll", daemon=True).start()
        message = wintypes.MSG()
        while self.user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            self.user32.TranslateMessage(ctypes.byref(message))
            self.user32.DispatchMessageW(ctypes.byref(message))
        return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="quotapool-tray")
    parser.add_argument("--runtime")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=17892)
    parser.add_argument("--poll-seconds", type=float, default=5.0)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--shutdown", action="store_true")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    if os.name != "nt":
        print("quotapool-tray is available on Windows only")
        return 78
    if args.shutdown:
        return 0 if _shutdown_existing() else 1
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        print("quotapool-tray dashboard must be loopback-only")
        return 78

    _start_dashboard_background(args.runtime, args.host, args.port, False)
    snapshot_url = f"http://{args.host}:{args.port}/api/snapshot"
    if args.once:
        try:
            status = status_from_snapshot(_fetch_snapshot(snapshot_url))
        except Exception as exc:
            status = status_from_snapshot(None, error=str(exc))
        # ``--once`` is also used by preflight scripts whose redirected
        # Windows console can still report cp1252. Keep that diagnostic path
        # ASCII-safe; the actual tray UI remains Unicode.
        print(json.dumps(status.__dict__, ensure_ascii=True, indent=2))
        return 0 if status.level != "error" else 1

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    mutex = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        webbrowser.open(f"http://{args.host}:{args.port}/")
        if mutex:
            kernel32.CloseHandle(mutex)
        return 0
    try:
        return NativeTray(args.runtime, args.host, args.port, args.poll_seconds).run()
    finally:
        if mutex:
            kernel32.CloseHandle(mutex)


if __name__ == "__main__":
    raise SystemExit(main())
