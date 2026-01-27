# 延河课堂下载器 (Yanhe Downloader) - 2026 Enhanced Edition

> **极速 · 稳定 · 智能**
>
> 本项目是基于 [AuYang261/BIT_yanhe_download](https://github.com/AuYang261/BIT_yanhe_download) 的深度重构版本，旨在为北京理工大学“延河课堂”提供企业级的高性能下载方案。针对校园网及 VPN 环境下高丢包、易断连的痛点，我们重新设计了底层下载引擎，实现了自适应流控与智能监控，并采用了现代化的图形界面。

![GUI Preview](https://github.com/lankerr/BIT_yanhe_download_2026/raw/main/assets/screenshot.png)

## 1. 项目背景与设计目标

延河课堂平台提供了丰富的课程录播资源，但官方并未开放离线下载功能。在复杂的网络环境（如 VPN 跨域访问）中，传统的单线程下载容易出现速度慢、中途断流或“假死”等问题。

本项目的设计目标是提供一个**零配置、高鲁棒性**的下载工具：
*   **对于用户**：无需关心复杂的网络参数，解压即用，支持断点续传与批量下载。
*   **对于网络**：像 TCP 协议一样智能感知带宽，网络好时全速下载，拥塞时自动避让。

---

## 2. 核心技术实现

本项目并非简单的爬虫脚本，而是一个实现了完整拥塞控制与状态监控的分布式下载系统。

### 2.1 自适应并发控制 (AIMD Engine)
这是本项目与传统下载器最大的区别。为了在最大化带宽利用率的同时保证连接稳定性，我们引入了 TCP 协议中的 **AIMD (Additive Increase Multiplicative Decrease)** 算法来动态调整线程池大小：

*   **慢启动 (Slow Start)**: 初始仅启动 16 个线程，避免突发流量触发服务端的 WAF 防火墙。
*   **线性增加 (Additive Increase)**: 每当一个分片下载成功 (`HTTP 200`)，线程池容量 **+1**（上限 64），探测网络带宽上限。
*   **指数退避 (Multiplicative Decrease)**: 一旦检测到超时或 HTTP 错误，线程池容量立即 **减半**。甚至在单片下载失败重试时，采用 `min(30, 2^retries)` 的指数级等待策略，防止网络状态进一步恶化。

### 2.2 智能看门狗机制 (Watchdog Monitor)
在不稳定的网络环境下，Python 的 `requests` 库偶尔会出现 Socket 读超时失效的情况，导致线程陷入“假死”状态。
*   **机制**: 程序内置独立的 Watchdog 守护线程，每 5 秒巡检一次全局下载状态。
*   **判定**: 维护 `_last_activity_time` 时间戳，若超过 **120秒** 无任何有效数据流入，判定下载任务已“僵死”。
*   **恢复**: Watchdog 会根据当前模式（GUI 或 CLI）自动触发重连或提示用户，确保无人值守的大文件下载任务能够顺利完成。

### 2.3 身份认证与动态签名
延河课堂采用 JWT (JSON Web Token) 进行无状态鉴权。本项目通过对前端代码的逆向分析，实现了完整的签名逻辑：
*   **Token 获取**: 用户仅需提供存储在 `localStorage.auth` 中的 32 位 Hex Token。
*   **动态签名**: 每次请求均通过内部算法生成 `Xclient-Signature`，算法逻辑为 `MD5(Path + Token + Timestamp + MagicSalt)`，其中 MagicSalt 已内置于 `utils.py` 中。这确保了请求能通过服务端的严格校验。

### 2.4 HLS 解析与无损合并
*   **M3U8 解析**: 完整支持 HLS 协议，包括自动识别顶级索引 (`EXT-X-STREAM-INF`) 和解析 AES-128 加密密钥 (`EXT-X-KEY`)。
*   **内存管理**: 通过自定义的 `ThreadPoolExecutorWithQueueSizeLimit` 实现有界队列（Backpressure），即使面对 5000+ 分片的超长课程，也能将内存占用控制在极低水平。
*   **无损合并**: 集成 FFmpeg 组件，利用 `Stream Copy` 技术 (`-acodec copy -vcodec copy`) 直接封装 TS 流，几秒内即可完成 GB 级视频的无损合并。

---

## 3. 功能特性

*   **现代化 GUI**: 基于 `CustomTkinter` 构建的“极客黑”主题界面，支持高分屏缩放与实时进度回显。
*   **多信号源支持**: 支持下载**教师机屏幕**或**教室摄像头**两种画面信号。
*   **音频提取**: [2026新增] 支持独立下载并保存教师的蓝牙麦克风音频轨道。
*   **极致便携**: 通过 PyInstaller 打包，内置 Python 运行时与 FFmpeg，无任何外部依赖。

---

## 4. 快速开始

### 4.1 获取认证码
由于安全策略限制，需要手动提取 Token：
1. 使用浏览器登录 [延河课堂](https://www.yanhekt.cn)。
2. 按 `F12` 打开控制台 (Console)。
3. 输入以下代码并回车：
   ```javascript
   javascript:alert(JSON.parse(localStorage.auth).token)
   ```
4. 复制弹出的 32 位字符串。

### 4.2 启动下载
1. 运行 `延河课堂下载器.exe`。
2. 在“身份认证码”处粘贴 Token。
3. 输入 5 位课程 ID (例如 `40524`)，点击“获取课程列表”。
4. 勾选需要下载的章节，选择“电脑屏幕”或“摄像头录像”，点击“开始下载”。

> **提示**: 输出目录默认为程序所在文件夹下的 `output/` 目录。

---

## 5. 开发者指南

如果你希望参与开发或自行构建：

### 环境要求
*   Python 3.9+
*   依赖库：`requests`, `customtkinter`, `pycryptodome` (用于AES解密)

### 运行源码
```bash
pip install -r requirements.txt
python gui_app.py
```

### 构建发布
使用内置脚本一键打包：
```bash
./build_exe.bat
```

---

## 6. 致谢与声明

本项目基于 [AuYang261/BIT_yanhe_download](https://github.com/AuYang261/BIT_yanhe_download) 开发。

**特别致谢原作者 [AuYang261](https://github.com/AuYang261)** —— 我在网安学院的好朋友，已经保研去北大深造了，非常厉害！

同时也感谢所有 Contributors 的开源贡献。

本项目仅供技术学习与交流使用，使用者应遵守相关法律法规及学校规定，严禁用于商业用途或侵犯他人知识产权。

_License: MIT_
