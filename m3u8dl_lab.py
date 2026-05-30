"""
m3u8 下载实验内核 —— 把 6 种方案的差异做成开关。

跟 m3u8dl.py 不同，本文件追求**纯净简洁**：
- 没有 watchdog 自重启
- 没有"尾部模式"（实验里发现尾部模式问题大于收益）
- 没有 print 噪音（事件流可控）
- 失败重试用单一指数退避机制

实验变体由 LabConfig 控制，开关清单：
  use_aimd                 是否启用 AIMD（True=类 cur 行为，False=固定 K）
  fixed_workers            use_aimd=False 时的固定线程数
  connect_timeout/read_timeout  HTTP 超时
  use_atomic_count         success 计数是否加 Lock
  use_binary_concat        合并方式：True=cat ts，False=ffmpeg
  max_segments             只下前 N 段（实验加速用，None=全下）

返回 LabResult 报告：耗时/吞吐/失败数/AIMD 窗口轨迹/合并耗时。
"""
from __future__ import annotations

import base64
import os
import queue
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, Optional

import requests
import urllib3
urllib3.disable_warnings()

import utils


# ============================================================
# 配置
# ============================================================
@dataclass
class LabConfig:
    # 线程模型
    use_aimd: bool = False
    fixed_workers: int = 16          # use_aimd=False 时用
    aimd_max_workers: int = 64
    aimd_min_workers: int = 4
    aimd_initial_workers: int = 16

    # 超时
    connect_timeout: int = 10
    read_timeout: int = 60

    # 重试
    max_retries: int = 5
    retry_base_wait: float = 1.0     # 指数退避基线 (1, 2, 4, 8 ...)

    # 计数原子性
    use_atomic_count: bool = True

    # 合并
    use_binary_concat: bool = True   # True = cat ts；False = ffmpeg

    # 实验加速
    max_segments: Optional[int] = None
    progress_callback: Optional[Callable] = None  # (done, total, status, threads, max_threads)

    # 日志
    verbose: bool = False


# ============================================================
# 报告
# ============================================================
@dataclass
class LabResult:
    ok: bool = False
    error: str = ""

    # 核心指标
    elapsed_s: float = 0.0
    bytes_total: int = 0
    segments_total: int = 0
    segments_done: int = 0
    segments_failed: int = 0

    # 派生
    throughput_mbps: float = 0.0     # MB/s
    per_seg_ms: float = 0.0

    # 阶段细分
    parse_m3u8_s: float = 0.0
    download_s: float = 0.0
    merge_s: float = 0.0

    # AIMD 轨迹（每秒采样一次，仅 use_aimd=True 才记录）
    aimd_window_history: list[tuple[float, int]] = field(default_factory=list)
    # 失败明细
    failed_segments: list[int] = field(default_factory=list)
    retry_count: int = 0

    # 合并方式
    merge_method: str = ""

    def derive(self):
        """填充派生字段。"""
        if self.elapsed_s > 0:
            self.throughput_mbps = round(self.bytes_total / 1024 / 1024 / self.elapsed_s, 2)
        if self.segments_done > 0:
            self.per_seg_ms = round(self.download_s * 1000 / self.segments_done, 1)
        return self


