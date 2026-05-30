"""
m3u8 分片下载性能对照实验。

5 个候选 × N 次重复，用同一份 ts 列表做公平对比。
输出：bench/results/<timestamp>/results.json + report.md

候选：
  baseline_serial : 单线程串行（理论下限的下限，对照组）
  req_pool_K      : requests.Session + ThreadPoolExecutor，K 可调
  cur_m3u8dl      : 当前 m3u8.py 走我们项目的下载内核
  aria2c          : 子进程 aria2c -j K
  n_m3u8dl_re     : 子进程 N_m3u8DL-RE.exe --thread-count K

用法：
  python scripts/perf_bench.py --trials 3 --workers 4 16 24 32 64
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Windows 控制台 UTF-8
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore
except Exception:
    pass

# Apple Bipbop 4x3 1080p：181 段独立 ts，每段 ~1.8MB，CDN 稳定全球可达
DEFAULT_M3U8 = "https://devstreaming-cdn.apple.com/videos/streaming/examples/bipbop_4x3/gear4/prog_index.m3u8"

ARIA2_EXE = ROOT / "tools" / "aria2" / "aria2-1.37.0-win-64bit-build1" / "aria2c.exe"
NRE_EXE = ROOT / "tools" / "n_m3u8dl_re" / "N_m3u8DL-RE.exe"


def http_get_text(url: str) -> str:
    return urllib.request.urlopen(url, timeout=20).read().decode("utf-8", errors="replace")


def parse_m3u8(url: str) -> tuple[list[str], str]:
    """返回 (绝对 ts URL 列表, 解析后的子 m3u8 URL)。"""
    text = http_get_text(url)
    if "EXT-X-STREAM-INF" in text:
        # master，选最高码率
        sub = None
        bw_max = 0
        lines = text.splitlines()
        for i, line in enumerate(lines):
            if line.startswith("#EXT-X-STREAM-INF"):
                bw = 0
                for tag in line.split(","):
                    if "BANDWIDTH=" in tag:
                        bw = int(tag.split("=", 1)[1].split(",")[0])
                for j in range(i + 1, min(i + 3, len(lines))):
                    cand = lines[j].strip()
                    if cand and not cand.startswith("#"):
                        if bw > bw_max:
                            bw_max = bw
                            sub = cand
                        break
        if not sub:
            raise RuntimeError("master m3u8 has no variant")
        if not sub.startswith("http"):
            sub = url.rsplit("/", 1)[0] + "/" + sub
        return parse_m3u8(sub)
    base = url.rsplit("/", 1)[0]
    ts_urls = []
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        ts_urls.append(ln if ln.startswith("http") else base + "/" + ln)
    return ts_urls, url


def fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


# ---------------- 候选 1: baseline serial ----------------

def cand_baseline_serial(ts_urls: list[str], out_dir: Path, **_) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    failed = 0
    bytes_total = 0
    for i, u in enumerate(ts_urls):
        p = out_dir / f"{i:04d}.ts"
        try:
            with urllib.request.urlopen(u, timeout=30) as r:
                data = r.read()
                p.write_bytes(data)
                bytes_total += len(data)
        except Exception:
            failed += 1
    return {"failed": failed, "bytes": bytes_total}


# ---------------- 候选 2: requests Session + ThreadPool ----------------

def cand_req_pool(ts_urls: list[str], out_dir: Path, workers: int = 24, **_) -> dict:
    import requests  # type: ignore
    out_dir.mkdir(parents=True, exist_ok=True)
    sess = requests.Session()
    sess.headers.update({"User-Agent": "Mozilla/5.0"})
    bytes_total = 0
    failed = 0
    lock_bytes = []  # 一个用线程安全的 list 收集字节数

    def one(idx_url):
        idx, u = idx_url
        p = out_dir / f"{idx:04d}.ts"
        try:
            r = sess.get(u, timeout=(10, 60), stream=True)
            r.raise_for_status()
            n = 0
            with open(p, "wb") as f:
                for chunk in r.iter_content(64 * 1024):
                    if chunk:
                        f.write(chunk)
                        n += len(chunk)
            return ("ok", n)
        except Exception as e:
            return ("fail", str(e))

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for status, payload in pool.map(one, list(enumerate(ts_urls))):
            if status == "ok":
                bytes_total += payload
                lock_bytes.append(payload)
            else:
                failed += 1
    sess.close()
    return {"failed": failed, "bytes": bytes_total, "workers": workers}


# ---------------- 候选 3: aria2c ----------------

def cand_aria2(ts_urls: list[str], out_dir: Path, workers: int = 16, **_) -> dict:
    if not ARIA2_EXE.is_file():
        return {"error": f"aria2c not found at {ARIA2_EXE}"}
    out_dir.mkdir(parents=True, exist_ok=True)
    # 写入 url 文件：每个 URL 后面紧跟 dir/out 选项
    url_file = out_dir / "_urls.txt"
    lines = []
    for i, u in enumerate(ts_urls):
        lines.append(u)
        lines.append(f"  out={i:04d}.ts")
    url_file.write_text("\n".join(lines), encoding="utf-8")
    log_file = out_dir / "_aria2.log"
    cmd = [
        str(ARIA2_EXE),
        "--input-file", str(url_file),
        "--dir", str(out_dir),
        f"--max-concurrent-downloads={workers}",
        "--max-connection-per-server=1",  # 每个分片只用 1 个连接（每片小，不用拆）
        "--split=1",
        "--retry-wait=2",
        "--max-tries=3",
        "--connect-timeout=10",
        "--timeout=60",
        "--console-log-level=warn",
        "--summary-interval=0",
        "--allow-overwrite=true",
        "--auto-file-renaming=false",
        f"--log={log_file}",
        "--log-level=warn",
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=600)
    except subprocess.TimeoutExpired:
        return {"error": "aria2c timed out"}
    bytes_total = sum((out_dir / f"{i:04d}.ts").stat().st_size
                      for i in range(len(ts_urls))
                      if (out_dir / f"{i:04d}.ts").is_file())
    failed = sum(1 for i in range(len(ts_urls))
                 if not (out_dir / f"{i:04d}.ts").is_file()
                 or (out_dir / f"{i:04d}.ts").stat().st_size == 0)
    return {"failed": failed, "bytes": bytes_total, "workers": workers,
            "exit_code": r.returncode}


# ---------------- 候选 4: N_m3u8DL-RE ----------------

def cand_nre(ts_urls: list[str], out_dir: Path, m3u8_url: str = "",
             workers: int = 16, **_) -> dict:
    if not NRE_EXE.is_file():
        return {"error": f"N_m3u8DL-RE not found at {NRE_EXE}"}
    out_dir.mkdir(parents=True, exist_ok=True)
    if not m3u8_url:
        return {"error": "m3u8_url required for N_m3u8DL-RE"}
    cmd = [
        str(NRE_EXE),
        m3u8_url,
        "--save-dir", str(out_dir),
        "--save-name", "out",
        "--tmp-dir", str(out_dir / "tmp"),
        "--thread-count", str(workers),
        "--download-retry-count", "3",
        "--http-request-timeout", "60",
        "--auto-select",
        "--del-after-done", "False",  # 保留 ts 用于体积统计
        "--no-log",
        "--no-ansi-color",
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=600)
    except subprocess.TimeoutExpired:
        return {"error": "N_m3u8DL-RE timed out"}
    # 查找产物
    bytes_total = 0
    failed_marker = None
    for p in out_dir.rglob("*.ts"):
        bytes_total += p.stat().st_size
    for p in out_dir.rglob("*.mp4"):
        # 也算上合并产物
        if p.parent == out_dir:
            bytes_total = max(bytes_total, p.stat().st_size)
    if r.returncode != 0:
        failed_marker = "exit_code != 0"
    return {"failed": 0 if r.returncode == 0 else len(ts_urls),
            "bytes": bytes_total,
            "workers": workers,
            "exit_code": r.returncode,
            "stderr_tail": (r.stderr.decode("utf-8", errors="replace")[-300:]
                            if r.stderr else "")}


# ---------------- 候选 5: 项目当前 m3u8dl.py ----------------

def cand_cur_m3u8dl(ts_urls: list[str], out_dir: Path, m3u8_url: str = "",
                    workers: int = 32, **_) -> dict:
    """复用项目的 m3u8dl.M3u8Download。"""
    import m3u8dl  # type: ignore
    out_dir.mkdir(parents=True, exist_ok=True)
    # m3u8dl 接受 workDir + name，会保存到 workDir/name/ 下
    work = out_dir.parent
    name = out_dir.name
    bytes_before = 0
    try:
        m3u8dl.M3u8Download(
            url=m3u8_url,
            workDir=str(work.relative_to(ROOT)) if work.is_relative_to(ROOT) else str(work),
            name=name,
            max_workers=workers,
            num_retries=3,
            gui_mode=True,
        )
    except Exception as e:
        return {"error": f"M3u8Download raised: {str(e)[:200]}"}
    bytes_total = 0
    for p in out_dir.rglob("*.ts"):
        bytes_total += p.stat().st_size
    mp4 = work / (name + ".mp4")
    if mp4.is_file():
        bytes_total = max(bytes_total, mp4.stat().st_size)
    return {"failed": 0, "bytes": bytes_total, "workers": workers,
            "mp4": str(mp4) if mp4.is_file() else None}


CANDIDATES = {
    "baseline_serial": cand_baseline_serial,
    "req_pool": cand_req_pool,
    "aria2c": cand_aria2,
    "n_m3u8dl_re": cand_nre,
    "cur_m3u8dl": cand_cur_m3u8dl,
}


def run_trial(name: str, fn, ts_urls: list[str], m3u8_url: str,
              out_dir: Path, workers: int, n_full: int) -> dict:
    if out_dir.exists():
        shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    try:
        info = fn(ts_urls, out_dir, m3u8_url=m3u8_url, workers=workers)
    except Exception as e:
        info = {"error": str(e)[:300]}
    elapsed = time.time() - t0
    info["elapsed"] = round(elapsed, 2)
    # 归一化：每段平均耗时（不同候选可能跑不同段数）
    n = info.get("segments_done") or len(ts_urls)
    if name in ("n_m3u8dl_re", "cur_m3u8dl"):
        # 这两个走 m3u8 URL，会跑全量 N 段
        n = n_full
    info["segments_done"] = n
    info["per_seg_ms"] = round(elapsed * 1000 / max(n, 1), 1)
    if info.get("bytes", 0) > 0:
        info["throughput_MBps"] = round(info["bytes"] / 1024 / 1024 / max(elapsed, 0.01), 2)
        info["throughput_Mbps"] = round(info["bytes"] * 8 / 1024 / 1024 / max(elapsed, 0.01), 2)
    return info


def render_report(out: Path, results: list[dict], meta: dict) -> Path:
    md = out / "report.md"
    L: list[str] = []
    L.append("# m3u8 下载性能对照实验")
    L.append("")
    L.append(f"- 实验时间：`{meta['started']}`")
    L.append(f"- 测试源：`{meta['m3u8_url']}`")
    L.append(f"- 分片数：{meta['ts_count']}")
    L.append(f"- 试验次数：每候选 {meta['trials']} 次")
    L.append(f"- 工人数 (workers)：{meta['workers']}")
    L.append("")

    L.append("## 1. 候选与配置")
    L.append("")
    L.append("| 候选 | 实现 | 备注 |")
    L.append("|------|------|------|")
    L.append("| `baseline_serial` | 单线程 urllib | 串行下限对照 |")
    L.append("| `req_pool` | requests.Session + ThreadPoolExecutor | Python 标准库做法 |")
    L.append("| `cur_m3u8dl` | 当前 m3u8dl.py（AIMD 4-K） | 项目现状 |")
    L.append("| `aria2c` | aria2c 子进程 (-j K) | C++ 工业级 |")
    L.append("| `n_m3u8dl_re` | N_m3u8DL-RE.exe | C# m3u8 标杆 |")
    L.append("")

    L.append("## 2. 单次结果")
    L.append("")
    L.append("| 候选 | workers | trial | 耗时(s) | 段数 | per_seg(ms) | 字节 | 吞吐(MB/s) | 失败 | 备注 |")
    L.append("|------|---------|-------|---------|------|-------------|------|------------|------|------|")
    for r in results:
        err = r.get("error", "")
        L.append(
            f"| {r['name']} | {r['workers']} | {r['trial']} | "
            f"{r.get('elapsed','—')} | {r.get('segments_done','—')} | "
            f"{r.get('per_seg_ms','—')} | {fmt_bytes(r.get('bytes',0))} | "
            f"{r.get('throughput_MBps','—')} | {r.get('failed','—')} | "
            f"{err[:60]} |"
        )
    L.append("")

    # 聚合：name × workers
    agg: dict = {}
    for r in results:
        if r.get("error") or r.get("elapsed") is None:
            continue
        key = (r["name"], r["workers"])
        agg.setdefault(key, []).append(r)

    L.append("## 3. 聚合（每 (候选,workers) 取中位数）")
    L.append("")
    L.append("| 候选 | workers | 中位 per_seg(ms) | 中位吞吐(MB/s) | 段数 | trial 数 |")
    L.append("|------|---------|------------------|----------------|------|----------|")
    rows = []
    for (name, w), rs in sorted(agg.items()):
        per_segs = sorted(r["per_seg_ms"] for r in rs)
        tps = sorted(r.get("throughput_MBps", 0) for r in rs)
        med_p = per_segs[len(per_segs) // 2]
        med_t = tps[len(tps) // 2]
        n = rs[0].get("segments_done", "—")
        rows.append((name, w, med_p, med_t, n, len(rs)))
        L.append(f"| {name} | {w} | {med_p} | {med_t} | {n} | {len(rs)} |")
    L.append("")

    # 排名（按 per_seg_ms 升序）
    if rows:
        rows_sorted = sorted(rows, key=lambda x: x[2])
        best = rows_sorted[0]
        L.append("## 4. 排行（按每段平均耗时，越小越快）")
        L.append("")
        L.append(f"**最快**：`{best[0]}` (workers={best[1]})，每段 {best[2]} ms，"
                 f"吞吐 {best[3]} MB/s")
        L.append("")
        L.append("| 排名 | 候选 | workers | per_seg(ms) | 相对最快 |")
        L.append("|------|------|---------|-------------|----------|")
        for i, row in enumerate(rows_sorted, 1):
            ratio = row[2] / best[2] if best[2] > 0 else 0
            L.append(f"| {i} | {row[0]} | {row[1]} | {row[2]} | ×{ratio:.2f} |")
        L.append("")

    L.append("## 5. 数学预期 vs 实测")
    L.append("")
    L.append("基于模型 `T = N·S/(K·B_per) + ...`：")
    L.append("- 给定带宽下，K 增大到饱和点 K\\* 之后无收益")
    L.append("- AIMD 在 p>1% 时窗口被压低，实际吞吐 < 固定线程")
    L.append("- 看下面 `req_pool` 在不同 K 下的吞吐曲线，K\\* 就在拐点处")
    L.append("")
    L.append("---")
    L.append("")
    L.append("> 自动生成 by `scripts/perf_bench.py`。原始数据见 `results.json`。")
    md.write_text("\n".join(L), encoding="utf-8")
    return md


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--m3u8", default=DEFAULT_M3U8)
    ap.add_argument("--trials", type=int, default=3)
    ap.add_argument("--workers", type=int, nargs="+", default=[16])
    ap.add_argument("--limit", type=int, default=0,
                    help="只跑前 N 段（0=全跑）")
    ap.add_argument("--candidates", nargs="+",
                    default=["baseline_serial", "req_pool", "aria2c",
                             "n_m3u8dl_re", "cur_m3u8dl"])
    args = ap.parse_args()

    print(f"[bench] 解析 m3u8: {args.m3u8}")
    ts_urls, sub_url = parse_m3u8(args.m3u8)
    if args.limit and args.limit < len(ts_urls):
        ts_urls = ts_urls[:args.limit]
        print(f"[bench] 截取前 {args.limit} 段做测试")
    print(f"[bench] {len(ts_urls)} 个 ts，将做 {args.trials} 次试验 × "
          f"{len(args.candidates)} 个候选 × {len(args.workers)} 种 workers 配置")

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_root = ROOT / "bench" / "results" / ts
    out_root.mkdir(parents=True, exist_ok=True)

    meta = {
        "started": datetime.now().isoformat(timespec="seconds"),
        "m3u8_url": sub_url,
        "ts_count": len(ts_urls),
        "trials": args.trials,
        "workers": args.workers,
    }

    results: list[dict] = []
    for cname in args.candidates:
        if cname not in CANDIDATES:
            print(f"  unknown candidate: {cname}, skip")
            continue
        fn = CANDIDATES[cname]

        # 单线程候选不需要遍历 workers
        worker_list = args.workers if cname != "baseline_serial" else [1]

        # cur_m3u8dl / n_m3u8dl_re 走 m3u8，会跑全量；其它跑 limit 子集
        n_full = len(ts_urls)
        is_m3u8_native = cname in ("n_m3u8dl_re", "cur_m3u8dl")
        run_ts_urls = ts_urls
        # 即使是 m3u8 native 候选，给个数量信息
        if is_m3u8_native and args.limit and args.limit < n_full:
            print(f"  ⚠ {cname} 走 m3u8 URL，将跑全量 {n_full} 段，"
                  f"对比时按 per_seg_ms 归一化")

        for w in worker_list:
            for trial in range(1, args.trials + 1):
                tag = f"{cname}_w{w}_t{trial}"
                out_dir = out_root / tag
                print(f"\n[run] {tag} ...")
                info = run_trial(cname, fn, run_ts_urls, sub_url, out_dir,
                                 w, n_full)
                rec = {"name": cname, "workers": w, "trial": trial, **info}
                print(f"  -> elapsed={info.get('elapsed','—')}s "
                      f"per_seg={info.get('per_seg_ms','—')}ms "
                      f"bytes={fmt_bytes(info.get('bytes', 0))} "
                      f"throughput={info.get('throughput_MBps','—')} MB/s "
                      f"failed={info.get('failed','—')} "
                      f"err={info.get('error', '')[:50]}")
                results.append(rec)
                # 立即写入，防止中断丢数据
                (out_root / "results.json").write_text(
                    json.dumps({"meta": meta, "results": results},
                               ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )

    # 清理产物（保留 results.json + report.md）
    for p in out_root.iterdir():
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)

    md = render_report(out_root, results, meta)
    print(f"\n=== 报告：{md}")
    print(f"=== 数据：{out_root / 'results.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
