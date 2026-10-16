# 完整版exe轻薄本黑屏 + 显卡自适应 + 纯CPU底层加速 —— 审计与方案请求

## 背景
- 仓库：`C:/Users/97290/Desktop/新建文件夹 (2)/BIT_yanhe_download_2026`（已发布 Release v2026.10.16）
- 完整版 exe：PyInstaller onefile，约365MB，spec=`延河课堂下载器-完整版.spec`，**upx=True、console=False**
- GUI：customtkinter（tkinter 系，非 webview），入口 `app_full.py` → `gui_app.main()`
- 转录：faster-whisper + ctranslate2（未打包 torch；GPU检测在 gui_app.py 后台线程先 `import torch`，失败则回退 nvidia-smi）
- 用户反馈：同学的**轻薄本**（大概率 Intel/AMD 核显、无NVIDIA独显）双击 exe"打开就是黑屏"

## 我方初步诊断（请你验证/排序/补充）
1. onefile 365MB 每次启动自解压到 %TEMP%，慢盘+Defender 实时扫描下可达数分钟无窗口 → 用户感知"黑屏/无反应"
2. UPX 压缩的 DLL 易被杀软误杀/隔离 → 静默崩溃，且 console=False 看不到任何报错
3. gui_debug.log 写在 app_dir，onefile 下是临时解压目录，退出即删 → 崩溃日志不可恢复
4. customtkinter 暗色主题 + 高DPI缩放（125%/150%）在旧 Intel 核显驱动上有已知"窗口黑底不重绘"问题
5. `audio_transcriber_gpu.py`: `DEFAULT_COMPUTE_TYPE="float16"`、`cpu_threads=os.cpu_count()`（轻薄本 E核/P核混布可能超订阅）

## 请你给出（gpt-6.1-sol 深度思考）
### A. 黑屏根治方案（按性价比排序）
- 是否改 onedir（免自解压）？onefile 下加极简启动闪屏（纯 tkinter，在任何重 import 之前弹出"正在启动，首次可能1-3分钟…"）？
- upx=False 的代价？console=False 但装全局 excepthook + 持久日志（%LOCALAPPDATA%/yanhe/log）+ 崩溃弹窗？
- customtkinter 在核显/高DPI下的兼容补丁（ctk.set_scaling_mode? appearance light 回退? 强制 update_idletasks/初次 resize 触发重绘?）
### B. 显卡自适应（无NVIDIA机器零配置可用）
- 用 `ctranslate2.get_cuda_device_count()` 替代 import torch 做检测（不依赖torch、瞬时完成）？
- device/compute_type 自动矩阵：cuda:0+float16 → cpu+int8；GUI 下拉默认 auto，检测失败不再报错而是静默降级+横幅提示
### C. 纯CPU底层加速（轻薄本核显也能用）
- int8 量化 + beam_size=1 + vad_filter 参数最优组合
- ctranslate2 线程模型：intra_threads=物理核数 vs os.cpu_count()；inter_threads 搭配；OMP/MKL 环境变量
- BatchedInferencePipeline 在 CPU 上是否可用/有收益；chunk_length 与 batch 组合
- 模型自动降级策略：large-v3→medium→small 按首次基准测试（跑10s音频计时）自动选择，并写缓存
- 预期倍速：给出保守估计区间（用于README，不要吹）
### D. `--syscheck` 自检命令
- exe 带 --syscheck 参数：导出 GPU/驱动/CPU/AVX2/AVX512/内存/磁盘/WebView2/杀软名单 到 txt，同学一键回传给我们定位
请输出具体补丁方案（文件级 diff 思路 + spec 修改点），我方负责实现与重打包验证。
