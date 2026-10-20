# 复核请求：师弟两问题的诊断与修复是否成立 + 还缺什么

## 问题① 延河课堂往期录播"消失"
师弟观察：网站只能看本学期录播，往期不见；但他保存的旧课程 ID 用我们的下载器仍能正常下载。

**我的判定：网站前端隐藏了往期入口，非权限关闭。** 证据（API 实测，2026-10-20）：
- `GET /v1/course/list?page=1&page_size=30`（带登录token）默认只返回 school_year=2026-2027, semester=1 的课
- 加参数 `&school_year=2025-2026&semester=1` 返回 30 条往期课
- 旧课直连 `GET /v2/course/session/list?course_id=59806`（2025秋计算成像）仍返回 9 个 session 各带 1 视频

请复核：判定是否成立？有没有"这其实是权限收紧前奏/API 即将下线"的风险信号需要我们提前备份往期课？是否建议在下载器里加"按学期列课"参数支持（现在 GUI 只支持输课程 ID）？

## 问题② Iris Xe 核显轻薄本"导出 PPT 一直黑屏、无报错、提取不了"
师弟机器：联想轻薄本，Intel Iris Xe 核显（无 NVIDIA），4 核左右 CPU，用的 onefile exe v2026.10.16。
症状（10-16 起，两次反馈+截图未解出）：点后处理导出 PPT 后窗口一直黑，无任何报错弹窗，最终拿不到 PPT。

**我的根因判定：弱 CPU 上提取是重计算循环，把调度/GIL 占满，tkinter 主线程停止重绘，Windows 把无响应窗口涂黑/涂白。** 我们 24 核台式机（RTX 5070）无法复现。备选假设：a) 视频硬解出黑帧；b) 异常被吞（GUI worker 原来只弹 str(e) 一句话且师弟说"没报错"——若 worker 内部先于我们修复版吞掉则可能）；c) onefile 自解压+慢盘导致启动等待被误报（但他能进到导出环节，排除启动问题）。

**我已实施的修复（commit 4aac4da，已发 Release）**，全部在 gui_app.py：
1. worker 线程开头 `kernel32.SetThreadPriority(GetCurrentThread(), -1)`（THREAD_PRIORITY_BELOW_NORMAL），让 UI 主线程恒赢调度
2. 失败弹窗改为带完整 traceback 尾部 8 行 + 持久日志路径（%LOCALAPPDATA%/yanhe/log，runtime_diagnostics 已就位）；所有 worker 线程里的 UI 更新改经 post() 队列回主线程（原来 self._log 直接跨线程调 tkinter）
3. GPU 探测（已改为 ctranslate2.get_cuda_device_count）确认无 NVIDIA 时，课件档位变量从 balanced 自动设为 fast

请复核：
- 根因判定优先级是否合理？备选假设 a/b 需不需要在没复现的情况下再加防御（如 OpenCV 解码黑帧检测）？
- SetThreadPriority 方案在 Python threading 下是否真能保住 tkinter 重绘（GIL 语义：C 层 PIL/numpy 调用释放 GIL 吗；是否还需在 slide_pipeline 帧循环里加 time.sleep(0) 主动让出）？
- post() 队列方案有没有死锁/丢失风险（queue.Queue + 主循环 30ms drain）？
- 还有什么"现在全部解决" checklist 缺口（比如：ffmpeg 子进程在弱机上的 merge 超时、pptx 写入大图 OOM、`--safe-ui` 也该在导出场景提示）？

请给出：每项"同意/不同意+理由"，以及你建议补的最终修复清单（文件级）。我方核对后实现。
