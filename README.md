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

## ✅ 2026-05-22 实测状态

截至 **2026-05-22**，本下载器仍可用于当前延河课堂。已使用课程 `67092`（空中目标探测前沿技术，王锐）完成端到端实测：

- 视频下载成功：297 个 HLS 分片全部下载并合并，MP4 约 776.7 MB。
- 完整版后处理成功：提取 PPT 59 页，生成 TXT 转写 2448 段，并同步生成 SRT。
- 双版本 exe 均已重新打包并启动冒烟：简易版约 182.1 MB，完整版约 295.5 MB，均内置 `ffmpeg.exe` / `ffprobe.exe`。

详细记录见 [docs/TEST_REPORT_2026.md](docs/TEST_REPORT_2026.md)。

---

## 🧠 核心技术

### 1. 固定并发引擎（v3）
瓶颈定位结论（docs/download_perf_conclusions.md）：真实内网 CDN 实测持续 9.26 MB/s
（峰值 19.31，K=32），而客户端自身 localhost 天花板 355-535 MB/s——瓶颈在服务端整形，
汇编/重写无收益；K=16-32 为最优区间，K≥48 自扰。
经全段实测（`docs/speed_stage4_fullseg.md`），删除原 AIMD 自适应并发，改用固定线程池（默认 K=16，延河 CDN 实测饱和点）。同等网络下比旧 AIMD 引擎稳定快 33-35%，且不再出现「卡尾部 / 死亡螺旋」。

### 2. 两阶段下载 + 缺片补齐
阶段 1 固定并发主拉全部分片，失败的进 deferred 队列；阶段 2 对缺片逐个串行重试（指数退避，
0.5s→1s→2s…）。`_success_sum` 计数加锁，消除多线程下「卡 99%」死循环。合并前对账：正常
补齐全部 297 段；极端网络下允许 ≤5% 缺片兜底合并并明确告警，避免一两段不可恢复就整节失败。
看门狗保留但降级为「真·僵死」兜底（180s 无进度），不再驱动流程。

### 3. 内嵌 FFmpeg
打包时通过 `fetch_ffmpeg.py` 把 `ffmpeg.exe` / `ffprobe.exe` 内嵌入 exe，运行时从 `_MEIPASS` 加载，**真正零依赖**。

### 4. PPT 智能提取（完整版，v2 融合算子内核）
单遍融合管线：FFmpeg 解码一次，`fps=5` 抽样（幻灯片一页停 ≥ 数秒，零漏检，设计参考
arXiv:2202.03540 SliTraNet / arXiv:1804.09066 ECO 的帧冗余抽样）→ 320px 灰度缩略图管道 →
numpy 批量向量化在同一遍内完成「帧差分变化检测 + 32×32 DCT mini-pHash 去重」→ 仅对幸存页
seek 提取全分辨率帧。实测比旧管线（全分辨率 scene 扫描 + 逐点 seek + PIL pHash 去重）
快 1.2-1.8×，判别力对齐（255bit pHash），并输出 JPG + PPTX + **PDF** 三格式。旧管线保留
为 `engine="legacy"` 回退。已实测否决的方案：硬解 d3d11va/cuda（省 6× CPU 但墙钟慢
1.7-5×，GPU 回传串行化）、CNN 检测（SAD 信号已零漏检，推理反而更慢）。

### 4.5 内容提取实测速度（1 小时 720p 课程 / 60 页幻灯片）

| 环节 | 纯 CPU | 有 GPU（RTX 5070 实测） |
|------|--------|------------------------|
| PPT 提取（60页 → JPG+PPTX+PDF） | **33.8s（107× 实时）** | 同左（GPU 无益：硬解实测墙钟更慢） |
| 转写 base 模型（→ TXT+SRT） | 4.5 分钟（13.3× 实时） | **~1 分钟（90× 实时，Batched 管线）** |
| 转写 large-v3（默认，中文最佳） | 20-30 分钟（建议 CPU 改 medium） | ~2.5-4 分钟（15-25× 实时） |
| **全套后处理合计（base）** | **≈ 6 分钟** | **≈ 1.5-2 分钟** |

> PPT 提取分解：单遍扫描 25.9s（76%，解码受限）+ 全分辨率提帧保存 7.9s（24%）。
> 1080p 课程按扫描线性外推约 2.5-3 分钟（20-25× 实时）。90 分钟课程全套（含
> large-v3 转写）在有 GPU 机器上 ≈ 5 分钟内完成。

### 5. 音频转录（完整版）
`faster-whisper` (CTranslate2)，GPU `float16` / CPU `int8` 自适应，默认走 `hf-mirror.com` 拉模型避免 huggingface.co 不可达。

