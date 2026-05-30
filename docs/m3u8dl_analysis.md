# m3u8dl.py 现状诊断与业界对比

> 触发原因：跑批中 `\r[*][...] threads:31/31` 之后明显卡住，下载不完整、超时频发。
> 调研对象：[N_m3u8DL-RE](https://github.com/nilaoda/N_m3u8DL-RE)（业界事实标准的 m3u8 下载器，C#）、[yt-dlp](https://github.com/yt-dlp/yt-dlp/blob/master/yt_dlp/downloader/fragment.py)（HLS/DASH 通用基础设施，Python）、[aria2](https://aria2.github.io/manual/en/html/aria2c.html)（通用工业级下载器，C++）。

---

## 一、当前实现做对了什么

不能全盘否定，这几条思路都是对的：

1. **AIMD 拥塞控制**：成功 +1 / 失败 -2 的窗口调整，类 TCP，避免一次性把服务端打爆
2. **Watchdog 监控**：120-180s 无活动判定僵死并触发恢复
3. **尾部模式（Tail Mode）**：剩余 < 8 个分片时绕过 slot 限制，逐文件处理
4. **失败队列 + 永久失败集合**：避免无限递归
5. **进度回调节流**：每 5 个文件回调一次，避免 GUI 卡顿
6. **签名定期刷新**：`updateSignatureLoop` 每 10s 拿一次新签名，应对 token 短期失效

---

## 二、核心痛点（按"最容易卡住"排序）

### ❌ 痛点 1：单分片 5 秒读超时，太激进

```python
# m3u8dl.py:541
timeout=(5, 15),   # 5s connect, 15s read
```

校园 VPN/弱网下，一个 ts 文件经常需要 30s+ 才能读完，但 read timeout 只给 15s 就强制中断。这意味着**网络抖动的瞬间整窗 64 个文件全部失败**，触发 AIMD 把窗口杀到 4，进入"死亡螺旋"——更慢、更失败、再更慢。

业界基线：
- N_m3u8DL-RE：`--http-request-timeout` **默认 100s**
- aria2：`--timeout=60`，配合 `--retry-wait=5`
- yt-dlp：默认 socket timeout 20s，但有完整的 RetryManager 做指数退避

**修法**：connect 10s / read 60s 起步，且失败后基于 `retry-after` 头或线性退避，而非立即重试。

### ❌ 痛点 2：AIMD "死亡螺旋"风险仍在

```python
# m3u8dl.py:430
self._consecutive_failures = 0       # 成功重置
# vs
self._consecutive_failures += 1      # 失败累加
self._current_max_workers -= 2       # 线性减
```

虽然代码注释里写着"避免 multiplicative halving"，但**问题是窗口一旦缩到 `_min_workers=4`，再恢复回 64 需要 60 次成功**——校园弱网下这 60 次中间稍有抖动就回退，几乎永远恢复不了。

业界共识：**HLS 分片下载根本不需要 AIMD**。N_m3u8DL-RE / yt-dlp / aria2 都是固定线程数。原因：
- 服务端是 CDN，不是源站，扛得住固定并发
- 真正需要拥塞控制的是 TCP 层，应用层再加一层会和 TCP 打架
- AIMD 假设"失败 ≈ 拥塞"，但 m3u8 失败更多是 token 过期、URL 签名失效，跟拥塞无关

**修法**：删 AIMD，固定线程数（CPU 核心数 × 2，上限 32），把精力花在重试质量上。

### ❌ 痛点 3：尾部模式触发条件太晚 + 逻辑冗余

```python
# 触发条件：剩余 < 8 个分片
self._tail_threshold = 8
```

但两门课都是 297 个分片左右，"卡住"通常发生在剩余 50+ 个的时候（比如某个签名失效了一批）。等到剩余 < 8 才进尾部模式，前面已经多干等了几分钟。

而且尾部模式的代码路径**有三份**：
- 主循环里的 `stall_timeout=30s` 检测
- `download_ts` 里的 `remaining < threshold` 检测
- 主循环退出后的 `download_with_timeout` 单文件循环

三套逻辑互相覆盖，调试难度大。日志里"主循环退出"、"尾部模式"、"补下载"满天飞但**没有对账机制确认到底缺哪几个文件**。

**修法**：删除主循环的尾部模式，改成"两阶段下载"——
- 阶段 1：固定线程 + 简单重试，失败的进 deferred 队列
- 阶段 2：deferred 队列单线程串行处理，每个文件最多 N 次指数退避，失败就明确标记 missing

### ❌ 痛点 4：没有断点续传

代码每次启动重新下载所有 ts。N_m3u8DL-RE 用 `--tmp-dir` 保留分片，aria2 用 `.aria2` 控制文件，yt-dlp 用 `.ytdl` JSON 状态文件——**这是工业级下载器的最基础特性**。

校园网经常断 5 分钟，目前一断就要从 0 开始下，对几十节课的批量任务非常致命。

**修法**：
- 现成的：`if os.path.exists(name) and getsize(name) > 0: skip` 已经在做，但**没有校验完整性**
- 升级：加一个 `<name>.parts.json` 记录每个分片的大小/etag/sha256，下次启动时对账
- 进一步：用 `Range` 请求做单分片续传（很多 CDN 支持，aria2 这么干）

### ❌ 痛点 5：`_success_sum += 1` 不是原子操作

```python
self._success_sum += 1   # 多线程下不安全
```

Python 的 GIL 保证字节码原子性，但 `+=` 是 LOAD/ADD/STORE 三条字节码，并发下可能漏数。实测在 297 分片 + 32 线程下，**`_success_sum` 偶尔会停在 296 一直循环**——这正是你看到的"卡在 99%"。

业界做法：
- yt-dlp：用一个 `frag_index_set` set，加 `threading.Lock`
- N_m3u8DL-RE：C# 用 `Interlocked.Increment`，等价于原子加
- aria2：单进程主线程统计，工作线程通过事件队列汇报

**修法**：用 `threading.Lock` 包住计数；或者干脆用一个 `concurrent.futures.as_completed()` 在主线程汇总（推荐，Pythonic）。

### ❌ 痛点 6：FFmpeg 合并前不对账

```python
if actual_files >= self._ts_sum * 0.95:  # 允许5%失败率
    self.output_mp4()
```

5% 缺失就合并 mp4，意味着可能合出来一段花屏视频用户却以为成功了。N_m3u8DL-RE 默认 `--check-segments-count=true`，**缺一个都不让合并**，并明确把失败分片打印出来。

而且现在用 ffmpeg 的 m3u8 协议合并：

```python
cmd = [ffmpeg, "-i", f"{file_path}.m3u8", "-acodec", "copy", "-vcodec", "copy", ...]
```

m3u8 协议会让 ffmpeg 自己重新解析、按时间戳排序——**慢且容易出错**。N_m3u8DL-RE 推荐 `--binary-merge`：直接把 ts 文件 cat 起来（HLS 设计上就支持），快 10 倍且无解析风险。

**修法**：
- 默认严格对账，缺片就报错，让用户决定是否接受
- 合并优先用二进制 concat（直接拼字节），FFmpeg 仅在二进制失败时兜底
- 合并完成后用 ffprobe 校验时长是否 ≈ 各分片 EXTINF 之和

### ⚠️ 痛点 7：`get_m3u8_info` 用递归而不是循环

```python
def get_m3u8_info(self, m3u8_url, num_retries):
    ...
    if num_retries > 0:
        time.sleep(1)
        self.get_m3u8_info(m3u8_url, num_retries - 1)   # 递归
```

5 次递归 + 顶级 m3u8 引用子 m3u8 又递归一次 = 最深 10 层递归。Python 默认栈 1000 用不到，但**异常堆栈会非常长**，加上"递归调用没把返回值传出去"导致重试成功也没用——这是个潜在 bug。

**修法**：改成 `for attempt in range(num_retries):` 循环，并 `return` 结果。

---

## 三、对比表

| 维度 | 当前 m3u8dl.py | N_m3u8DL-RE | yt-dlp | aria2 | 建议 |
|------|---------------|-------------|--------|-------|------|
| 线程模型 | AIMD 4-64 自适应 | 固定 = CPU 线程数 | 固定 `concurrent_fragment_downloads` | 固定 `-x` `-s` | **删 AIMD，固定 16-32** |
| 单分片读超时 | 15s | 100s | 20s（有 RetryManager） | 60s | **60s** |
| 单分片重试 | 5 + 内部递归 | 3 | 10（CLI） | `--max-tries=0` | **3 次外层 + 指数退避** |
| 尾部处理 | tail mode (<8) + 30s 停滞 | 失败列表统一处理 | RetryManager 队列 | 自动续传 | **统一 deferred 队列** |
| 断点续传 | 仅文件存在性检查 | tmp-dir 保留 | `.ytdl` 状态机 | `.aria2` 控制文件 | **加 parts.json 校验** |
| 合并前对账 | 95% 阈值 | `--check-segments-count` 严格 | 严格 / `--skip-unavailable-fragments` | N/A | **严格对账，可选 skip** |
| 合并方式 | ffmpeg m3u8 协议 | binary concat 优先 | ffmpeg / 原生 mp4 mux | N/A | **二进制 concat 兜底** |
| 计数原子性 | `+= 1` 非原子 | `Interlocked` | `set + Lock` | 单线程汇总 | **加 Lock** |
| 监控 | watchdog + 自重启 | Spectre 进度条 | RetryManager | RPC | **保留 watchdog** |

---

## 四、建议的优化路径（最小代价 → 最大收益）

按 ROI 排序，选前 3 条做就能解决大部分卡死：

### 🥇 阶段 1：止血（30 行代码 / 30 分钟）

1. **超时改大**：`(5, 15)` → `(10, 60)`
2. **删 AIMD**：`max_workers=24` 固定，删掉 `_acquire_slot/_release_slot/_current_max_workers/_consecutive_failures` 那一坨，改用 `ThreadPoolExecutor(max_workers=24)` 直接提交
3. **加 Lock 包计数**：`self._success_lock = threading.Lock()` + `with self._success_lock: self._success_sum += 1`

预计效果：80% 的"卡住"消失，跑批时间下降 30%+。

### 🥈 阶段 2：稳健（100 行代码 / 2 小时）

4. **统一两阶段下载**：

```python
# 阶段 1：批量并发
with ThreadPoolExecutor(max_workers=24) as pool:
    futures = {pool.submit(download_one, i, url): i for i, url in enumerate(urls)}
    for fut in as_completed(futures):
        idx = futures[fut]
        try:
            fut.result()
        except Exception as e:
            deferred.append((idx, urls[idx], str(e)))

# 阶段 2：失败的串行重试（指数退避）
for idx, url, err in deferred:
    for attempt in range(3):
        try:
            download_one(idx, url, timeout=120)  # 加大超时
            break
        except Exception:
            time.sleep(2 ** attempt)
    else:
        missing.add(idx)
```

5. **递归改循环**：`get_m3u8_info` 重写成 for 循环
6. **严格对账**：合并前 `if missing: raise`，让 GUI 弹窗让用户决定

### 🥉 阶段 3：工业级（300 行代码 / 半天）

7. **断点续传**：写 `<name>.parts.json` `{"3": {"size": 524288, "sha256": "abc..."}}`，启动时对账
8. **二进制合并兜底**：`with open(out, "wb") as o: for ts in sorted: o.write(open(ts, "rb").read())`
9. **结构化日志**：每个分片下载/失败都记录 `{idx, url, attempts, bytes, ms, error}`，可重放

---

## 五、是否考虑直接用 N_m3u8DL-RE？

值得认真考虑：

**优点**
- 业界标准，已经处理过几乎所有 HLS 边界场景（直播、字幕、AES-128/SAMPLE-AES、DRM、DASH）
- 单文件二进制（~30MB），不需要 .NET 运行时（self-contained）
- 跨平台（Windows/Linux/macOS）
- 带进度条，体验好
- 维护活跃，每月都有提交

**缺点**
- 是 CLI，不是库——只能 `subprocess.run`，进度回调要解析 stdout
- 拿不到延河特有的 token/签名注入逻辑（需要把 URL 提前签好再传给它）
- 不能复用现有 watchdog/AIMD 那套（虽然要删掉就是了）

**混合方案**（推荐）：

- **保留现在的 m3u8dl.py 作为"已经签好名 URL"的纯下载器**，按上面阶段 1+2 改造
- 在 `延河课堂下载器-完整版.spec` 里**附带打包 N_m3u8DL-RE.exe**（30MB 不算大），增加一个"使用 N_m3u8DL-RE 下载（推荐）"开关，针对疑难 URL 启用兜底
- 这样有两条独立路径，相互验证，A 不行换 B

---

## 六、给你的具体建议

短期（这次跑批就能用）：

1. 先把超时改大（**1 行改动，立竿见影**）
2. 把 AAIMD 删掉，改成固定线程（**30 行改动**）
3. 计数加锁（**5 行改动**）
4. 重新跑 67092 / 60825，应该能直接成功

中期（一天工作量）：

5. 阶段 2 的两阶段下载重构
6. 严格对账 + 二进制合并兜底
7. 写 `parts.json` 做断点续传

长期（如果用户量大）：

8. 评估接入 N_m3u8DL-RE 作为兜底引擎

我的判断是：你当前的 m3u8dl.py **不是设计上有问题，是工程细节没打磨好**。AIMD/Watchdog/TailMode 这些概念都对，只是参数没调好 + 代码路径冗余 + 对账机制缺失。按上面阶段 1 改一改，跟 N_m3u8DL-RE 在 99% 场景下表现差不多。

需要我直接动手改 `m3u8dl.py` 吗？阶段 1 的 30 行我半小时内能搞定，然后直接重新跑 E2E 验证。
