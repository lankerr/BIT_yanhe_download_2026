# 下载速度优化 · 实验计划

> 原则：**实验为准，不迷信工业级**。
> 工业级方案是给"通用场景"做的最稳妥设计；我们只下延河课堂——可能在
> 这个特殊场景里，简单方案反而比工业级方案快。先全部跑一遍，最后用数据说话。

---

## 一、为什么不直接抄业界方案

业界方案（yt-dlp / N_m3u8DL-RE / aria2）针对**多 CDN、跨地域、多种鉴权、需要稳健**的通用场景。
我们只下**延河课堂**这一个目标，特点：

- CDN：阿里云北京节点，路径稳定
- 鉴权：Token + 签名（已搞清楚）
- 单分片：~1-2MB，10-20s 一段
- 单课节：~300 段
- 用户：BIT 校园网为主，少数 VPN 用户

也就是说**多数场景**都是同一种网络条件。**对这一种条件做的极致优化**，可能反而比通用工业级方案快。

---

## 二、当前实现的真实表现

### 已知数据点（之前真实跑批观察）

| 测试 | 结果 |
|------|------|
| 软件漏洞利用及渗透 第 1 节（294 段，~700 MB） | **708 MB / 跑通**，耗时未精确测量（估计 5-10 分钟） |
| Apple bipbop 4x3 公开测试流（30 段切片） | K=1: 54s, K=4: 47s, **K=16 偶发断崖**(失败 5+，吞吐降到 0.56 MB/s) |
| 一次失败的 K 扫描 | K=1→K=4 加速比仅 1.17x（远小于理论 4x） |

### 性能瓶颈推断

按数学模型 `T = N·S/(K·B_per) + N·p·T_retry_wait + T_merge`：

1. **B_per（单连接吞吐）远低于估计** —— 校园网 K=1 时 ~1 MB/s，不是工业级假设的 2-5 MB/s
2. **K\*（饱和点）很低** —— 校园网下 K=4-8 就饱和，K=24+ 不增益反而抖
3. **失败惩罚被放大** —— `_success_sum += 1` 非原子，偶尔 297/298 卡死浪费 30+ 秒

---

## 三、实验设计

### 3.1 候选方案列表（6 个变体）

每个方案都是"对当前 m3u8dl.py 的最小改动"，便于精准对比：

| ID | 名称 | 核心改动 | 预期效果 |
|----|------|---------|---------|
| `cur` | 当前版本 | 不动（基线） | — |
| `v1_aimd_off` | 删 AIMD | 固定 K=8 / 16 / 32（取最优）| 消除死亡螺旋 → +20-50% |
| `v2_timeout_loose` | 超时放宽 | (5,15) → (10,60) | 让 p 降到 1% → +5-15% |
| `v3_atomic_count` | 计数加锁 | `with lock: success_sum += 1` | 消除 297/298 卡死 → +5-10% |
| `v4_simple_pool` | 重写 | 删 AIMD/Watchdog/Tail，固定 K + 重试队列 | 简化 → 速度待测 |
| `v5_bin_concat` | 二进制合并 | 替换 ffmpeg 协议 → cat ts 文件 | 合并 +0-5% |
| `v_combo` | 全部合 | v1+v2+v3+v5 | 累加效果 |

### 3.2 测试矩阵

每个方案 × 多个并发数 × 3 次重复：

```
方案: cur, v1_aimd_off, v2_timeout_loose, v3_atomic_count, v4_simple_pool, v_combo
K:    4, 8, 16, 24, 32
重复: 3 trials
```

= 6 × 5 × 3 = **90 次试验**。每次 ~5 分钟 = **7.5 小时**。

太多。我要做**第一轮筛选**：

### 3.3 分阶段策略

**第一轮：筛掉无效方案（30 分钟）**

每个方案只跑 K=16，1 次：6 次 × 5 分钟 ≈ 30 分钟。
看哪些方案明显比基线慢/快。

**第二轮：精测 top-2 方案（1.5 小时）**

赢家方案 × K=4,8,16,24,32 × 3 trials = 30 次 × 3 分钟 ≈ 1.5 小时。

**第三轮：决胜（30 分钟）**

最优配置 × 3 trials × 3 个不同课程的不同节 = 9 次。

### 3.4 测试源

**问题**：之前用 Apple bipbop 测出来 K\*=4 太低，跟延河实际场景不符。
**对策**：直接用延河课程做基准。课程要满足：
- 当前有 sessions（已验证 40524 / 60825 / 67092 / 51303 等都有）
- 第 1 节体量 ~700 MB（典型）
- 单节够大让差异明显

候选课程：
- **40524 第 1 节** （已知 294 段、708 MB） ← 主力
- **60825 第 1 节** （3 节中第一节）
- **67092 第 1 节** （仅 1 节，时间最短）

为节省时间：**主力测 40524 第 1 节的前 60 段**（不下整节，避免每次 5 分钟）。

---

## 四、要做的工程改动

### 4.1 改造 m3u8dl，加变体开关

现在 `m3u8dl.py` 不支持参数化切换内部行为。我加一个新版 `m3u8dl_lab.py`，把 6 种行为都内置：

```python
class M3u8DownloadLab:
    def __init__(self, url, work_dir, name,
                 # 实验开关
                 use_aimd: bool = True,
                 fixed_workers: int = None,         # 删 AIMD 时固定 K
                 connect_timeout: int = 5,
                 read_timeout: int = 15,
                 use_atomic_count: bool = False,
                 use_binary_concat: bool = False,
                 max_segments: int = None,           # 限段数（实验用）
                 ):
        ...
```