### 6. VPN/代理环境自动直连
延河 API、m3u8、ts 分片、音频下载均使用不读取系统代理的直连会话，减少 VPN、系统代理、透明代理导致的 403、ProxyError、长时间卡住等问题。仍然可以开着 VPN 做别的事，下载器会尽量绕开系统代理直连延河域名。

在此基础上还支持 **IP 直连模式**：设置 `YHKT_NET_MODE=ip=<IPv4>`（例：先通过 `ping cbiz.yanhekt.cn` 拿到校内解析出的 IP，再填入如 `YHKT_NET_MODE=ip=10.x.x.x`），即可把延河域名重定向到校内服务器 IP 直连，Host 头仍为原域名。这是竞品标称 110MB/s 的同款原理；注意该模式下证书校验会关闭（verify=False），因为证书是对域名签发的，直连 IP 必然对不上。

### 7. 长课转写分块
超过 30 分钟的音频会自动切成约 20 分钟一段逐段转写，再把时间戳拼回原视频时间线，避免 90 分钟以上课程一次性送入 Whisper 时内存分配失败。

### 8. 限流感知（全局熔断退避）
延河 WAF 拒绝（HTTP 403/429/503 或藏在 200 里的 `code=61101114`"系统繁忙"）会触发**全局熔断**：
所有请求统一退避（10s 起、连续触发指数翻倍至 60s 封顶，成功一次即恢复），cbiz API 另加全局
0.8s 限速。弱网超时不触发熔断，不影响 v3 引擎吞吐。本地 mock 压测：被限 3 秒期间 16 线程
请求量 ~4600 → ~26（详见 `docs/RATE_LIMIT_NOTES.md` 与 `scripts/waf_breaker_check.py`）。

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
├── m3u8dl.py                  # 固定并发下载引擎 + HLS 合并
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
├── scripts/bench_maxspeed.py     # 最高速度压测（K扫描+自动报告，--mock 可自检）
└── scripts/legacy/               # 历史/实验脚本（不参与打包）
```

---

## 📜 更新日志

### v2026.09 (二) – 内核算子优化
- **PPT 提取 v2 融合算子**：单遍解码完成检测+去重（详见核心技术 4），实测 1.2-1.8× 提速，
  判别力对齐旧管线；`fps=5` 冗余抽样 + numpy 批量向量化 + 无效重扫短路。
- **新增**：课件 PDF 导出（`pdf_export.py`，批处理自动与 PPTX 一同产出）。
- **新增**：内网直连模式 `YHKT_NET_MODE=ip=<IPv4>`（竞品 110MB/s 同款原理，详见核心技术 6）。
- **m3u8 内核微优化**：分片写盘 chunk 64KB→1MB（落盘循环实测 1.39×）；GUI/重定向模式
  不再每片刷进度条；空文件判定去 stat。

### v2026.09 – AI 提速 + 限流对策 + 新版 Python 兼容 + 压测工具
- **新增**：**最高速度压测工具** `scripts/bench_maxspeed.py`——生产引擎整节课全段 K 扫描
  （4..64 任意档位、多轮交错抗网络漂移），引擎内置字节级仪表（0.5s 采样），自动产出
  交付级 `report.md` + 吞吐曲线图 + CSV/JSON；`--mock` 本地模拟 CDN 自检无需 token。
  操作手册见 `docs/maxspeed_runbook.md`。
- **新增**：引擎观测仪表（`stats()` / `final_stats()` / `stats_hook`）：字节计数、单段
  耗时 p50-p99、阶段2重试、缺片计数，纯增量不影响下载行为；限流熔断器新增累计触发
  次数与累计退避时长统计。引擎并发上限 32 → 64（仅压测探索用，GUI 滑块维持默认 16）。
- **修复**：补提交缺失的 `theme.py`（此前 `gui_app.py` import 即崩，仓库 main 无法启动）。
- **AI 提速**：Whisper 启用 `BatchedInferencePipeline` 批量推理（本机 RTX 5070 实测
  **12.7x**：120s 音频 16.9s → 1.3s），CPU 显式给满线程数，批量管线 OOM 自动降级经典管线。
- **修复**：模型下载被系统代理劫持时强制直连重试（挂代理时 hf-mirror 会 308 回
  huggingface.co 导致 `FileMetadataError`）；`NO_PROXY` 必须写具体域名（requests 不认 `*`）。
- **修复**：音频提取错误处理——真正读出 ffmpeg stderr（原先 `e.stderr.decode` 崩溃吞错），
  无音轨视频给出明确提示"该视频可能是纯画面录制"。
- **GUI 不卡**：GPU 检测移到后台线程（原先在主线程 `import torch` 冻结界面数秒）；
  后处理进度回调节流（150ms）+ 日志框限行（原先每段 3 次 after，一节课 7000+ 次主线程任务）。
- **兼容**：Python 3.13+ 禁止跨线程 `after()`，全量迁移到队列轮询调度器 `post()`
  （56 处调用点），新版 Python 上课程获取/下载进度/后处理不再静默失效。
- **新增**：后处理"停止"按钮（`threading.Event` 贯穿课件提取与转写批次）。
- **新增**：限流全局熔断退避 + cbiz API 限速（见上"核心技术 8"），`Xclient-Timestamp`
  头与签名查询参数同步刷新。
- **修复**：PPT 提帧在 `fps=0` 的封装上不再全部读到 0 位置（退回毫秒定位）。

### v2026.05.29 – 下载引擎 v3（删 AIMD 固定并发）
- **重构**：下载引擎从 AIMD 自适应并发改为**固定线程池**。经 `docs/speed_stage4_fullseg.md`
  全段（297 段）三轮实测：交错对照下同等网络稳定**提速 33-35%**；backport 后用新生产引擎
  全段回归 405.7s / 1.91 MB/s，297/297 全成功、0 缺片。
- **修复**：根除 AIMD「死亡螺旋」——旧引擎全段必进「尾部模式」、弱网吞吐崩到 0.86 MB/s；
  新引擎从不触发尾部模式 / 看门狗。
- **修复**：`_success_sum` 计数加锁，消除多线程下「卡 99%」。
- **重构**：两阶段下载（阶段1并发主拉 + 阶段2缺片串行指数退避补）取代原先三套互相覆盖的
  尾部模式代码路径。
- **变更**：看门狗降级为「真·僵死」兜底（180s 无进度），不再驱动下载流程。
- **变更**：GUI 默认并发 32 → **16**（全段实测饱和点），上限 64 → 32。

### v2.0.0 (2026-05) – 双版本发布
- **修复**：适配 2026 年延河 HLS 路径签名，从旧 `_100` 更新到当前 `_200`
- **修复**：延河 API / 视频分片请求默认绕过系统代理，降低 VPN/代理导致的 403、超时和卡死概率
- **修复**：长课转写自动分块，解决 90 分钟以上课程一次性 Whisper 转写内存不足
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

---

## ⚡ 2026-10 超级优化：PPT提取引擎 v3（stable）

重写课件提取内核（`slide_pipeline.py` + `screen_content.py`），新增三档配置 `fast / balanced / thorough`，**默认 balanced**。在 85 分钟真实研究生课件独立留出集上实测（对照真值 5122 张全量参考帧）：

| 引擎 | 速度 | 召回率 | 精确率 | 说明 |
|------|------|--------|--------|------|
| **stable-balanced（新默认）** | 22.4× 实时 | **99.35%** | 72.9% | 召回/速度最优平衡 |
| stable-thorough | 18.9× 实时 | 99.35% | 66.8% | 同召回，更慢 |
| stable-fast | 20.7× 实时 | 96.7% | **82.9%** | 精确优先 |
| 旧 fused 引擎 | 24.0× 实时 | **9.15%** | 45.2% | 真实课件上漏页严重，仅保留兼容 |

新增能力：内容级分块去重（页脚/页码保护、鼠标噪声抑制、逐步加字保留最终版）、视频段智能抽取（连续运动段取代表帧）、每图匹配依据 SHA256 审计留痕。`batch_extract_slides()` 调用签名**完全向后兼容**，老代码无感升级；GUI 新增「课件档位」下拉框。

> 评测协议：真值先行封存、未按留出分数调参、全部候选目视审核；低于95%的指标照报。已知限制：黑边课件页码位置识别有反例（见 `bench/RESULTS.md`）。28 项 pytest 全部通过。

## 🔑 迷你登录器：token_login.py（2026-10 新增）

解决「课程列表接口返回空」的头号元凶——**登录 token 过期**（服务端对过期 token 静默返回空列表，极易误判为 IP 封锁）。

```bash
py token_login.py            # 打开复用本机 Edge(WebView2) 内核的登录窗口
                             # 手动完成统一身份认证 → 自动捕获token写入 auth.txt → 自动关闭
py token_login.py --check    # 静默检查当前 token 是否有效
```

设计原则：**只做登录和抓 token，不支持任何其他功能**（不下载/不浏览）。依赖 `pip install pywebview`（Windows 11 自带 WebView2 运行时）。

## 🧠 实战运维经验（2026-09/10 两轮封锁复盘）

- session 列表返回空 ≠ IP 被封：先用 `token_login.py --check` 排除 token 过期
- CDN 分片下载并发 ≤8：高并发突发会触发 WAF 整段 403（冷却数十分钟）
- 下载请求头必须带完整 UA + Origin + Referer，简短 UA 会被 WAF 拦（会造出假 403 诊断）
