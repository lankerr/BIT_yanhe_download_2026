#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
课件 PPT 提取器
================
从课程录播视频中提取 PPT 幻灯片截图

原理：
1. 使用 PySceneDetect 检测场景变化
2. 计算帧间差异（哈希 + 结构相似度）
3. 去重并保存关键帧

使用方法：
    python ppt_extractor.py video.mp4
    python ppt_extractor.py video.mp4 --output slides/
    python ppt_extractor.py folder/ --batch
"""

import os
import sys
import cv2
import numpy as np
from pathlib import Path
from datetime import datetime
from typing import List, Tuple, Optional
import argparse
import json
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor
import hashlib
import multiprocessing as mp

# 进度条
try:
    from tqdm import tqdm
    HAS_TQDM = True
except ImportError:
    HAS_TQDM = False
    print("提示: 安装 tqdm 可获得更好的进度显示 (pip install tqdm)")

# GPU加速解码
try:
    from decord import VideoReader, cpu, gpu
    HAS_DECORD = True
    DECORD_CTX = cpu(0)  # 使用CPU解码（批量读取仍然快很多）
    print("✓ 使用 decord 加速解码")
except ImportError:
    HAS_DECORD = False
    DECORD_CTX = None
    print("提示: 安装 decord 可获得10x加速 (pip install decord)")

# ==================== 配置 ====================
DEFAULT_OUTPUT_DIR = "extracted_slides"
THUMBNAIL_SIZE = (1920, 1080)  # 保存的图片尺寸
HASH_SIZE = 16  # 感知哈希的尺寸
SIMILARITY_THRESHOLD = 0.92  # 相似度阈值，低于此值认为是不同幻灯片
MIN_SCENE_LENGTH = 1.0  # 最小场景长度（秒）
SAMPLE_INTERVAL = 0.5  # 采样间隔（秒）

# 视频段检测参数
MIN_SLIDE_INTERVAL = 3.0  # 两张幻灯片之间的最小间隔（秒）
VIDEO_DETECT_WINDOW = 6  # 检测窗口：连续N个采样点
VIDEO_CHANGE_RATIO = 0.7  # 如果窗口内超过70%都在变化，认为是视频段
STABLE_FRAMES_REQUIRED = 4  # 视频段结束后需要连续N帧稳定才提取

# ==================== 感知哈希 ====================
def compute_phash(frame: np.ndarray, hash_size: int = 16) -> str:
    """计算感知哈希 (pHash)"""
    # 转灰度
    if len(frame.shape) == 3:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    else:
        gray = frame
    
    # 缩放到 hash_size+1 x hash_size
    resized = cv2.resize(gray, (hash_size + 1, hash_size))
    
    # 计算差分
    diff = resized[:, 1:] > resized[:, :-1]
    
    # 转换为哈希字符串
    return ''.join(['1' if b else '0' for b in diff.flatten()])


def compute_dhash(frame: np.ndarray, hash_size: int = 16) -> str:
    """计算差分哈希 (dHash)"""
    if len(frame.shape) == 3:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    else:
        gray = frame
    
    resized = cv2.resize(gray, (hash_size + 1, hash_size))
    diff = resized[:, 1:] > resized[:, :-1]
    return ''.join(['1' if b else '0' for b in diff.flatten()])


def hamming_distance(hash1: str, hash2: str) -> int:
    """计算汉明距离"""
    return sum(c1 != c2 for c1, c2 in zip(hash1, hash2))


def hash_similarity(hash1: str, hash2: str) -> float:
    """计算哈希相似度 (0-1)"""
    if not hash1 or not hash2:
        return 0.0
    dist = hamming_distance(hash1, hash2)
    return 1.0 - (dist / len(hash1))


# ==================== 帧提取和比较 ====================
def extract_keyframes(video_path: str, 
                      sample_interval: float = SAMPLE_INTERVAL,
                      similarity_threshold: float = SIMILARITY_THRESHOLD,
                      progress_callback=None) -> List[Tuple[float, np.ndarray]]:
    """
    从视频中提取关键帧（智能检测视频片段）
    支持 GPU 加速解码 (decord) 和批量处理
    
    Args:
        video_path: 视频文件路径
        sample_interval: 采样间隔（秒）
        similarity_threshold: 相似度阈值
        progress_callback: 进度回调函数 (current, total, message)
    
    Returns:
        List of (timestamp, frame) tuples
    """
    # 优先使用 decord GPU 加速
    if HAS_DECORD:
        return extract_keyframes_decord(video_path, sample_interval, 
                                         similarity_threshold, progress_callback)
    
    # 回退到 OpenCV
    return extract_keyframes_opencv(video_path, sample_interval,
                                     similarity_threshold, progress_callback)


def extract_keyframes_decord(video_path: str,
                              sample_interval: float = SAMPLE_INTERVAL,
                              similarity_threshold: float = SIMILARITY_THRESHOLD,
                              progress_callback=None) -> List[Tuple[float, np.ndarray]]:
    """
    使用 decord GPU 加速提取关键帧
    """
    # 打开视频
    vr = VideoReader(video_path, ctx=DECORD_CTX)
    fps = vr.get_avg_fps()
    total_frames = len(vr)
    duration = total_frames / fps if fps > 0 else 0
    
    print(f"视频信息: {duration:.1f}秒 ({duration/60:.1f}分钟), {fps:.1f}fps, {total_frames}帧")
    print(f"检测参数: 最小间隔{MIN_SLIDE_INTERVAL}秒, 视频检测窗口{VIDEO_DETECT_WINDOW}帧")
    print(f"🚀 使用 decord 批量读取加速")
    
    # 计算采样帧索引
    frame_interval = int(fps * sample_interval)
    sample_indices = list(range(0, total_frames, frame_interval))
    total_samples = len(sample_indices)
    min_interval_samples = int(MIN_SLIDE_INTERVAL / sample_interval)
    
    print(f"批量读取 {total_samples} 个采样帧...")
    
    # 批量读取所有采样帧 (这是最大的加速点)
    BATCH_SIZE = 64  # 每批读取的帧数
    all_frames = []
    
    if HAS_TQDM:
        pbar = tqdm(total=len(sample_indices), desc="读取帧", unit="帧",
                    ncols=80, bar_format='{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}]')
    else:
        pbar = None
    
    for i in range(0, len(sample_indices), BATCH_SIZE):
        batch_indices = sample_indices[i:i+BATCH_SIZE]
        # decord 批量读取非常快
        batch_frames = vr.get_batch(batch_indices).asnumpy()
        for j, frame in enumerate(batch_frames):
            # decord 返回 RGB，转换为 BGR (OpenCV格式)
            frame_bgr = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
            all_frames.append((sample_indices[i+j] / fps, frame_bgr))
        
        if pbar:
            pbar.update(len(batch_indices))
    
    if pbar:
        pbar.close()
    
    print(f"帧读取完成，开始分析...")
    
    # 分析关键帧
    keyframes = []
    change_history = []
    last_hash = None
    last_keyframe_idx = -min_interval_samples
    stable_count = 0
    in_video_segment = False
    pending_frame = None
    video_skip_count = 0
    
    if HAS_TQDM:
        pbar = tqdm(total=len(all_frames), desc="分析帧", unit="帧",
                    ncols=80, bar_format='{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}]')
    else:
        pbar = None
    
    for idx, (timestamp, frame) in enumerate(all_frames):
        current_hash = compute_phash(frame)
        
        # 计算与上一帧的相似度
        is_changed = False
        if last_hash is not None:
            similarity = hash_similarity(current_hash, last_hash)
            is_changed = similarity < similarity_threshold
        
        # 更新变化历史
        change_history.append(is_changed)
        if len(change_history) > VIDEO_DETECT_WINDOW:
            change_history.pop(0)
        
        # 检测是否在视频片段中
        if len(change_history) >= VIDEO_DETECT_WINDOW:
            change_ratio = sum(change_history) / len(change_history)
            was_in_video = in_video_segment
            in_video_segment = change_ratio >= VIDEO_CHANGE_RATIO
            
            if not in_video_segment and was_in_video:
                pending_frame = (timestamp, frame.copy(), current_hash)
                stable_count = 1
        
        # 判断是否提取关键帧
        should_extract = False
        
        if in_video_segment:
            video_skip_count += 1
            stable_count = 0
            pending_frame = None
        elif pending_frame is not None:
            if not is_changed:
                stable_count += 1
                if stable_count >= STABLE_FRAMES_REQUIRED:
                    if idx - last_keyframe_idx >= min_interval_samples:
                        keyframes.append((pending_frame[0], pending_frame[1]))
                        last_keyframe_idx = idx
                        last_hash = pending_frame[2]
                    pending_frame = None
                    stable_count = 0
            else:
                pending_frame = (timestamp, frame.copy(), current_hash)
                stable_count = 0
        else:
            if last_hash is None:
                should_extract = True
            elif is_changed and (idx - last_keyframe_idx >= min_interval_samples):
                should_extract = True
        
        if should_extract:
            keyframes.append((timestamp, frame.copy()))
            last_keyframe_idx = idx
            last_hash = current_hash
        elif not in_video_segment and pending_frame is None:
            last_hash = current_hash
        
        if pbar:
            pbar.update(1)
            status = "📹" if in_video_segment else "📊"
            pbar.set_postfix_str(f"{status} 找到{len(keyframes)}页")
        
        if progress_callback:
            progress_callback(idx + 1, len(all_frames), f"分析中... {len(keyframes)}个关键帧")
    
    if pbar:
        pbar.close()
    
    print(f"初步提取: {len(keyframes)} 个关键帧 (跳过视频段 {video_skip_count} 帧)")
    return keyframes


def extract_keyframes_opencv(video_path: str,
                              sample_interval: float = SAMPLE_INTERVAL,
                              similarity_threshold: float = SIMILARITY_THRESHOLD,
                              progress_callback=None) -> List[Tuple[float, np.ndarray]]:
    """
    使用 OpenCV 提取关键帧 (回退方案)
    """
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise ValueError(f"无法打开视频: {video_path}")
    
    fps = cap.get(cv2.CAP_PROP_FPS)
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    duration = total_frames / fps if fps > 0 else 0
    
    print(f"视频信息: {duration:.1f}秒 ({duration/60:.1f}分钟), {fps:.1f}fps, {total_frames}帧")
    print(f"检测参数: 最小间隔{MIN_SLIDE_INTERVAL}秒, 视频检测窗口{VIDEO_DETECT_WINDOW}帧")
    print(f"⚠️ 使用 OpenCV CPU 解码 (较慢)")
    
    keyframes = []
    frame_interval = int(fps * sample_interval)
    total_samples = total_frames // frame_interval if frame_interval > 0 else total_frames
    min_interval_samples = int(MIN_SLIDE_INTERVAL / sample_interval)
    
    # 历史记录用于视频段检测
    change_history = []
    last_hash = None
    last_keyframe_sample = -min_interval_samples
    stable_count = 0
    in_video_segment = False
    pending_frame = None
    
    frame_idx = 0
    sample_idx = 0
    video_skip_count = 0
    
    # 创建进度条
    if HAS_TQDM:
        pbar = tqdm(total=total_samples, desc="扫描视频", unit="帧", 
                    ncols=80, bar_format='{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}]')
    else:
        pbar = None
        print(f"开始扫描 {total_samples} 个采样点...")
    
    while True:
        ret, frame = cap.read()
        if not ret:
            break
        
        if frame_idx % frame_interval == 0:
            timestamp = frame_idx / fps
            current_hash = compute_phash(frame)
            
            # 计算与上一帧的相似度
            is_changed = False
            if last_hash is not None:
                similarity = hash_similarity(current_hash, last_hash)
                is_changed = similarity < similarity_threshold
            
            # 更新变化历史
            change_history.append(is_changed)
            if len(change_history) > VIDEO_DETECT_WINDOW:
                change_history.pop(0)
            
            # 检测是否在视频片段中
            if len(change_history) >= VIDEO_DETECT_WINDOW:
                change_ratio = sum(change_history) / len(change_history)
                was_in_video = in_video_segment
                in_video_segment = change_ratio >= VIDEO_CHANGE_RATIO
                
                if in_video_segment and not was_in_video:
                    # 刚进入视频段
                    if pbar:
                        pbar.set_postfix_str(f"📹视频段 | 找到{len(keyframes)}页")
                elif not in_video_segment and was_in_video:
                    # 刚离开视频段，标记pending
                    pending_frame = (timestamp, frame.copy(), current_hash)
                    stable_count = 1
            
            # 判断是否提取关键帧
            should_extract = False
            
            if in_video_segment:
                video_skip_count += 1
                stable_count = 0
                pending_frame = None
            elif pending_frame is not None:
                # 视频段刚结束，等待稳定
                if not is_changed:
                    stable_count += 1
                    if stable_count >= STABLE_FRAMES_REQUIRED:
                        # 稳定足够久，提取pending帧
                        if sample_idx - last_keyframe_sample >= min_interval_samples:
                            keyframes.append((pending_frame[0], pending_frame[1]))
                            last_keyframe_sample = sample_idx
                            last_hash = pending_frame[2]
                        pending_frame = None
                        stable_count = 0
                else:
                    # 又开始变化，更新pending
                    pending_frame = (timestamp, frame.copy(), current_hash)
                    stable_count = 0
            else:
                # 正常PPT模式
                if last_hash is None:
                    should_extract = True
                elif is_changed and (sample_idx - last_keyframe_sample >= min_interval_samples):
                    should_extract = True
            
            if should_extract:
                keyframes.append((timestamp, frame.copy()))
                last_keyframe_sample = sample_idx
                last_hash = current_hash
            elif not in_video_segment and pending_frame is None:
                # 即使不提取，也更新last_hash（跟踪当前状态）
                last_hash = current_hash
            
            sample_idx += 1
            
            # 更新进度
            if pbar:
                pbar.update(1)
                status = "📹" if in_video_segment else "📊"
                pbar.set_postfix_str(f"{status} 找到{len(keyframes)}页")
            elif sample_idx % 200 == 0:
                pct = sample_idx / total_samples * 100
                print(f"  [{pct:5.1f}%] 已扫描 {sample_idx}/{total_samples}, 找到 {len(keyframes)} 页")
            
            if progress_callback:
                progress_callback(sample_idx, total_samples, 
                                  f"扫描中... {len(keyframes)}个关键帧")
        
        frame_idx += 1
    
    if pbar:
        pbar.close()
    
    cap.release()
    print(f"初步提取: {len(keyframes)} 个关键帧 (跳过视频段 {video_skip_count} 帧)")
    
    return keyframes


def deduplicate_keyframes(keyframes: List[Tuple[float, np.ndarray]],
                          threshold: float = 0.95) -> List[Tuple[float, np.ndarray]]:
    """
    去除重复的关键帧
    """
    if not keyframes:
        return []
    
    unique_frames = [keyframes[0]]
    
    for timestamp, frame in keyframes[1:]:
        current_hash = compute_phash(frame)
        is_duplicate = False
        
        # 与已有的所有帧比较
        for _, existing_frame in unique_frames[-5:]:  # 只与最近5帧比较
            existing_hash = compute_phash(existing_frame)
            if hash_similarity(current_hash, existing_hash) > threshold:
                is_duplicate = True
                break
        
        if not is_duplicate:
            unique_frames.append((timestamp, frame))
    
    print(f"去重后: {len(unique_frames)}个关键帧")
    return unique_frames


def filter_blank_frames(keyframes: List[Tuple[float, np.ndarray]], 
                        variance_threshold: float = 500) -> List[Tuple[float, np.ndarray]]:
    """
    过滤掉空白帧（纯色背景、黑屏等）
    """
    filtered = []
    
    for timestamp, frame in keyframes:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        variance = np.var(gray)
        
        # 方差太小说明是纯色背景
        if variance > variance_threshold:
            filtered.append((timestamp, frame))
    
    print(f"过滤空白帧后: {len(filtered)}个关键帧")
    return filtered


# ==================== 保存和输出 ====================
def save_image_unicode(filepath: str, image: np.ndarray, quality: int = 95) -> bool:
    """
    保存图片到支持Unicode路径的文件系统
    cv2.imwrite在Windows上不支持中文路径，使用imencode+写二进制解决
    """
    try:
        # 编码为JPEG
        encode_param = [int(cv2.IMWRITE_JPEG_QUALITY), quality]
        result, encoded = cv2.imencode('.jpg', image, encode_param)
        if not result:
            return False
        # 写入文件（支持中文路径）
        with open(filepath, 'wb') as f:
            f.write(encoded.tobytes())
        return True
    except Exception as e:
        print(f"保存失败 {filepath}: {e}")
        return False


def save_keyframes(keyframes: List[Tuple[float, np.ndarray]], 
                   output_dir: str,
                   video_name: str,
                   resize: Tuple[int, int] = None) -> List[str]:
    """
    保存关键帧为图片
    """
    os.makedirs(output_dir, exist_ok=True)
    saved_files = []
    
    for i, (timestamp, frame) in enumerate(keyframes, 1):
        # 格式化时间戳
        minutes = int(timestamp // 60)
        seconds = int(timestamp % 60)
        time_str = f"{minutes:02d}m{seconds:02d}s"
        
        # 生成文件名
        filename = f"{video_name}_slide_{i:03d}_{time_str}.jpg"
        filepath = os.path.join(output_dir, filename)
        
        # 调整尺寸
        if resize:
            h, w = frame.shape[:2]
            target_w, target_h = resize
            if w != target_w or h != target_h:
                # 保持宽高比
                scale = min(target_w / w, target_h / h)
                new_w, new_h = int(w * scale), int(h * scale)
                frame = cv2.resize(frame, (new_w, new_h), interpolation=cv2.INTER_LANCZOS4)
        
        # 保存（使用Unicode兼容方法）
        if save_image_unicode(filepath, frame):
            saved_files.append(filepath)
    
    return saved_files


def format_timestamp(seconds: float) -> str:
    """格式化时间戳"""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


# ==================== 主函数 ====================
def extract_slides_from_video(video_path: str, 
                               output_dir: str = None,
                               similarity_threshold: float = SIMILARITY_THRESHOLD,
                               progress_callback=None) -> dict:
    """
    从视频提取 PPT 幻灯片
    
    Args:
        video_path: 视频文件路径
        output_dir: 输出目录
        similarity_threshold: 相似度阈值
        progress_callback: 进度回调
    
    Returns:
        结果字典 {slides_count, output_dir, files, duration}
    """
    video_path = Path(video_path)
    if not video_path.exists():
        raise FileNotFoundError(f"视频文件不存在: {video_path}")
    
    video_name = video_path.stem
    
    # 设置输出目录
    if output_dir is None:
        output_dir = video_path.parent / f"{video_name}_slides"
    output_dir = Path(output_dir)
    
    print(f"\n{'='*60}")
    print(f"提取课件: {video_path.name}")
    print(f"输出目录: {output_dir}")
    print(f"{'='*60}\n")
    
    start_time = datetime.now()
    
    # Step 1: 提取关键帧
    print("[1/4] 扫描视频提取关键帧...")
    keyframes = extract_keyframes(str(video_path), 
                                   similarity_threshold=similarity_threshold,
                                   progress_callback=progress_callback)
    
    # Step 2: 去重
    print("[2/4] 去除重复帧...")
    keyframes = deduplicate_keyframes(keyframes)
    
    # Step 3: 过滤空白帧
    print("[3/4] 过滤空白帧...")
    keyframes = filter_blank_frames(keyframes)
    
    # Step 4: 保存
    print("[4/4] 保存幻灯片...")
    saved_files = save_keyframes(keyframes, str(output_dir), video_name)
    
    elapsed = (datetime.now() - start_time).total_seconds()
    
    # 生成结果报告
    result = {
        "video": str(video_path),
        "video_name": video_name,
        "slides_count": len(saved_files),
        "output_dir": str(output_dir),
        "files": [os.path.basename(f) for f in saved_files],
        "timestamps": [format_timestamp(t) for t, _ in keyframes],
        "processing_time": elapsed,
        "extracted_at": datetime.now().isoformat()
    }
    
    # 保存结果 JSON
    result_file = output_dir / "extraction_result.json"
    with open(result_file, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    
    print(f"\n✅ 提取完成!")
    print(f"   幻灯片数量: {len(saved_files)}")
    print(f"   输出目录: {output_dir}")
    print(f"   耗时: {elapsed:.1f}秒")
    
    return result


def batch_extract(input_dir: str, output_dir: str = None) -> List[dict]:
    """批量提取目录下所有视频的 PPT"""
    input_dir = Path(input_dir)
    video_extensions = {'.mp4', '.mkv', '.avi', '.mov', '.wmv', '.flv'}
    
    videos = [f for f in input_dir.iterdir() 
              if f.is_file() and f.suffix.lower() in video_extensions]
    
    if not videos:
        print(f"目录中没有找到视频文件: {input_dir}")
        return []
    
    print(f"找到 {len(videos)} 个视频文件")
    
    results = []
    for i, video_path in enumerate(videos, 1):
        print(f"\n[{i}/{len(videos)}] 处理: {video_path.name}")
        
        if output_dir:
            video_output = Path(output_dir) / video_path.stem
        else:
            video_output = None
        
        try:
            result = extract_slides_from_video(str(video_path), video_output)
            results.append(result)
        except Exception as e:
            print(f"❌ 处理失败: {e}")
            results.append({"video": str(video_path), "error": str(e)})
    
    # 保存汇总结果
    if output_dir:
        summary_file = Path(output_dir) / "batch_summary.json"
        with open(summary_file, 'w', encoding='utf-8') as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        print(f"\n汇总结果已保存: {summary_file}")
    
    return results


# ==================== CLI ====================
def main():
    parser = argparse.ArgumentParser(
        description="课件 PPT 提取器 - 从课程录播视频中提取幻灯片",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python ppt_extractor.py video.mp4
  python ppt_extractor.py video.mp4 --output slides/
  python ppt_extractor.py folder/ --batch
  python ppt_extractor.py video.mp4 --threshold 0.90
        """
    )
    
    parser.add_argument("input", help="视频文件或目录路径")
    parser.add_argument("-o", "--output", help="输出目录")
    parser.add_argument("-b", "--batch", action="store_true", 
                        help="批量模式：处理目录下所有视频")
    parser.add_argument("-t", "--threshold", type=float, default=SIMILARITY_THRESHOLD,
                        help=f"相似度阈值 (默认: {SIMILARITY_THRESHOLD})")
    
    args = parser.parse_args()
    
    input_path = Path(args.input)
    
    if not input_path.exists():
        print(f"错误: 路径不存在: {input_path}")
        sys.exit(1)
    
    if args.batch or input_path.is_dir():
        # 批量模式
        batch_extract(str(input_path), args.output)
    else:
        # 单文件模式
        extract_slides_from_video(str(input_path), args.output, args.threshold)


if __name__ == "__main__":
    main()
