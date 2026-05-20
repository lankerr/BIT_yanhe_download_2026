"""
延河课堂下载器 - 简易版 入口
仅包含视频下载功能，不带 PPT 提取与 Whisper 转录。
打包后体积 ~80MB，启动秒级。
"""
import app_paths
app_paths.set_edition("simple")

from gui_app import main

if __name__ == "__main__":
    main()
