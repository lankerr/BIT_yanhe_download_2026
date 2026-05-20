# 延河课堂下载器 (BIT_yanhe_download_2026)

> **端到端、零依赖、双版本发布**
>
> 北京理工大学「延河课堂」录播视频下载工具，针对 VPN/校园网高丢包环境深度优化。本仓库由 [AuYang261/BIT_yanhe_download](https://github.com/AuYang261/BIT_yanhe_download) 重构而来。

![GUI](https://github.com/lankerr/BIT_yanhe_download_2026/raw/main/assets/screenshot.png)

---

## 🚀 两个版本，按需取用

| 版本 | exe 文件 | 体积 | 功能 | 适合 |
|------|---------|------|------|------|
| **简易版** | `延河课堂下载器-简易版.exe` | ~80 MB | 课程视频下载（含合并 MP4） | 只想看回放、节省空间 |
| **完整版** | `延河课堂下载器-完整版.exe` | ~500 MB | 下载 + **PPT 智能提取** + **音频转录(Whisper GPU)** | 需要做笔记 / 课件 / 字幕 |

两者都是**单文件 exe**，自带 Python 运行时与 FFmpeg，解压双击即用。

> 💡 **GPU 用户**：完整版的语音转录推荐在装好 **NVIDIA CUDA 12.x + cuDNN 9** 的机器上运行，可获得 30-50× 实时的转录速度。无 GPU 时会自动回退 CPU + int8 模型，但 `large-v3` 在 CPU 上会比较慢，建议改用 `medium` 或 `small`。

---

## 📦 获取发行版

前往 [Releases](https://github.com/lankerr/BIT_yanhe_download_2026/releases) 下载需要的版本。

首次使用：
1. 浏览器登录 [延河课堂](https://www.yanhekt.cn) → 按 `F12` 打开控制台
2. 粘贴 `javascript:alert(JSON.parse(localStorage.auth).token)` 回车，复制弹出的 32 位 Token
3. 启动 exe，粘贴 Token + 输入 5 位课程 ID（来自 `yanhekt.cn/course/****`） → 「获取课程列表」
4. 勾选要下载的章节 → 「开始下载」

---

## 🧠 核心技术

### 1. 自适应并发 (AIMD Engine)
TCP 风格的拥塞控制：慢启动 16 → 成功 +1（上限 64）→ 失败折半，避免触发服务端 WAF。

### 2. 看门狗 + 尾部模式
- Watchdog 每 5 秒巡检，120 秒无数据流入判定僵死并恢复
- 剩余 < 8 个分片时进入 Tail Mode，绕过 AIMD slot 限制，逐文件 30 秒超时重启

### 3. 内嵌 FFmpeg
打包时通过 `fetch_ffmpeg.py` 把 `ffmpeg.exe` / `ffprobe.exe` 内嵌入 exe，运行时从 `_MEIPASS` 加载，**真正零依赖**。

### 4. PPT 智能提取（完整版）
`FFmpeg scene` 滤镜检测画面剧变 → 时间戳过滤 → pHash 去重 → 输出 JPG + PPTX，约 50× 实时。

### 5. 音频转录（完整版）
`faster-whisper` (CTranslate2)，GPU `float16` / CPU `int8` 自适应，默认走 `hf-mirror.com` 拉模型避免 huggingface.co 不可达。

---

## 🛠 自行打包

### 环境要求
- Python 3.9 ~ 3.12
- Windows 10+（其他平台 spec 需自行调整）

### 步骤

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 准备 ffmpeg / ffprobe（若 PATH 里有会自动拷贝）
python fetch_ffmpeg.py

# 3a. 打简易版（~80MB）
build_simple.bat

# 3b. 打完整版（~500MB，需先 pip install faster-whisper 等完整依赖）
build_full.bat
```

产物：`dist\延河课堂下载器-简易版.exe` / `dist\延河课堂下载器-完整版.exe`

> ⚠️ 完整版若想在 exe 内直接跑 GPU，构建机需先 `pip install torch --index-url https://download.pytorch.org/whl/cu121`，否则 exe 启动时 `torch.cuda.is_available()` 会返回 False。

---

## 📁 仓库结构

```
.
├── app_simple.py              # 简易版入口
├── app_full.py                # 完整版入口
├── app_paths.py               # 路径 / 版本 / ffmpeg 查找统一入口
├── gui_app.py                 # 主 GUI (CustomTkinter)
├── m3u8dl.py                  # AIMD 下载引擎 + HLS 合并
├── utils.py                   # 签名 / 课程列表 / 认证
├── ppt_extractor_gpu.py       # 课件提取（完整版）
├── audio_transcriber_gpu.py   # Whisper 转录（完整版）
├── batch_process.py           # 后处理批量编排
├── fetch_ffmpeg.py            # 打包前 ffmpeg 准备
├── 延河课堂下载器-简易版.spec
├── 延河课堂下载器-完整版.spec
├── build_simple.bat
├── build_full.bat
├── requirements.txt
└── scripts/legacy/            # 历史/实验脚本（不参与打包）
```

---

## 📜 更新日志

### v2.0.0 (2026-05) – 双版本发布
- **新增**：简易版 / 完整版双 exe 发布形态
- **新增**：FFmpeg / FFprobe 内嵌打包，零外部依赖
- **新增**：完整版自动检测 GPU 并设置默认 device / compute_type
- **重构**：`app_paths.py` 统一资源路径与版本标识
- **整理**：实验脚本迁移至 `scripts/legacy/`

### v1.2.0 (2026-02) – 尾部优化
- Tail Mode：剩余 <8 文件时绕过 AIMD slot 限制
- Stall Detection：30 秒无进度强制进入尾部模式
- GUI 改用 Queue 替代直接 after() 调用，彻底解决 GUI 卡顿
- 修复 exe 路径问题（auth.txt / output）与 FFmpeg 弹窗

### v1.1.0 (2026-01) – GUI 重构版
- CustomTkinter「极客黑」主题
- 集成 AIMD 自适应流控

### v1.0.0
- 基于 AuYang261 的原始版本

---

## 🙏 致谢

- 原项目作者 [AuYang261](https://github.com/AuYang261)（网安学院好朋友，已保研北大）
- [SYSTRAN/faster-whisper](https://github.com/SYSTRAN/faster-whisper) – CTranslate2 Whisper
- [Gyan.Dev FFmpeg builds](https://www.gyan.dev/ffmpeg/builds/) – Windows FFmpeg 二进制

本项目仅供技术学习与交流，使用者应遵守相关法律法规与学校规定，**严禁用于商业用途或侵犯他人知识产权**。

License: MIT
