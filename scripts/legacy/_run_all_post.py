"""批量对每门课(output/*-screen/)执行 PPT提取 + 音频转录
- 跳过已完成
- 不触发下载
- 课程间互不影响 (单课异常不会拖垮全局)
"""
import os, sys, time
from pathlib import Path

# 强制 UTF-8 stdout (Windows 后台运行避免 emoji/中文 GBK 崩溃)
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from batch_process import batch_process_all

ROOT = Path(r"C:\Users\97290\Desktop\BIT_yanhe_download_2026\output")
courses = sorted([d for d in ROOT.iterdir()
                  if d.is_dir() and d.name.endswith('-screen')])

print(f"=== 共 {len(courses)} 门课程 ===")
for c in courses:
    print(f"  - {c.name}")

t0 = time.time()
for i, c in enumerate(courses, 1):
    print(f"\n\n{'#'*70}\n# [{i}/{len(courses)}] {c.name}\n{'#'*70}")
    try:
        batch_process_all(
            str(c),
            do_slides=True,
            do_transcribe=True,
            model_size="large-v3",
            device="cuda",
            compute_type="float16",
            language="zh",
        )
    except Exception as e:
        print(f"!! 课程处理异常: {e}")
print(f"\n全部完成, 总耗时 {(time.time()-t0)/60:.1f} 分钟")
