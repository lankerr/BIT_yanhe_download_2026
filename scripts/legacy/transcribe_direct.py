"""直接GPU转录 - 跳过WAV提取，让faster-whisper内部处理"""
import os
import sys
import time

os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

from faster_whisper import WhisperModel
from tqdm import tqdm

BASE = r"H:\我的云端硬盘\YanheRecordings_2026Spring\自然辩证法概论-朱冬香"
TRANSCRIPT_DIR = r"H:\我的云端硬盘\YanheRecordings_2026Spring\transcripts"

# 第5周优先
WEEKS = [5, 3, 4, 1]

def get_duration(path):
    import subprocess, json
    r = subprocess.run(['ffprobe','-v','quiet','-print_format','json','-show_format',path],
                       capture_output=True, timeout=30, encoding='utf-8', errors='replace')
    return float(json.loads(r.stdout)['format']['duration'])

def fmt(s):
    return f"{int(s)//60:02d}:{int(s)%60:02d}"

# 加载模型（只加载一次）
print("加载 Whisper large-v3 模型...")
t0 = time.time()
model = WhisperModel("large-v3", device="auto", compute_type="float16")
print(f"模型加载完成 ({time.time()-t0:.1f}s)")

for week in WEEKS:
    name = f"自然辩证法概论-朱冬香-第{week}周 星期四 第2大节"
    mp4 = os.path.join(BASE, f"{name}.mp4")

    # 检查已有转录
    out_dir = os.path.join(TRANSCRIPT_DIR, name)
    txt_path = os.path.join(out_dir, f"{name}.txt")
    if os.path.exists(txt_path) and os.path.getsize(txt_path) > 1000:
        print(f"\n✅ 第{week}周: 已有转录 ({os.path.getsize(txt_path)} bytes), 跳过")
        continue

    duration = get_duration(mp4)
    size_mb = os.path.getsize(mp4) / 1024 / 1024
    print(f"\n{'='*60}")
    print(f"第{week}周: {name}")
    print(f"视频时长: {fmt(duration)} ({duration:.0f}s), 大小: {size_mb:.0f}MB")
    print(f"{'='*60}")

    # 先拷贝到本地临时目录（Google Drive读取太慢）
    import shutil, tempfile
    local_mp4 = os.path.join(tempfile.gettempdir(), f"{name}.mp4")
    if not os.path.exists(local_mp4) or os.path.getsize(local_mp4) != os.path.getsize(mp4):
        print(f"拷贝到本地临时目录...")
        copy_start = time.time()
        with tqdm(total=int(size_mb), unit="MB", desc="拷贝视频",
                  bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt}MB [{elapsed}<{remaining}]") as pbar:
            with open(mp4, 'rb') as fsrc, open(local_mp4, 'wb') as fdst:
                while True:
                    chunk = fsrc.read(8 * 1024 * 1024)  # 8MB chunks
                    if not chunk:
                        break
                    fdst.write(chunk)
                    pbar.update(len(chunk) / 1024 / 1024)
        print(f"拷贝完成 ({time.time()-copy_start:.1f}s)")
    else:
        print(f"本地已有缓存: {local_mp4}")

    # 直接用 faster-whisper 转录本地文件
    start = time.time()
    segments_iter, info = model.transcribe(
        local_mp4,
        language="zh",
        beam_size=5,
        vad_filter=True,
        vad_parameters=dict(min_silence_duration_ms=500, speech_pad_ms=200),
    )
    print(f"语言: {info.language} ({info.language_probability:.1%})")

    segments = []
    pbar = tqdm(total=int(duration), unit="s", desc=f"第{week}周转录",
                bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt}s [{elapsed}<{remaining}, {rate_fmt}]")
    last_pos = 0

    for seg in segments_iter:
        segments.append((seg.start, seg.end, seg.text.strip()))
        new_pos = min(int(seg.end), int(duration))
        pbar.update(new_pos - last_pos)
        last_pos = new_pos

    pbar.update(int(duration) - last_pos)
    pbar.close()

    elapsed = time.time() - start
    print(f"完成! {len(segments)}段, {elapsed:.1f}s ({duration/elapsed:.1f}x 实时)")

    # 保存
    os.makedirs(out_dir, exist_ok=True)
    with open(txt_path, "w", encoding="utf-8") as f:
        for s, e, t in segments:
            f.write(f"[{fmt(s)}-{fmt(e)}] {t}\n")
        f.write(f"\n{'='*60}\n纯文本:\n{'='*60}\n\n")
        f.write("\n".join(t for _, _, t in segments))
    print(f"  TXT: {txt_path}")

    srt_path = os.path.join(out_dir, f"{name}.srt")
    with open(srt_path, "w", encoding="utf-8") as f:
        for i, (s, e, t) in enumerate(segments, 1):
            sh,sm,ss = int(s//3600), int(s%3600//60), s%60
            eh,em,es = int(e//3600), int(e%3600//60), e%60
            f.write(f"{i}\n{sh:02d}:{sm:02d}:{ss:06.3f} --> {eh:02d}:{em:02d}:{es:06.3f}\n{t}\n\n")
    print(f"  SRT: {srt_path}")

    # 删除本地临时文件
    if os.path.exists(local_mp4):
        os.remove(local_mp4)
        print(f"  已清理临时文件")

print("\n=== 全部完成 ===")
