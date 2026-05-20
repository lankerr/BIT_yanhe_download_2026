"""
全量增量下载器 - 下载本学期全部课程到指定目录
特性：
- 从 down_list.txt 读取课程ID列表
- 增量下载：自动跳过已存在的 mp4 文件
- 课程级并行：同时下载多节课（每节课内部已有64线程）
- 本地临时目录下载 .ts 碎片，合并后移至目标目录（避免 Google Drive 锁文件）
- 自动去重：API返回重复session时只保留第一条
- 同时下载屏幕录播(VGA) + 蓝牙话筒音频(用于后续转录)
- 下载完成后自动执行后处理（课件提取 + 音频转录）
"""
import os
import sys
import time
import json
import shutil
import threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import m3u8dl
import utils


# ==================== 配置 ====================
DEFAULT_OUTPUT_ROOT = r"H:\我的云端硬盘\YanheRecordings_2026Spring"
PARALLEL_SESSIONS = 3  # 同时下载的课节数（每节内部已有64线程，3节≈192线程）
# 本地临时下载目录（避免 Google Drive 同步干扰 .ts 碎片写入）
LOCAL_TEMP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_temp_download")

# 线程安全的统计计数器
_stats_lock = threading.Lock()


def load_course_ids(list_file: str = None) -> list:
    """从 down_list.txt 加载课程ID"""
    if list_file is None:
        list_file = os.path.join(utils.get_app_path(), "down_list.txt")
    if not os.path.exists(list_file):
        print(f"错误: 找不到课程列表文件 {list_file}")
        sys.exit(1)
    with open(list_file, "r", encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip() and not line.startswith("#")]


def sanitize_filename(name: str) -> str:
    """清理文件名中的非法字符"""
    for ch in r'\/:"*?<>|':
        name = name.replace(ch, "_")
    return name.strip()


def _download_one_session(task: dict, stats: dict) -> dict:
    """
    下载单节课（在线程池中运行）
    策略：先下载到本地临时目录，合并 .mp4 后移到最终目标目录（避免 Google Drive 锁文件）
    """
    final_dir = task["course_dir"]       # H:\...\课程名-教师\
    local_dir = task["local_dir"]        # _temp_download\课程名-教师\
    file_name = task["file_name"]
    vga_url = task["vga_url"]
    session = task["session"]
    download_audio = task["download_audio"]
    label = task["label"]
    max_workers = task.get("max_workers", 64)
    result = {"name": file_name, "status": "成功"}

    os.makedirs(local_dir, exist_ok=True)
    os.makedirs(final_dir, exist_ok=True)

    try:
        print(f"  ▶ [{label}] 开始下载: {file_name}")
        # 下载到本地临时目录（.ts碎片 + 合并 .mp4 都在本地完成）
        m3u8dl.M3u8Download(vga_url, local_dir, file_name, max_workers=max_workers)

        # 合并完成后，把 .mp4 移到最终目标目录
        local_mp4 = os.path.join(local_dir, f"{file_name}.mp4")
        final_mp4 = os.path.join(final_dir, f"{file_name}.mp4")
        if os.path.exists(local_mp4):
            shutil.move(local_mp4, final_mp4)

        with _stats_lock:
            stats["downloaded"] += 1
        print(f"  ✓ [{label}] 视频完成: {file_name}")
    except Exception as e:
        with _stats_lock:
            stats["failed"] += 1
        result["status"] = f"失败: {e}"
        print(f"  ✗ [{label}] 视频失败: {file_name} - {e}")
        return result

    # 下载音频（直接存到最终目录，单文件不怕锁）
    if download_audio and session.get("video_ids"):
        try:
            audio_url = utils.get_audio_url(session["video_ids"][0])
            if audio_url:
                utils.download_audio(audio_url, final_dir, file_name)
                with _stats_lock:
                    stats["audio_ok"] += 1
                print(f"  ✓ [{label}] 音频完成: {file_name}")
        except Exception as e:
            print(f"  ⚠ [{label}] 音频失败: {file_name} - {e}")

    return result


