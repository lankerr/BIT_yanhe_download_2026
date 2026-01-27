import base64
import os
import queue
import re
import signal
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from subprocess import run

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
        max_workers=128,
        num_retries=99,
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
        
        # Adaptive Watchdog: 增加超时时间适应网络波动
        self._watchdog_timeout = 120  # 增加到120秒
        self._gui_mode = gui_mode
        self._watchdog_triggered = False
        
        if not os.path.exists(os.path.join(os.getcwd(), self._workDir)):
            os.makedirs(os.path.join(os.getcwd(), self._workDir))
        self._file_path = os.path.join(os.getcwd(), self._workDir, self._name)
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

        self.get_m3u8_info(self._url, self._num_retries)

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
        
        print(f"Downloading: {self._name}", f"Save path: {self._file_path}", sep="\n")
        
        # Use a larger pool size to allow queuing, but control execution with condition variable
        with ThreadPoolExecutorWithQueueSizeLimit(self._max_workers * 2) as pool:
            pool.submit(self.updateSignatureLoop)
            for k, ts_url in enumerate(self._ts_url_list):
                pool.submit(
                    self.download_ts,
                    ts_url,
                    # The `.ts` extension is mandatory for FFmpeg 7.1.1+.
                    # https://git.ffmpeg.org/gitweb/ffmpeg.git/commit/b753bac08f6881b2d3dea8f1ab84c81550f35897
                    # https://git.ffmpeg.org/gitweb/ffmpeg.git/commit/6c4e56f07d1a703435854f2156c881885f7798da
                    os.path.join(self._file_path, f"{k}.ts"),
                    self._num_retries,
                )
        if self._success_sum == self._ts_sum:
            self._progress_callback(self._success_sum, self._ts_sum, 1)
            self.output_mp4()
            self.delete_file()
            print(f"Download successfully --> {self._name}")
            with open("debug.log", "a", encoding="utf-8") as f:
                f.write(f"[{time.strftime('%H:%M:%S')}] {self._name} COMPLETED. Final Threads: {self._current_max_workers}\n")
            self._progress_callback(self._success_sum, self._ts_sum, 2)
        else:
            print(f"Download failed or incomplete for {self._name}. Success: {self._success_sum}, Total: {self._ts_sum}")
            raise Exception(f"Download incomplete: {self._success_sum}/{self._ts_sum}")
        self._downloading = False

    def watchdog(self):
        while self._downloading:
            time.sleep(5)
            # Use the adaptive timeout value
            if time.time() - self._last_activity_time > self._watchdog_timeout:
                print(f"\n[Watchdog] Download hung for {self._watchdog_timeout}s!")
                self._watchdog_triggered = True
                if self._gui_mode:
                    # GUI模式下不强制退出，只是设置标记
                    print("[Watchdog] GUI mode - marking download as failed")
                    self._downloading = False
                    return
                else:
                    print("Exiting with code 100 to restart...")
                    os._exit(100)

    def updateSignatureLoop(self):
        while self._success_sum != self._ts_sum:
            self.timestamp, self.signature = utils.getSignature()
            time.sleep(10)

    def get_m3u8_info(self, m3u8_url: str, num_retries: int) -> None:
        """
        获取m3u8信息
        """

        if not self._token:
            self._token = utils.getToken()
        token = self._token
        url = utils.add_signature_for_url(
            m3u8_url, token, self.timestamp, self.signature
        )
        try:
            with requests.get(
                url, timeout=(3, 30), verify=False, headers=self._headers
            ) as res:
                if res.status_code != 200:
                    raise Exception(f"Failed to get m3u8 info: {res.status_code}")
                self._front_url = res.url.split(res.request.path_url)[0]
                if "EXT-X-STREAM-INF" in res.text:  # 判定为顶级M3U8文件
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
        except Exception as e:
            print(e)
            if num_retries > 0:
                self.get_m3u8_info(m3u8_url, num_retries - 1)

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
        with self._thread_cond:
            while self._active_threads >= self._current_max_workers:
                self._thread_cond.wait()
            self._active_threads += 1

    def _release_slot(self, success=True):
        with self._thread_cond:
            self._active_threads -= 1
            if success:
                # Additive Increase
                if self._current_max_workers < self._max_workers:
                    old = self._current_max_workers
                    self._current_max_workers += 1
                    if old != self._current_max_workers:
                        with open("debug.log", "a", encoding="utf-8") as f:
                            f.write(f"[{time.strftime('%H:%M:%S')}] INCREASE Threads: {old} -> {self._current_max_workers} (Total TS: {self._ts_sum})\n")
            else:
                # Multiplicative Decrease
                old = self._current_max_workers
                self._current_max_workers = max(self._min_workers, self._current_max_workers // 2)
                if old != self._current_max_workers:
                     with open("debug.log", "a", encoding="utf-8") as f:
                        f.write(f"[{time.strftime('%H:%M:%S')}] DECREASE Threads: {old} -> {self._current_max_workers} (Failure detected)\n")
            self._thread_cond.notify_all()

    def download_ts(self, ts_url_original: str, name: str, num_retries: int) -> None:
        """
        下载 .ts 文件
        """
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
                    return  # 其他线程正在下载，跳过
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
                            with open(name, "wb") as ts:
                                for chunk in res.iter_content(chunk_size=1024):
                                    if chunk:
                                        ts.write(chunk)
                            
                            # Validate file size
                            if os.path.getsize(name) < 1024:
                                raise Exception(f"Downloaded file too small ({os.path.getsize(name)} bytes)")

                            self._last_activity_time = time.time()
                            self._success_sum += 1
                            sys.stdout.write(
                                "\r[%-25s](%d/%d) threads:%d/%d"
                                % (
                                    "*" * (100 * self._success_sum // self._ts_sum // 4),
                                    self._success_sum,
                                    self._ts_sum,
                                    self._active_threads,
                                    self._current_max_workers
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

            self._progress_callback(self._success_sum, self._ts_sum, 0, self._active_threads, self._current_max_workers)
            
        except Exception:
            # Failure case
            self._release_slot(success=False)
            if os.path.exists(name):
                os.remove(name)
            if num_retries > 0:
                # Recurse. Note: Recursion will call download_ts which acquires a NEW slot.
                # This corresponds to "retrying later"
                # Exponential backoff: wait before retrying
                # attempt = self._num_retries - num_retries
                # Wait: 2^attempt, capped at 30s
                # User request: "Linearly increase exponential backoff" - mixing logic slightly or just standard exp.
                # "每一个加一个" -> implies checking per item.
                attempt = self._num_retries - num_retries
                wait_time = min(30, 2 ** attempt)
                print(f"Retrying {name} in {wait_time}s (Left: {num_retries})...")
                time.sleep(wait_time)
                self.download_ts(ts_url_original, name, num_retries - 1)
            return # IMPORTANT: Return here to avoid double release

        finally:
            # 释放锁
            with self._ts_lock_mutex:
                self._ts_locks.discard(name)
        
        # Success case release (if we got here, we didn't recurse or return early)
        self._release_slot(success=True)

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
        ]
        
        # print("Executing FFmpeg:", " ".join(cmd))
        run(cmd, check=True)

    def delete_file(self):
        file = os.listdir(self._file_path)
        for item in file:
            os.remove(os.path.join(self._file_path, item))
        os.removedirs(self._file_path)
        os.remove(self._file_path + ".m3u8")
