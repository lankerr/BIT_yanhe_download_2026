"""
PPT/幻灯片提取器 - GPU加速版本
使用FFmpeg scene filter进行场景变化检测，速度约为50x实时
"""

import subprocess
import os
import sys
import re
import argparse
import cv2
import numpy as np
from pathlib import Path
from typing import List, Tuple, Optional
from tqdm import tqdm
import imagehash
from PIL import Image

try:
    from app_paths import ffmpeg_path, ffprobe_path
except ImportError:
    def ffmpeg_path(): return "ffmpeg"
    def ffprobe_path(): return "ffprobe"

# 尝试导入pptx
try:
    from pptx import Presentation
    from pptx.util import Inches
    HAS_PPTX = True
except ImportError:
    HAS_PPTX = False

# 配置参数
SCENE_THRESHOLD = 0.1  # FFmpeg scene filter阈值（0.0-1.0）
MIN_SCENE_INTERVAL = 2.0  # 最小场景间隔（秒）
SIMILARITY_THRESHOLD = 0.90  # 感知哈希相似度阈值
JPG_QUALITY = 95  # JPEG质量

def get_video_duration(video_path: str) -> float:
    """获取视频时长（秒）"""
    cmd = [
        ffprobe_path(), '-v', 'error',
        '-show_entries', 'format=duration',
        '-of', 'csv=p=0',
        video_path
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        return float(result.stdout.strip())
    except:
        return 0


def detect_scenes_ffmpeg(video_path: str, threshold: float = SCENE_THRESHOLD) -> List[float]:
    """
    使用FFmpeg scene filter检测场景变化时间点
    返回场景变化的时间戳列表（秒）

    注：曾试过在 select 前加 scale=640 预缩放降打分开销，实测
    （ffmpeg 8 / 1080p，-benchmark）CPU 反而多耗约 50%——scene 打分
    本身已全分辨率 SIMD 优化，加 scale 等于多处理一遍帧，故不加。
    """
    duration = get_video_duration(video_path)
    duration_str = f"{int(duration//60)}分{int(duration%60)}秒" if duration > 0 else "未知"

    cmd = [
        ffmpeg_path(), '-hide_banner',
        '-i', video_path,
        '-vf', f"select='gt(scene,{threshold})',showinfo",
        '-an', '-sn', '-dn', '-f', 'null', '-'
    ]
    
    print(f"正在使用 FFmpeg 分析场景变化 (threshold={threshold})...")
    print(f"视频时长: {duration_str}，预计处理时间: {int(duration/50)+1}秒 (约50x实时)")
    print("请等待处理完成...")
    
    timestamps = []
    
    try:
        # 使用Popen实时读取输出
        process = subprocess.Popen(
            cmd,
            stderr=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            text=True,
            encoding='utf-8',
            errors='replace'
        )
        
        pattern = r'pts_time:(\d+\.?\d*)'
        time_pattern = r'time=(\d+:\d+:\d+\.\d+)'
        last_progress = 0
        
        for line in process.stderr:
            # 检测场景变化点
            match = re.search(pattern, line)
            if match:
                timestamps.append(float(match.group(1)))
                print(f"  找到场景 #{len(timestamps)}: {float(match.group(1)):.1f}s")
            
            # 显示进度
            time_match = re.search(time_pattern, line)
            if time_match and duration > 0:
                time_str = time_match.group(1)
                parts = time_str.split(':')
                current_time = int(parts[0])*3600 + int(parts[1])*60 + float(parts[2])
                progress = int(current_time / duration * 100)
                if progress > last_progress and progress % 10 == 0:
                    print(f"  进度: {progress}%")
                    last_progress = progress
        
        process.wait()
        
    except Exception as e:
        print(f"FFmpeg执行错误: {e}")
        return []
    
    print(f"检测到 {len(timestamps)} 个场景变化点")
    return timestamps


def filter_close_timestamps(timestamps: List[float], min_interval: float = MIN_SCENE_INTERVAL) -> List[float]:
    """过滤太接近的时间戳"""
    if not timestamps:
        return []
    
    filtered = [timestamps[0]]
    for t in timestamps[1:]:
        if t - filtered[-1] >= min_interval:
            filtered.append(t)
    
    return filtered


def extract_frame_at_time(video_path: str, timestamp: float) -> Optional[np.ndarray]:
    """使用FFmpeg在指定时间点提取单帧"""
    cmd = [
        ffmpeg_path(), '-hide_banner', '-loglevel', 'error',
        '-ss', str(timestamp),
        '-i', video_path,
        '-vframes', '1',
        '-f', 'image2pipe',
        '-pix_fmt', 'bgr24',
        '-vcodec', 'rawvideo',
        '-'
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, timeout=10)
        if result.returncode != 0:
            return None

        # 获取视频尺寸
        probe_cmd = [
            ffprobe_path(), '-v', 'error',
            '-select_streams', 'v:0',
            '-show_entries', 'stream=width,height',
            '-of', 'csv=p=0',
            video_path
        ]
        probe_result = subprocess.run(probe_cmd, capture_output=True, text=True)
        w, h = map(int, probe_result.stdout.strip().split(','))
        
        frame = np.frombuffer(result.stdout, dtype=np.uint8).reshape((h, w, 3))
        return frame
    except Exception as e:
        return None


def extract_frames_batch(video_path: str, timestamps: List[float]) -> List[Tuple[float, np.ndarray]]:
    """批量提取帧（使用OpenCV，按时间点 seek）"""
    if not timestamps:
        return []

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        print(f"无法打开视频: {video_path}")
        return []

    fps = cap.get(cv2.CAP_PROP_FPS)
    # 某些封装的 fps 会返回 0/NaN，此时按帧号定位会把所有帧都读到 0 位置，
    # 导致全部拿到同一帧——退回按毫秒定位
    fps_valid = fps and fps > 0 and fps == fps  # 排除 NaN
    frames = []

    for timestamp in tqdm(sorted(timestamps), desc="提取帧", unit="帧"):
        if fps_valid:
            frame_num = int(timestamp * fps)
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame_num)
        else:
            cap.set(cv2.CAP_PROP_POS_MSEC, int(timestamp * 1000))
        ret, frame = cap.read()
        if ret:
            frames.append((timestamp, frame))

    cap.release()
    return frames


