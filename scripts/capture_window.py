"""
启动指定 exe，等待其窗口出现，截图保存到 PNG。
仅依赖 pillow + psutil（不再依赖 pywin32，靠 ctypes 调用 user32）。

用法：
    python scripts/capture_window.py <exe_path> <out_png> [<settle_seconds>]
"""
import sys
import time
import ctypes
import ctypes.wintypes as wt
import subprocess
from pathlib import Path

import psutil  # type: ignore
from PIL import ImageGrab  # type: ignore

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
SW_RESTORE = 9


def get_window_text(hwnd):
    buf = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(hwnd, buf, 512)
    return buf.value


def get_window_pid(hwnd):
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
        if get_window_pid(hwnd) not in targets:
            return True
        title = get_window_text(hwnd)
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


def capture(exe_path: str, out_png: str, settle: float = 6.0) -> dict:
    out = Path(out_png)
    out.parent.mkdir(parents=True, exist_ok=True)

    proc = subprocess.Popen([exe_path])
    print(f"[start] pid={proc.pid}")

    deadline = time.time() + settle + 12
    win = None
    while time.time() < deadline:
        time.sleep(0.5)
        win = find_top_window(proc.pid)
        if win:
            break

    if not win:
        proc.kill()
        return {"ok": False, "error": "window not found"}

    hwnd, title, rect, _ = win
    print(f"[found] hWnd={hwnd} title={title!r} rect={rect}")

    try:
        user32.ShowWindow(hwnd, SW_RESTORE)
        user32.SetForegroundWindow(hwnd)
    except Exception as e:
        print(f"[warn] focus failed: {e}")
    time.sleep(settle)

    img = ImageGrab.grab(bbox=rect, all_screens=True)
    img.save(out_png)
    print(f"[saved] {out_png}  size={img.size}")

    try:
        ps = psutil.Process(proc.pid)
        total = ps.memory_info().rss
        for c in ps.children(recursive=True):
            try:
                total += c.memory_info().rss
            except psutil.NoSuchProcess:
                pass
        mem_mb = round(total / (1024 * 1024), 1)
    except Exception:
        mem_mb = -1

    proc.terminate()
    try:
        proc.wait(timeout=3)
    except Exception:
        proc.kill()

    return {"ok": True, "title": title, "rect": rect, "memory_mb": mem_mb}


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    exe = sys.argv[1]
    out = sys.argv[2]
    settle = float(sys.argv[3]) if len(sys.argv) > 3 else 6.0
    if not Path(exe).is_file():
        print(f"[ERR] not found: {exe}")
        sys.exit(2)
    r = capture(exe, out, settle)
    print(r)


if __name__ == "__main__":
    main()
