# -*- coding: utf-8 -*-
"""
PDF 导出模块: 将幻灯片图片目录合并成 PDF 文件
与 ppt_extractor_gpu.create_pptx 行为保持一致 (仅收集 .jpg, sorted 排序)
"""

import glob
import os
from pathlib import Path

from PIL import Image


def create_pdf(image_dir: str, output_pdf: str = None) -> str:
    """
    将图片目录中的所有图片合并成PDF
    返回生成的PDF文件路径
    """
    # 获取所有图片并排序 (与 create_pptx 一致)
    images = sorted(glob.glob(os.path.join(image_dir, '*.jpg')))
    if not images:
        print(f"未找到图片: {image_dir}")
        return None

    # 确定输出文件名
    if output_pdf is None:
        dir_name = Path(image_dir).name
        output_pdf = os.path.join(os.path.dirname(image_dir), f"{dir_name}.pdf")

    print(f"\n=== 生成PDF ===\n  图片数量: {len(images)}")

    # 逐个打开图片, 用完及时 close, 避免大目录句柄耗尽
    # 2026-10-20 codex复核：原实现把全部页面以原生分辨率 RGB 驻留内存
    # （300张1080p 峰值约 1.7GiB，弱机 OOM/换页卡死）。PDF 供阅读不需要
    # 原生分辨率——统一降采样到最长边 1600 再驻留，峰值降约一个数量级。
    MAX_SIDE = 1600
    first_img = None
    append_imgs = []
    try:
        for img_path in images:
            img = Image.open(img_path).convert('RGB')
            if max(img.size) > MAX_SIDE:
                img.thumbnail((MAX_SIDE, MAX_SIDE), Image.LANCZOS)
            if first_img is None:
                first_img = img
            else:
                append_imgs.append(img)

        first_img.save(output_pdf, save_all=True, append_images=append_imgs, resolution=150)
    finally:
        for img in append_imgs:
            try:
                img.close()
            except Exception:
                pass
        if first_img is not None:
            first_img.close()

    size_mb = os.path.getsize(output_pdf) / (1024 * 1024)
    print(f"PDF已保存: {output_pdf}")
    print(f"文件大小: {size_mb:.2f} MB")

    return output_pdf


if __name__ == '__main__':
    import sys
    if len(sys.argv) >= 2:
        create_pdf(sys.argv[1])
