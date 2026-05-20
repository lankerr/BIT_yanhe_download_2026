"""批量转录自然辩证法缺失的课程 - 先删损坏wav再重新提取"""
import os
import sys
import subprocess
import time

BASE = r"H:\我的云端硬盘\YanheRecordings_2026Spring\自然辩证法概论-朱冬香"
TRANSCRIPT_DIR = r"H:\我的云端硬盘\YanheRecordings_2026Spring\transcripts"

# 需要转录的周次（第5周优先）
WEEKS = [5, 3, 4, 1]

# 1. 先删掉所有损坏的temp wav
print("=== 删除损坏的残留wav文件 ===")
for f in os.listdir(BASE):
    if f.startswith("_temp_audio_") and f.endswith(".wav"):
        path = os.path.join(BASE, f)
        size_mb = os.path.getsize(path) / 1024 / 1024
        try:
            os.remove(path)
            print(f"  删除: {f} ({size_mb:.1f} MB)")
        except PermissionError:
            print(f"  跳过(被锁定): {f} ({size_mb:.1f} MB)")

# 2. 逐个转录
for week in WEEKS:
    name = f"自然辩证法概论-朱冬香-第{week}周 星期四 第2大节"
    mp4 = os.path.join(BASE, f"{name}.mp4")
    
    # 检查是否已有完整转录
    txt = os.path.join(TRANSCRIPT_DIR, name, f"{name}.txt")
    if os.path.exists(txt) and os.path.getsize(txt) > 1000:
        print(f"\n=== 第{week}周: 已有转录 ({os.path.getsize(txt)} bytes), 跳过 ===")
        continue
    
    print(f"\n{'='*60}")
    print(f"转录第{week}周: {name}")
    print(f"{'='*60}")
    
    # 运行转录
    cmd = [
        sys.executable, "audio_transcriber_gpu.py",
        mp4, "-m", "large-v3", "-l", "zh",
        "-o", TRANSCRIPT_DIR
    ]
    
    start = time.time()
    result = subprocess.run(cmd, text=True, encoding='utf-8', errors='replace')
    elapsed = time.time() - start
    
    if result.returncode == 0:
        print(f"✅ 第{week}周转录完成 ({elapsed:.0f}秒)")
    else:
        print(f"❌ 第{week}周转录失败 (returncode={result.returncode})")
    
    # 清理临时wav
    temp_wav = os.path.join(BASE, f"_temp_audio_{name}.wav")
    if os.path.exists(temp_wav):
        os.remove(temp_wav)
        print(f"  清理临时文件: {temp_wav}")

print("\n=== 全部完成 ===")
