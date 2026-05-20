# -*- mode: python ; coding: utf-8 -*-
"""
延河课堂下载器 - 完整版
包含下载 + 课件提取 (FFmpeg scene) + 音频转录 (faster-whisper)。
体积 ~500MB（不含 Whisper 模型，首次启动自动下载到 %APPDATA%）。
打包命令： pyinstaller --noconfirm --clean 延河课堂下载器-完整版.spec
"""
from PyInstaller.utils.hooks import collect_all, collect_submodules
import os

block_cipher = None

datas = [('yhkt.ico', '.')]
binaries = []
for _bin in ('ffmpeg.exe', 'ffprobe.exe'):
    if os.path.isfile(_bin):
        binaries.append((_bin, '.'))

hiddenimports = [
    'customtkinter', 'PIL', '_tkinter',
    'requests', 'urllib3', 'charset_normalizer', 'idna', 'certifi',
    'tkinter', 'tkinter.ttk', 'tkinter.messagebox', 'tkinter.filedialog',
    'queue', 'threading', 'concurrent.futures',
    'app_paths', 'utils', 'm3u8dl',
    'ppt_extractor_gpu', 'audio_transcriber_gpu', 'batch_process',
    'cv2', 'numpy', 'imagehash', 'pptx',
    'faster_whisper', 'ctranslate2', 'tokenizers', 'huggingface_hub',
    'tqdm',
]

for _pkg in ('customtkinter', 'faster_whisper', 'ctranslate2',
             'huggingface_hub', 'tokenizers', 'pptx'):
    try:
        _d, _b, _h = collect_all(_pkg)
        datas += _d
        binaries += _b
        hiddenimports += _h
    except Exception as _e:
        print(f"[spec] collect_all({_pkg!r}) failed: {_e}")

# Whisper 子模块通常通过 importlib 动态导入，必须 collect_submodules 兜底
hiddenimports += collect_submodules('faster_whisper')

excludes = [
    'pandas', 'matplotlib', 'scipy', 'sklearn',
    'tensorflow', 'transformers',
    'IPython', 'jupyter', 'notebook',
    'torch.distributions', 'torch.utils.tensorboard',
]

a = Analysis(
    ['app_full.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='延河课堂下载器-完整版',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=['vcruntime140.dll', 'python3*.dll'],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['yhkt.ico'],
)
