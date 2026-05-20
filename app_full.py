"""
延河课堂下载器 - 完整版 入口
包含视频下载 + 课件提取(FFmpeg scene) + 音频转录(faster-whisper GPU)。
首次启动会自动下载 Whisper 模型（默认走 hf-mirror.com，国内可达）。
"""
import os
# 提前注入 HF 镜像，避免 faster-whisper 自动下载走 huggingface.co
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

import app_paths
app_paths.set_edition("full")

from gui_app import main

if __name__ == "__main__":
    main()
