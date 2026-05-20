import subprocess, json, os

base = r'H:\我的云端硬盘\YanheRecordings_2026Spring\自然辩证法概论-朱冬香'
weeks = [1, 3, 4, 5]

for w in weeks:
    name = f'自然辩证法概论-朱冬香-第{w}周 星期四 第2大节.mp4'
    v = os.path.join(base, name)
    r = subprocess.run(['ffprobe', '-v', 'quiet', '-print_format', 'json', '-show_format', v],
                       capture_output=True, text=True, timeout=30, encoding='utf-8', errors='replace')
    info = json.loads(r.stdout)
    dur = float(info['format']['duration'])
    print(f'第{w}周: {dur/60:.1f} min, size={os.path.getsize(v)/1024/1024:.0f} MB')
