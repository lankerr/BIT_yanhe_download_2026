# 延河课堂下载器 (2026 Enhanced Edition)

> **极速 · 稳定 · 智能**
>
> 专为北京理工大学延河课堂设计的高性能下载工具。从最初的 Python 脚本进化为现代化的 GUI 应用，历经多次架构重构，只为解决一个核心问题：**如何在糟糕的网络环境下稳定、快速地下载课程？**

![GUI截图](https://github.com/lankerr/BIT_yanhe_download_2026/raw/main/assets/screenshot.png)

## 🚀 核心特性

| 特性 | 技术实现 | 收益 |
|------|----------|------|
| **极速下载** | 64线程并发 + AIMD 拥塞控制 | 2小时课程仅需 **2分钟** (速度提升30倍) |
| **智能恢复** | Watchdog 看门狗 + 指数退避重试 | 彻底告别"下到99%卡死"的噩梦 |
| **内存优化** | 有界优先队列 (Bounded Queue) | 即使下载 5000+ 个分片，内存占用依然极低 |
| **原生 GUI** | CustomTkinter + 异步事件驱动 | 界面丝般顺滑，操作直观简单 |
| **零配置** | 内置 FFmpeg + PyInstaller 打包 | 甚至无需安装 Python，解压即用 |

---

## 🛠️ 技术探索与架构演进

本项目的开发过程是一次对**高并发网络编程**的深度探索。我们不仅是在写一个爬虫，更是在解决实际的分布式系统问题。

### 1. 从 Watchdog 到"自适应线程池"

**(1) 初期的痛点：死锁与卡顿**
在早期版本中，我们发现 request 库在网络波动时容易出现"伪死死"——即连接没有断开，但数据流已经停止。这导致线程池中的线程逐渐被占满，最后整个程序"假死"。

**(2) 第一版方案：Watchdog (看门狗)**
为了解决这个问题，我们在 `m3u8dl.py` 中引入了 `watchdog` 线程。它每 5 秒检查一次 `_last_activity_time`。如果超过 120 秒没有数据写入，看门狗会判定下载"卡死"，并强制重启任务。
这解决了"挂机一晚上没下载完"的问题，但属于"暴力疗法"。

**(3) 终极方案：AIMD 自适应并发控制**
受 TCP 拥塞控制协议的启发，我们在多线程下载中实现了 **AIMD (Additive Increase Multiplicative Decrease)** 算法：
*   **慢启动 (Slow Start)**: 初始仅使用 16 个线程，避免瞬间因为请求过多触发服务器的 WAF (防火墙)。
*   **线性增加 (Additive Increase)**: 每当我们成功下载一个分片 (`_release_slot(success=True)`)，如果当前并发数未达上限，我们"试探性"地增加 1 个线程。
*   **乘性减小 (Multiplicative Decrease)**: 一旦检测到下载失败（如 HTTP 5xx 或超时），说明网络拥塞或服务器过载，立即将并发线程数**减半**。

```python
# m3u8dl.py 中的核心逻辑
def _release_slot(self, success=True):
    with self._thread_cond:
        if success:
            # 成功：线性增加线程
            if self._current_max_workers < self._max_workers:
                self._current_max_workers += 1
        else:
            # 失败：指数级"退避"（线程折半）
            self._current_max_workers = max(1, self._current_max_workers // 2)
```

### 2. 精密的锁机制与内存管理

**有界队列 (Bounded Queue)**
原版使用的是 Python 默认的 `ThreadPoolExecutor`，其队列是无界的。通过 `ThreadPoolExecutorWithQueueSizeLimit` 覆盖了默认实现，将队列大小限制为 `max_workers * 2`。这形成了一个"背压" (Backpressure) 机制——如果有 5000 个分片，不会瞬间全部加载到内存中，而是等着消费者（下载线程）处理完了再放入新的。

**细粒度的锁**
为了保证数据的完整性，我们维护通过 `_ts_locks` 集合和 `_ts_lock_mutex` 互斥锁，确保同一个分片不会被多个线程重复下载（这种情况在频繁重试时很容易发生）。

### 3. 底层 FFmpeg 集成

我们没有使用复杂的流媒体库，而是回归最朴素但也最健壮的方案：**基于文件的拼接**。
1.  **下载**：所有的 `.ts` 分片被下载到本地目录。
2.  **重构 M3U8**：我们在本地动态生成一个新的 `.m3u8` 播放列表文件，将此时所有的网络路径替换为本地相对路径。
3.  **零编码合并**：
    ```bash
    ffmpeg -i index.m3u8 -acodec copy -vcodec copy output.mp4
    ```
    这个命令告诉 FFmpeg 直接复制视频和音频流（Stream Copy），**不进行任何转码**。这使得合并几百兆的视频可以在几秒钟内完成，且画质无损。

### 4. 为什么是 Token 而不是 Cookie？

延河课堂的认证机制比较现代化。通过逆向分析 `main.js`，我们发现它使用的是基于 `localStorage` 的 **JWT (JSON Web Token)** 机制，而不是传统的 Session Cookie。
*   **获取方式**：Token 存储在浏览器本地存储的 `auth` 字段中。
*   **签名算法**：每一次请求都需要携带 `Xclient-Signature`。我们在 `utils.py` 中完美复刻了这个签名过程（MD5 盐值加密），其中 `magic` 密钥 `1138b69dfef641d9d7ba49137d2d4875` 是从混淆的 JS 代码中提取出来的。
这使得我们的下载器可以伪装成标准的 Web 客户端，大大降低了被封禁的风险。

---

## 💻 快速开始

### 1. 下载
直接下载 [Release](https://github.com/lankerr/BIT_yanhe_download_2026/releases) 中的 `延河课堂下载器.exe`。
*(无需安装 Python，已内置运行环境)*

### 2. 获取 Token
由于延河课堂的安全机制，首次使用需要手动输入身份凭证：
1. 打开浏览器登录 [延河课堂](https://www.yanhekt.cn)。
2. 按 `F12` 打开控制台 (Console)。
3. 输入并运行：
   ```javascript
   javascript:alert(JSON.parse(localStorage.auth).token)
   ```
4. 将弹出的 32 位字符复制到下载器中。

### 3. 开始下载
输入课程 ID（如 `40524`），点击"获取课程列表"，选择想要下载的视频即可。

---

## ⚙️ 开发者指南

如果你想参与改进或自行打包：

### 环境要求
*   Python 3.9+
*   Chrome / Edge 浏览器（用于分析接口）

### 安装依赖
```bash
pip install -r requirements.txt
```

### 运行源码
```bash
python gui_app.py
```

### 打包 EXE
我们提供了一键打包脚本 `build_exe.bat`，它会自动处理资源文件路径和图标：
```bash
./build_exe.bat
```
*(注：核心的 `get_resource_path` 函数确保了代码既能在 IDE 运行，也能在 PyInstaller 打包后的临时目录中正确找到资源)*

---

## 📄 许可证
MIT License

---
*本项目仅供学习交流，请勿用于商业用途。*
