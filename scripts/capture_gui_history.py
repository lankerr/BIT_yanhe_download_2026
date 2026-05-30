"""
启动源码版 GUI（python app_simple.py），等待窗口出现，
分别截图 ▼ 历史 折叠 / 展开两种状态。

用法：
    python scripts/capture_gui_history.py [入口脚本] [输出前缀]
    默认：app_simple.py  → docs/screenshots/simple_login.png + simple_history_open.png

依赖：pillow + psutil（已在 requirements 中）。
"""
from __future__ import annotations

import os
import sys
import time
import ctypes
import ctypes.wintypes as wt
import subprocess
from pathlib import Path

import psutil  # type: ignore
from PIL import ImageGrab  # type: ignore

ROOT = Path(__file__).resolve().parent.parent

user32 = ctypes.WinDLL("user32", use_last_error=True)

EnumWindowsProc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
user32.EnumWindows.argtypes = [EnumWindowsProc, wt.LPARAM]
user32.EnumWindows.restype = wt.BOOL
user32.IsWindowVisible.argtypes = [wt.HWND]
user32.IsWindowVisible.restype = wt.BOOL
user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
user32.GetWindowThreadProcessId.restype = wt.DWORD
user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
user32.GetWindowRect.restype = wt.BOOL
user32.SetForegroundWindow.argtypes = [wt.HWND]
user32.SetForegroundWindow.restype = wt.BOOL
user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
user32.ShowWindow.restype = wt.BOOL

# 鼠标
user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
user32.SetCursorPos.restype = wt.BOOL
user32.mouse_event.argtypes = [
    wt.DWORD, wt.DWORD, wt.DWORD, wt.DWORD, ctypes.c_void_p,
]
user32.mouse_event.restype = None

SW_RESTORE = 9
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004


def _text(hwnd):
    buf = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(hwnd, buf, 512)
    return buf.value


def _pid(hwnd):
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def find_top_window(pid: int):
    targets = {pid}
    try:
        for c in psutil.Process(pid).children(recursive=True):
            targets.add(c.pid)
    except psutil.NoSuchProcess:
        pass

    found = []

    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        if _pid(hwnd) not in targets:
            return True
        title = _text(hwnd)
        if not title:
            return True
        rect = wt.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        w = rect.right - rect.left
        h = rect.bottom - rect.top
        if w < 200 or h < 150:
            return True
        found.append((hwnd, title, (rect.left, rect.top, rect.right, rect.bottom), w * h))
        return True

    user32.EnumWindows(EnumWindowsProc(cb), 0)
    if not found:
        return None
    found.sort(key=lambda x: -x[3])
    return found[0]


def click_at(x: int, y: int):
    user32.SetCursorPos(int(x), int(y))
    time.sleep(0.15)
    user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, None)
    time.sleep(0.05)
    user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, None)


def grab(rect, out: Path):
    out.parent.mkdir(parents=True, exist_ok=True)
    img = ImageGrab.grab(bbox=rect, all_screens=True)
    img.save(str(out))
    return img.size


def main():
    entry = sys.argv[1] if len(sys.argv) > 1 else "app_simple.py"
    prefix = sys.argv[2] if len(sys.argv) > 2 else "simple"
    out_dir = ROOT / "docs" / "screenshots"
    out_collapsed = out_dir / f"{prefix}_login.png"
    out_expanded = out_dir / f"{prefix}_history_open.png"

    proc = subprocess.Popen([sys.executable, str(ROOT / entry)], cwd=str(ROOT))
    print(f"[start] {entry}  pid={proc.pid}")

    deadline = time.time() + 20
    win = None
    while time.time() < deadline:
        time.sleep(0.6)
        win = find_top_window(proc.pid)
        if win:
            break
    if not win:
        proc.kill()
        raise SystemExit("[ERR] window not found")

    hwnd, title, rect, _ = win
    print(f"[found] hwnd={hwnd} title={title!r} rect={rect}")

    try:
        user32.ShowWindow(hwnd, SW_RESTORE)
        user32.SetForegroundWindow(hwnd)
    except Exception:
        pass
    time.sleep(2.0)

    # 折叠状态
    size1 = grab(rect, out_collapsed)
    print(f"[saved] {out_collapsed}  {size1}")

    # 计算 ▼ 历史 按钮的大致中心：
    #   课程 ID 输入行位于标题与副标题之下，左右 padx=40，按钮宽 80，高 40。
    # 经验定位：按钮中心约在 窗口右侧内边距 80 px 处，垂直方向距顶部 ≈ 230 px
    left, top, right, bottom = rect
    btn_x = right - 40 - 40  # 右内边距 40 + 按钮半宽 40
    btn_y = top + 230
    print(f"[click] history button at ({btn_x},{btn_y})")
    click_at(btn_x, btn_y)
    time.sleep(1.5)

    size2 = grab(rect, out_expanded)
    print(f"[saved] {out_expanded}  {size2}")

    proc.terminate()
    try:
        proc.wait(timeout=3)
    except Exception:
        proc.kill()


if __name__ == "__main__":
    main()
