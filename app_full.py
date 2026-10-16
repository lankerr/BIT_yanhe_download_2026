"""
延河课堂下载器 - 完整版 入口
包含视频下载 + 课件提取(FFmpeg scene) + 音频转录(faster-whisper，GPU自适应/CPU可回退)。
首次启动会自动下载 Whisper 模型（默认走 hf-mirror.com，国内可达）。

CLI 分流（在导入任何重依赖之前）：
  --syscheck        生成系统自检报告（无NVIDIA/黑屏排障首选）
  --probe-asr       内部探针：CT2/ORT 运行时探测（子进程调用）
  --safe-ui         关闭CTk自动DPI感知并固定缩放1.0（窗口黑底/错位时使用）
"""
import os
import sys

# 提前注入 HF 镜像，避免 faster-whisper 自动下载走 huggingface.co
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")


def entry():
    # 诊断初始化必须先于 customtkinter/requests/ctranslate2 等一切重导入
    from runtime_diagnostics import initialize, phase
    initialize()

    if "--probe-asr" in sys.argv:
        from syscheck import run_probe_asr
        return run_probe_asr()

    if "--syscheck" in sys.argv:
        from syscheck import run_syscheck_cli
        return run_syscheck_cli()

    if "--safe-ui" in sys.argv:
        os.environ["YH_SAFE_UI"] = "1"

    import app_paths
    app_paths.set_edition("full")

    phase("before_gui_import")
    from gui_app import main
    phase("gui_imported")
    main()
    return 0


if __name__ == "__main__":
    raise SystemExit(entry())
