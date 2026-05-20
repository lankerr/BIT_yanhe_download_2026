import os, subprocess, shutil, traceback

out = []

try:
    out.append("=== DRIVES ===")
    for d in "CDEFGHIJK":
        p = d + ":\\"
        if os.path.exists(p):
            t, u, f = shutil.disk_usage(p)
            out.append(f"  {p} total={t//1073741824}GB free={f//1073741824}GB")

    out.append("\n=== H DRIVE ===")
    out.append(f"  H:\\ exists: {os.path.exists(os.sep.join(['H:','']))}")
    
    yanhe = os.path.join("H:", os.sep, "\u6211\u7684\u4e91\u7aef\u786c\u76d8", "YanheRecordings_2026Spring")
    out.append(f"  YanheRecordings exists: {os.path.exists(yanhe)}")
    if os.path.exists(yanhe):
        count = 0
        for root, dirs, files in os.walk(yanhe):
            for fn in files:
                if fn.endswith(".mp4"):
                    sz = os.path.getsize(os.path.join(root, fn)) // 1048576
                    out.append(f"    {fn} ({sz}MB)")
                    count += 1
        out.append(f"  Total MP4: {count}")

    out.append("\n=== GOOGLE DRIVE PROCESS ===")
    r = subprocess.run(["tasklist"], capture_output=True, text=True, timeout=15)
    for line in r.stdout.split("\n"):
        if "oogle" in line.lower():
            out.append(f"  {line.strip()}")
    if not any("oogle" in x.lower() for x in out[-5:]):
        out.append("  Google Drive NOT FOUND in tasklist")

    out.append("\n=== NETWORK ===")
    r = subprocess.run(["ping", "-n", "1", "-w", "3000", "www.baidu.com"],
                       capture_output=True, text=True, timeout=10)
    out.append(f"  Baidu: {'OK' if 'TTL=' in r.stdout else 'FAIL'}")

except Exception:
    out.append(traceback.format_exc())

with open(os.path.join("C:\\Users\\97290\\Desktop", "diag.txt"), "w", encoding="utf-8") as f:
    f.write("\n".join(out))