def compute_phash(image: np.ndarray, hash_size: int = 16) -> imagehash.ImageHash:
    """计算感知哈希"""
    pil_image = Image.fromarray(cv2.cvtColor(image, cv2.COLOR_BGR2RGB))
    return imagehash.phash(pil_image, hash_size=hash_size)


# ==================== 单遍融合算子（v2 内核）====================
# 设计依据（arXiv:2202.03540 SliTraNet：幻灯片切换检测；
#          arXiv:1804.09066 ECO：相邻帧高度冗余，可抽样处理）：
#   课堂视频一页幻灯片静止数秒到数分钟，翻页是全画面/大面积突变。
# 旧管线 = ffmpeg 全分辨率扫场景（完整解码一遍）
#        + 逐时间点 seek 提帧（每个候选点再解码一遍）
#        + PIL/imagehash 全分辨率去重 —— 共两次以上解码、多次格式往返。
# 新管线 = 单遍融合：ffmpeg 解码一次，缩放成 320px 灰度缩略图流入 Python，
#   numpy 向量化在同一遍内完成「帧差分（变化检测）+ dHash（去重）」，
#   只对幸存的 ~几十页做全分辨率 seek 提取。消灭第二遍解码与格式往返。

FUSED_SAMPLE_WIDTH = 320      # 缩略图宽度（gray 差分与 dHash 足够）
FUSED_FPS_SAMPLE = 5.0        # 抽样帧率：幻灯片一页停 ≥ 数秒，5fps 采样零漏检（ECO 冗余抽样）
FUSED_BATCH = 64              # 每批读入帧数（批量 numpy 向量化，消除逐帧 Python 开销）
FUSED_DIFF_THRESHOLD = 0.10   # 平均绝对差/255 的触发阈值
FUSED_FRAC_THRESHOLD = 0.20   # 显著变化像素占比（|d|>24）的触发阈值
FUSED_HASH_DIST = 24          # mini-pHash(255bit) 汉明距离 >= 此值视为不同页（≈legacy 相似度 0.90）


