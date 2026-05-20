"""
统一路径与版本探测：
- ffmpeg / ffprobe 二进制查找：_MEIPASS → exe 同目录 → 当前工作目录 → PATH
- 资源文件查找（图标等）
- EDITION 标记简易版 / 完整版（由入口脚本注入环境变量）
"""
import os
import sys
import shutil


def get_app_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def get_bundle_dir() -> str:
    return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))


def resource_path(rel: str) -> str:
    return os.path.join(get_bundle_dir(), rel)


def _find_binary(name: str) -> str:
    name_exe = name + ".exe" if sys.platform == "win32" and not name.endswith(".exe") else name
    for base in (get_bundle_dir(), get_app_dir(), os.getcwd()):
        cand = os.path.join(base, name_exe)
        if os.path.isfile(cand):
            return cand
    found = shutil.which(name)
    return found if found else name


def ffmpeg_path() -> str:
    return _find_binary("ffmpeg")


def ffprobe_path() -> str:
    return _find_binary("ffprobe")


def has_ffmpeg() -> bool:
    p = ffmpeg_path()
    return os.path.isfile(p) or shutil.which(p) is not None


EDITION = "full"  # 由入口脚本（app_simple.py / app_full.py）在 import 前显式覆写


def set_edition(name: str) -> None:
    """入口脚本调用，明确指定当前 exe 的版本。"""
    global EDITION
    EDITION = (name or "full").lower()


def is_full_edition() -> bool:
    return EDITION == "full"


def edition_label() -> str:
    return "完整版" if is_full_edition() else "简易版"
