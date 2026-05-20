"""守护监控：监控 _run_all_post.py 进程
- 进程死亡 -> 自动重启（脚本本身会跳过已完成项）
- 10 分钟无新日志输出 -> 视为卡死, kill 后重启
- 每 60 秒写状态快照到 _watchdog_status.json
- 完成后退出
"""
import os, sys, time, json, subprocess, signal
from pathlib import Path
from datetime import datetime

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

BASE = Path(__file__).parent
PY = r"C:\Users\97290\.conda\envs\rtx5070_cu128\python.exe"
TARGET = BASE / "_run_all_post.py"
LOG = BASE / "_run_all_post.log"
STATUS = BASE / "_watchdog_status.json"
WD_LOG = BASE / "_watchdog.log"

STALL_SECONDS = 600         # 10 分钟无新日志判定卡死
STATUS_INTERVAL = 60        # 状态写入间隔
MAX_RESTART = 20            # 最大重启次数

def log(msg):
    line = f"[{datetime.now().isoformat(timespec='seconds')}] {msg}"
    try:
        print(line, flush=True)
    except Exception:
        try:
            sys.stdout.buffer.write((line + "\n").encode('utf-8', 'replace'))
            sys.stdout.flush()
        except Exception:
            pass
    try:
        with open(WD_LOG, 'a', encoding='utf-8') as f:
            f.write(line + "\n")
    except Exception:
        pass

def inventory():
    """统计当前完成情况"""
    root = BASE / "output"
    summary = {}
    total_mp4 = total_pptx = total_txt = 0
    for c in sorted(root.iterdir()):
        if not c.is_dir() or not c.name.endswith('-screen'):
            continue
        mp4s = [m for m in c.rglob('*.mp4')
                if 'extracted_ppt' not in m.parts and 'transcripts' not in m.parts]
        pptx_dir = c / "extracted_ppt"
        tx_dir = c / "transcripts"
        pptxs = list(pptx_dir.glob("*.pptx")) if pptx_dir.exists() else []
        txts = list(tx_dir.rglob("*.txt")) if tx_dir.exists() else []
        summary[c.name] = {
            "mp4": len(mp4s),
            "pptx": len(pptxs),
            "txt": len(txts),
        }
        total_mp4 += len(mp4s); total_pptx += len(pptxs); total_txt += len(txts)
    return {"total": {"mp4": total_mp4, "pptx": total_pptx, "txt": total_txt},
            "courses": summary}

def write_status(state, restart_count, last_log_size, last_log_change_ts):
    inv = inventory()
    snap = {
        "timestamp": datetime.now().isoformat(timespec='seconds'),
        "state": state,
        "restart_count": restart_count,
        "log_size": last_log_size,
        "seconds_since_log_change": int(time.time() - last_log_change_ts) if last_log_change_ts else None,
        "inventory": inv,
        "done": inv["total"]["pptx"] >= inv["total"]["mp4"] and inv["total"]["txt"] >= inv["total"]["mp4"],
    }
    STATUS.write_text(json.dumps(snap, ensure_ascii=False, indent=2), encoding='utf-8')
    return snap

def start_proc():
    """启动主流水线进程, stdout/stderr 写到 LOG (utf-8 追加)"""
    f = open(LOG, 'ab')
    f.write(f"\n\n===== restart at {datetime.now().isoformat()} =====\n".encode('utf-8'))
    f.flush()
    env = os.environ.copy()
    env['PYTHONIOENCODING'] = 'utf-8'
    env['PYTHONUNBUFFERED'] = '1'
    flags = 0
    if os.name == 'nt':
        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        CREATE_NO_WINDOW = 0x08000000
        flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
    p = subprocess.Popen(
        [PY, '-u', str(TARGET)],
        stdout=f, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        cwd=str(BASE), env=env,
        creationflags=flags,
        close_fds=True,
    )
    log(f"主进程已启动 PID={p.pid}")
    return p, f

def kill_proc(p):
    try:
        if os.name == 'nt':
            subprocess.run(['taskkill', '/F', '/T', '/PID', str(p.pid)],
                           capture_output=True)
        else:
            p.terminate()
            time.sleep(2)
            p.kill()
        log(f"已强制结束 PID={p.pid}")
    except Exception as e:
        log(f"kill 失败: {e}")

def main():
    restart_count = 0
    p, f = start_proc()
    last_log_size = LOG.stat().st_size if LOG.exists() else 0
    last_change = time.time()
    next_status = time.time()

    while True:
      try:
        time.sleep(5)
        # 状态快照
        if time.time() >= next_status:
            inv = write_status("running", restart_count, last_log_size, last_change)
            t = inv["inventory"]["total"]
            log(f"快照: pptx={t['pptx']}/{t['mp4']}  txt={t['txt']}/{t['mp4']}  重启次数={restart_count}")
            if inv["done"]:
                log("[OK] 全部完成")
                kill_proc(p); break
            next_status = time.time() + STATUS_INTERVAL

        # 检查进程存活
        rc = p.poll()
        if rc is not None:
            log(f"[WARN] 主进程退出, returncode={rc}")
            inv = write_status("exited", restart_count, last_log_size, last_change)
            if inv["done"]:
                log("[OK] 任务已完成"); break
            if restart_count >= MAX_RESTART:
                log("[ABORT] 重启次数超限, 放弃")
                write_status("aborted", restart_count, last_log_size, last_change)
                break
            try: f.close()
            except Exception: pass
            time.sleep(5)
            restart_count += 1
            log(f"[RESTART] 自动重启 ({restart_count}/{MAX_RESTART})")
            p, f = start_proc()
            last_log_size = LOG.stat().st_size if LOG.exists() else 0
            last_change = time.time()
            continue

        # 检查日志增长
        try:
            sz = LOG.stat().st_size
        except FileNotFoundError:
            sz = 0
        if sz != last_log_size:
            last_log_size = sz
            last_change = time.time()
        elif time.time() - last_change > STALL_SECONDS:
            log(f"[WARN] 日志 {STALL_SECONDS}s 无更新, 判定卡死, 准备重启")
            write_status("stalled", restart_count, last_log_size, last_change)
            kill_proc(p)
            try: f.close()
            except Exception: pass
            time.sleep(5)
            restart_count += 1
            log(f"[RESTART] 卡死后重启 ({restart_count}/{MAX_RESTART})")
            if restart_count >= MAX_RESTART:
                log("[ABORT] 重启次数超限, 放弃"); break
            p, f = start_proc()
            last_log_size = LOG.stat().st_size if LOG.exists() else 0
            last_change = time.time()
      except Exception as e:
        try:
            log(f"[LOOP-ERR] {type(e).__name__}: {e}")
        except Exception:
            pass
        time.sleep(10)

    write_status("finished", restart_count, last_log_size, last_change)
    log("watchdog 退出")

if __name__ == '__main__':
    main()