def download_all(output_root: str = DEFAULT_OUTPUT_ROOT,
                 download_audio: bool = True,
                 parallel: int = PARALLEL_SESSIONS,
                 do_postprocess: bool = False,
                 whisper_model: str = "large-v3"):
    """
    并行下载全部课程到指定目录，然后可选后处理

    目录结构:
        output_root/
            课程名-教师/
                课程名-教师-第X周 星期X 第X大节.mp4
                课程名-教师-第X周 星期X 第X大节.aac
    """
    course_ids = load_course_ids()
    print(f"{'='*60}")
    print(f"延河课堂 - 全量增量下载器 (并行版)")
    print(f"{'='*60}")
    print(f"课程数: {len(course_ids)}")
    print(f"并行数: {parallel} 节课同时下载")
    print(f"输出目录: {output_root}")
    print(f"临时目录: {LOCAL_TEMP_DIR}")
    print(f"下载音频: {'是' if download_audio else '否'}")
    print(f"自动后处理: {'是' if do_postprocess else '否'}")
    print(f"{'='*60}")

    # 验证认证
    if not utils.read_auth():
        print("错误: 没有找到认证 token")
        sys.exit(1)

    first_id = course_ids[0]
    if not utils.test_auth(courseID=first_id):
        print("错误: Token 已过期，请重新获取")
        sys.exit(1)
    print("✓ 认证有效\n")

    os.makedirs(output_root, exist_ok=True)
    os.makedirs(LOCAL_TEMP_DIR, exist_ok=True)

    # ==================== 阶段1: 收集全部待下载任务 ====================
    stats = {"downloaded": 0, "skipped": 0, "failed": 0, "audio_ok": 0}
    all_tasks = []  # 待下载的任务列表
    seen_file_names = set()  # 去重：防止 API 返回重复 session
    task_counter = 0
    # Reduce per-session threads when running in parallel to avoid CDN overload
    per_session_workers = max(16, 64 // parallel)
    print(f"每节课线程数: {per_session_workers} (总并发约 {per_session_workers * parallel})")

    for idx, courseID in enumerate(course_ids):
        print(f"\n[课程 {idx+1}/{len(course_ids)}] 获取 {courseID} 信息...")
        try:
            videoList, courseName, professor = utils.get_course_info(courseID=courseID)
            folder_name = sanitize_filename(f"{courseName}-{professor}")
            course_dir = os.path.join(output_root, folder_name)
            local_course_dir = os.path.join(LOCAL_TEMP_DIR, folder_name)
            os.makedirs(course_dir, exist_ok=True)

            print(f"  {courseName} - {professor} | {len(videoList)}节")

            for i, session in enumerate(videoList):
                title = session.get("title", f"第{i+1}节")
                file_name = sanitize_filename(f"{courseName}-{professor}-{title}")

                # 去重：跳过 API 返回的重复 session
                if file_name in seen_file_names:
                    print(f"  ⚠ [去重跳过] {title}")
                    continue
                seen_file_names.add(file_name)

                # 检查视频
                if not session.get("videos") or not session["videos"]:
                    stats["skipped"] += 1
                    continue
                vga_url = session["videos"][0].get("vga", "")
                if not vga_url:
                    stats["skipped"] += 1
                    continue

                # 增量检查（检查最终目录）
                mp4_path = os.path.join(course_dir, f"{file_name}.mp4")
                if os.path.exists(mp4_path):
                    size_mb = os.path.getsize(mp4_path) / 1024 / 1024
                    print(f"  ✓ [已存在 {size_mb:.0f}MB] {title}")
                    stats["skipped"] += 1

                    # 补音频
                    if download_audio:
                        aac_path = os.path.join(course_dir, f"{file_name}.aac")
                        if not os.path.exists(aac_path) and session.get("video_ids"):
                            try:
                                audio_url = utils.get_audio_url(session["video_ids"][0])
                                if audio_url:
                                    utils.download_audio(audio_url, course_dir, file_name)
                                    stats["audio_ok"] += 1
                            except Exception:
                                pass
                    continue

                task_counter += 1
                all_tasks.append({
                    "course_dir": course_dir,
                    "local_dir": local_course_dir,
                    "file_name": file_name,
                    "vga_url": vga_url,
                    "session": session,
                    "download_audio": download_audio,
                    "label": f"{task_counter}",
                    "max_workers": per_session_workers,
                })

        except Exception as e:
            print(f"  ✗ 获取课程 {courseID} 失败: {e}")
            stats["failed"] += 1

    print(f"\n{'='*60}")
    print(f"待下载: {len(all_tasks)} 节 | 已跳过: {stats['skipped']} 节")
    print(f"并行下载 {parallel} 节，每节内部 ≤64 线程")
    print(f"{'='*60}\n")

    if not all_tasks:
        print("所有视频都已存在，无需下载！")
    else:
        # ==================== 阶段2: 并行下载 ====================
        start_time = time.time()
        results = []

        with ThreadPoolExecutor(max_workers=parallel) as executor:
            futures = {
                executor.submit(_download_one_session, task, stats): task
                for task in all_tasks
            }
            for future in as_completed(futures):
                task = futures[future]
                try:
                    result = future.result()
                    results.append(result)
                except Exception as e:
                    print(f"  ✗ 任务异常: {task['file_name']} - {e}")
                    with _stats_lock:
                        stats["failed"] += 1

        elapsed = time.time() - start_time
        print(f"\n{'='*60}")
        print(f"下载阶段完成! 耗时: {elapsed/60:.1f} 分钟")
        print(f"{'='*60}")

        # 清理临时目录
        try:
            shutil.rmtree(LOCAL_TEMP_DIR, ignore_errors=True)
            print(f"  临时目录已清理: {LOCAL_TEMP_DIR}")
        except Exception:
            pass

    print(f"  新下载: {stats['downloaded']}")
    print(f"  已跳过: {stats['skipped']}")
    print(f"  失败:   {stats['failed']}")
    print(f"  音频:   {stats['audio_ok']}")
    print(f"  输出:   {output_root}")

    # 保存下载记录
    record_path = os.path.join(output_root, "_download_record.json")
    record = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "stats": stats,
        "courses": course_ids,
    }
    with open(record_path, "w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False, indent=2)

    # ==================== 阶段3: 自动后处理 ====================
    if do_postprocess:
        print(f"\n{'='*60}")
        print(f"开始后处理: 课件提取 + 音频转录")
        print(f"{'='*60}")
        try:
            import batch_process
            batch_process.batch_process_all(
                output_root,
                do_slides=True,
                do_transcribe=True,
                model_size=whisper_model,
                device="auto",
                compute_type="float16",
                language="zh",
            )
        except ImportError as e:
            print(f"后处理模块导入失败: {e}")
            print("请确保在 GPU 环境中运行: conda run -n rtx5070_cu128 python download_all.py --post-process")
        except Exception as e:
            print(f"后处理失败: {e}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="延河课堂全量增量下载器 (并行版)")
    parser.add_argument("-o", "--output", default=DEFAULT_OUTPUT_ROOT, help="输出目录")
    parser.add_argument("-p", "--parallel", type=int, default=PARALLEL_SESSIONS,
                        help=f"并行下载课节数 (默认{PARALLEL_SESSIONS})")
    parser.add_argument("--no-audio", action="store_true", help="不下载音频")
    parser.add_argument("--post-process", action="store_true",
                        help="下载完成后自动执行课件提取+音频转录")
    parser.add_argument("-m", "--model", default="large-v3",
                        help="Whisper模型 (tiny/base/small/medium/large-v3)")
    args = parser.parse_args()
    download_all(
        output_root=args.output,
        download_audio=not args.no_audio,
        parallel=args.parallel,
        do_postprocess=args.post_process,
        whisper_model=args.model,
    )
