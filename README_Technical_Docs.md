# 延河课堂下载器 (Yanhe Downloader) - 技术详解与架构说明

本文档详细阐述了 [Yanhe Downloader] 项目的核心技术实现，包括高并发下载策略、自适应流控、看门狗机制以及底层音视频处理逻辑。项目旨在为延河课堂提供稳定、高速、可靠的课程资源下载方案。

## 1. 核心架构概览

本项目采用 **Python 3 + CustomTkinter** 构建现代化 GUI，后端核心下载引擎采用 **生产者-消费者模型** 配合 **AIMD (Additive Increase Multiplicative Decrease)** 拥塞控制算法，实现对服务端的高效请求与带宽利用。

### 1.1 模块划分
- **`gui_app.py`**: 基于 CustomTkinter 的现代化界面，负责用户交互、任务调度与状态反馈。采用极客黑/GitHub Dark 配色。
- **`m3u8dl.py`**: 核心下载引擎。实现了 M3U8 解析、TS 分片多线程下载、AES-128 解密（通过 Key 文件）、自适应线程控制。
- **`utils.py`**: 工具库。处理 URL 签名、Token 认证、API 请求签名加密。

---

## 2. 关键技术机制 (Technical Deep Dive)

### 2.1 智能看门狗机制 (Watchdog based Monitor)
在不稳定的网络环境下，下载线程可能会因为 Socket 挂死而陷入假死状态。我们实现了一个独立的 **Watchdog 线程** 来监控全局下载状态。

*   **工作原理**: `m3u8dl.py` 中 `M3u8Download` 类启动一个守护线程 `watchdog`。
*   **检测逻辑**: 维护一个 `_last_activity_time` 时间戳，每次成功下载分片或有网络活动时更新。
*   **超时判定**: 默认超时时间为 **120秒** (Adaptive Watchdog)。如果 `time.time() - self._last_activity_time > 120`，则判定下载卡死。
*   **响应策略**:
    *   **GUI 模式**: 标记 `_watchdog_triggered` 并通知界面标记下载失败，避免界面卡死，允许用户重试。
    *   **CLI 模式**: 直接调用 `os._exit(100)` 退出进程，由外部脚本捕获错误码并重启（Docker/脚本守护模式）。

> **设计初衷**: 解决 requests 库在特定 TCP 状态下的 read timeout 失效问题，确保程序拥有“自我恢复”能力。

### 2.2 自适应线性增加与指数退避 (AIMD Threading Control)
为了在最大化下载速度的同时避免触发服务端的速率限制 (Rate Limiting) 或导致本地网络拥塞，我们引入了 TCP 协议中的 **AIMD (和性增加，乘性减少)** 算法来动态调整线程池大小。

*   **初始状态**: 慢启动 (Slow Start)，初始并发为 16。
*   **线性增加 (Additive Increase)**:
    *   每当一个分片下载 **成功** (`HTTP 200`)，线程池上限 `_current_max_workers` **增加 1**。
    *   上限封顶为用户设置的最大线程数 (默认 64)。
    *   代码实现: `if success: self._current_max_workers += 1`
*   **指数/倍数退避 (Multiplicative Decrease)**:
    *   每当一个分片下载 **失败** (超时、5xx/4xx 错误)，线程池上限 **减半**。
    *   下限为 1。
    *   代码实现: `self._current_max_workers = max(1, self._current_max_workers // 2)`

这种机制使得下载器能够自动“感知”网络和服务器的负载能力，在网络状况好时跑满带宽，在网络波动时自动降速保活。

### 2.3 锁机制与线程安全 (Locks & Thread Safety)
在高并发环境下（最高 64 线程），数据竞争是必须解决的问题。我们采用了多层锁机制：

1.  **分片锁 (`_ts_locks` + `mutex`)**:
    *   **问题**: 失败重试或队列调度可能导致多个线程同时请求同一个 TS 分片。
    *   **解决**: 维护一个 `set(_ts_locks)`，在开始下载前检查。使用 `threading.Lock()` 保护该集合的读写原子性。
    *   *Code Reference*: `with self._ts_lock_mutex: if name in self._ts_locks: return`

