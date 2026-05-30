"""
打包后 exe 的健康检查：
- 运行 exe，等其展开 _MEIPASS
- 在 %TEMP%/_MEI* 下查找 ffmpeg.exe / ffprobe.exe / 关键模块
"""
import os
import sys
import time
import glob
import subprocess
from pathlib import Path


def check(exe_path: str, settle: float = 8.0) -> dict:
    if not Path(exe_path).is_file():
        return {"ok": False, "error": f"not found: {exe_path}"}

    proc = subprocess.Popen([exe_path])
    time.sleep(settle)

    temp = os.environ.get("TEMP") or os.environ.get("TMP") or "C:\\Windows\\Temp"
    candidates = sorted(
        glob.glob(os.path.join(temp, "_MEI*")),
        key=lambda p: os.path.getmtime(p),
        reverse=True,
    )
    mei = None
    for c in candidates:
        if os.path.isdir(c) and time.time() - os.path.getmtime(c) < 60:
            mei = c
            break

    found = {"ffmpeg.exe": False, "ffprobe.exe": False}
    big_files = []
    if mei:
        for k in found:
            found[k] = os.path.isfile(os.path.join(mei, k))
        # 列出几个大文件
        for root, _, files in os.walk(mei):
            for f in files:
                p = os.path.join(root, f)
                try:
                    sz = os.path.getsize(p)
                    if sz > 5 * 1024 * 1024:
                        big_files.append((sz, os.path.relpath(p, mei)))
                except OSError:
                    pass
        big_files.sort(reverse=True)
        big_files = big_files[:10]

    proc.terminate()
    try:
        proc.wait(timeout=3)
    except Exception:
        proc.kill()

    return {
        "ok": True,
        "exe_size_mb": round(os.path.getsize(exe_path) / (1024 * 1024), 1),
        "_MEIPASS": mei,
        "binaries": found,
        "top10_big_files_mb": [(round(s / 1024 / 1024, 1), n) for s, n in big_files],
    }


def main():
    exe = sys.argv[1] if len(sys.argv) > 1 else r"dist\延河课堂下载器-简易版.exe"
    settle = float(sys.argv[2]) if len(sys.argv) > 2 else 8.0
    r = check(exe, settle)
    import json
    print(json.dumps(r, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
