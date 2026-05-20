"""
音频转录器 - GPU加速版本
使用 faster-whisper (CTranslate2) 进行 GPU 加速语音识别
支持从视频中提取音频并生成 TXT/SRT 格式转录文本
"""

import os
import sys
import re
import subprocess
import argparse
import time
from pathlib import Path
from typing import Optional, Callable, List, Tuple

# 设置 HuggingFace 镜像（国内加速）
if not os.environ.get("HF_ENDPOINT"):
    os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"

try:
    from app_paths import ffmpeg_path, ffprobe_path
except ImportError:
    def ffmpeg_path(): return "ffmpeg"
    def ffprobe_path(): return "ffprobe"

# 尝试导入 faster-whisper
try:
    from faster_whisper import WhisperModel
    HAS_WHISPER = True
except ImportError:
    HAS_WHISPER = False

# 尝试导入 tqdm
try:
    from tqdm import tqdm
    HAS_TQDM = True
except ImportError:
    HAS_TQDM = False

# ==================== 配置 ====================
DEFAULT_MODEL_SIZE = "large-v3"  # 中文效果最好的模型
DEFAULT_DEVICE = "auto"  # auto 自动选择 GPU/CPU
DEFAULT_COMPUTE_TYPE = "float16"  # GPU 用 float16，CPU 用 int8
DEFAULT_LANGUAGE = "zh"  # 默认中文
DEFAULT_BEAM_SIZE = 5
DEFAULT_VAD_FILTER = True  # 启用 VAD 过滤静音段
SUPPORTED_AUDIO_EXT = {".mp3", ".wav", ".aac", ".flac", ".ogg", ".m4a", ".wma"}
SUPPORTED_VIDEO_EXT = {".mp4", ".mkv", ".avi", ".mov", ".flv", ".wmv", ".ts"}


