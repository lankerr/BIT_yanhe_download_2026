"""
打包前置：确保项目根目录有 ffmpeg.exe / ffprobe.exe，供 PyInstaller 内嵌进 exe。

策略：
1. 已存在 → 直接返回
2. 系统 PATH 能找到 → 拷贝过来
3. 都没有 → 提示用户手动下载（不自动联网下载，避免在墙内卡住或下到不可信副本）
"""
import os
import shutil
import sys

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
TARGETS = ["ffmpeg.exe", "ffprobe.exe"]


def main() -> int:
    missing = []
    for name in TARGETS:
        dst = os.path.join(THIS_DIR, name)
        if os.path.isfile(dst):
            print(f"[OK]   已就绪: {dst}")
            continue
        src = shutil.which(name.replace(".exe", ""))
        if src and os.path.isfile(src):
            shutil.copy2(src, dst)
            print(f"[COPY] 从 PATH 拷贝: {src} -> {dst}")
        else:
            missing.append(name)

    if missing:
        print()
        print("=" * 60)
        print("未找到以下二进制，无法继续打包：")
        for m in missing:
            print(f"  - {m}")
        print()
        print("请从 https://www.gyan.dev/ffmpeg/builds/ 下载 ffmpeg-release-essentials.zip，")
        print(f"解压后把 bin/ffmpeg.exe 与 bin/ffprobe.exe 放到：{THIS_DIR}")
        print("=" * 60)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