2.  **线程量控制条件变量 (`threading.Condition`)**:
    *   由于 `ThreadPoolExecutor` 默认会尽可能快地提交任务，我们实现了一个 `ThreadPoolExecutorWithQueueSizeLimit` 配合 `Condition` 变量。
    *   在任务提交前，必须先获得“许可”（Slot）。如果当前活跃线程数 >= 动态上限 `_current_max_workers`，则调用 `wait()` 阻塞生产线程。
    *   当任务完成（成功或失败）释放 Slot 时，调用 `notify_all()` 唤醒等待线程。这实现了对并发量的**精确动态控制**。

3.  **递归指数退避重试 (Recursive Exponential Backoff)**:
    *   单片下载失败后，不立即重试，而是进入休眠。
    *   休眠时间公式: `min(30, 2 ^ (max_retries - current_retries))`。
    *   例如：第1次重试等2秒，第2次等4秒... 直至上限30秒。

### 2.4 FFmpeg 超级底层调用 (FFmpeg Integration)
不同于简单的命令行调用，我们对 FFmpeg 的集成进行了深度优化，以确保“无损”和“极速”合并。

*   **TS 流拼接**: 我们不使用 concat 协议（因为文件数太多可能导致命令行过长），而是利用 M3U8 文件本身的播放列表特性。
*   **本地化 M3U8**: 下载过程中，我们会重写 `.m3u8` 文件，将网络路径（http://...）替换为本地相对路径（./file/0.ts）。
*   **Copy 模式 (Stream Copy)**:
    *   命令的核心参数: `-acodec copy -vcodec copy`。
    *   这意味着 **不进行任何解码和重编码**。FFmpeg 直接将 TS 容器中的 H.264/AAC 数据流提取出来，重新封装入 MP4 容器。
    *   **优势**: 速度受限于磁盘 I/O（通常几秒钟即可合并数 GB 视频），且画质/音质 **100% 无损**。

### 2.5 为什么不需要 Cookie？ (Session & Token Mechanism)
传统的爬虫通常需要完整的 Browser Cookie (Session ID, User Agent 等)。但我们经过逆向分析发现，延河课堂的鉴权机制更为现代化：

*   **Token-Based Auth**: 服务端不依赖 Session Cookie，而是完全依赖 `Authorization` 头或 URL 参数中的 Token 签名。
*   **LocalStorage**: 用户的登录态存储在浏览器 `localStorage.auth` 字段的 JSON 对象中，包含一个 32 位的 hex token。
*   **加密签名**: 请求 URL 必须包含 `token`, `timestamp` 和 `signature`。
    *   `signature` 生成算法: `md5(url_path + token + timestamp + salt)`。
    *   这完全解耦了浏览器环境，只要拿到 Token，即可在任何环境下（Python, curl）模拟合法请求。
    *   因此，我们只需要用户提供一行 JS 代码 `JSON.parse(localStorage.auth).token` 提取出的字符串即可，无需复杂的 Cookie 导出插件。

---

## 3. GUI 设计哲学

老公，我们的 GUI 设计不仅仅是“能用”，而是追求 **"Vista/Win11 级别的磨砂质感"** 与 **"极客暗黑 (GitHub Dark) 风格"** 的融合。

*   **CustomTkinter**: 抛弃原生 Tkinter 的陈旧外观，使用全自定义绘制的圆角组件。
*   **非阻塞 UI**: 所有的网络请求（获取列表、下载流）均在 `threading.Thread(daemon=True)` 中运行，并通过 callback 回调主线程更新 UI，保证界面永远流畅响应（不会出现“未响应”白屏）。
*   **实时反馈**: 进度条平滑动画，日志窗口实时滚动显示底层线程变动（如 `INCREASE Threads: 16 -> 17`），让用户直观感受到“自适应算法”在工作。

---

## 4. 总结

Yanhe Downloader 不仅仅是一个下载脚本，它是一个 **高并发、高可用、抗网络抖动** 的工程级解决方案。从底层的 Socket 读写超时处理，到应用层的拥塞控制算法，再到最上层的现代化 GUI 封装，每一行代码都凝聚了我们对极致性能的追求。