def check_ffmpeg() -> bool:
    """检查 FFmpeg 是否可用"""
    try:
        subprocess.run(
            [ffmpeg_path(), "-version"],
            capture_output=True,
            timeout=10,
        )
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def get_media_duration(filepath: str) -> float:
    """获取媒体文件时长（秒）"""
    cmd = [
        ffprobe_path(), "-v", "error",
        "-show_entries", "format=duration",
        "-of", "csv=p=0",
        filepath,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return float(result.stdout.strip())
    except Exception:
        return 0.0


def extract_audio_from_video(video_path: str, output_audio: str = None,
                              progress_callback: Optional[Callable] = None) -> str:
    """
    从视频文件中提取音频为 WAV 格式
    返回音频文件路径
    """
    if output_audio is None:
        output_audio = Path(video_path).with_suffix(".wav").as_posix()
        # 放到本地临时目录（避免Google Drive慢速IO）
        import tempfile
        local_tmp = os.path.join(tempfile.gettempdir(), f"_temp_audio_{Path(video_path).stem}.wav")
        output_audio = local_tmp

    duration = get_media_duration(video_path)

    if os.path.exists(output_audio):
        size = os.path.getsize(output_audio)
        expected_size = duration * 16000 * 2 if duration > 0 else 0  # 16kHz mono 16bit
        if size > max(1000, expected_size * 0.1):  # at least 10% of expected
            print(f"音频文件已存在，跳过提取: {output_audio}")
            return output_audio
        else:
            print(f"音频文件不完整 ({size/1024/1024:.1f}MB)，重新提取...")
            try:
                os.remove(output_audio)
            except (PermissionError, OSError):
                # 换个文件名
                import tempfile
                output_audio = os.path.join(tempfile.gettempdir(), f"_temp_audio2_{Path(video_path).stem}.wav")

    duration_str = f"{int(duration // 60)}分{int(duration % 60)}秒" if duration > 0 else "未知"

    print(f"正在从视频中提取音频 → 本地临时目录...")
    print(f"视频时长: {duration_str}, 输出: {output_audio}")

    cmd = [
        ffmpeg_path(), "-hide_banner", "-loglevel", "error",
        "-progress", "pipe:1",
        "-i", video_path,
        "-vn",  # 不要视频
        "-acodec", "pcm_s16le",  # PCM 16bit
        "-ar", "16000",  # 16kHz 采样率（Whisper最佳）
        "-ac", "1",  # 单声道
        "-y",  # 覆盖输出
        output_audio,
    ]

    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        pbar = None
        if HAS_TQDM and duration > 0:
            pbar = tqdm(total=int(duration), unit="s", desc="提取音频",
                        bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt}s [{elapsed}<{remaining}]")
            last_pos = 0
        for line in proc.stdout:
            line = line.strip()
            if line.startswith("out_time_us="):
                try:
                    us = int(line.split("=")[1])
                    sec = us / 1_000_000
                    if pbar:
                        new_pos = min(int(sec), int(duration))
                        pbar.update(new_pos - last_pos)
                        last_pos = new_pos
                except (ValueError, IndexError):
                    pass
        if pbar:
            pbar.update(int(duration) - last_pos)
            pbar.close()
        proc.wait(timeout=600)
        if proc.returncode != 0:
            raise subprocess.CalledProcessError(proc.returncode, cmd)
        print(f"音频提取完成: {output_audio}")
        return output_audio
    except subprocess.CalledProcessError as e:
        print(f"音频提取失败: {e.stderr.decode('utf-8', errors='replace')}")
        raise RuntimeError(f"FFmpeg 提取音频失败: {e}")


def format_timestamp_srt(seconds: float) -> str:
    """将秒数格式化为 SRT 时间戳格式 HH:MM:SS,mmm"""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int((seconds - int(seconds)) * 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def format_timestamp_readable(seconds: float) -> str:
    """将秒数格式化为可读时间 MM:SS"""
    minutes = int(seconds // 60)
    secs = int(seconds % 60)
    return f"{minutes:02d}:{secs:02d}"


class AudioTranscriber:
    """GPU 加速音频转录器"""

    def __init__(
        self,
        model_size: str = DEFAULT_MODEL_SIZE,
        device: str = DEFAULT_DEVICE,
        compute_type: str = DEFAULT_COMPUTE_TYPE,
        language: str = DEFAULT_LANGUAGE,
        beam_size: int = DEFAULT_BEAM_SIZE,
        vad_filter: bool = DEFAULT_VAD_FILTER,
    ):
        if not HAS_WHISPER:
            raise ImportError(
                "faster-whisper 未安装。请运行: pip install faster-whisper\n"
                "GPU 加速还需要: pip install ctranslate2"
            )

        self.model_size = model_size
        self.device = device
        self.compute_type = compute_type
        self.language = language
        self.beam_size = beam_size
        self.vad_filter = vad_filter
        self.model = None

    def load_model(self, progress_callback: Optional[Callable] = None):
        """加载 Whisper 模型"""
        if self.model is not None:
            return

        print(f"正在加载 Whisper 模型: {self.model_size}")
        print(f"设备: {self.device}, 计算类型: {self.compute_type}")

        if progress_callback:
            progress_callback(0, 100, "正在加载模型...")

        start = time.time()
        self.model = WhisperModel(
            self.model_size,
            device=self.device,
            compute_type=self.compute_type,
        )
        elapsed = time.time() - start
        print(f"模型加载完成 ({elapsed:.1f}秒)")

        if progress_callback:
            progress_callback(100, 100, "模型加载完成")

    def transcribe_file(
        self,
        audio_path: str,
        output_dir: str = None,
        output_txt: bool = True,
        output_srt: bool = True,
        progress_callback: Optional[Callable] = None,
    ) -> dict:
        """
        转录单个音频/视频文件

        Args:
            audio_path: 音频或视频文件路径
            output_dir: 输出目录（默认与输入文件同目录）
            output_txt: 是否输出 TXT
            output_srt: 是否输出 SRT
            progress_callback: 进度回调 (current, total, message)

        Returns:
            dict: {
                "text": 完整文本,
                "segments": [(start, end, text), ...],
                "language": 语言代码,
                "duration": 音频时长,
                "txt_path": TXT路径,
                "srt_path": SRT路径,
            }
        """
        audio_path = os.path.abspath(audio_path)
        if not os.path.exists(audio_path):
            raise FileNotFoundError(f"文件不存在: {audio_path}")

        file_ext = Path(audio_path).suffix.lower()
        stem = Path(audio_path).stem

        # 如果是视频文件，先提取音频
        temp_audio = None
        if file_ext in SUPPORTED_VIDEO_EXT:
            if progress_callback:
                progress_callback(0, 100, "提取音频中...")
            temp_audio = extract_audio_from_video(audio_path, progress_callback=progress_callback)
            actual_audio = temp_audio
        elif file_ext in SUPPORTED_AUDIO_EXT or file_ext == ".wav":
            actual_audio = audio_path
        else:
            raise ValueError(f"不支持的文件格式: {file_ext}")

        # 确定输出目录
        if output_dir is None:
            output_dir = str(Path(audio_path).parent)
        os.makedirs(output_dir, exist_ok=True)

        # 加载模型
        self.load_model(progress_callback)

        # 获取时长
        duration = get_media_duration(actual_audio)
        duration_str = f"{int(duration // 60)}分{int(duration % 60)}秒"
        print(f"\n开始转录: {Path(audio_path).name}")
        print(f"音频时长: {duration_str}")

        if progress_callback:
            progress_callback(5, 100, f"开始转录 ({duration_str})...")

        # 执行转录
        start_time = time.time()
        segments_iter, info = self.model.transcribe(
            actual_audio,
            language=self.language,
            beam_size=self.beam_size,
            vad_filter=self.vad_filter,
            vad_parameters=dict(
                min_silence_duration_ms=2000,
                speech_pad_ms=400,
            ),
            condition_on_previous_text=False,
        )

        detected_lang = info.language
        lang_prob = info.language_probability
        print(f"检测语言: {detected_lang} (置信度: {lang_prob:.2%})")

        # 收集所有段落
        segments = []
        full_text_parts = []

        # tqdm 进度条（按音频秒数跟踪）
        pbar = None
        if HAS_TQDM and duration > 0:
            pbar = tqdm(total=int(duration), unit="s", desc="转录中",
                        bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt}s [{elapsed}<{remaining}, {rate_fmt}]")
            last_pos = 0

        for seg in segments_iter:
            segments.append((seg.start, seg.end, seg.text.strip()))
            full_text_parts.append(seg.text.strip())

            # 更新 tqdm
            if pbar is not None:
                new_pos = min(int(seg.end), int(duration))
                pbar.update(new_pos - last_pos)
                last_pos = new_pos

            # 更新进度回调（GUI用）
            if duration > 0 and progress_callback:
                progress = min(95, int(5 + 90 * seg.end / duration))
                progress_callback(
                    progress, 100,
                    f"转录中 {format_timestamp_readable(seg.end)}/{duration_str}..."
                )

            # 无tqdm时的回退打印
            if pbar is None and len(segments) % 10 == 0:
                print(f"  已转录 {len(segments)} 段, 到 {format_timestamp_readable(seg.end)}")

        if pbar is not None:
            pbar.update(int(duration) - last_pos)  # 补齐到100%
            pbar.close()

        elapsed = time.time() - start_time
        full_text = "\n".join(full_text_parts)
        speed_ratio = duration / elapsed if elapsed > 0 else 0

        print(f"\n转录完成!")
        print(f"  总段数: {len(segments)}")
        print(f"  耗时: {elapsed:.1f}秒 (速度: {speed_ratio:.1f}x 实时)")
        print(f"  总字符数: {len(full_text)}")

        result = {
            "text": full_text,
            "segments": segments,
            "language": detected_lang,
            "duration": duration,
            "elapsed": elapsed,
            "txt_path": None,
            "srt_path": None,
        }

        # 保存 TXT
        if output_txt:
            txt_path = os.path.join(output_dir, f"{stem}.txt")
            with open(txt_path, "w", encoding="utf-8") as f:
                # 带时间戳的文本
                for start, end, text in segments:
                    f.write(f"[{format_timestamp_readable(start)}-{format_timestamp_readable(end)}] {text}\n")
                f.write(f"\n{'='*60}\n")
                f.write(f"纯文本:\n{'='*60}\n\n")
                f.write(full_text)
            result["txt_path"] = txt_path
            print(f"  TXT 已保存: {txt_path}")

        # 保存 SRT
        if output_srt:
            srt_path = os.path.join(output_dir, f"{stem}.srt")
            with open(srt_path, "w", encoding="utf-8") as f:
                for i, (start, end, text) in enumerate(segments, 1):
                    f.write(f"{i}\n")
                    f.write(f"{format_timestamp_srt(start)} --> {format_timestamp_srt(end)}\n")
                    f.write(f"{text}\n\n")
            result["srt_path"] = srt_path
            print(f"  SRT 已保存: {srt_path}")

        # 清理临时音频
        if temp_audio and os.path.exists(temp_audio):
            try:
                os.remove(temp_audio)
            except OSError:
                pass

        if progress_callback:
            progress_callback(100, 100, "转录完成")

        return result


def batch_transcribe(
    input_dir: str,
    output_dir: str = None,
    model_size: str = DEFAULT_MODEL_SIZE,
    device: str = DEFAULT_DEVICE,
    compute_type: str = DEFAULT_COMPUTE_TYPE,
    language: str = DEFAULT_LANGUAGE,
    progress_callback: Optional[Callable] = None,
) -> List[dict]:
    """
    批量转录目录中的所有视频/音频文件

    Args:
        input_dir: 输入目录（递归搜索）
        output_dir: 输出目录（默认 input_dir/transcripts/）
        model_size: Whisper 模型大小
        device: 设备
        compute_type: 计算类型
        language: 语言
        progress_callback: 进度回调 (current, total, message)

    Returns:
        List[dict]: 每个文件的转录结果
    """
    input_dir = os.path.abspath(input_dir)
    if output_dir is None:
        output_dir = os.path.join(input_dir, "transcripts")
    os.makedirs(output_dir, exist_ok=True)

    # 收集文件
    media_files = []
    all_ext = SUPPORTED_VIDEO_EXT | SUPPORTED_AUDIO_EXT | {".wav"}
    for root, dirs, files in os.walk(input_dir):
        # 跳过输出目录
        if "transcripts" in root or "extracted_ppt" in root:
            continue
        for f in sorted(files):
            if Path(f).suffix.lower() in all_ext:
                media_files.append(os.path.join(root, f))

    if not media_files:
        print(f"在 {input_dir} 中未找到媒体文件")
        return []

    print(f"{'=' * 60}")
    print(f"批量音频转录 (GPU加速)")
    print(f"共找到 {len(media_files)} 个文件")
    print(f"模型: {model_size}, 设备: {device}")
    print(f"{'=' * 60}")
    for i, f in enumerate(media_files):
        print(f"  [{i + 1}] {Path(f).name}")
    print()

    # 创建转录器（模型只加载一次）
    transcriber = AudioTranscriber(
        model_size=model_size,
        device=device,
        compute_type=compute_type,
        language=language,
    )
    transcriber.load_model(progress_callback)

    results = []
    for i, filepath in enumerate(media_files):
        name = Path(filepath).stem
        file_output_dir = os.path.join(output_dir, name)

        print(f"\n{'=' * 60}")
        print(f"[{i + 1}/{len(media_files)}] {Path(filepath).name}")
        print(f"{'=' * 60}")

        # 检查是否已转录
        txt_path = os.path.join(file_output_dir, f"{name}.txt")
        if os.path.exists(txt_path):
            print(f"  已存在转录文件，跳过: {txt_path}")
            results.append({"file": filepath, "status": "跳过(已存在)", "elapsed": 0})
            continue

        start = time.time()
        try:
            def file_progress(current, total, msg,
                              file_idx=i, file_count=len(media_files)):
                if progress_callback:
                    overall = int((file_idx / file_count + current / total / file_count) * 100)
                    progress_callback(overall, 100, f"[{file_idx+1}/{file_count}] {msg}")

            result = transcriber.transcribe_file(
                filepath,
                output_dir=file_output_dir,
                progress_callback=file_progress,
            )
            elapsed = time.time() - start
            result["file"] = filepath
            result["status"] = f"成功 ({len(result['segments'])}段)"
            result["elapsed"] = elapsed
            results.append(result)
            print(f"  耗时: {elapsed:.1f}秒")

        except Exception as e:
            elapsed = time.time() - start
            print(f"  转录失败: {e}")
            results.append({"file": filepath, "status": f"失败: {e}", "elapsed": elapsed})

    # 汇总
    print(f"\n{'=' * 60}")
    print(f"批量转录完成!")
    print(f"{'=' * 60}")
    for r in results:
        name = Path(r["file"]).name
        t = f" ({r['elapsed']:.0f}s)" if r["elapsed"] > 0 else ""
        print(f"  {name}: {r['status']}{t}")
    print(f"\n输出目录: {output_dir}")

    return results


def main():
    parser = argparse.ArgumentParser(
        description="音频转录器 - GPU加速版本 (faster-whisper)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python audio_transcriber_gpu.py video.mp4
  python audio_transcriber_gpu.py video.mp4 -m large-v3 -l zh
  python audio_transcriber_gpu.py folder/ --batch -o output/transcripts
  python audio_transcriber_gpu.py audio.wav --device cpu --compute-type int8
        """,
    )

    parser.add_argument("input", help="视频/音频文件路径，或目录（配合 --batch）")
    parser.add_argument("-o", "--output", default=None, help="输出目录")
    parser.add_argument(
        "-m", "--model", default=DEFAULT_MODEL_SIZE,
        help=f"Whisper 模型 (tiny/base/small/medium/large-v3，默认: {DEFAULT_MODEL_SIZE})"
    )
    parser.add_argument(
        "-l", "--language", default=DEFAULT_LANGUAGE,
        help=f"语言代码 (默认: {DEFAULT_LANGUAGE})"
    )
    parser.add_argument(
        "--device", default=DEFAULT_DEVICE,
        help=f"设备 (auto/cuda/cpu，默认: {DEFAULT_DEVICE})"
    )
    parser.add_argument(
        "--compute-type", default=DEFAULT_COMPUTE_TYPE,
        help=f"计算类型 (float16/int8/float32，默认: {DEFAULT_COMPUTE_TYPE})"
    )
    parser.add_argument("--batch", action="store_true", help="批量模式：递归处理目录中的所有文件")
    parser.add_argument("--no-txt", action="store_true", help="不输出 TXT 文件")
    parser.add_argument("--no-srt", action="store_true", help="不输出 SRT 文件")
    parser.add_argument("--no-vad", action="store_true", help="不使用 VAD 过滤")

    args = parser.parse_args()

    if not check_ffmpeg():
        print("错误: FFmpeg 未安装或不在 PATH 中")
        print("请安装 FFmpeg: https://ffmpeg.org/download.html")
        sys.exit(1)

    print("=" * 60)
    print("音频转录器 - GPU加速版本")
    print("=" * 60)
    print(f"输入: {args.input}")
    print(f"模型: {args.model}")
    print(f"设备: {args.device}")
    print(f"语言: {args.language}")
    print("=" * 60)

    if args.batch or os.path.isdir(args.input):
        results = batch_transcribe(
            args.input,
            output_dir=args.output,
            model_size=args.model,
            device=args.device,
            compute_type=args.compute_type,
            language=args.language,
        )
        return 0 if results else 1
    else:
        transcriber = AudioTranscriber(
            model_size=args.model,
            device=args.device,
            compute_type=args.compute_type,
            language=args.language,
            vad_filter=not args.no_vad,
        )
        result = transcriber.transcribe_file(
            args.input,
            output_dir=args.output,
            output_txt=not args.no_txt,
            output_srt=not args.no_srt,
        )
        return 0 if result["segments"] else 1


if __name__ == "__main__":
    sys.exit(main())
