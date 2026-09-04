import base64
import logging
import os
import queue
import re
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import requests
import urllib3

import utils
import threading

# ====== Module-level file logger ======
logger = logging.getLogger("m3u8dl")
if not logger.handlers:
    _log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "download.log")
    _fh = logging.FileHandler(_log_path, encoding="utf-8")
    _fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(_fh)
    # Also log to console for real-time visibility
    _ch = logging.StreamHandler()
    _ch.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    _ch.setLevel(logging.WARNING)  # Console only shows warnings+
    logger.addHandler(_ch)
    logger.setLevel(logging.DEBUG)


def make_sum():
    ts_num = 0
    while True:
        yield ts_num
        ts_num += 1


def dummy_func(downloaded, total, merge_status, active_threads=0, max_threads=0):
    return


class M3u8Download:
    """延河课堂 HLS 下载器（固定并发引擎）。

    阶段4全段实验（docs/speed_stage4_fullseg.md）定稿：
      - 删除 AIMD 自适应窗口（弱网下陷入“死亡螺旋”，全段比固定 K 慢 33-35%）
      - 固定线程池（饱和点 K≈8-16），两阶段下载：并发 + 失败串行补
      - 计数加锁，消除 `+= 1` 非原子导致的“卡 99%”
      - watchdog 降级为“真僵死”兜底，不再驱动复杂的尾部模式

    :param url: 完整的 m3u8 文件链接
    :param name: 保存的文件名
    :param max_workers: 固定并发线程数（钳制到 4..64）
    :param num_retries: m3u8 信息获取重试次数
    :param base64_key: base64 编码的解密 key（可选）
    :param stats_hook: 可选回调 stats_hook(engine)，在并发下载启动前被调用，
                       供压测脚本拿到引擎引用做字节级采样（GUI 不用传）
    """

    def __init__(
        self,
        url,
        workDir,
        name,
        max_workers=16,
        num_retries=5,
        base64_key=None,
        progress_callback=dummy_func,
        gui_mode=False,
        stats_hook=None,
    ):
        self._url = url
        self._token = None
        self._workDir = workDir
        self._name = name
        # 固定并发：家用宽带实测饱和点 K≈8-16；上限放到 64 供校园网压测
        # 探索更高档位（GUI 滑块仍维持默认 16、上限 32）。
        self._max_workers = max(4, min(max_workers, 64))
        self._num_retries = num_retries
        self._progress_callback = progress_callback

        # 进度回调节流：每 N 个文件更新一次
        self._last_progress_count = 0
        self._progress_step = 5

        # watchdog 仅作“真僵死”兜底（单分片自带超时，正常不触发）
        self._watchdog_timeout = 180
        self._gui_mode = gui_mode
        self._watchdog_triggered = False

        # 使用 get_app_path() 确保 exe 和 Python 模式下路径一致
        app_path = utils.get_app_path()
        if not os.path.exists(os.path.join(app_path, self._workDir)):
            os.makedirs(os.path.join(app_path, self._workDir))
        self._file_path = os.path.join(app_path, self._workDir, self._name)
        print(f"[M3U8] File path: {self._file_path}")
        if os.path.exists(self._file_path + ".mp4"):
            print(f"File '{self._file_path}.mp4' already exists, skip download")
            self._progress_callback(100, 100, 2)
            return
        self._front_url = None
        self._ts_url_list = []
        self._success_sum = 0
        self._ts_sum = 0
        self._key = base64.b64decode(base64_key.encode()) if base64_key else None
        self._last_activity_time = time.time()
        self._downloading = True

        # 计数加锁：消除“卡 99%”死循环
        self._success_lock = threading.Lock()
        self._permanently_failed = set()  # 永久失败的文件

        # ---- 压测观测仪表（bench_maxspeed 采样用，不影响下载行为）----
        self._bytes_done = 0            # 成功分片累计字节数
        self._bytes_lock = threading.Lock()
        self._seg_times = []            # 每片成功耗时(s)，list.append 原子免锁
        self._deferred_count = 0        # 阶段1失败进补下载队列的分片数
        self._stage2_attempts = 0       # 阶段2重试总次数
        self._stage2_ok = 0             # 阶段2补回的分片数
        self._t_download_start = None   # 并发下载阶段起点
        self._stats_hook = stats_hook

        self._watchdog_thread = threading.Thread(target=self.watchdog, daemon=True)
        self._watchdog_thread.start()

        self._total_timeout = 120  # 单个 .ts 文件墙钟总超时
        self._headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/93.0.4577.82 Safari/537.36 Edg/93.0.961.52",
            "Origin": "https://www.yanhekt.cn",
            "referer": "https://www.yanhekt.cn/",
        }
        self.timestamp, self.signature = utils.getSignature()
        urllib3.disable_warnings()

        self._url = utils.encryptURL(self._url)
        self._last_error = ""

        # 通知 GUI 正在获取视频信息 (status=-2 表示正在初始化)
        self._progress_callback(0, 0, -2, 0, 0)
        print(f"[M3u8Download] 开始获取 m3u8 信息: {self._name}")
        print(f"[M3u8Download] 原始 URL: {self._url[:100]}...")

        self.get_m3u8_info(self._url, self._num_retries)

        if self._ts_sum == 0:
            error_detail = self._last_error if self._last_error else "未知错误"
            print(f"[M3u8Download] 错误: 未获取到任何 ts 文件. 原因: {error_detail}")
            raise Exception(f"无法获取视频信息: {error_detail}")

        print(f"[M3u8Download] 成功获取 {self._ts_sum} 个 ts 文件")

        # signal 只能在主线程中使用，GUI模式下跳过
        if not self._gui_mode:
            def signal_handler(sig, frame):
                print("Caught KeyboardInterrupt. Shutting down...")
                os._exit(1)

            try:
                signal.signal(signal.SIGINT, signal_handler)
            except ValueError:
                pass

        print(f"Downloading: {self._name}", f"Save path: {self._file_path}", sep="\n")

        # ========== 两阶段下载 ==========
        self._download_all()

        # ========== 最终状态检查 ==========
        actual_files = len([f for f in os.listdir(self._file_path) if f.endswith('.ts')])
        print(f"\n[下载统计] 成功: {self._success_sum}/{self._ts_sum}, 实际文件: {actual_files}")

        if actual_files >= self._ts_sum * 0.95:  # 允许 5% 失败率兜底
            self._progress_callback(self._success_sum, self._ts_sum, 1)
            self.output_mp4()
            self.delete_file()
            print(f"Download successfully --> {self._name}")
            debug_log_path = os.path.join(utils.get_app_path(), "debug.log")
            with open(debug_log_path, "a", encoding="utf-8") as f:
                f.write(f"[{time.strftime('%H:%M:%S')}] {self._name} COMPLETED. Files: {actual_files}/{self._ts_sum}\n")
            self._progress_callback(self._success_sum, self._ts_sum, 2)
        else:
            failed_list = list(self._permanently_failed)[:10]
            print(f"Download failed for {self._name}. Success: {actual_files}/{self._ts_sum}")
            print(f"Failed files (first 10): {failed_list}")
            raise Exception(f"Download incomplete: {actual_files}/{self._ts_sum}")
        self._downloading = False

    # ------------------------------------------------------------
    # 压测观测
    # ------------------------------------------------------------
    def stats(self) -> dict:
        """采样快照（线程安全），压测脚本每 0.5s 轮询一次。"""
        with self._bytes_lock:
            b = self._bytes_done
        return {
            "bytes": b,
            "success": self._success_sum,
            "total": self._ts_sum,
            "seg_done": len(self._seg_times),
            "download_elapsed": (
                time.time() - self._t_download_start
                if self._t_download_start else None
            ),
        }

    def final_stats(self) -> dict:
        """下载结束后的完整统计（含分片耗时分布）。"""
        times = sorted(self._seg_times)
        def pct(p):
            return round(times[min(int(len(times) * p), len(times) - 1)], 3) if times else None
        return {
            "bytes_done": self._bytes_done,
            "ts_sum": self._ts_sum,
            "success_sum": self._success_sum,
            "deferred_count": self._deferred_count,
            "stage2_attempts": self._stage2_attempts,
            "stage2_ok": self._stage2_ok,
            "permanently_failed": len(self._permanently_failed),
            "seg_time_p50": pct(0.50),
            "seg_time_p95": pct(0.95),
            "seg_time_p99": pct(0.99),
            "seg_time_max": round(times[-1], 3) if times else None,
        }

    # ------------------------------------------------------------
    # 计数 / 进度
    # ------------------------------------------------------------
    def _bump_success(self):
        """原子地 +1，并按节流推送进度。返回当前成功数。"""
        with self._success_lock:
            self._success_sum += 1
            done = self._success_sum
        self._last_activity_time = time.time()
        if done >= self._last_progress_count + self._progress_step or done == self._ts_sum:
            self._last_progress_count = done
            self._progress_callback(done, self._ts_sum, 0, 0, self._max_workers)
        return done

    # ------------------------------------------------------------
    # 两阶段下载
    # ------------------------------------------------------------
    def _download_all(self):
        """阶段1：固定线程池并发；阶段2：失败分片串行指数退避补下载。"""
        deferred = queue.Queue()

        # ---- 阶段1：并发 ----
        self._t_download_start = time.time()
        if self._stats_hook:
            try:
                self._stats_hook(self)
            except Exception:
                pass
        sig_thread = threading.Thread(target=self.updateSignatureLoop, daemon=True)
        sig_thread.start()

        with ThreadPoolExecutor(max_workers=self._max_workers) as pool:
            # 提交循环确认：name 逐片依赖序号 k，os.path.join 无法再外提；
            # self._file_path 已是构造时算好的绝对路径，循环内无重复计算
            futures = []
            for k, ts_url in enumerate(self._ts_url_list):
                name = os.path.join(self._file_path, f"{k}.ts")
                futures.append(pool.submit(self._download_one, ts_url, name, deferred))
            for f in futures:
                try:
                    f.result()
                except Exception:
                    pass  # 内部已处理，不让线程池抛出

        print(f"\n[阶段1结束] 成功: {self._success_sum}/{self._ts_sum}, "
              f"待补下载: {deferred.qsize()}")
        self._deferred_count = deferred.qsize()

        # ---- 阶段2：失败分片串行补下载（指数退避）----
        retry_items = []
        while not deferred.empty():
            retry_items.append(deferred.get())

        if retry_items:
            print(f"[阶段2] 补下载 {len(retry_items)} 个失败分片")
        for ts_url, name in retry_items:
            if self._watchdog_triggered:
                break
            ok = False
            for attempt in range(self._num_retries):
                self._stage2_attempts += 1
                if self._download_one_blocking(ts_url, name):
                    self._stage2_ok += 1
                    self._bump_success()
                    ok = True
                    print(f"[补下载成功] {os.path.basename(name)} (尝试{attempt + 1})")
                    break
                time.sleep(min(0.5 * (2 ** attempt), 8))
            if not ok:
                self._permanently_failed.add(name)
                print(f"[补下载失败] {os.path.basename(name)} (已尝试{self._num_retries}次)")

    def _download_one(self, ts_url_original, name, deferred):
        """阶段1 worker：成功 +1，失败丢进 deferred（不阻塞线程）。"""
        if self._watchdog_triggered:
            return
        if self._download_one_blocking(ts_url_original, name):
            self._bump_success()
            self._render_progress_bar()
        else:
            deferred.put((ts_url_original, name))

    def _download_one_blocking(self, ts_url_original, name):
        """下载单个 ts。成功返回 True；失败返回 False（并清理残片）。"""
        if os.path.exists(name) and os.path.getsize(name) > 0:
            # 断点续传：已存在且非空（计入字节数，不记分片耗时）
            with self._bytes_lock:
                self._bytes_done += os.path.getsize(name)
            return True
        if os.path.exists(name):
            try:
                os.remove(name)
            except OSError:
                pass
        try:
            if not self._token:
                self._token = utils.getToken()
            ts_url = utils.add_signature_for_url(
                ts_url_original.split("\n")[0], self._token,
                self.timestamp, self.signature,
            )
            _dl_start = time.time()
            _received = 0
            with utils.direct_get(
                ts_url, stream=True, timeout=(5, 15),
                verify=False, headers=self._headers,
            ) as res:
                if res.status_code != 200:
                    # 403/429/503 由 utils.direct_get 自动触发全局熔断，
                    # 其余线程会统一退避，避免继续硬敲导致封禁升级
                    raise Exception(f"Status {res.status_code}")
                dir_path = os.path.dirname(name)
                if not os.path.exists(dir_path):
                    os.makedirs(dir_path, exist_ok=True)
                with open(name, "wb") as ts:
                    # 算子级优化：chunk 64KB -> 1MB。每片约 1.8MB，64KB 要循环约
                    # 29 次（含 29 次 Python 层调度 + write 系统调用 + 超时检查），
                    # 1MB 只需 2 次；iter_content 是"最多 chunk_size"，分片尾部
                    # 不足 1MB 时自动返回剩余部分，落盘内容字节级等价。
                    # 总超时检查粒度从"每 64KB"变为"每 1MB"，1MB@百兆宽带约 0.1s，
                    # 对 120s 总超时的判定精度影响可忽略。
                    for chunk in res.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            ts.write(chunk)
                            _received += len(chunk)
                        if time.time() - _dl_start > self._total_timeout:
                            raise TimeoutError(
                                f"Total timeout {self._total_timeout}s for "
                                f"{os.path.basename(name)}"
                            )
            # 算子级优化：不再 os.path.getsize() 重新 stat 读文件元数据，
            # 直接用本地累计的写入字节数判断空文件，语义等价（文件刚由
            # 本线程写完关闭，字节数即 _received）
            if _received == 0:
                raise Exception("Downloaded file is empty")
            with self._bytes_lock:
                self._bytes_done += _received
            self._seg_times.append(time.time() - _dl_start)
            return True
        except Exception as e:
            logger.info(f"FAIL {os.path.basename(name)}: {type(e).__name__}: {e}")
            if os.path.exists(name):
                try:
                    os.remove(name)
                except OSError:
                    pass
            return False

    def _render_progress_bar(self):
        """终端进度条（GUI 模式下无副作用）。"""
        # 算子级优化：GUI 模式 / 输出被重定向（非 tty）时进度条无人观看，
        # 直接 return，避免每个分片成功都白写一次 stdout（297 片 = 297 次
        # write+flush 系统调用）。PyInstaller --noconsole 下 sys.stdout 可能为
        # None，先防御 None 再调 isatty()。
        if self._gui_mode or sys.stdout is None or not sys.stdout.isatty():
            return
        if self._ts_sum <= 0:
            return
        done = self._success_sum
        bar = "*" * (100 * done // self._ts_sum // 4)
        sys.stdout.write(
            "\r[%-25s](%d/%d) workers:%d" % (bar, done, self._ts_sum, self._max_workers)
        )
        sys.stdout.flush()

    def watchdog(self):
        # GUI 模式开销评估：每 5s 醒一次只做 time.time() 比较，几乎零开销，
        # 是"真僵死"兜底所必需，保持原样
        while self._downloading:
            time.sleep(5)
            idle = time.time() - self._last_activity_time
            if idle > 30 and int(idle) % 30 < 6:
                logger.info(
                    f"Watchdog: {self._name} idle={idle:.0f}s "
                    f"progress={self._success_sum}/{self._ts_sum} "
                    f"workers={self._max_workers}"
                )
            if idle > self._watchdog_timeout:
                logger.error(
                    f"Watchdog TRIGGERED for {self._name}: "
                    f"idle={idle:.0f}s > {self._watchdog_timeout}s, "
                    f"progress={self._success_sum}/{self._ts_sum}"
                )
                print(f"\n[Watchdog] Download hung for {self._watchdog_timeout}s!")
                self._watchdog_triggered = True
                self._downloading = False
                return

    def updateSignatureLoop(self):
        # GUI 模式开销评估：每 10s 一次 utils.getSignature()（一次轻量 HTTP），
        # 相对 16 线程的分片流量可忽略，且签名过期会导致 ts 请求 403，
        # 不能安全省略，保持原样
        while self._success_sum != self._ts_sum and self._downloading:
            self.timestamp, self.signature = utils.getSignature()
            time.sleep(10)

    def get_m3u8_info(self, m3u8_url: str, num_retries: int) -> None:
        """
        获取m3u8信息
        """
        print(f"[M3u8Download] get_m3u8_info 尝试获取: {m3u8_url[:80]}... (剩余重试: {num_retries})")

        try:
            if not self._token:
                print("[M3u8Download] 正在获取 video token...")
                self._token = utils.getToken()
                print(f"[M3u8Download] Token 获取成功: {self._token[:8]}...")
        except Exception as e:
            print(f"[M3u8Download] 获取 Token 失败: {type(e).__name__}: {e}")
            if num_retries > 0:
                time.sleep(1)
                self.get_m3u8_info(m3u8_url, num_retries - 1)
            else:
                self._last_error = f"获取Token失败: {e}"
            return

        token = self._token
        url = utils.add_signature_for_url(
            m3u8_url, token, self.timestamp, self.signature
        )
        try:
            print(f"[M3u8Download] 请求 m3u8 文件...")
            with utils.direct_get(
                url, timeout=(10, 60), verify=False, headers=self._headers
            ) as res:
                print(f"[M3u8Download] 响应状态码: {res.status_code}")
                if res.status_code != 200:
                    error_msg = f"获取 m3u8 失败: HTTP {res.status_code}"
                    print(f"[M3u8Download] {error_msg}")
                    if res.status_code == 403:
                        error_msg += " (Token可能已过期，请重新获取认证码)"
                    elif res.status_code == 404:
                        error_msg += " (视频不存在或已被删除)"
                    raise Exception(error_msg)

                self._front_url = res.url.split(res.request.path_url)[0]
                m3u8_text = res.content.decode("utf-8", errors="replace")
                content_preview = m3u8_text[:200] if m3u8_text else "(空响应)"
                print(f"[M3u8Download] m3u8 内容预览: {content_preview}")

                if "EXT-X-STREAM-INF" in m3u8_text:  # 判定为顶级M3U8文件
                    print("[M3u8Download] 检测到顶级 m3u8，正在解析子文件...")
                    for line in m3u8_text.split("\n"):
                        if "#" in line:
                            continue
                        elif line.startswith("http"):
                            self._url = line
                        elif line.startswith("/"):
                            self._url = self._front_url + line
                        else:
                            self._url = self._url.rsplit("/", 1)[0] + "/" + line
                    self.get_m3u8_info(self._url, self._num_retries)
                else:
                    self.get_ts_url(m3u8_text)
                    print(f"[M3u8Download] 成功解析 {self._ts_sum} 个 ts 文件")
        except requests.exceptions.Timeout as e:
            print(f"[M3u8Download] 请求超时: {e}")
            if num_retries > 0:
                time.sleep(2)
                self.get_m3u8_info(m3u8_url, num_retries - 1)
            else:
                self._last_error = f"请求超时，网络连接不稳定"
        except requests.exceptions.ConnectionError as e:
            print(f"[M3u8Download] 连接错误: {e}")
            if num_retries > 0:
                time.sleep(2)
                self.get_m3u8_info(m3u8_url, num_retries - 1)
            else:
                self._last_error = f"网络连接失败，请检查网络或关闭VPN"
        except Exception as e:
            print(f"[M3u8Download] 获取 m3u8 异常: {type(e).__name__}: {e}")
            if num_retries > 0:
                time.sleep(1)
                self.get_m3u8_info(m3u8_url, num_retries - 1)
            else:
                self._last_error = str(e)

    def get_ts_url(self, m3u8_text_str: str) -> None:
        """
        获取每一个ts文件的链接
        """
        if not os.path.exists(self._file_path):
            os.mkdir(self._file_path)
        new_m3u8_str = ""
        ts = make_sum()
        for line in m3u8_text_str.split("\n"):
            if "#" in line:
                if "EXT-X-KEY" in line and "URI=" in line:
                    if os.path.exists(os.path.join(self._file_path, "key")):
                        continue
                    key = self.download_key(line, 5)
                    if key:
                        new_m3u8_str += f"{key}\n"
                        continue
                new_m3u8_str += f"{line}\n"
                if "EXT-X-ENDLIST" in line:
                    break
            else:
                if line.startswith("http"):
                    self._ts_url_list.append(line)
                elif line.startswith("/"):
                    self._ts_url_list.append(self._front_url + line)
                else:
                    self._ts_url_list.append(self._url.rsplit("/", 1)[0] + "/" + line)
                new_m3u8_str += os.path.join(self._file_path, f"{next(ts)}.ts") + "\n"
        self._ts_sum = next(ts)
        with open(self._file_path + ".m3u8", "wb") as f:
            f.write(new_m3u8_str.encode("utf-8"))

    def download_key(self, key_line, num_retries):
        """
        下载key文件
        """
        mid_part = re.search(r"URI=[\'|\"].*?[\'|\"]", key_line).group()
        may_key_url = mid_part[5:-1]
        if self._key:
            with open(os.path.join(self._file_path, "key"), "wb") as f:
                f.write(self._key)
            return f'{key_line.split(mid_part)[0]}URI="./{self._name}/key"'
        if may_key_url.startswith("http"):
            true_key_url = may_key_url
        elif may_key_url.startswith("/"):
            true_key_url = self._front_url + may_key_url
        else:
            true_key_url = self._url.rsplit("/", 1)[0] + "/" + may_key_url
        try:
            with utils.direct_get(
                true_key_url, timeout=(5, 30), verify=False, headers=self._headers
            ) as res:
                with open(os.path.join(self._file_path, "key"), "wb") as f:
                    f.write(res.content)
            return f'{key_line.split(mid_part)[0]}URI="./{self._name}/key"{key_line.split(mid_part)[-1]}'
        except Exception as e:
            print(e)
            if os.path.exists(os.path.join(self._file_path, "key")):
                os.remove(os.path.join(self._file_path, "key"))
            print("加密视频,无法加载key,解密失败")
            if num_retries > 0:
                self.download_key(key_line, num_retries - 1)

    def output_mp4(self) -> None:
        """
        合并.ts文件，输出mp4格式视频，需要ffmpeg
        """
        from app_paths import ffmpeg_path
        ffmpeg_cmd = ffmpeg_path()

        cmd = [
            ffmpeg_cmd,
            "-i", f"{self._file_path}.m3u8",
            "-acodec", "copy",
            "-vcodec", "copy",
            "-f", "mp4",
            f"{self._file_path}.mp4",
            "-y",  # 覆盖已存在文件
            "-loglevel", "error",  # 减少输出
        ]

        # 隐藏 FFmpeg 窗口 (Windows)
        startupinfo = None
        if sys.platform == "win32":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = subprocess.SW_HIDE

        subprocess.run(cmd, check=True, startupinfo=startupinfo,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def delete_file(self):
        file = os.listdir(self._file_path)
        for item in file:
            os.remove(os.path.join(self._file_path, item))
        os.removedirs(self._file_path)
        os.remove(self._file_path + ".m3u8")