### 4.2 实验跑批 harness

`scripts/perf_yhkt.py`：

```python
"""
延河场景的下载速度对照实验。

用法：
  # 阶段 1：6 方案快速筛
  python scripts/perf_yhkt.py --stage 1 --course 40524 --segments 60

  # 阶段 2：top-2 K 扫描
  python scripts/perf_yhkt.py --stage 2 --variants v1_aimd_off,v_combo

  # 阶段 3：决胜
  python scripts/perf_yhkt.py --stage 3 --variant v_combo --workers 8
"""
```

输出：`bench/yhkt_results/<timestamp>/results.json` + `report.md`。

每次试验记录：
- wall_clock_s
- 字节 / 段数 / 吞吐 MBps
- 失败分片数 + 重试次数
- AIMD 窗口轨迹（对比 v1）
- 进入 tail mode 的剩余段数（如有）
- ffmpeg 合并耗时

### 4.3 报告自动生成

跑完后输出 markdown 报告：

```markdown
| 方案 | 中位耗时 | 吞吐 | 加速比 vs cur |
| cur  | 287s | 2.4 MB/s | 1.00x |
| v1   | 198s | 3.5 MB/s | 1.45x |
| ...
```

附图（matplotlib 画进度曲线对比）：
- 每段下载完成时间分布
- AIMD 窗口随时间变化（v1 是直线、cur 在抖）

---

## 五、关于 Selenium 利用率（你的第二个问题）

你说"只用来拿 token 太大材小用"——**完全对**。

undetected_chromedriver 真正能干的事，按 ROI 排：

### 5.1 现在已经做的（用了 1/10）
- 弹 Chrome 让用户登录 → 拿 `localStorage.auth.token`

### 5.2 应该做的（一次拿全所有凭证）

**a. 同时抓 cookies + token + 设备指纹**
登录成功一次就拿完，免得后续每次签名 token 又过期再登录：
```python
# 浏览器 DevTools 协议拿 Network 流量，找到 /v1/auth/video/token 响应
# 把响应的 token + expired_at 也持久化下来
```

**b. 抓 m3u8 真实带签名 URL**
不用我们自己拼签名（Xclient-Signature 那套 magic 字符串脆弱），直接：
```python
driver.get(f'https://www.yanhekt.cn/session/{session_id}')
# 等播放器初始化，浏览器会自动签名后请求 /VGA.m3u8
# 用 CDP（Chrome DevTools Protocol）拦截 Network → 拿到 m3u8 完整签名 URL
m3u8_url = wait_for_request(driver, '*VGA.m3u8*')
```
这样**完全不用我们自己算签名**，反爬升级也不影响。

**c. 走浏览器 fetch() 下 m3u8（避开 IP 段限流）**
某些 WAF 看 IP 段。Python 直发 → IP 段被识别为"工具" → 被限速。
浏览器发 → 被识别为"用户访问" → 不限速。
对慢节点用浏览器代答效果显著。

### 5.3 这次实验加一个变体

| ID | 名称 | 改动 |
|----|------|------|
| `v6_browser_assist` | 浏览器协助 | 关键 m3u8 URL 通过浏览器拿，ts 还是 Python 多线程拉 |

如果它比 `v_combo` 还快（或一样快但更稳），说明把 Selenium 用得更深是对的。

---

## 六、执行计划

我会按这个顺序做：

### 第一步：写 m3u8dl_lab.py（30 分钟）
6 个变体的开关代码就位。

### 第二步：写 perf_yhkt.py 跑批 harness（30 分钟）
能输出 results.json + report.md。

### 第三步：阶段 1 跑批（30 分钟）
6 方案 × K=16 × 1 次。**给你看结果**。

### 第四步：根据阶段 1 结果决定阶段 2（1-2 小时）

### 第五步：（可选）做 v6_browser_assist 实验

### 第六步：把胜出方案 backport 到 m3u8dl.py + 三个版本（简易/浏览器/Pro）一起重打

---

## 七、关于"简便版本和全量版本是不是一样"

你问简易版（PyInstaller 81MB）和完整版（PyInstaller 含 Whisper+OpenCV+CUDA，~500MB）下载速度是否一样——

**核心下载链路是 100% 一致**：都用同一个 `m3u8dl.py`，所以速度**一样**。
区别只在**额外功能**：
- 简易版：只下 mp4
- 完整版：mp4 + PPT 提取 + Whisper 转录字幕

完整版的 PPT/转录是**下载完之后**单独触发的"后处理"，不影响下载速度。

但 Tauri Pro 版（前端 React）会有一点点 IPC 开销（Python 子进程 + Rust 桥接），
预计比直跑 Python GUI **慢 1-3%**——这次实验也会顺便测一下，看是否能忽略。

---

## 八、最后的预算

总计：约 **3-4 小时** 给我，你期间可以干别的（不用盯着）。
我会**每个阶段跑完出一份 markdown 报告**，你看完再决定是否继续。

---

需要你回的：

1. **OK 开干吗？**
2. **第一阶段用 40524 第 1 节前 60 段做基准**，对吗？60 段 ≈ 60×1.8MB ≈ 108MB ≈ 单次 1-2 分钟，6 方案 × 1 trial 半小时内出第一份报告。
3. **如果出现 token 风控（之前发生过），自动跑 `browser_auth.login_interactive()` 拿新 token 后续接，OK 吗？**

回个 OK 我就开干。
