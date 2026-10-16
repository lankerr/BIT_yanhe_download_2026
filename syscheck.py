"""--syscheck 系统自检：先写标准库基础报告，再运行可能崩溃的探针（子进程隔离）。

用法（由 app_full.py 在导入 GUI 前分流）：
    完整版.exe --syscheck            生成报告并弹窗告知路径
    完整版.exe --syscheck --output p  指定输出路径
    完整版.exe --probe-asr           内部探针：CT2/ORT 导入与支持精度（写 JSON 到环境
                                     YH_PROBE_OUT 指定文件；失败写 error 字段）

报告不含任何凭据/token；exe 体积较大时 SHA-256 计算约 1-2 秒。
"""

import ctypes
import hashlib
import json
import os
import platform
import subprocess
import sys
import time
from datetime import datetime

# Win32 处理器特性号（Win10 2004+ 可用；旧系统查询失败记 unknown）
_PF = {"SSE4.1": 37, "AVX": 39, "AVX2": 40, "AVX512F": 41}


def _cim(ws_class, props, ns="root/cimv2"):
    out = []
    try:
        ps = ["powershell", "-NoProfile", "-NonInteractive",
              f"Get-CimInstance -Namespace {ns} -ClassName {ws_class} "
              f"| Select-Object {','.join(props)} | ConvertTo-Json -Compress"]
        r = subprocess.run(ps, capture_output=True, timeout=30)
        if r.returncode == 0 and r.stdout.strip():
            data = json.loads(r.stdout.decode("utf-8", "replace"))
            return data if isinstance(data, list) else [data]
    except Exception:
        pass
    return out


def _probe_simd():
    res = {}
    try:
        k32 = ctypes.windll.kernel32
        for name, pf in _PF.items():
            try:
                res[name] = bool(k32.IsProcessorFeaturePresent(pf))
            except Exception:
                res[name] = "unknown"
    except Exception:
        for name in _PF:
            res[name] = "unknown"
    return res


def _probe_gpu():
    gpus = []
    for item in _cim("Win32_VideoController",
                     ["Name", "DriverVersion", "AdapterRAM", "PNPDeviceID"]):
        if isinstance(item, dict) and item.get("Name"):
            gpus.append({k: item.get(k) for k in
                         ("Name", "DriverVersion", "AdapterRAM", "PNPDeviceID")})
    return gpus


def _probe_cpu_mem():
    cpu = {"logical": os.cpu_count()}
    for item in _cim("Win32_Processor",
                     ["Name", "NumberOfCores", "NumberOfLogicalProcessors"]):
        if isinstance(item, dict) and item.get("Name"):
            cpu.update({k: item.get(k) for k in
                        ("Name", "NumberOfCores", "NumberOfLogicalProcessors")})
            break

    class _MS(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong)]
    try:
        ms = _MS()
        ms.dwLength = ctypes.sizeof(_MS)
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        if k32.GlobalMemoryStatusEx(ctypes.byref(ms)):
            cpu["mem_total_gb"] = round(ms.ullTotalPhys / 2**30, 1)
            cpu["mem_avail_gb"] = round(ms.ullAvailPhys / 2**30, 1)
            cpu["mem_load_pct"] = ms.dwMemoryLoad
    except Exception:
        pass
    if "mem_total_gb" not in cpu:
        # CIM 回退（TotalVisibleMemorySize/FreePhysicalMemory 单位 KB）
        for item in _cim("Win32_OperatingSystem",
                         ["TotalVisibleMemorySize", "FreePhysicalMemory"]):
            if isinstance(item, dict) and item.get("TotalVisibleMemorySize"):
                cpu["mem_total_gb"] = round(item["TotalVisibleMemorySize"] / 2**20, 1)
                cpu["mem_avail_gb"] = round(item.get("FreePhysicalMemory", 0) / 2**20, 1)
                break
    return cpu


def _probe_disk(paths):
    out = {}
    for label, p in paths.items():
        try:
            free = os.statvfs(p).f_bavail * os.statvfs(p).f_frsize / 2**30 if hasattr(os, "statvfs") else None
        except Exception:
            free = None
        try:
            probe = os.path.join(p, f".yh_probe_{os.getpid()}")
            with open(probe, "w") as f:
                f.write("ok")
            os.remove(probe)
            writable = True
        except Exception:
            writable = False
        out[label] = {"path": p, "free_gb": round(free, 1) if free else None,
                      "writable": writable}
    return out


def _probe_webview2():
    try:
        import winreg
        for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                key = winreg.OpenKey(root,
                    r"Software\Microsoft\EdgeUpdate\Clients"
                    r"\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}")
                return winreg.QueryValueEx(key, "pv")[0]
            except OSError:
                continue
    except Exception:
        pass
    return "not-found"