def _probe_video_info(video_path: str):
    """返回 (宽, 高, fps, 时长秒)。失败返回 (0,0,0,0)。"""
    def _probe(entries):
        cmd = [ffprobe_path(), '-v', 'error', '-select_streams', 'v:0',
               '-show_entries', f'stream={entries}', '-of', 'csv=p=0', video_path]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            return r.stdout.strip()
        except Exception:
            return ''
    wh = _probe('width,height')
    try:
        w, h = map(int, wh.split(','))
    except Exception:
        w = h = 0
    fps = 0.0
    fr = _probe('avg_frame_rate')
    try:
        num, den = fr.split('/')
        if int(den) != 0:
            fps = int(num) / int(den)
    except Exception:
        pass
    dur = get_video_duration(video_path)
    return w, h, fps, dur


def _dhash_bits(gray_small: np.ndarray) -> np.ndarray:
    """9x8 灰度 → 64bit dHash（水平梯度符号），packbits 打包。O(n) 无 DCT。"""
    g = cv2.resize(gray_small, (9, 8), interpolation=cv2.INTER_AREA)
    bits = g[:, 1:] > g[:, :-1]
    return np.packbits(bits)


def _thumb_phash_bits(gray_small: np.ndarray) -> np.ndarray:
    """缩略图 → 255bit mini-pHash：32x32 DCT 取 16x16 低频块中值符号。

    仅在触发帧（几十次/部）调用，成本可忽略；判别力对齐 legacy 的
    imagehash.phash(hash_size=16)。注意不能只取 8x8 块——结构性强的
    页面（竖条纹类）能量集中在极少系数上，8x8 只剩个位数有效位，
    实测任意两页距离恒为 8，完全失去判别力。
    """
    g = cv2.resize(gray_small, (32, 32), interpolation=cv2.INTER_AREA).astype(np.float32)
    coeff = cv2.dct(g)[:16, :16].flatten()[1:]  # 去 DC，255 bits
    return np.packbits(coeff > np.median(coeff))


