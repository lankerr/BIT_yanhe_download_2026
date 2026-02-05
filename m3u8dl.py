import base64
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


class ThreadPoolExecutorWithQueueSizeLimit(ThreadPoolExecutor):
    """
    实现多线程有界队列
    队列数为线程数的2倍
    """

    def __init__(self, max_workers=None, *args, **kwargs):
        super().__init__(max_workers, *args, **kwargs)
        self._work_queue = queue.Queue(max_workers * 2)


def make_sum():
    ts_num = 0
    while True:
        yield ts_num
        ts_num += 1


def dummy_func(downloaded, total, merge_status, active_threads=0, max_threads=0):
    return


class M3u8Download:
    """
    :param url: 完整的m3u8文件链接 如"https://www.bilibili.com/example/index.m3u8"
    :param name: 保存m3u8的文件名 如"index"
    :param max_workers: 多线程最大线程数
    :param num_retries: 重试次数
    :param base64_key: base64编码的字符串
    """

    def __init__(
        self,
        url,
        workDir,
        name,
        max_workers=64,
        num_retries=5,  # 降低到5次，避免无限重试
        base64_key=None,
        progress_callback=dummy_func,
        gui_mode=False,
    ):
        self._url = url
        self._token = None
        self._workDir = workDir
        self._name = name
        self._max_workers = min(max_workers, 64) # Cap at 64 to avoid overhead
        self._current_max_workers = 16 # Slow Start
        self._num_retries = num_retries
        self._progress_callback = progress_callback
        
        # 进度回调节流：每N个文件更新一次
        self._last_progress_count = 0
        self._progress_step = 5  # 每5个文件更新一次
        
        # Adaptive Watchdog: 增加超时时间适应网络波动
        self._watchdog_timeout = 180  # 增加到180秒(3分钟)
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
        self._ts_locks = set()  # 锁定正在下载的ts文件
        self._ts_lock_mutex = threading.Lock()  # 保护_ts_locks的互斥锁
        
        # 工业级优化：失败队列机制
        self._failed_queue = queue.Queue()  # 失败的任务队列
        self._permanently_failed = set()  # 永久失败的文件（超过最大重试）
        
        # 尾部模式：当剩余文件<8时自动开启，绕过slot限制
        self._tail_mode = False
        self._tail_threshold = 8  # 剩余少于8个文件时进入尾部模式
        self._max_single_retries = 5  # 单个文件最大重试次数
        
        self._watchdog_thread = threading.Thread(target=self.watchdog, daemon=True)
        self._watchdog_thread.start()
        
        # Adaptive threading control
        # self._current_max_workers initialized in __init__
        self._active_threads = 0
        self._thread_cond = threading.Condition()
        self._min_workers = 1
        self._headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/93.0.4577.82 Safari/537.36 Edg/93.0.961.52",
            "Origin": "https://www.yanhekt.cn",
            "referer": "https://www.yanhekt.cn/",
        }
        self.timestamp, self.signature = utils.getSignature()
        urllib3.disable_warnings()


        self._url = utils.encryptURL(self._url)
        self._last_error = ""  # 存储最后一次错误信息
        
        # 通知 GUI 正在获取视频信息 (status=-2 表示正在初始化)
        self._progress_callback(0, 0, -2, 0, 0)
        print(f"[M3u8Download] 开始获取 m3u8 信息: {self._name}")
        print(f"[M3u8Download] 原始 URL: {self._url[:100]}...")

        self.get_m3u8_info(self._url, self._num_retries)
        
        # 检查是否成功获取到 ts 列表
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
                # 不在主线程中，忽略
                pass
        
        # ========== 第一轮下载（带超时控制）==========
        print(f"Downloading: {self._name}", f"Save path: {self._file_path}", sep="\n")
        
        # 提交所有任务到线程池
        pool = ThreadPoolExecutorWithQueueSizeLimit(self._max_workers * 2)
        pool.submit(self.updateSignatureLoop)
        for k, ts_url in enumerate(self._ts_url_list):
            pool.submit(
                self.download_ts,
                ts_url,
                os.path.join(self._file_path, f"{k}.ts"),
                self._num_retries,
            )
        
        # 不等待线程池完成！改用轮询检测进度
        stall_timeout = 30  # 30秒无新进度就认为停滞
        last_progress = 0
        last_progress_time = time.time()
        
        while self._success_sum < self._ts_sum:
            time.sleep(2)  # 每2秒检查一次
            
            current_progress = self._success_sum
            remaining = self._ts_sum - current_progress
            
            if current_progress > last_progress:
                # 有进度，更新
                last_progress = current_progress
                last_progress_time = time.time()
            else:
                # 无进度，检查是否超时
                stall_duration = time.time() - last_progress_time
                
                # 如果剩余文件少且停滞超时，进入尾部模式
                if remaining > 0 and remaining < 20 and stall_duration > stall_timeout:
                    print(f"\n[停滞检测] {stall_duration:.0f}秒无进度，剩余{remaining}个文件，强制进入尾部模式")
                    self._tail_mode = True
                    # 释放所有等待的线程
                    with self._thread_cond:
                        self._current_max_workers = 9999
                        self._thread_cond.notify_all()
                    break
            
            # 检测尾部模式触发
            if remaining > 0 and remaining < self._tail_threshold and not self._tail_mode:
                self._tail_mode = True
                print(f"\n[尾部模式] 剩余{remaining}个文件，开启尾部模式")
                with self._thread_cond:
                    self._current_max_workers = 9999
                    self._thread_cond.notify_all()
        
        # 关闭线程池，不等待
        pool.shutdown(wait=False)
        print(f"[主下载结束] 成功: {self._success_sum}/{self._ts_sum}")
        
        # 等待2秒，让后台线程完成正在进行的下载
        time.sleep(2)
        
        # ========== 尾部补下载：带卡死检测和自动重启 ==========
        def get_missing_files():
            """获取所有缺失的ts文件"""
            missing = []
            for k, ts_url in enumerate(self._ts_url_list):
                ts_path = os.path.join(self._file_path, f"{k}.ts")
                if not os.path.exists(ts_path) or os.path.getsize(ts_path) == 0:
                    missing.append((ts_url, ts_path))
            return missing
        
        def download_with_timeout(ts_url, name, timeout=30):
            """带超时的单文件下载，返回是否成功"""
            import concurrent.futures
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(self.download_ts_simple, ts_url, name)
                try:
                    future.result(timeout=timeout)
                    # 检查文件是否真的下载成功
                    return os.path.exists(name) and os.path.getsize(name) > 0
                except concurrent.futures.TimeoutError:
                    print(f"[尾部超时] {os.path.basename(name)} 超过{timeout}秒，取消并重试")
                    future.cancel()
                    return False
                except Exception as e:
                    print(f"[尾部异常] {os.path.basename(name)}: {e}")
                    return False
        
        # 尾部模式参数
        tail_timeout = 30  # 单个文件超时30秒
        max_restart_per_file = 3  # 每个文件最多重启3次
        file_restart_count = {}  # 记录每个文件的重启次数
        
        missing_files = get_missing_files()
        if missing_files:
            print(f"\n[尾部模式] 开始处理 {len(missing_files)} 个缺失文件")
            self._tail_mode = True
            
        while missing_files and not self._watchdog_triggered:
            # 对每个缺失文件单独处理，带超时控制
            still_missing = []
            
            for ts_url, name in missing_files:
                if self._watchdog_triggered:
                    break
                    
                basename = os.path.basename(name)
                restart_count = file_restart_count.get(name, 0)
                
                if restart_count >= max_restart_per_file:
                    print(f"[尾部放弃] {basename} 已重试{restart_count}次，标记为失败")
                    self._permanently_failed.add(name)
                    continue
                
                print(f"[尾部下载] {basename} (尝试 {restart_count + 1}/{max_restart_per_file})")
                
                # 删除可能存在的损坏文件
                if os.path.exists(name):
                    try:
                        os.remove(name)
                    except:
                        pass
                
                # 带超时下载
                success = download_with_timeout(ts_url, name, tail_timeout)
                
                if success:
                    # 注意：download_ts_simple 内部已经做了 success_sum += 1
                    self._last_activity_time = time.time()
                    print(f"[尾部成功] {basename} ✓ ({self._success_sum}/{self._ts_sum})")
                    # 更新进度
                    self._progress_callback(self._success_sum, self._ts_sum, 0, 1, 1)
                else:
                    # 下载失败或超时，增加重启计数
                    file_restart_count[name] = restart_count + 1
                    still_missing.append((ts_url, name))
                    print(f"[尾部失败] {basename} 将在下一轮重试")
            
            # 更新缺失列表
            missing_files = still_missing
            
            if missing_files:
                print(f"[尾部状态] 还剩 {len(missing_files)} 个文件，1秒后继续...")
                time.sleep(1)
        
        # ========== 最终状态检查 ==========
        # 检查实际下载的文件数
        actual_files = len([f for f in os.listdir(self._file_path) if f.endswith('.ts')])
        print(f"\n[下载统计] 成功: {self._success_sum}/{self._ts_sum}, 实际文件: {actual_files}")
        
        if actual_files >= self._ts_sum * 0.95:  # 允许5%的失败率
            self._progress_callback(self._success_sum, self._ts_sum, 1)
            self.output_mp4()
            self.delete_file()
            print(f"Download successfully --> {self._name}")
            debug_log_path = os.path.join(utils.get_app_path(), "debug.log")
            with open(debug_log_path, "a", encoding="utf-8") as f:
                f.write(f"[{time.strftime('%H:%M:%S')}] {self._name} COMPLETED. Files: {actual_files}/{self._ts_sum}\n")
            self._progress_callback(self._success_sum, self._ts_sum, 2)
        else:
            failed_list = list(self._permanently_failed)[:10]  # 只显示前10个
            print(f"Download failed for {self._name}. Success: {actual_files}/{self._ts_sum}")
            print(f"Failed files (first 10): {failed_list}")
            raise Exception(f"Download incomplete: {actual_files}/{self._ts_sum}")
        self._downloading = False

    def watchdog(self):
        while self._downloading:
            time.sleep(5)
            # Use the adaptive timeout value
            if time.time() - self._last_activity_time > self._watchdog_timeout:
                print(f"\n[Watchdog] Download hung for {self._watchdog_timeout}s!")
                self._watchdog_triggered = True
                # 不再强制退出，设置标记让主逻辑处理
                self._downloading = False
                return

    def updateSignatureLoop(self):
        while self._success_sum != self._ts_sum:
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
            with requests.get(
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
                content_preview = res.text[:200] if res.text else "(空响应)"
                print(f"[M3u8Download] m3u8 内容预览: {content_preview}")
                
                if "EXT-X-STREAM-INF" in res.text:  # 判定为顶级M3U8文件
                    print("[M3u8Download] 检测到顶级 m3u8，正在解析子文件...")
                    for line in res.text.split("\n"):
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
                    m3u8_text_str = res.text
                    self.get_ts_url(m3u8_text_str)
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

    def _acquire_slot(self):
        # 尾部模式：不限制slot，直接通过
        if self._tail_mode:
            with self._thread_cond:
                self._active_threads += 1
            return
            
        with self._thread_cond:
            while self._active_threads >= self._current_max_workers:
                self._thread_cond.wait()
            self._active_threads += 1

    def _release_slot(self, success=True):
        with self._thread_cond:
            self._active_threads -= 1
            
            # 尾部模式：不调整窗口大小
            if self._tail_mode:
                self._thread_cond.notify_all()
                return
                
            if success:
                # Additive Increase
                if self._current_max_workers < self._max_workers:
                    self._current_max_workers += 1
            else:
                # Multiplicative Decrease
                self._current_max_workers = max(self._min_workers, self._current_max_workers // 2)
            self._thread_cond.notify_all()

    def download_ts(self, ts_url_original: str, name: str, num_retries: int) -> None:
        """
        下载 .ts 文件
        """
        # 检查 watchdog 是否已触发，如果是则快速退出
        if self._watchdog_triggered:
            return
            
        # Acquire slot before starting work
        self._acquire_slot()
        try:
            if not self._token:
                self._token = utils.getToken()
            token = self._token
            ts_url = utils.add_signature_for_url(
                ts_url_original.split("\n")[0], token, self.timestamp, self.signature
            )
            
            # 检查是否已经有其他线程在下载此文件
            with self._ts_lock_mutex:
                if name in self._ts_locks:
                    # 其他线程正在下载，释放 slot 并跳过
                    self._release_slot(success=True)
                    return
                self._ts_locks.add(name)
            
            if not os.path.exists(name):
                try:
                    with requests.get(
                        ts_url,
                        stream=True,
                        timeout=(5, 60),
                        verify=False,
                        headers=self._headers,
                    ) as res:
                        if res.status_code == 200:
                            # 确保目录存在
                            dir_path = os.path.dirname(name)
                            if not os.path.exists(dir_path):
                                os.makedirs(dir_path, exist_ok=True)
                            
                            with open(name, "wb") as ts:
                                total_bytes = 0
                                for chunk in res.iter_content(chunk_size=8192):
                                    if chunk:
                                        ts.write(chunk)
                                        total_bytes += len(chunk)
                            
                            file_size = os.path.getsize(name)
                            # 放宽文件大小验证：只有完全空的文件才是问题
                            if file_size == 0:
                                raise Exception(f"Downloaded file is empty")
                            
                            # 调试输出
                            if total_bytes != file_size:
                                print(f"[WARN] {os.path.basename(name)}: wrote {total_bytes} but file is {file_size}")

                            self._last_activity_time = time.time()
                            self._success_sum += 1
                            
                            # 检测尾部模式：当剩余文件 < threshold 时开启
                            remaining = self._ts_sum - self._success_sum
                            if not self._tail_mode and remaining > 0 and remaining < self._tail_threshold:
                                self._tail_mode = True
                                print(f"\n[尾部模式] 剩余{remaining}个文件，开启尾部模式，绕过slot限制")
                                # 重置窗口大小，让等待的线程可以继续
                                with self._thread_cond:
                                    self._current_max_workers = self._max_workers
                                    self._thread_cond.notify_all()
                            
                            sys.stdout.write(
                                "\r[%-25s](%d/%d) threads:%d/%d%s"
                                % (
                                    "*" * (100 * self._success_sum // self._ts_sum // 4),
                                    self._success_sum,
                                    self._ts_sum,
                                    self._active_threads,
                                    self._current_max_workers,
                                    " [TAIL]" if self._tail_mode else ""
                                )
                            )
                            sys.stdout.flush()
                        else:
                            # Not a success 200, reduce threads
                            raise Exception(f"Status {res.status_code}")
                except Exception as e:
                     # Handled in outer except
                     raise e
            else:
                self._success_sum += 1

            # 按文件数量节流进度回调
            if self._success_sum >= self._last_progress_count + self._progress_step or self._success_sum == self._ts_sum:
                self._last_progress_count = self._success_sum
                self._progress_callback(self._success_sum, self._ts_sum, 0, self._active_threads, self._current_max_workers)
            
            # 成功情况下释放 slot (只在这里释放一次)
            self._release_slot(success=True)
            
        except Exception as e:
            # Failure case - 直接放入失败队列，不阻塞线程
            self._release_slot(success=False)
            if os.path.exists(name):
                try:
                    os.remove(name)
                except:
                    pass
            
            # ❌ 不再递归重试！直接放入失败队列，让线程立即释放
            # 这样其他任务可以使用这个线程
            self._failed_queue.put((ts_url_original, name))
            return

        finally:
            # 释放锁
            with self._ts_lock_mutex:
                self._ts_locks.discard(name)
    
    def download_ts_simple(self, ts_url_original: str, name: str) -> None:
        """
        简化版下载，用于尾部补下载，不使用 slot 限制
        """
        # 检查 watchdog 是否已触发
        if self._watchdog_triggered:
            return
            
        if os.path.exists(name) and os.path.getsize(name) > 0:
            # 文件已存在且非空，跳过
            return
        
        # 删除可能存在的空文件
        if os.path.exists(name):
            try:
                os.remove(name)
            except:
                pass
        
        max_attempts = 3  # 内部重试3次
        for attempt in range(max_attempts):
            if self._watchdog_triggered:
                return
                
            try:
                if not self._token:
                    self._token = utils.getToken()
                ts_url = utils.add_signature_for_url(
                    ts_url_original.split("\n")[0], self._token, self.timestamp, self.signature
                )
                
                with requests.get(
                    ts_url,
                    stream=True,
                    timeout=(10, 90),  # 连接10秒，读取90秒
                    verify=False,
                    headers=self._headers,
                ) as res:
                    if res.status_code == 200:
                        dir_path = os.path.dirname(name)
                        if not os.path.exists(dir_path):
                            os.makedirs(dir_path, exist_ok=True)
                        
                        with open(name, "wb") as ts:
                            for chunk in res.iter_content(chunk_size=8192):
                                if chunk:
                                    ts.write(chunk)
                        
                        if os.path.getsize(name) > 0:
                            self._success_sum += 1
                            self._last_activity_time = time.time()
                            print(f"[补下载成功] {os.path.basename(name)} (尝试{attempt+1})")
                            return  # 成功，退出
                        else:
                            raise Exception("Empty file")
                    else:
                        raise Exception(f"Status {res.status_code}")
                        
            except Exception as e:
                if os.path.exists(name):
                    try:
                        os.remove(name)
                    except:
                        pass
                        
                if attempt < max_attempts - 1:
                    wait = 0.5 * (attempt + 1)  # 0.5秒, 1秒, 1.5秒
                    time.sleep(wait)
                else:
                    print(f"[补下载失败] {os.path.basename(name)}: {e} (已尝试{max_attempts}次)")
                    self._permanently_failed.add(name)

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
            with requests.get(
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
        # Check for local ffmpeg
        ffmpeg_cmd = "ffmpeg"
        if os.path.exists("ffmpeg.exe"):
            ffmpeg_cmd = os.path.abspath("ffmpeg.exe")
            
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
