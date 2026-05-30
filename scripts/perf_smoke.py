"""
冒烟性能测试：测两个 exe 的启动时间、内存占用、产物体积。
用法： python scripts/perf_smoke.py <exe_path>
"""
import os
import sys
import time
import subprocess
from pathlib import Path

try:
    import psutil
except ImportError:
    print("[ERR] 需要 psutil: pip install psutil")
    sys.exit(1)


def measure(exe_path: str, settle: float = 4.0) -> dict:
    p = Path(exe_path)
    if not p.is_file():
        return {"error": f"not found: {exe_path}"}

    size_mb = round(p.stat().st_size / (1024 * 1024), 1)
    t0 = time.perf_counter()
    proc = subprocess.Popen([str(p)])
    # 等待 GUI 进入空闲（用 settle 时间窗）
    time.sleep(settle)
    elapsed = round(time.perf_counter() - t0, 2)

    try:
        ps = psutil.Process(proc.pid)
        # 包含子进程
        mem_total = ps.memory_info().rss
        for child in ps.children(recursive=True):
            try:
                mem_total += child.memory_info().rss
            except psutil.NoSuchProcess:
                pass
        mem_mb = round(mem_total / (1024 * 1024), 1)
        cpu = ps.cpu_percent(interval=0.5)
        alive = True
    except psutil.NoSuchProcess:
        mem_mb = -1
        cpu = -1
        alive = False
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    return {
        "name": p.name,
        "size_mb": size_mb,
        "startup_s": elapsed,
        "memory_mb": mem_mb,
        "cpu_pct": cpu,
        "alive_after_settle": alive,
    }


def main():
    targets = sys.argv[1:] or [
        r"dist\延河课堂下载器-简易版.exe",
        r"dist\延河课堂下载器-完整版.exe",
    ]
    print(f"{'EXE':<40} {'体积MB':>8} {'启动s':>8} {'内存MB':>8} {'CPU%':>6} {'存活':>5}")
    print("-" * 80)
    for t in targets:
        r = measure(t)
        if "error" in r:
            print(f"{Path(t).name:<40} {'N/A':>8} {'N/A':>8} {'N/A':>8} {'N/A':>6} {'-':>5}  ({r['error']})")
            continue
        print(
            f"{r['name']:<40} {r['size_mb']:>8} {r['startup_s']:>8} "
            f"{r['memory_mb']:>8} {r['cpu_pct']:>6} {str(r['alive_after_settle']):>5}"
        )


if __name__ == "__main__":
    main()
