"""
批量提取所有下载视频中的PPT幻灯片
使用 ppt_extractor_gpu.py 的 extract_slides + create_pptx
"""
import os
import sys
import time
from pathlib import Path

# 导入已有的提取器
import ppt_extractor_gpu as extractor


def batch_extract():
    output_root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
    slides_root = os.path.join(output_root, "extracted_ppt")
    os.makedirs(slides_root, exist_ok=True)

    # 收集所有 mp4 文件
    mp4_files = []
    for root, dirs, files in os.walk(output_root):
        # 跳过提取结果目录
        if "extracted_ppt" in root:
            continue
        for f in sorted(files):
            if f.endswith(".mp4"):
                mp4_files.append(os.path.join(root, f))

    print(f"{'='*60}")
    print(f"批量PPT提取")
    print(f"共找到 {len(mp4_files)} 个视频文件")
    print(f"{'='*60}")
    for i, f in enumerate(mp4_files):
        print(f"  [{i+1}] {Path(f).name}")
    print()

    total_slides = 0
    results = []

    for i, video_path in enumerate(mp4_files):
        video_name = Path(video_path).stem
        slide_dir = os.path.join(slides_root, video_name)

        print(f"\n{'='*60}")
        print(f"[{i+1}/{len(mp4_files)}] {video_name}")
        print(f"{'='*60}")

        # 检查是否已经提取过
        pptx_path = os.path.join(slides_root, f"{video_name}.pptx")
        if os.path.exists(pptx_path):
            print(f"  已存在 PPT 文件，跳过: {pptx_path}")
            results.append((video_name, "跳过(已存在)", 0))
            continue

        start_time = time.time()

        try:
            count = extractor.extract_slides(
                video_path,
                slide_dir,
                scene_threshold=0.1,
                min_interval=2.0,
                similarity_threshold=0.90,
            )

            if count > 0:
                # 生成PPTX
                extractor.create_pptx(slide_dir, pptx_path)
                total_slides += count

            elapsed = time.time() - start_time
            results.append((video_name, f"成功 ({count}张)", elapsed))
            print(f"  耗时: {elapsed:.1f}秒")

        except Exception as e:
            elapsed = time.time() - start_time
            print(f"  提取失败: {e}")
            results.append((video_name, f"失败: {e}", elapsed))

    # 汇总
    print(f"\n{'='*60}")
    print(f"批量PPT提取完成！")
    print(f"{'='*60}")
    for name, status, elapsed in results:
        t = f" ({elapsed:.0f}s)" if elapsed > 0 else ""
        print(f"  {name}: {status}{t}")
    print(f"\n总计提取: {total_slides} 张幻灯片")
    print(f"输出目录: {slides_root}")


if __name__ == "__main__":
    batch_extract()
