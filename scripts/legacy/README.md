# scripts/legacy/

这里存放历史/实验/一次性脚本，**不参与正式打包**，也不保证持续维护。

| 文件 | 用途 |
|------|------|
| `cli_main.py` | 原 CLI 版本入口（无 GUI） |
| `download_all.py` | 批量下载本学期全部课程（按 `down_list.txt`） |
| `batch_*.py` | 一次性批处理（屏幕下载、PPT 批量提取） |
| `_*.py` | 个人调试/补救脚本，临时排障时用 |
| `ppt_extractor_legacy.py` | OpenCV 版 PPT 提取（旧），现已被 `ppt_extractor_gpu.py` 取代 |
| `transcribe_*.py` | 早期转录实验，被 `audio_transcriber_gpu.py` 取代 |

如果未来需要回收这些功能到正式 GUI/CLI，再单独迁回根目录并加入 spec。
