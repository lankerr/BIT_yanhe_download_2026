"""CPU-first VGA extraction. No CUDA/OpenCV-CUDA dependency in the default path.

The decoder emits small frames; a streaming state machine keeps stable content,
collapses additive builds, and bounds moving-video output. Full-size decoding is
done only for retained pages. GPU/keyframe/segment modes are opt-in experiments.
"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, asdict, replace
from pathlib import Path
import json
import subprocess
import time
import threading
import queue
import re
from collections import deque
import cv2
import numpy as np
from screen_content import content_box,content_pair,clean_delta,classify,context_equal,number_changed,heading_equal,OTHER_DIR

try:
    from app_paths import ffmpeg_path, ffprobe_path
except ImportError:
    def ffmpeg_path(): return "ffmpeg"
    def ffprobe_path(): return "ffprobe"


@dataclass(frozen=True)
class SlideConfig:
    width: int = 320
    fps: float = 2.0
    stable_seconds: float = 1.0
    stable_frac: float = .0015
    stable_mad: float = .8
    pixel_delta: int = 24
    change_frac: float = .0025
    change_mad: float = 1.0
    duplicate_frac: float = .0008
    duplicate_mad: float = .75
    hash_distance: int = 16
    revisit_pixel_gate: float = 4.0
    ink_threshold: int = 185
    bright_threshold: int = 180
    bright_min_fraction: float = .30
    background_threshold: int = 160
    background_kernel: int = 31
    minimum_ink_fraction: float = .003
    build_retention: float = .98
    build_erased_max: float = .0005
    build_added_min: float = .002
    build_added_max: float = .12
    title_frac: float = .025
    title_height: float = .20
    motion_seconds: float = 3.0
    motion_max_pages: int = 1
    local_motion_changes: int = 6
    motion_context_height: float = .53
    motion_context_frac: float = .003
    fade_max_seconds: float = 8.0
    fade_reversal_fraction: float = .05
    fade_step_delta: int = 2
    threads: int = 0
    filter_threads: int = 1
    seek_workers: int = 4
    jpeg_quality: int = 95
    scale_flags: str = "fast_bilinear"
    sample_mode: str = "grid"
    sample_epsilon: float = .0001
    decoder: str = "cpu"
    fast_flags: bool = False
    lowres: int = 0
    keyframe_max_gap: float = .75
    seek_preroll_seconds: float = 2.0
    seek_retry_extra_seconds: float = 4.0
    seek_verify_mad: float = 10.0
    seek_verify_frac: float = .12
    auto_corner_mask: bool = False
    corner_size: float = .20
    corner_activity_fraction: float = .08
    corner_outside_fraction: float = .003
    corner_learning_seconds: float = 10.0
    # Round-3 additions. Existing profile thresholds above are frozen unchanged.
    tile_grid: int = 8
    tile_mad: float = 2.0
    tile_frac: float = .012
    cursor_component_pixels: int = 24  # measured at the 320x180 reference size
    cursor_component_extent: int = 9
    cursor_max_components: int = 2
    footer_top: float = .85
    footer_right: float = .82
    number_component_width: float = .12
    number_component_height: float = .12
    number_marker_pixels: int = 24
    border_black: int = 32
    border_active_fraction: float = .20
    class_aspect_min: float = 1.64
    class_aspect_max: float = 1.92
    class_bright_level: int = 160
    class_bright_fraction: float = .45
    class_chrome_height: float = .22
    class_chrome_delta: int = 18
    class_chrome_width: float = .65
    class_chrome_bands: int = 3
    class_taskbar_height: float = .15
    class_taskbar_level: int = 100
    class_taskbar_fraction: float = .28
    context_title_height: float = .20
    context_change_fraction: float = .18
    native_workers: int = 4
    native_chunk_seconds: float = 20.0
    video_min_seconds: float = 8.0
    video_motion_fraction: float = .12
    video_high_rate: float = .80
    video_quiet_seconds: float = 1.0
    video_max_pages: int = 2
    # Normalized (x0,y0,x1,y1); never automatically crop slide content by default.
    masks: tuple = ()


PROFILES = {
    "fast": SlideConfig(fps=1.0, stable_seconds=1.5, seek_workers=4),
    "balanced": SlideConfig(),
    "thorough": SlideConfig(width=480, fps=5.0, stable_seconds=.6,
                             change_frac=.002, duplicate_frac=.0006),
}


def profile_config(profile="balanced", **overrides):
    if profile not in PROFILES:
        raise ValueError(f"Unknown slide profile: {profile}")
    cfg = replace(PROFILES[profile], **overrides)
    if cfg.fps <= 0 or cfg.width < 32 or cfg.threads < 0 or cfg.seek_workers < 1:
        raise ValueError("fps, width and worker counts must be positive")
    if cfg.stable_seconds < 0 or cfg.motion_max_pages < 1:
        raise ValueError("Invalid temporal settings")
    if cfg.native_chunk_seconds<=0 or cfg.native_workers<1:
        raise ValueError('Native interval and worker count must be positive')
    if cfg.background_kernel<1 or cfg.background_kernel%2!=1 or not 0<cfg.corner_size<.5:
        raise ValueError('Invalid background kernel or corner geometry')
    for x0,y0,x1,y1 in cfg.masks:
        if not (0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1):
            raise ValueError("Mask coordinates must be normalized rectangles")
    return cfg


def probe(path):
    r = subprocess.run([ffprobe_path(), "-v", "error", "-select_streams", "v:0",
                        "-show_entries", "stream=width,height,avg_frame_rate,start_time:format=duration",
                        "-of", "json", str(path)], capture_output=True, timeout=30)
    if r.returncode:
        raise RuntimeError(r.stderr.decode("utf-8", errors="replace")[-2000:])
    info = json.loads(r.stdout)
    if not info.get("streams"):
        raise ValueError("Video has no video stream")
    s = info["streams"][0]
    return {"width": int(s["width"]), "height": int(s["height"]),
            "duration": float(info["format"].get("duration", 0)),
            "start_time": float(s.get("start_time", 0))}


def wait_for_gpu(max_used_mb=1536, timeout=300):
    """Check BEFORE creating a CUDA context. No silent CPU substitution in benches."""
    start = time.monotonic()
    while True:
        r = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                           capture_output=True, text=True, timeout=15)
        if r.returncode:
            raise RuntimeError("nvidia-smi failed: " + r.stderr)
        used = int(r.stdout.strip().splitlines()[0])
        if used <= max_used_mb:
            return used
        if time.monotonic() - start > timeout:
            raise RuntimeError(f"GPU busy ({used} MB); GPU candidate not measured")
        print(f"GPU busy ({used} MB); waiting", flush=True)
        time.sleep(5)


def thumbnails(path, cfg, start=None, end=None, stats=None,video_info=None,phase_offset=None):
    info = video_info or probe(path)
    w = cfg.width
    h = max(2, round(info["height"] * w / info["width"] / 2) * 2)
    cmd = [ffmpeg_path(), "-hide_banner", "-loglevel", "info", "-nostats"]
    if cfg.decoder == "cuda":
        used = wait_for_gpu()
        if stats is not None:
            stats["gpu_used_before_mb"] = used
        cmd += ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"]
    elif cfg.decoder not in ("cpu","keyframes_ffmpeg"):
        raise ValueError("decoder must be cpu or cuda")
    cmd += ["-threads", str(cfg.threads)]
    if cfg.decoder=='keyframes_ffmpeg':
        cmd += ['-skip_frame','nokey']
    if cfg.fast_flags:
        cmd += ["-flags2", "+fast"]
    if cfg.lowres:
        cmd += ["-lowres", str(cfg.lowres)]
    if start is not None:
        cmd += ["-ss", f"{start:.6f}"]
    cmd += ["-i", str(path), "-an", "-sn", "-dn"]
    if end is not None:
        cmd += ["-t", f"{end - (start or 0):.6f}"]
    if cfg.decoder == "cuda":
        # fps first drops GPU frame references; scale BEFORE downloading to CPU.
        vf = f"scale_cuda={w}:{h}:format=nv12,hwdownload,format=nv12,format=gray"
    else:
        vf = f"scale={w}:{h}:flags={cfg.scale_flags},format=gray"
    if cfg.sample_mode=='grid':
        phase=(start or 0) if phase_offset is None else phase_offset
        vf=f"select='isnan(prev_selected_t)+gt(floor((t+{phase:.9f})*{cfg.fps}+{cfg.sample_epsilon}),floor((prev_selected_t+{phase:.9f})*{cfg.fps}+{cfg.sample_epsilon}))',"+vf
    elif cfg.sample_mode=='select':
        vf=f"select='isnan(prev_selected_t)+gte(t-prev_selected_t,{1/cfg.fps:.9f})',"+vf
    elif cfg.sample_mode=='keyframes':
        # Decoder skip is set separately before -i by the keyframe helper.
        pass
    else:
        vf=f"fps={cfg.fps},"+vf
    vf+=",showinfo"
    cmd += ["-filter_threads", str(cfg.filter_threads), "-vf", vf,"-fps_mode","passthrough",
            "-threads", "1", "-f", "rawvideo", "-"]
    if stats is not None:
        stats.setdefault("decode_commands", []).append(cmd)
    frame_bytes = w*h
    # Drain stderr concurrently and pair each small frame with its real source
    # PTS. Ordinal/fps timestamps can choose the wrong frame in VFR animations.
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=frame_bytes*16)
    times=queue.Queue()
    errors=deque(maxlen=100)
    def drain():
        try:
            for rawline in p.stderr:
                line=rawline.decode('utf-8',errors='replace')
                m=re.search(r'\bpts_time:([-+\d.eE]+)',line)
                if m:
                    times.put(float(m[1]))
                elif 'showinfo' not in line:
                    errors.append(line)
        finally:
            times.put(None)
    reader=threading.Thread(target=drain,daemon=True)
    reader.start()
    n = 0
    try:
        while True:
            b = p.stdout.read(frame_bytes*16)
            if not b:
                break
            if len(b) % frame_bytes:
                raise RuntimeError("Truncated thumbnail stream")
            a = np.frombuffer(b, np.uint8).reshape(-1,h,w)
            for f in a:
                pts=times.get(timeout=60)
                if pts is None:
                    raise RuntimeError('Missing thumbnail PTS: '+''.join(errors)[-4000:])
                yield (start or 0) + pts, f
                n += 1
        rc = p.wait()
        if rc:
            raise RuntimeError("ffmpeg thumbnail decode failed: " + ''.join(errors)[-4000:])
        if not n:
            raise RuntimeError("Decoder produced no thumbnails")
    finally:
        p.stdout.close()
        if p.poll() is None:
            p.terminate()
        p.wait()
        reader.join(timeout=5)
        p.stderr.close()
    if stats is not None:
        stats["decoded_thumbnails"] = stats.get("decoded_thumbnails",0)+n


def key_thumbnails(path, cfg, stats=None):
    """Actual PTS, not ordinal/GOP assumptions. PyAV is lazy and optional."""
    import av
    info = probe(path)
    height = max(2, round(info["height"] * cfg.width / info["width"] / 2)*2)
    with av.open(str(path)) as container:
        s = container.streams.video[0]
        s.codec_context.skip_frame = "NONKEY"
        s.codec_context.thread_count = cfg.threads
        origin = float(s.start_time * s.time_base) if s.start_time is not None else 0
        n = 0
        for frame in container.decode(s):
            if frame.pts is None:
                continue
            t = float(frame.pts * frame.time_base) - origin
            a = frame.reformat(width=cfg.width, height=height, format="gray").to_ndarray()
            yield max(0,t), a
            n += 1
        if not n:
            raise RuntimeError("No keyframes decoded")
        if stats is not None:
            stats["decoded_keyframes"] = n


def phash_bits(gray):
    c = cv2.dct(cv2.resize(gray,(32,32),interpolation=cv2.INTER_AREA).astype(np.float32))[:16,:16].ravel()[1:]
    return np.packbits(c > np.median(c))


_POPCOUNT = np.array([int(i).bit_count() for i in range(256)], np.uint8)


def hamming(a,b):
    return int(_POPCOUNT[np.bitwise_xor(a,b)].sum())


def masked(gray, masks):
    if not masks:
        return gray
    a = gray.copy()
    h,w = a.shape
    for x0,y0,x1,y1 in masks:
        a[int(y0*h):int(y1*h),int(x0*w):int(x1*w)] = 255
    return a


def metrics(a,b,cfg):
    d = cv2.absdiff(a,b)
    return float(d.mean()), float((d > cfg.pixel_delta).mean())


def same_page(a,b,cfg):
    # Hash is a gate; pixel evidence must ALSO support equality. Similar-layout
    # slides with one changed equation must survive.
    d,protected=clean_delta(a,b,cfg)
    if protected:return False
    mad,frac = float(d.mean()),float((d>cfg.pixel_delta).mean())
    return mad <= cfg.duplicate_mad and frac <= cfg.duplicate_frac and not block_change(a,b,cfg)


def block_change(a,b,cfg):
    """Any meaningful tile vetoes equality, including a small footer number.

    Remove tiny compact cursor components away from the footer. Thresholds scale
    with image size; broad/long ink and persistent footer edits remain evidence.
    Stability uses the same veto so small real edits reach commit in the first place.
    """
    d,protected=clean_delta(a,b,cfg)
    if protected:return True
    if not d.any():return False
    for ys in np.array_split(np.arange(d.shape[0]),min(cfg.tile_grid,d.shape[0])):
        for xs in np.array_split(np.arange(d.shape[1]),min(cfg.tile_grid,d.shape[1])):
            part=d[ys[0]:ys[-1]+1,xs[0]:xs[-1]+1]
            if part.mean()>cfg.tile_mad or (part>cfg.pixel_delta).mean()>cfg.tile_frac:
                return True
    return False


def additive_build(a,b,cfg):
    # Conservative whiteboard/text build: dark ink retained, little erased,
    # same title. Other backgrounds and dense photographs are excluded.
    old_raw,new_raw=a<cfg.ink_threshold,b<cfg.ink_threshold
    # Most candidate pairs replace ink. Reject them BEFORE the more expensive
    # background estimate; repeated GIF cells should never look like writing.
    if np.count_nonzero(old_raw & ~new_raw)/a.size>cfg.build_erased_max:
        return False
    raw_added=np.count_nonzero(new_raw & ~old_raw)/a.size
    if not cfg.build_added_min<=raw_added<=cfg.build_added_max:
        return False
    if number_changed(a,b,cfg):return False
    if np.mean(a > cfg.bright_threshold) < cfg.bright_min_fraction or np.mean(b > cfg.bright_threshold) < cfg.bright_min_fraction:
        return False
    # Exclude black letterboxes: otherwise retained border pixels dominate ink
    # overlap and incorrectly merge unrelated same-layout lecture pages.
    kernel=(cfg.background_kernel,cfg.background_kernel)
    bright_background=(cv2.blur(a,kernel)>cfg.background_threshold)&(cv2.blur(b,kernel)>cfg.background_threshold)
    old,new = (a < cfg.ink_threshold)&bright_background,(b < cfg.ink_threshold)&bright_background
    old_count = old.sum()
    if old_count < a.size*cfg.minimum_ink_fraction:
        return False
    retained = np.count_nonzero(old & new) / old_count
    added = np.count_nonzero(new & ~old) / a.size
    erased=np.count_nonzero(old & ~new)/a.size
    th = max(1,round(a.shape[0]*cfg.title_height))
    _, title_frac = metrics(a[:th],b[:th],cfg)
    return (retained >= cfg.build_retention and erased<=cfg.build_erased_max and cfg.build_added_min <= added <= cfg.build_added_max
            and title_frac <= cfg.title_frac)


def _detect_stream(stream, cfg, stats=None):
    """Streaming stable-run detector; bounded memory independent of video length."""
    pages = []
    page_thumbs = []
    hashes = []
    run_a = None
    run_start = run_end = 0
    motion_start = None
    motion_last = None
    motion_first = None
    suppression = {"global_duplicates":0,"builds_replaced":0,"small_changes":0,"motion_segments":0,"fades_suppressed":0}
    masks = list(cfg.masks)
    corner_votes = np.zeros(4,dtype=np.int32)
    cs=cfg.corner_size
    corner_boxes = [(0,0,cs,cs),(1-cs,0,1,cs),(0,1-cs,cs,1),(1-cs,1-cs,1,1)]
    previous = None
    motion_context=None
    context_start=context_end=0
    context_changes=0
    local_motion=[]
    context_previous=None
    context_best=None
    motion_sign=None
    motion_active=motion_reversals=0

    def context_same(a,b):
        return context_equal(a,b,cfg)

    def close_context():
        if (motion_context is not None and context_changes>=cfg.local_motion_changes
                and context_end-context_start>=cfg.motion_seconds):
            local_motion.append((context_start,context_end,motion_context.copy(),context_best))

    def commit(a, t, start, end, kind="stable"):
        if page_thumbs:
            mad,frac = metrics(page_thumbs[-1],a,cfg)
            if same_page(page_thumbs[-1],a,cfg):
                suppression["small_changes"] += 1
                return
            if additive_build(page_thumbs[-1],a,cfg):
                page_thumbs[-1] = a.copy()
                hashes[-1] = phash_bits(a)
                pages[-1].update(timestamp=t,start=start,end=end,kind="build_final",
                    _thumb=a.copy(),
                    reference_thumb=cv2.resize(a,(32,18),interpolation=cv2.INTER_AREA).tobytes().hex())
                suppression["builds_replaced"] += 1
                return
        hs = phash_bits(a)
        for i,old_h in enumerate(hashes):
            # Builds may be interrupted by a brief slide revisit. Replace older
            # incomplete states too; revisiting a subset of the final page adds
            # no useful new output.
            hh=max(1,round(a.shape[0]*cfg.motion_context_height))
            cmad,cfrac=metrics(page_thumbs[i][:hh],a[:hh],cfg)
            if cmad<=cfg.stable_mad and cfrac<=cfg.motion_context_frac:
                if additive_build(page_thumbs[i],a,cfg):
                    page_thumbs[i]=a.copy()
                    hashes[i]=hs
                    pages[i].update(timestamp=t,start=start,end=end,kind="build_final",
                        _thumb=a.copy(),
                        reference_thumb=cv2.resize(a,(32,18),interpolation=cv2.INTER_AREA).tobytes().hex())
                    suppression["builds_replaced"]+=1
                    return
                if additive_build(a,page_thumbs[i],cfg):
                    suppression["global_duplicates"]+=1
                    return
            if (hamming(hs,old_h) <= cfg.hash_distance or metrics(page_thumbs[i],a,cfg)[0]<=cfg.revisit_pixel_gate) and same_page(page_thumbs[i],a,cfg):
                suppression["global_duplicates"] += 1
                return
        pages.append({"timestamp":float(t),"start":float(start),"end":float(end),"first_seen":float(start),"kind":kind,"_thumb":a.copy(),
                      "reference_thumb":cv2.resize(a,(32,18),interpolation=cv2.INTER_AREA).tobytes().hex()})
        page_thumbs.append(a.copy())
        hashes.append(hs)

    def flush_run():
        if run_a is not None and run_end-run_start+1/cfg.fps >= cfg.stable_seconds:
            commit(run_a,(run_start+run_end)/2,run_start,run_end)

    def flush_motion(end):
        if motion_start is not None and end-motion_start >= cfg.motion_seconds:
            # A dissolve moves each pixel monotonically toward the next slide.
            # Moving video reverses many pixel directions. Never save fade halves.
            if (end-motion_start<=cfg.fade_max_seconds and motion_active>0
                    and motion_reversals/motion_active<=cfg.fade_reversal_fraction):
                suppression['fades_suppressed']+=1
                return
            suppression["motion_segments"] += 1
            t,a = motion_first
            commit(a,t,motion_start,end,"motion_start")
            if cfg.motion_max_pages > 1 and motion_last is not None:
                t,a = motion_last
                commit(a,t,motion_start,end,"motion_end")

    last_t = None
    for t,raw in stream:
        if last_t is not None and t < last_t:
            raise ValueError("Thumbnail timestamps are not ordered")
        if cfg.auto_corner_mask and previous is not None:
            delta = cv2.absdiff(previous,raw)>cfg.pixel_delta
            # Evidence only: sustained dense motion in a small corner while the
            # rest is quiet. Explicit masks are preferred for known camera layouts.
            for i,(x0,y0,x1,y1) in enumerate(corner_boxes):
                hh,ww = delta.shape
                zone = delta[int(y0*hh):int(y1*hh),int(x0*ww):int(x1*ww)]
                outside = delta.copy()
                outside[int(y0*hh):int(y1*hh),int(x0*ww):int(x1*ww)] = False
                if zone.mean()>cfg.corner_activity_fraction and outside.mean()<cfg.corner_outside_fraction:
                    corner_votes[i] += 1
                    if corner_votes[i]>=cfg.fps*cfg.corner_learning_seconds and corner_boxes[i] not in masks:
                        masks.append(corner_boxes[i])
                        if run_a is not None:
                            run_a = masked(run_a,masks)
                        page_thumbs[:] = [masked(a,masks) for a in page_thumbs]
                        hashes[:] = [phash_bits(a) for a in page_thumbs]
            previous = raw.copy()
        elif cfg.auto_corner_mask:
            previous = raw.copy()
        a = masked(raw,masks)
        if motion_context is None:
            motion_context=a.copy()
            context_start=context_end=t
            context_changes=0
            context_best=(t,a.copy(),int(np.count_nonzero(a<cfg.ink_threshold)))
        elif context_same(motion_context,a):
            context_end=t
            ink=int(np.count_nonzero(a<cfg.ink_threshold))
            if ink>context_best[2]:
                context_best=(t,a.copy(),ink)
            if context_previous is not None:
                mad_step,frac_step=metrics(context_previous,a,cfg)
                if mad_step>cfg.stable_mad or frac_step>cfg.stable_frac:
                    context_changes+=1
        else:
            close_context()
            motion_context=a.copy()
            context_start=context_end=t
            context_changes=0
            context_best=(t,a.copy(),int(np.count_nonzero(a<cfg.ink_threshold)))
        context_previous=a.copy()
        if run_a is None:
            run_a,run_start,run_end = a.copy(),t,t
            last_t=t
            continue
        mad,frac = metrics(run_a,a,cfg)
        stable = mad <= cfg.stable_mad and frac <= cfg.stable_frac and not block_change(run_a,a,cfg)
        if stable:
            run_end=t
            if motion_start is not None and t-run_start+1/cfg.fps>=cfg.stable_seconds:
                flush_motion(run_start)
                motion_start=motion_last=motion_first=None
                motion_sign=None
                motion_active=motion_reversals=0
        else:
            was_stable = run_end-run_start+1/cfg.fps >= cfg.stable_seconds
            if was_stable:
                flush_run()
                # A single cut is not moving video. Motion starts at this cut
                # and is canceled if a stable new run follows promptly.
                if motion_start is None:
                    motion_start=t
                    motion_first=(t,a.copy())
                    motion_active=motion_reversals=0
                    motion_sign=np.zeros(a.shape,np.int8)
            elif motion_start is None:
                motion_start=run_start
                motion_first=(run_start,run_a.copy())
                motion_active=motion_reversals=0
                motion_sign=np.zeros(a.shape,np.int8)
            if motion_last is not None and motion_sign is not None:
                step=a.astype(np.int16)-motion_last[1].astype(np.int16)
                active=np.abs(step)>cfg.fade_step_delta
                sign=np.sign(step).astype(np.int8)
                motion_active+=int(active.sum())
                motion_reversals+=int(np.count_nonzero(active & (motion_sign!=0) & (sign!=motion_sign)))
                motion_sign[active]=sign[active]
            motion_last=(t,a.copy())
            run_a,run_start,run_end=a.copy(),t,t
        last_t=t
    if run_a is not None:
        flush_motion(run_start)
        flush_run()
    close_context()
    # Slow looping GIFs can have one-second pauses. Continuous-motion detection
    # alone treats those pauses as static pages. Group only ranges with repeated
    # motion AND unchanged surrounding text, then cap representatives globally.
    clusters=[]
    for lo,hi,a,best in local_motion:
        for c in clusters:
            if context_same(c['thumb'],a):
                c['ranges'].append((lo,hi))
                if best[2]>c['best'][2]: c['best']=best
                break
        else:
            clusters.append({'thumb':a,'ranges':[(lo,hi)],'best':best})
    remove=set()
    for c in clusters:
        indices=[i for i,p in enumerate(pages) if context_same(page_thumbs[i],c['thumb'])
                 ]
        indices.sort(key=lambda i:pages[i]['timestamp'])
        keep=indices[:1]+indices[-1:] if cfg.motion_max_pages>1 else indices[:1]
        if indices:
            # On looping builds a full drawing may last < stable_seconds. The
            # representative is the observed frame with most ink in the same
            # unchanged context, so a completed sum/output diagram wins over an
            # empty intermediate state. This never synthesizes a frame.
            t,a,_=c['best']
            target=keep[-1]
            pages[target].update(timestamp=t,kind='motion_representative',
                _thumb=a.copy(),
                reference_thumb=cv2.resize(a,(32,18),interpolation=cv2.INTER_AREA).tobytes().hex())
        for i in indices:
            if i not in keep:
                remove.add(i)
    if stats is not None:
        stats['local_motion_ranges']=[{'start':lo,'end':hi} for lo,hi,_,_ in local_motion]
        stats['motion_pages_suppressed']=len(remove)
    pages=[p for i,p in enumerate(pages) if i not in remove]
    if stats is not None:
        stats.update(suppression)
        stats["masks_used"] = masks
    # A completed build may be selected from a later revisit. Keep the course's
    # first-appearance order rather than moving that page into a later topic.
    return sorted(pages,key=lambda p:p["first_seen"])


def detect_stream(stream,cfg,stats=None):
    """Observe sustained broad motion independently of slide/global hash logic.

    Bounded frame memory: first and sharpest actually observed frames per run.
    Removed candidates and source timestamps remain recoverable from the source.
    No optional classifier/model is loaded by the default CPU implementation.
    """
    cv2.setNumThreads(1)
    stats=stats if stats is not None else {}
    segments=[]
    state={}
    previous=None

    def close(end):
        if not state:return
        duration=end-state['start']
        rate=state['high']/max(1,state['steps'])
        if duration>=cfg.video_min_seconds and rate>=cfg.video_high_rate:
            segments.append(dict(start=state['start'],end=end,high_rate=rate,
                                 first=state['first'],best=state['best'],observed=state['observed']))
        state.clear()

    def observe():
        nonlocal previous
        for t,raw in stream:
            a=masked(raw,cfg.masks)
            if previous is not None:
                pt,pa=previous
                _,frac=metrics(pa,a,cfg)
                high=frac>=cfg.video_motion_fraction
                if high and not state:
                    state.update(start=t,first=(t,a.copy()),best=(t,a.copy(),-1),
                                 steps=0,high=0,quiet=None,observed=[])
                if state:
                    if high:
                        state['quiet']=None
                        sharp=float(cv2.Laplacian(a,cv2.CV_32F).var())
                        if sharp>state['best'][2]:state['best']=(t,a.copy(),sharp)
                    elif state['quiet'] is None:state['quiet']=pt
                    if state['quiet'] is not None and t-state['quiet']>=cfg.video_quiet_seconds:
                        close(state['quiet'])
                    else:
                        state['steps']+=1;state['high']+=int(high)
                        state['observed'].append(float(t))
            previous=(t,a.copy())
            yield t,raw
        if previous is not None:close(state.get('quiet') if state.get('quiet') is not None else previous[0]+1/cfg.fps)

    pages=_detect_stream(observe(),cfg,stats)
    removed=[];public_segments=[]
    for index,s in enumerate(segments):
        keep=[]
        for p in pages:
            if s['start']<=p['timestamp']<s['end']:
                removed.append(dict({k:v for k,v in p.items() if k!='_thumb'},reason='video_segment',segment=index))
            else:keep.append(p)
        pages=keep
        representatives=[s['first']]
        if cfg.video_max_pages>1 and abs(s['best'][0]-s['first'][0])>=1/cfg.fps:
            representatives.append(s['best'][:2])
        for t,a in representatives[:cfg.video_max_pages]:
            pages.append(dict(timestamp=float(t),start=float(s['start']),end=float(s['end']),
                first_seen=float(t),kind='video',video_segment=index,
                _thumb=a.copy(),
                reference_thumb=cv2.resize(a,(32,18),interpolation=cv2.INTER_AREA).tobytes().hex()))
        public_segments.append({k:v for k,v in s.items() if k not in ('first','best')})
    stats.update(video_segments=public_segments,removed=removed,
                 removed_basis='Retained source video/TS + exact timestamps; no source is deleted.')
    import base64
    final=[]
    for p in pages:
        a=p.pop('_thumb')
        category,reason,features=classify(a,cfg,p['kind'])
        p.update(category=category,classification_reason=reason,class_features=features)
        if category=='other' and p['kind']=='motion_start':
            stats['removed'].append(dict(p,reason='other_not_stable'))
            continue
        p['analysis_thumb']=base64.b64encode(cv2.imencode('.png',a)[1]).decode('ascii')
        final.append(p)
    return sorted(final,key=lambda p:p['first_seen'])


def detect_hybrid(path, cfg, stats=None,key_backend="pyav"):
    """Keyframe proposals + fine decoding around every detected change.

    It cannot prove absence of a brief between-keyframes slide. Fall back to full
    sampling if GOP exceeds the explicitly configured safety bound.
    """
    keys = list(thumbnails(path,replace(cfg,decoder='keyframes_ffmpeg',sample_mode='keyframes'),stats=stats)
                if key_backend=='ffmpeg' else key_thumbnails(path,cfg,stats))
    gaps = np.diff([t for t,_ in keys])
    gap = float(gaps.max()) if len(gaps) else 0
    if stats is not None:
        stats["gop_max_seconds"] = gap
    if gap > cfg.keyframe_max_gap:
        if stats is not None:
            stats["keyframe_fallback"] = "GOP exceeds safety bound; full sampling"
        return detect_stream(thumbnails(path,cfg,stats=stats),cfg,stats)
    intervals=[]
    for (t0,a),(t1,b) in zip(keys,keys[1:]):
        mad,frac=metrics(masked(a,cfg.masks),masked(b,cfg.masks),cfg)
        if mad>cfg.stable_mad or frac>cfg.stable_frac:
            lo,hi=max(0,t0-1/cfg.fps),t1+cfg.stable_seconds
            if intervals and lo<=intervals[-1][1]:
                intervals[-1][1]=hi
            else:
                intervals.append([lo,hi])
    info=probe(path)
    fine=[]
    for lo,hi in intervals:
        fine.extend(thumbnails(path,cfg,start=lo,end=min(hi,info["duration"]),stats=stats))
    # All unchanged coarse spans remain represented. Fine points replace keys
    # only within rescanned intervals.
    combined=[(t,a) for t,a in keys if not any(lo<=t<hi for lo,hi in intervals)]+fine
    combined.sort(key=lambda x:x[0])
    if stats is not None:
        stats["fine_intervals"] = intervals
    return detect_stream(combined,cfg,stats)


def _segment_task(args):
    path,cfg,offset,info=args
    # Return ordered samples, NOT locally deduplicated pages: the parent state
    # machine handles stable runs/motion/builds across all boundaries.
    data=list(thumbnails(path,cfg,video_info=info,phase_offset=offset))
    return info["duration"],data


def segment_stream(paths,cfg,workers=4):
    """Bounded in-flight per-TS parallelism; caller supplies concat order.

    Offsets follow segment duration, including missing-source segments compressed
    out by concat. Do not re-sort filenames lexically.
    """
    paths=list(paths)
    # Each task launches its own FFmpeg PROCESS. Python threads only schedule
    # pipe reads, avoiding Windows sandbox-restricted multiprocessing named IPC.
    with ThreadPoolExecutor(max_workers=workers) as pool:
        infos=list(pool.map(probe,paths))
        jobs=[];base=0.
        for path,info in zip(paths,infos):
            jobs.append((path,cfg,base,info));base+=info['duration']
        source=iter(jobs)
        pending=deque()
        offset=0;last_bucket=-1
        for _ in range(workers):
            try: path=next(source)
            except StopIteration: break
            pending.append(pool.submit(_segment_task,path))
        while pending:
            duration,data=pending.popleft().result()
            for t,a in data:
                bucket=int(np.floor((offset+t)*cfg.fps+cfg.sample_epsilon))
                if cfg.sample_mode!='grid' or bucket>last_bucket:
                    yield offset+t,a
                    last_bucket=bucket
            offset+=duration
            try: path=next(source)
            except StopIteration: continue
            pending.append(pool.submit(_segment_task,path))


def detect_pages(path,profile="balanced",config=None,stats=None,segments=None):
    cfg=config or profile_config(profile)
    cv2.setNumThreads(1)
    if stats is not None:
        stats["config"]=asdict(cfg)
    if segments is not None:
        return detect_stream(segment_stream(segments,cfg),cfg,stats)
    if cfg.decoder == "keyframes":
        return detect_stream(key_thumbnails(path,cfg,stats),cfg,stats)
    if cfg.decoder in ("hybrid","hybrid_ffmpeg"):
        return detect_hybrid(path,replace(cfg,decoder="cpu"),stats,
                             key_backend='ffmpeg' if cfg.decoder=='hybrid_ffmpeg' else 'pyav')
    return detect_stream(thumbnails(path,cfg,stats=stats),cfg,stats)


def native_chunk_stream(path,cfg):
    """Independent native decoder processes over bounded MP4 time intervals.

    Used when the downloader retained only MP4. No temporary video copies.
    Source PTS and one parent detector state handle every interval boundary.
    """
    info=probe(path);duration=info['duration']
    jobs=iter((float(lo),min(duration,float(lo)+cfg.native_chunk_seconds))
              for lo in np.arange(0,duration,cfg.native_chunk_seconds))
    def decode(bounds):
        lo,hi=bounds
        return list(thumbnails(path,cfg,start=lo,end=hi,video_info=info))
    with ThreadPoolExecutor(max_workers=cfg.native_workers) as pool:
        pending=deque()
        for _ in range(cfg.native_workers):
            try:pending.append(pool.submit(decode,next(jobs)))
            except StopIteration:break
        last_bucket=-1
        while pending:
            for t,a in pending.popleft().result():
                bucket=int(np.floor(t*cfg.fps+cfg.sample_epsilon))
                if cfg.sample_mode!='grid' or bucket>last_bucket:
                    yield t,a;last_bucket=bucket
            try:pending.append(pool.submit(decode,next(jobs)))
            except StopIteration:pass


def detect_native(path,profile='balanced',config=None,stats=None,segments=None):
    cfg=config or profile_config(profile);cv2.setNumThreads(1)
    if stats is not None:stats.update(config=asdict(cfg),native_source='ts_files' if segments else 'mp4_time_intervals')
    return detect_stream(segment_stream(segments,cfg,cfg.native_workers) if segments
                         else native_chunk_stream(path,cfg),cfg,stats)


def discover_segments(path):
    """Only a same-stem sibling directory is auto-discovered; unrelated TS ignored."""
    p=Path(path);folder=p.parent/(p.stem+'_ts')
    if not folder.is_dir():return None
    paths=list(folder.glob('*.ts'))
    if not paths or any(not p.stem.isdigit() for p in paths):return None
    return sorted(paths,key=lambda p:int(p.stem))


def write_pages(path,output_dir,pages,cfg,stats=None):
    """One accurate input seek per final page, bounded parallel decode/JPEG encode."""
    info=probe(path)
    w,h=info["width"],info["height"]
    out=Path(output_dir)
    out.mkdir(parents=True,exist_ok=True)
    name=Path(path).stem

    def one(item):
        i,page=item
        t=min(max(0,page["timestamp"]),max(0,info["duration"]-.05))
        # Input -ss is accurate with transcoding (default accurate_seek).
        for attempt,preroll in enumerate([cfg.seek_preroll_seconds,cfg.seek_preroll_seconds+cfg.seek_retry_extra_seconds]):
            seek=max(0,t-preroll)
            cmd=[ffmpeg_path(),"-v","error","-threads","1","-ss",f"{seek:.6f}","-i",str(path),
                 "-ss",f"{t-seek:.6f}","-an","-sn","-dn","-frames:v","1","-threads","1","-f","rawvideo","-pix_fmt","bgr24","-"]
            r=subprocess.run(cmd,capture_output=True,timeout=60)
            if r.returncode or len(r.stdout)!=w*h*3:
                raise RuntimeError(f"Full-resolution seek failed at {t:.3f}s: " + r.stderr.decode("utf-8",errors="replace")[-1000:])
            a=np.frombuffer(r.stdout,np.uint8).reshape(h,w,3)
            if 'reference_thumb' not in page:
                break
            expected=np.frombuffer(bytes.fromhex(page['reference_thumb']),np.uint8).reshape(18,32)
            actual=cv2.resize(cv2.cvtColor(a,cv2.COLOR_BGR2GRAY),(32,18),interpolation=cv2.INTER_AREA)
            mad,frac=metrics(expected,actual,cfg)
            page['seek_verify_mad']=mad
            page['seek_retries']=attempt
            if mad<=cfg.seek_verify_mad and frac<=cfg.seek_verify_frac:
                break
        else:
            raise RuntimeError(f'Seek image differs from retained content at {t:.3f}s (MAD {mad:.2f})')
        ok,b=cv2.imencode(".jpg",a,[cv2.IMWRITE_JPEG_QUALITY,cfg.jpeg_quality])
        if not ok:
            raise RuntimeError("JPEG encoding failed")
        suffix='_video' if page.get('kind')=='video' else ''
        filename=f"{name}_slide_{i+1:03d}_{int(t//60):02d}m{int(t%60):02d}s{suffix}.jpg"
        dest=out/filename
        tmp=dest.with_suffix(".jpg.tmp")
        tmp.write_bytes(b.tobytes())
        tmp.replace(dest)
        return filename

    # executor.map is ordered; only worker-count full-size images are in flight.
    with ThreadPoolExecutor(max_workers=cfg.seek_workers) as pool:
        files=list(pool.map(one,enumerate(pages)))
    # Clean only this extractor's exact generated filenames, after all seeks
    # succeeded. Other jpgs and other video stems in the output dir are untouched.
    import re
    owned=re.compile(re.escape(name)+r'_slide_\d+_\d+m\d+s(?:_video)?\.jpg$')
    keep=set(files)
    for p in out.glob('*.jpg'):
        if owned.fullmatch(p.name) and p.name not in keep:
            p.unlink()
    if stats is not None:
        stats["output_files"]=files
    return len(files)


def write_split_pages(path,output_dir,pages,cfg,stats=None):
    stats=stats if stats is not None else {}
    primary=[p for p in pages if p.get('category','slide')=='slide']
    others=[p for p in pages if p.get('category')=='other']
    one={};two={}
    count=write_pages(path,output_dir,primary,cfg,one)
    write_pages(path,Path(output_dir)/OTHER_DIR,others,cfg,two)
    for p,f in zip(primary,one['output_files']):p['output_file']=f
    for p,f in zip(others,two['output_files']):p['output_file']=OTHER_DIR+'/'+f
    stats.update(output_files=one['output_files'],other_output_files=two['output_files'],
                 slide_count=count,other_count=len(others))
    return count


def extract_stable(path,output_dir,profile="balanced",config=None,engine='stable',segments=None):
    cfg=config or profile_config(profile)
    stats={"profile":profile}
    t0=time.perf_counter()
    if engine=='ts_parallel_native':
        pages=detect_native(path,profile,cfg,stats,segments or discover_segments(path))
    else:pages=detect_pages(path,profile,cfg,stats,segments)
    stats["detect_wall_s"]=time.perf_counter()-t0
    t1=time.perf_counter()
    count=write_split_pages(path,output_dir,pages,cfg,stats)
    stats["write_wall_s"]=time.perf_counter()-t1
    stats["total_wall_s"]=time.perf_counter()-t0
    stats["pages"]=pages
    # Extra sidecar is ignored by the existing jpg->pptx/pdf layout.
    (Path(output_dir)/"extraction.json").write_text(json.dumps(stats,ensure_ascii=False,indent=2),encoding="utf-8")
    print(f"[stable/{profile}] {count} pages, detection {stats['detect_wall_s']:.2f}s, output {stats['write_wall_s']:.2f}s",flush=True)
    return count


class SlideStream:
    """Incremental ordered TS arrivals, with one detector state across boundaries.

    feed_ts blocks until that segment's small frames have been consumed. Memory
    is bounded by 32 thumbnails plus detector candidates, never by lecture length.
    finish performs final dedup/video grouping and seeks only retained pages.
    Existing merged video is optional; without it, seek original TS files directly.
    All TS files must remain available until finish. No downloader is imported.
    """
    def __init__(self,output_dir,profile='balanced',config=None,video_path=None):
        self.output_dir=Path(output_dir)
        self.cfg=config or profile_config(profile)
        cv2.setNumThreads(1)
        self.video_path=Path(video_path) if video_path else None
        self.stats={'profile':profile,'config':asdict(self.cfg),'sources':[]}
        self._queue=queue.Queue(maxsize=32)
        self._offset=0.;self._sources=[];self._error=None;self._pages=None
        self._last_bucket=-1
        self._closed=False;self._started=time.perf_counter()
        def inputs():
            while True:
                item=self._queue.get()
                if item is None:return
                if isinstance(item,threading.Event):item.set();continue
                yield item
        def work():
            try:self._pages=detect_stream(inputs(),self.cfg,self.stats)
            except BaseException as exc:self._error=exc
        self._thread=threading.Thread(target=work,daemon=True)
        self._thread.start()

    def _put(self,item):
        while True:
            if self._error is not None:raise RuntimeError('Stream detector failed') from self._error
            try:self._queue.put(item,timeout=.1);return
            except queue.Full:continue

    def feed_ts(self,path):
        if self._closed:raise RuntimeError('Stream is finished')
        path=Path(path).resolve();info=probe(path)
        start=time.perf_counter();n=0
        for t,a in thumbnails(path,self.cfg,video_info=info,phase_offset=self._offset):
            bucket=int(np.floor((self._offset+t)*self.cfg.fps+self.cfg.sample_epsilon))
            if self.cfg.sample_mode!='grid' or bucket>self._last_bucket:
                self._put((self._offset+t,a.copy()));n+=1;self._last_bucket=bucket
        done=threading.Event();self._put(done)
        while not done.wait(.1):
            if self._error is not None:raise RuntimeError('Stream detector failed') from self._error
        record={'path':str(path),'offset':self._offset,'duration':info['duration'],
                'thumbnails':n,'feed_wall_s':time.perf_counter()-start}
        self.stats['sources'].append(record);self._sources.append(record)
        self._offset+=info['duration']
        return dict(record)

    def finish(self,write_output=True):
        if self._closed:raise RuntimeError('Stream is finished')
        self._closed=True;self._put(None);self._thread.join()
        if self._error is not None:raise RuntimeError('Stream detector failed') from self._error
        pages=self._pages
        self.stats['detect_wall_s']=time.perf_counter()-self._started
        start=time.perf_counter()
        if not write_output:
            self.output_dir.mkdir(parents=True,exist_ok=True)
            self.stats.update(output_files=[],other_output_files=[])
        elif self.video_path is not None:
            write_split_pages(self.video_path,self.output_dir,pages,self.cfg,self.stats)
        else:
            # No concat or re-decode of the full lecture at finish. Each retained
            # page resolves back to its original TS and local presentation time.
            self.output_dir.mkdir(parents=True,exist_ok=True)
            def output(item):
                i,p=item
                src=next(s for s in reversed(self._sources) if s['offset']<=p['timestamp'])
                local=dict(p,timestamp=p['timestamp']-src['offset'])
                target=self.output_dir/OTHER_DIR if p.get('category')=='other' else self.output_dir
                target.mkdir(parents=True,exist_ok=True)
                temp=target/f'.stream_page_{i:05d}'
                stat={}
                write_pages(src['path'],temp,[local],replace(self.cfg,seek_workers=1),stat)
                suffix='_video' if p.get('kind')=='video' else ''
                t=p['timestamp'];name=f'stream_slide_{i+1:03d}_{int(t//60):02d}m{int(t%60):02d}s{suffix}.jpg'
                (temp/stat['output_files'][0]).replace(target/name);temp.rmdir()
                p.update(source_path=src['path'],source_timestamp=local['timestamp'],
                         output_file=(OTHER_DIR+'/' if p.get('category')=='other' else '')+name)
                return p['output_file']
            with ThreadPoolExecutor(max_workers=self.cfg.seek_workers) as pool:
                files=list(pool.map(output,enumerate(pages)))
            for folder,prefix in [(self.output_dir,''),(self.output_dir/OTHER_DIR,OTHER_DIR+'/')]:
                for p in folder.glob('stream_slide_*.jpg'):
                    if re.fullmatch(r'stream_slide_\d+_\d+m\d+s(?:_video)?\.jpg',p.name) and prefix+p.name not in files:p.unlink()
            self.stats['output_files']=[f for f in files if '/' not in f]
            self.stats['other_output_files']=[f for f in files if '/' in f]
            self.stats.update(slide_count=len(self.stats['output_files']),other_count=len(self.stats['other_output_files']))
        self.stats.update(pages=pages,count=len(pages),write_wall_s=time.perf_counter()-start,
                          total_wall_s=time.perf_counter()-self._started)
        (self.output_dir/'extraction.json').write_text(json.dumps(self.stats,indent=2),encoding='utf-8')
        return pages