# ============================================================
# 内核
# ============================================================
class M3u8DownloadLab:
    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/107.0.0.0 Safari/537.36",
        "Origin": "https://www.yanhekt.cn",
        "Referer": "https://www.yanhekt.cn/",
    }

    def __init__(self, url: str, work_dir: str, name: str, cfg: LabConfig):
        self.url = url
        self.work_dir = work_dir
        self.name = name
        self.cfg = cfg
        self.result = LabResult()

        # 解析后的 ts URL 列表
        self.ts_urls: list[str] = []
        self.front_url: Optional[str] = None

        # 计数（按 cfg.use_atomic_count 决定是否加锁）
        self._success = 0
        self._failed = 0
        self._lock = threading.Lock()

        # AIMD 状态
        self._cur_max = cfg.aimd_initial_workers if cfg.use_aimd else cfg.fixed_workers
        self._active = 0
        self._aimd_cond = threading.Condition()
        self._aimd_history_thread: Optional[threading.Thread] = None
        self._aimd_running = False

        # 路径
        self.app_path = utils.get_app_path()
        self.full_dir = os.path.join(self.app_path, work_dir, name)
        os.makedirs(self.full_dir, exist_ok=True)
        self.mp4_path = os.path.join(self.app_path, work_dir, name + ".mp4")

        # 签名
        self.timestamp, self.signature = utils.getSignature()
        self._token: Optional[str] = None

    # ------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------
    def run(self) -> LabResult:
        t_total = time.time()
        try:
            # 1. 解析 m3u8
            t0 = time.time()
            self._parse_m3u8(self.url)
            self.result.parse_m3u8_s = round(time.time() - t0, 2)

            if not self.ts_urls:
                self.result.error = "解析 m3u8 失败（0 段）"
                return self.result.derive()

            if self.cfg.max_segments and self.cfg.max_segments < len(self.ts_urls):
                self.ts_urls = self.ts_urls[:self.cfg.max_segments]
                self._log(f"截取前 {self.cfg.max_segments} 段实验")

            self.result.segments_total = len(self.ts_urls)
            self._log(f"待下载 {len(self.ts_urls)} 段")

            # 2. 下载
            t0 = time.time()
            self._download_all()
            self.result.download_s = round(time.time() - t0, 2)
            self.result.segments_done = self._success
            self.result.segments_failed = self._failed

            # 3. 计算字节
            self.result.bytes_total = sum(
                os.path.getsize(os.path.join(self.full_dir, f"{i}.ts"))
                for i in range(len(self.ts_urls))
                if os.path.exists(os.path.join(self.full_dir, f"{i}.ts"))
            )

            # 4. 严格对账（缺片就失败，区别于 m3u8dl 的 95% 阈值）
            if self._failed > 0:
                self.result.error = f"缺 {self._failed} 段（严格对账）"
                return self.result.derive()

            # 5. 合并
            t0 = time.time()
            if self.cfg.use_binary_concat:
                self._merge_binary()
                self.result.merge_method = "binary_concat"
            else:
                self._merge_ffmpeg()
                self.result.merge_method = "ffmpeg"
            self.result.merge_s = round(time.time() - t0, 2)

            self.result.ok = True

        except Exception as e:
            self.result.error = f"{type(e).__name__}: {e}"
            import traceback; traceback.print_exc()
        finally:
            self.result.elapsed_s = round(time.time() - t_total, 2)
            self._stop_aimd_history()
        return self.result.derive()

    # ------------------------------------------------------------
    # m3u8 解析
    # ------------------------------------------------------------
    def _parse_m3u8(self, m3u8_url: str, depth: int = 0):
        if depth > 5:
            raise RuntimeError("m3u8 嵌套太深")
        if not self._token:
            self._token = utils.getToken()
        url_signed = utils.add_signature_for_url(m3u8_url, self._token, self.timestamp, self.signature)

        for attempt in range(3):
            try:
                r = requests.get(url_signed, timeout=(self.cfg.connect_timeout, self.cfg.read_timeout),
                                 verify=False, headers=self.HEADERS)
                if r.status_code != 200:
                    raise RuntimeError(f"HTTP {r.status_code}")
                text = r.content.decode("utf-8", errors="replace")
                self.front_url = r.url.split(r.request.path_url)[0]

                if "EXT-X-STREAM-INF" in text:
                    # master playlist，找子 m3u8
                    sub = None
                    for line in text.splitlines():
                        line = line.strip()
                        if not line or line.startswith("#"):
                            continue
                        sub = line if line.startswith("http") else (
                            self.front_url + line if line.startswith("/")
                            else m3u8_url.rsplit("/", 1)[0] + "/" + line
                        )
                        break
                    if sub:
                        return self._parse_m3u8(sub, depth + 1)

                # leaf playlist，提 ts URL
                base = r.url.rsplit("/", 1)[0]
                for line in text.splitlines():
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if line.startswith("http"):
                        self.ts_urls.append(line)
                    elif line.startswith("/"):
                        self.ts_urls.append(self.front_url + line)
                    else:
                        self.ts_urls.append(base + "/" + line)
                return

            except Exception as e:
                if attempt < 2:
                    time.sleep(1.5 ** attempt)
                else:
                    raise

    # ------------------------------------------------------------
    # 下载
    # ------------------------------------------------------------
    def _download_all(self):
        # AIMD 模式启动窗口采样线程
        if self.cfg.use_aimd:
            self._start_aimd_history()

        # 用于失败任务的 deferred 队列
        deferred: queue.Queue[int] = queue.Queue()

        # ---- 阶段 1：批量并发 ----
        max_workers = self.cfg.fixed_workers if not self.cfg.use_aimd else self.cfg.aimd_max_workers
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = []
            for i, ts_url in enumerate(self.ts_urls):
                futures.append(pool.submit(self._download_one, i, ts_url, deferred))
            for f in futures:
                f.result()  # 等所有任务跑完（不会抛，内部 catch）

        # ---- 阶段 2：失败 deferred 串行重试（指数退避） ----
        while not deferred.empty():
            i = deferred.get()
            for attempt in range(self.cfg.max_retries):
                try:
                    self._download_one_blocking(i, self.ts_urls[i])
                    if self.cfg.use_atomic_count:
                        with self._lock:
                            self._success += 1
                    else:
                        self._success += 1
                    self._emit_progress()
                    break
                except Exception as e:
                    self.result.retry_count += 1
                    if attempt < self.cfg.max_retries - 1:
                        time.sleep(self.cfg.retry_base_wait * (2 ** attempt))
                    else:
                        if self.cfg.use_atomic_count:
                            with self._lock:
                                self._failed += 1
                        else:
                            self._failed += 1
                        self.result.failed_segments.append(i)

    def _download_one(self, idx: int, ts_url: str, deferred: queue.Queue):
        """阶段 1 用：成功就 +1，失败就丢进 deferred。"""
        # AIMD slot acquire
        if self.cfg.use_aimd:
            self._aimd_acquire()
        try:
            self._download_one_blocking(idx, ts_url)
            if self.cfg.use_atomic_count:
                with self._lock:
                    self._success += 1
            else:
                self._success += 1
            self._emit_progress()
            if self.cfg.use_aimd:
                self._aimd_release(success=True)
        except Exception as e:
            deferred.put(idx)
            self.result.retry_count += 1
            if self.cfg.use_aimd:
                self._aimd_release(success=False)

    def _download_one_blocking(self, idx: int, ts_url: str):
        ts_path = os.path.join(self.full_dir, f"{idx}.ts")
        if os.path.exists(ts_path) and os.path.getsize(ts_path) > 0:
            return  # 已下载（断点续传）
        if not self._token:
            self._token = utils.getToken()
        signed = utils.add_signature_for_url(ts_url.split("\n")[0], self._token,
                                              self.timestamp, self.signature)
        r = requests.get(signed, stream=True,
                         timeout=(self.cfg.connect_timeout, self.cfg.read_timeout),
                         verify=False, headers=self.HEADERS)
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}")
        with open(ts_path, "wb") as f:
            for chunk in r.iter_content(64 * 1024):
                if chunk:
                    f.write(chunk)
        if os.path.getsize(ts_path) == 0:
            raise RuntimeError("0 bytes")

    # ------------------------------------------------------------
    # AIMD slot
    # ------------------------------------------------------------
    def _aimd_acquire(self):
        with self._aimd_cond:
            while self._active >= self._cur_max:
                self._aimd_cond.wait(timeout=5)
            self._active += 1

    def _aimd_release(self, success: bool):
        with self._aimd_cond:
            self._active -= 1
            if success and self._cur_max < self.cfg.aimd_max_workers:
                self._cur_max += 1
            elif not success and self._cur_max > self.cfg.aimd_min_workers:
                self._cur_max = max(self.cfg.aimd_min_workers, self._cur_max - 2)
            self._aimd_cond.notify_all()

    def _start_aimd_history(self):
        self._aimd_running = True
        self._aimd_t0 = time.time()

        def loop():
            while self._aimd_running:
                self.result.aimd_window_history.append(
                    (round(time.time() - self._aimd_t0, 1), self._cur_max)
                )
                time.sleep(0.5)

        self._aimd_history_thread = threading.Thread(target=loop, daemon=True)
        self._aimd_history_thread.start()

    def _stop_aimd_history(self):
        self._aimd_running = False

    # ------------------------------------------------------------
    # 合并
    # ------------------------------------------------------------
    def _merge_binary(self):
        """直接拼接 ts 字节流。MPEG-TS 容器原生支持。"""
        with open(self.mp4_path, "wb") as out:
            for i in range(len(self.ts_urls)):
                ts_path = os.path.join(self.full_dir, f"{i}.ts")
                with open(ts_path, "rb") as f:
                    while chunk := f.read(1 << 20):
                        out.write(chunk)

    def _merge_ffmpeg(self):
        """走 ffmpeg concat demuxer（比 m3u8 协议快）。"""
        from app_paths import ffmpeg_path
        list_path = os.path.join(self.full_dir, "list.txt")
        with open(list_path, "w", encoding="utf-8") as f:
            for i in range(len(self.ts_urls)):
                f.write(f"file '{i}.ts'\n")
        cmd = [
            ffmpeg_path(),
            "-f", "concat", "-safe", "0",
            "-i", list_path,
            "-c", "copy",
            self.mp4_path,
            "-y", "-loglevel", "error",
        ]
        startupinfo = None
        if sys.platform == "win32":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = subprocess.SW_HIDE
        subprocess.run(cmd, check=True, startupinfo=startupinfo,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # ------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------
    def _emit_progress(self):
        if self.cfg.progress_callback:
            try:
                self.cfg.progress_callback(
                    self._success, len(self.ts_urls), 0,
                    self._active, self._cur_max,
                )
            except Exception:
                pass

    def _log(self, msg: str):
        if self.cfg.verbose:
            print(f"[lab] {msg}")


# ============================================================
# 预设：6 个变体
# ============================================================
VARIANTS = {
    "cur": LabConfig(
        # 模拟当前 m3u8dl.py 的 AIMD 4-64 + 短超时 + 非原子计数
        use_aimd=True,
        aimd_initial_workers=16,
        connect_timeout=5,
        read_timeout=15,
        max_retries=5,
        retry_base_wait=0.5,
        use_atomic_count=False,
        use_binary_concat=False,    # 跟当前 m3u8dl.py 保持一致用 ffmpeg
    ),
    "v1_aimd_off": LabConfig(
        use_aimd=False,
        fixed_workers=16,
        connect_timeout=5,
        read_timeout=15,
        use_atomic_count=False,
        use_binary_concat=False,
    ),
    "v2_timeout_loose": LabConfig(
        use_aimd=True,
        aimd_initial_workers=16,
        connect_timeout=10,
        read_timeout=60,
        use_atomic_count=False,
        use_binary_concat=False,
    ),
    "v3_atomic_count": LabConfig(
        use_aimd=True,
        aimd_initial_workers=16,
        connect_timeout=5,
        read_timeout=15,
        use_atomic_count=True,
        use_binary_concat=False,
    ),
    "v4_simple_pool": LabConfig(
        # 全删：固定 K + 严格对账 + 简单重试
        use_aimd=False,
        fixed_workers=16,
        connect_timeout=10,
        read_timeout=30,
        max_retries=3,
        retry_base_wait=1.0,
        use_atomic_count=True,
        use_binary_concat=False,
    ),
    "v5_bin_concat": LabConfig(
        # 仅改合并方式
        use_aimd=True,
        aimd_initial_workers=16,
        connect_timeout=5,
        read_timeout=15,
        use_atomic_count=False,
        use_binary_concat=True,
    ),
    "v_combo": LabConfig(
        # 阶段 1 学到：增加超时反而慢，所以保留短超时
        # 保留：删 AIMD + 二进制合并 + 原子计数
        use_aimd=False,
        fixed_workers=16,
        connect_timeout=5,
        read_timeout=20,        # 比 cur 略宽，避免误超时
        max_retries=3,
        retry_base_wait=1.0,
        use_atomic_count=True,
        use_binary_concat=True,
    ),
    "v_combo_no_atomic": LabConfig(
        # 把 v_combo 的 atomic 拿掉对比
        use_aimd=False,
        fixed_workers=16,
        connect_timeout=5,
        read_timeout=20,
        max_retries=3,
        retry_base_wait=1.0,
        use_atomic_count=False,
        use_binary_concat=True,
    ),
    "v_minimal": LabConfig(
        # 最小化：只删 AIMD + 二进制合并，其他全保持 cur
        use_aimd=False,
        fixed_workers=16,
        connect_timeout=5,
        read_timeout=15,
        max_retries=5,
        retry_base_wait=0.5,
        use_atomic_count=False,
        use_binary_concat=True,
    ),
    "v_winner": LabConfig(
        # 阶段 2 最优组合：v1_aimd_off 参数 + 二进制合并
        use_aimd=False,
        fixed_workers=8,           # 阶段 2 实测最优 K
        connect_timeout=5,
        read_timeout=15,
        max_retries=5,
        retry_base_wait=0.5,
        use_atomic_count=False,
        use_binary_concat=True,
    ),
}
