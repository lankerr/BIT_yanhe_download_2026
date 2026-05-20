"""Quick check: drives, H: availability, network, Google Drive status"""
import os, subprocess, shutil, sys, traceback

OUTPUT_FILE = r"C:\Users\97290\Desktop\BIT_yanhe_download_2026\check_result.txt"
try:
    f = open(OUTPUT_FILE, "w", encoding="utf-8")
except:
    f = open(r"C:\Users\97290\Desktop\check_result.txt", "w", encoding="utf-8")

def p(msg):
    f.write(str(msg) + "\n")
    f.flush()

try:
    p("=" * 50)
    p("1. Available drives:")
    for d in "CDEFGHIJK":
        path = f"{d}:\\"
        if os.path.exists(path):
            try:
                total, used, free = shutil.disk_usage(path)
                p(f"  {path}  total={total//1024//1024//1024}GB  free={free//1024//1024//1024}GB")
            except Exception as e:
                p(f"  {path}  ERROR: {e}")
    
    p("\n2. H: drive check:")
    p(f"  H:\\ exists: {os.path.exists('H:\\')}")
    h_path = r"H:\我的云端硬盘"
    p(f"  {h_path} exists: {os.path.exists(h_path)}")
    yanhe = r"H:\我的云端硬盘\YanheRecordings_2026Spring"
    p(f"  {yanhe} exists: {os.path.exists(yanhe)}")
    if os.path.exists(yanhe):
        mp4s = []
        for root, dirs, files in os.walk(yanhe):
            for fn in files:
                if fn.endswith(".mp4"):
                    fp = os.path.join(root, fn)
                    sz = os.path.getsize(fp) / 1024 / 1024
                    mp4s.append((fn, sz))
        p(f"  MP4 files found: {len(mp4s)}")
        for name, sz in sorted(mp4s):
            p(f"    {name}  ({sz:.0f} MB)")
    
    p("\n3. Google Drive process check:")
    r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq GoogleDriveFS.exe"],
                       capture_output=True, text=True, timeout=10)
    lines = [l for l in r.stdout.strip().split("\n") if "GoogleDriveFS" in l]
    if lines:
        p(f"  GoogleDriveFS.exe is running ({len(lines)} processes)")
    else:
        p("  GoogleDriveFS.exe is NOT running!")
    
    p("\n4. Network check:")
    r = subprocess.run(["ping", "-n", "2", "www.baidu.com"],
                       capture_output=True, text=True, timeout=10)
    if "TTL=" in r.stdout:
        p("  Baidu: OK")
    else:
        p(f"  Baidu: FAIL - {r.stdout[-200:]}")
    
    p("\n5. _temp_download check:")
    temp = r"C:\Users\97290\Desktop\BIT_yanhe_download_2026\_temp_download"
    if os.path.exists(temp):
        for item in os.listdir(temp):
            full = os.path.join(temp, item)
            if os.path.isdir(full):
                files = os.listdir(full)
                mp4_in = [x for x in files if x.endswith(".mp4")]
                ts_in = [x for x in files if x.endswith(".ts")]
                p(f"  {item}/  (mp4={len(mp4_in)}, ts_dirs/files={len(files)})")
                for mp in mp4_in:
                    sz = os.path.getsize(os.path.join(full, mp)) / 1024 / 1024
                    p(f"    {mp}  ({sz:.0f} MB)")
    else:
        p("  _temp_download does not exist")
    
    # Check download.log tail
    p("\n6. download.log (last 10 lines):")
    logf = r"C:\Users\97290\Desktop\BIT_yanhe_download_2026\download.log"
    if os.path.exists(logf):
        with open(logf, "r", encoding="utf-8") as lf:
            lines = lf.readlines()
            for l in lines[-10:]:
                p("  " + l.rstrip())
    else:
        p("  No log file")
    
    p("\nDONE")
except Exception:
    p(traceback.format_exc())
finally:
    f.close()