def _probe_av():
    return [a.get("displayName") for a in
            _cim("AntiVirusProduct", ["displayName", "productState"],
                 ns="root/SecurityCenter2") if isinstance(a, dict)]


def _probe_asr_child(timeout=90):
    """隔离子进程探测 CT2/ORT；结果经文件回传（windowed 下不能依赖 stdout）。"""
    tmp = os.path.join(os.environ.get("TEMP", "."), f"yh_probe_{os.getpid()}.json")
    env = dict(os.environ, YH_PROBE_OUT=tmp)
    try:
        subprocess.run([sys.executable, "--probe-asr"], env=env,
                       timeout=timeout,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        with open(tmp, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def run_probe_asr():
    """--probe-asr 分支：在独立进程里导入重依赖并写结果。"""
    out_path = os.environ.get("YH_PROBE_OUT")
    result = {}
    try:
        import ctranslate2
        result["ctranslate2_version"] = ctranslate2.__version__
        result["cuda_device_count"] = ctranslate2.get_cuda_device_count()
        result["cpu_compute_types"] = sorted(
            ctranslate2.get_supported_compute_types("cpu"))
        if result["cuda_device_count"] > 0:
            result["cuda_compute_types"] = sorted(
                ctranslate2.get_supported_compute_types("cuda", 0))
    except Exception as e:
        result["ctranslate2_error"] = f"{type(e).__name__}: {e}"
    try:
        import onnxruntime
        result["onnxruntime"] = onnxruntime.__version__
    except Exception as e:
        result["onnxruntime_error"] = f"{type(e).__name__}: {e}"
    if out_path:
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=1)
        return 0
    print(json.dumps(result, ensure_ascii=False))
    return 0


def _exe_info():
    exe = sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__)
    info = {"exe": exe}
    if getattr(sys, "frozen", False):
        # onefile 的 _MEIPASS 在 %TEMP% 下；onedir 的在 exe 同级 _internal
        meipass = getattr(sys, "_MEIPASS", "") or ""
        tmp_root = os.path.normcase(os.environ.get("TEMP", ""))
        info["packaging"] = ("onefile" if tmp_root and os.path.normcase(meipass).startswith(tmp_root)
                             else "onedir")
        t0 = time.time()
        try:
            h = hashlib.sha256()
            with open(exe, "rb") as f:
                for blk in iter(lambda: f.read(1 << 20), b""):
                    h.update(blk)
            info["sha256"] = h.hexdigest()
            info["hash_seconds"] = round(time.time() - t0, 1)
        except Exception as e:
            info["sha256_error"] = str(e)
    return info


def build_report():
    local = os.environ.get("LOCALAPPDATA", os.path.expanduser("~"))
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "app": _exe_info(),
        "python": sys.version.split()[0],
        "os": platform.platform(),
        "cpu_memory": _probe_cpu_mem(),
        "simd": _probe_simd(),
        "gpu_hardware": _probe_gpu(),
        "asr_runtime_probe": _probe_asr_child(),
        "disk": _probe_disk({
            "exe_dir": os.path.dirname(sys.executable if getattr(sys, "frozen", False)
                                       else os.path.abspath(__file__)),
            "temp": os.environ.get("TEMP", "."), "localappdata": local}),
        "dpi_scale_pct": (lambda: (
            lambda dc: None if not dc else round(ctypes.windll.user32.GetDpiForSystem() / 96 * 100)
        )(ctypes.windll.user32.GetDC(0)))(),
        "webview2_runtime": _probe_webview2(),
        "antivirus": _probe_av(),
    }


def run_syscheck_cli() -> int:
    out_dir = os.path.join(os.environ.get("LOCALAPPDATA",
                                          os.path.expanduser("~")),
                           "yanhe", "diagnostics")
    os.makedirs(out_dir, exist_ok=True)
    base = os.path.join(out_dir, f"syscheck-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{os.getpid()}")

    report = build_report()
    with open(base + ".json", "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)

    lines = [f"# 延河课堂下载器 自检报告 {report['generated_at']}", ""]
    for k, v in report.items():
        lines.append(f"## {k}")
        lines.append(json.dumps(v, ensure_ascii=False, indent=1))
    with open(base + ".txt", "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    try:
        ctypes.windll.user32.MessageBoxW(
            0, f"自检完成，报告已保存：\n\n{base}.txt\n\n请把该 txt 文件发回给开发者。",
            "延河课堂下载器 - 自检", 0x40)
    except Exception:
        pass
    print(base + ".txt")
    return 0


if __name__ == "__main__":
    if "--probe-asr" in sys.argv:
        raise SystemExit(run_probe_asr())
    raise SystemExit(run_syscheck_cli())
