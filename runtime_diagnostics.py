"""运行时诊断基础设施（仅标准库，必须在 gui_app/重依赖导入前初始化）。

职责：
- 持久日志：%LOCALAPPDATA%/yanhe/log/（失败回退 %TEMP%/yanhe/log/），按会话PID分文件
- sys.excepthook / threading.excepthook：未处理异常落盘
- faulthandler：原生崩溃尽力记录到独立 fatal 文件
- windowed 模式下 sys.stdout/stderr 为 None 的兜底
- 启动阶段打点：entry → before_gui_import → gui_imported → root_created
  → view_built → event_loop_alive → closing

打包（onefile/onedir）下 exe 不可写目录不再导致启动即崩：这里所有写失败都降级而非抛出。
"""

import faulthandler
import os
import sys
import threading
import time
from datetime import datetime

_PHASES = []
_LOG_DIR = None
_LOG_FILE = None
_INITIALIZED = False


def _ensure_dir(path: str) -> bool:
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, ".w")
        with open(probe, "w") as f:
            f.write("ok")
        os.remove(probe)
        return True
    except Exception:
        return False


class _NullStream:
    """windowed exe 下 stdout/stderr 为 None 时提供给进度条等第三方库的安全替身。"""

    def write(self, *_args, **_kwargs):
        return 0

    def flush(self):
        pass

    def isatty(self):
        return False

    @property
    def encoding(self):
        return "utf-8"


def _log_line(msg: str):
    if _LOG_FILE:
        try:
            with open(_LOG_FILE, "a", encoding="utf-8") as f:
                f.write(msg + "\n")
        except Exception:
            pass


def _excepthook(exc_type, exc_value, exc_tb):
    import traceback
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _log_line(f"[{stamp}] UNHANDLED {exc_type.__name__}: {exc_value}\n"
              + "".join(traceback.format_tb(exc_tb)))
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(
            0,
            f"程序遇到未处理错误，即将退出。\n\n{exc_type.__name__}: {exc_value}\n\n"
            f"日志已保存:\n{_LOG_FILE}",
            "延河课堂下载器 - 错误", 0x10)
    except Exception:
        pass


def _thread_excepthook(args):
    import traceback
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _log_line(
        f"[{stamp}] THREAD {args.name if args.thread else '?'} "
        f"{args.exc_type.__name__}: {args.exc_value}\n"
        + "".join(traceback.format_tb(args.exc_tb)))


def initialize() -> dict:
    """初始化持久诊断。必须在 customtkinter/requests/ctranslate2 等导入之前调用。"""
    global _LOG_DIR, _LOG_FILE, _INITIALIZED
    if _INITIALIZED:
        return {"log_dir": _LOG_DIR, "log_file": _LOG_FILE}
    _INITIALIZED = True

    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    candidates = [
        os.path.join(base, "yanhe", "log"),
        os.path.join(os.environ.get("TEMP", base), "yanhe", "log"),
    ]
    _LOG_DIR = next((d for d in candidates if _ensure_dir(d)), None)

    if _LOG_DIR:
        session = datetime.now().strftime("%Y%m%d-%H%M%S")
        _LOG_FILE = os.path.join(_LOG_DIR, f"session-{session}-{os.getpid()}.log")

    if _LOG_FILE:
        try:
            faulthandler.enable(file=open(
                os.path.join(_LOG_DIR, f"fatal-{os.getpid()}.log"), "a",
                encoding="utf-8"), all_threads=True)
        except Exception:
            pass

    # windowed 模式流兜底（在第三方库拿到 None 前替换）
    if sys.stdout is None:
        sys.stdout = _NullStream()
    if sys.stderr is None:
        sys.stderr = _NullStream()

    sys.excepthook = _excepthook
    threading.excepthook = _thread_excepthook

    phase("entry")
    _log_line(f"=== 会话开始 pid={os.getpid()} argv={sys.argv[:8]} "
              f"frozen={bool(getattr(sys, 'frozen', False))} ===")
    return {"log_dir": _LOG_DIR, "log_file": _LOG_FILE}


def phase(name: str):
    """启动阶段打点（区分『没有窗口』与『窗口黑着』的第一手证据）。"""
    entry_t = _PHASES[0][1] if _PHASES else time.monotonic()
    rec = (name, time.monotonic(), time.monotonic() - entry_t)
    _PHASES.append(rec)
    _log_line(f"PHASE {name} +{rec[2]:.3f}s")


def get_log_file():
    return _LOG_FILE