def detect_slides_single_pass(
    video_path: str,
    diff_threshold: float = FUSED_DIFF_THRESHOLD,
    frac_threshold: float = FUSED_FRAC_THRESHOLD,
    min_interval: float = MIN_SCENE_INTERVAL,
    hash_dist: int = FUSED_HASH_DIST,
    sample_width: int = FUSED_SAMPLE_WIDTH,
    fps_sample: float = FUSED_FPS_SAMPLE,
    verbose: bool = True,
    stats: dict = None,
):
    """单遍融合检测：一次解码内完成变化检测 + 去重。

    ffmpeg 侧 fps 抽样 + 缩放灰度 rawvideo 管道；Python 侧按批 numpy
    向量化打分（帧间 SAD + 显著像素占比），仅对触发帧算 dHash 去重。
    时间戳精度 = 1/fps_sample（默认 ±0.2s，对幻灯片时间轴足够）。

    返回去重后的翻页时间戳列表（秒）。触发条件（满足其一）：
      - 平均绝对差 >= diff_threshold（全画面突变，如换页/切屏）
      - 显著变化像素占比 >= frac_threshold（大面积内容更新）
    连续触发但 dHash 距离 < hash_dist 的视为同一页（动画渐变/镜头抖动）。
    """
    w, h, fps_src, duration = _probe_video_info(video_path)
    if not w or not fps_src:
        if verbose:
            print("无法探测视频信息，退回旧管线")
        return None  # None 表示不可用，调用方回退 legacy
    tw = sample_width
    th = max(2, int(round(h * tw / w / 2)) * 2)
    frame_bytes = tw * th

    cmd = [
        ffmpeg_path(), '-hide_banner', '-loglevel', 'error',
        '-i', video_path, '-an', '-sn', '-dn',
        '-vf', f'fps={fps_sample},scale={tw}:{th},format=gray',
        '-f', 'rawvideo', '-',
    ]
    if verbose:
        dur_str = f"{int(duration // 60)}分{int(duration % 60)}秒" if duration > 0 else "未知"
        print(f"[融合算子] 单遍扫描 {tw}x{th} 灰度 @ {fps_sample}fps 抽样"
              f"（源 {fps_src:.2f}fps），时长 {dur_str}")

    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, bufsize=frame_bytes * FUSED_BATCH,
    )
    total_frames = int(duration * fps_sample) if duration > 0 else 0
    carry = None          # 上一批最后一帧（拼接用，保证跨批帧对连续）
    consumed = 0          # 已处理帧的全局序号
    last_t = -1e9
    last_hash = None
    candidates = []
    last_pct = -10
    max_mad = 0.0
    max_frac = 0.0

    try:
        while True:
            want = frame_bytes * FUSED_BATCH
            chunk = proc.stdout.read(want)
            if not chunk:
                break
            n = len(chunk) // frame_bytes
            if n == 0:
                break
            arr = np.frombuffer(chunk[:n * frame_bytes], dtype=np.uint8).reshape(n, th, tw)
            if carry is not None:
                arr = np.concatenate((carry[None, :, :], arr), axis=0)
            # 批量向量化打分：arr[k] 与 arr[k+1] 的差分
            d = np.abs(arr[1:].astype(np.int16) - arr[:-1].astype(np.int16))
            mads = d.mean(axis=(1, 2)) / 255.0
            fracs = (d > 24).mean(axis=(1, 2))
            if stats is not None:
                # 记录全程最大指标：调用方据此判断低阈值重扫是否必然徒劳
                stats["max_mad"] = max(stats.get("max_mad", 0.0), float(mads.max()))
                stats["max_frac"] = max(stats.get("max_frac", 0.0), float(fracs.max()))
            hits = np.nonzero((mads >= diff_threshold) | (fracs >= frac_threshold))[0]
            # hits[k] 对应变化出现在帧 (consumed + hits[k] + 1)
            for hk in hits:
                g = consumed + int(hk) + 1  # 变化后那一帧的全局序号
                t = g / fps_sample
                if t - last_t < min_interval:
                    continue
                hsh = _thumb_phash_bits(arr[int(hk) + 1])
                if last_hash is None or \
                        int(np.count_nonzero(np.bitwise_xor(hsh, last_hash))) >= hash_dist:
                    candidates.append(t)
                    last_hash = hsh
                last_t = t
            consumed += n
            carry = arr[-1].copy()
            if total_frames and verbose and (consumed * 100 // total_frames) >= last_pct + 10:
                last_pct = consumed * 100 // total_frames
                print(f"  进度: {min(last_pct, 100)}%（发现 {len(candidates)} 页）")
    finally:
        proc.stdout.close()
        proc.wait()

    if verbose:
        print(f"[融合算子] 单遍完成：{consumed} 帧（抽样后），去重后 {len(candidates)} 个翻页点")
    return candidates


def deduplicate_frames(frames: List[Tuple[float, np.ndarray]], 
                       threshold: float = SIMILARITY_THRESHOLD) -> List[Tuple[float, np.ndarray]]:
    """使用感知哈希去重"""
    if not frames:
        return []
    
    unique_frames = [frames[0]]
    prev_hash = compute_phash(frames[0][1])
    
    for timestamp, frame in tqdm(frames[1:], desc="去重", unit="帧"):
        current_hash = compute_phash(frame)
        similarity = 1 - (prev_hash - current_hash) / (16 * 16)  # hash_size=16
        
        if similarity < threshold:
            unique_frames.append((timestamp, frame))
            prev_hash = current_hash
    
    return unique_frames


def save_image_unicode(image: np.ndarray, path: str, quality: int = JPG_QUALITY) -> bool:
    """保存图像，支持Unicode路径"""
    try:
        _, buf = cv2.imencode('.jpg', image, [cv2.IMWRITE_JPEG_QUALITY, quality])
        with open(path, 'wb') as f:
            f.write(buf.tobytes())
        return True
    except Exception as e:
        print(f"保存失败 {path}: {e}")
        return False


def extract_slides(video_path: str,
                   output_dir: str,
                   scene_threshold: float = SCENE_THRESHOLD,
                   min_interval: float = MIN_SCENE_INTERVAL,
                   similarity_threshold: float = SIMILARITY_THRESHOLD,
                   engine: str = "fused") -> int:
    """
    主函数：从视频中提取幻灯片
    返回保存的幻灯片数量

    engine="fused"：单遍融合算子（默认，一次解码完成检测+去重，详见
    detect_slides_single_pass 的设计说明；不可用或无结果时自动回退 legacy）
    engine="legacy"：旧管线（ffmpeg scene 全分辨率扫描 + 逐点 seek 提帧 + pHash 去重）
    """
    if engine == "legacy":
        return extract_slides_legacy(video_path, output_dir, scene_threshold,
                                     min_interval, similarity_threshold)

    video_path = os.path.abspath(video_path)
    output_dir = os.path.abspath(output_dir)

    if not os.path.exists(video_path):
        print(f"视频文件不存在: {video_path}")
        return 0
    os.makedirs(output_dir, exist_ok=True)
    video_name = Path(video_path).stem

    # Step 1: 单遍融合检测（变化检测 + 去重一遍完成）
    print("\n=== [融合管线] 步骤 1/2: 单遍检测+去重 ===")
    stats = {}
    candidates = detect_slides_single_pass(
        video_path, min_interval=min_interval, stats=stats,
    )
    if candidates is not None and not candidates:
        # 全程最大差分连一半阈值都够不着 → 低阈值重扫必然徒劳，短路跳过
        if stats.get("max_mad", 0.0) >= FUSED_DIFF_THRESHOLD * 0.5 or                 stats.get("max_frac", 0.0) >= FUSED_FRAC_THRESHOLD * 0.5:
            print("未检测到翻页，降低阈值重试...")
            candidates = detect_slides_single_pass(
                video_path,
                diff_threshold=FUSED_DIFF_THRESHOLD * 0.5,
                frac_threshold=FUSED_FRAC_THRESHOLD * 0.5,
                min_interval=min_interval,
            )
        else:
            print(f"未检测到翻页（全程最大差分 mad={stats.get('max_mad', 0):.3f} "
                  f"frac={stats.get('max_frac', 0):.3f}，远低于阈值，跳过重扫）")

    # 融合算子不可用（探测失败）→ 回退旧管线
    if candidates is None:
        return extract_slides_legacy(video_path, output_dir, scene_threshold,
                                     min_interval, similarity_threshold)
    if not candidates:
        print("无法检测到场景变化")
        return 0

    # Step 2: 仅对幸存页做全分辨率 seek 提取 + 保存
    print(f"\n=== [融合管线] 步骤 2/2: 提取 {len(candidates)} 页全分辨率帧 ===")
    frames = extract_frames_batch(video_path, candidates)
    print(f"成功提取 {len(frames)} 帧")

    saved_count = 0
    for i, (timestamp, frame) in enumerate(frames):
        minutes = int(timestamp // 60)
        seconds = int(timestamp % 60)
        filename = f"{video_name}_slide_{i+1:03d}_{minutes:02d}m{seconds:02d}s.jpg"
        filepath = os.path.join(output_dir, filename)
        if save_image_unicode(frame, filepath):
            saved_count += 1

    print(f"\n完成！保存了 {saved_count} 张幻灯片到: {output_dir}")
    return saved_count


def extract_slides_legacy(video_path: str,
                          output_dir: str,
                          scene_threshold: float = SCENE_THRESHOLD,
                          min_interval: float = MIN_SCENE_INTERVAL,
                          similarity_threshold: float = SIMILARITY_THRESHOLD) -> int:
    """
    旧管线（保留作回退与对照）：ffmpeg scene 扫描 → 逐点 seek 提帧 → pHash 去重
    返回保存的幻灯片数量
    """
    video_path = os.path.abspath(video_path)
    output_dir = os.path.abspath(output_dir)
    
    if not os.path.exists(video_path):
        print(f"视频文件不存在: {video_path}")
        return 0
    
    os.makedirs(output_dir, exist_ok=True)
    
    video_name = Path(video_path).stem
    
    # Step 1: 场景检测
    print("\n=== 步骤 1/4: 场景检测 ===")
    timestamps = detect_scenes_ffmpeg(video_path, scene_threshold)
    
    if not timestamps:
        print("未检测到场景变化，尝试降低阈值...")
        timestamps = detect_scenes_ffmpeg(video_path, scene_threshold * 0.5)
    
    if not timestamps:
        print("无法检测到场景变化")
        return 0
    
    # Step 2: 过滤
    print("\n=== 步骤 2/4: 过滤时间戳 ===")
    filtered_timestamps = filter_close_timestamps(timestamps, min_interval)
    print(f"过滤后: {len(timestamps)} → {len(filtered_timestamps)} 个时间点")
    
    # Step 3: 提取帧
    print("\n=== 步骤 3/4: 提取帧 ===")
    frames = extract_frames_batch(video_path, filtered_timestamps)
    print(f"成功提取 {len(frames)} 帧")
    
    # Step 4: 去重
    print("\n=== 步骤 4/4: 去重并保存 ===")
    unique_frames = deduplicate_frames(frames, similarity_threshold)
    print(f"去重后: {len(frames)} → {len(unique_frames)} 帧")
    
    # 保存
    saved_count = 0
    for i, (timestamp, frame) in enumerate(tqdm(unique_frames, desc="保存", unit="张")):
        minutes = int(timestamp // 60)
        seconds = int(timestamp % 60)
        filename = f"{video_name}_slide_{i+1:03d}_{minutes:02d}m{seconds:02d}s.jpg"
        filepath = os.path.join(output_dir, filename)
        
        if save_image_unicode(frame, filepath):
            saved_count += 1
    
    print(f"\n完成！保存了 {saved_count} 张幻灯片到: {output_dir}")
    return saved_count


def create_pptx(image_dir: str, output_pptx: str = None) -> str:
    """
    将图片目录中的所有图片合并成PPT
    返回生成的PPT文件路径
    """
    if not HAS_PPTX:
        print("警告: python-pptx未安装，无法生成PPT。请运行: pip install python-pptx")
        return None
    
    import glob
    
    # 获取所有图片并排序
    images = sorted(glob.glob(os.path.join(image_dir, '*.jpg')))
    if not images:
        print(f"未找到图片: {image_dir}")
        return None
    
    # 确定输出文件名
    if output_pptx is None:
        dir_name = Path(image_dir).name
        output_pptx = os.path.join(os.path.dirname(image_dir), f"{dir_name}.pptx")
    
    print(f"\n=== 生成PPT ===\n  图片数量: {len(images)}")
    
    # 创建PPT (16:9)
    prs = Presentation()
    prs.slide_width = Inches(16)
    prs.slide_height = Inches(9)
    
    # 添加每张图片为一页幻灯片
    for img_path in tqdm(images, desc="生成PPT", unit="页"):
        slide = prs.slides.add_slide(prs.slide_layouts[6])  # 空白布局
        slide.shapes.add_picture(img_path, Inches(0), Inches(0), width=Inches(16), height=Inches(9))
    
    # 保存
    prs.save(output_pptx)
    file_size = os.path.getsize(output_pptx) / 1024 / 1024
    print(f"  PPT已保存: {output_pptx} ({file_size:.2f} MB)")
    
    return output_pptx


def main():
    parser = argparse.ArgumentParser(
        description="PPT/幻灯片提取器 - GPU加速版本",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python ppt_extractor_gpu.py video.mp4 -o output/slides
  python ppt_extractor_gpu.py video.mp4 -t 0.2 -m 3.0
        """
    )
    
    parser.add_argument('video', help='视频文件路径')
    parser.add_argument('-o', '--output', default='output/ppt_slides', help='输出目录')
    parser.add_argument('-t', '--threshold', type=float, default=SCENE_THRESHOLD,
                        help=f'场景检测阈值 (默认: {SCENE_THRESHOLD})')
    parser.add_argument('-m', '--min-interval', type=float, default=MIN_SCENE_INTERVAL,
                        help=f'最小场景间隔秒数 (默认: {MIN_SCENE_INTERVAL})')
    parser.add_argument('-s', '--similarity', type=float, default=SIMILARITY_THRESHOLD,
                        help=f'相似度阈值 (默认: {SIMILARITY_THRESHOLD})')
    parser.add_argument('-p', '--pptx', action='store_true',
                        help='同时生成PPT文件')
    
    args = parser.parse_args()
    
    print("=" * 60)
    print("PPT/幻灯片提取器 - GPU加速版本")
    print("=" * 60)
    print(f"视频: {args.video}")
    print(f"输出目录: {args.output}")
    print(f"场景阈值: {args.threshold}")
    print(f"最小间隔: {args.min_interval}s")
    print(f"相似度阈值: {args.similarity}")
    print(f"生成PPT: {'是' if args.pptx else '否'}")
    print("=" * 60)
    
    count = extract_slides(
        args.video,
        args.output,
        scene_threshold=args.threshold,
        min_interval=args.min_interval,
        similarity_threshold=args.similarity
    )
    
    # 如果需要生成PPT
    if count > 0 and args.pptx:
        create_pptx(args.output)
    
    return 0 if count > 0 else 1


if __name__ == '__main__':
    sys.exit(main())
