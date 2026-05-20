# -*- mode: python ; coding: utf-8 -*-
"""
延河课堂下载器 - 简易版
仅包含视频下载功能，排除 PPT / Whisper / OpenCV / Torch，体积控制在 ~80MB。
打包命令： pyinstaller --noconfirm --clean 延河课堂下载器-简易版.spec
"""
from PyInstaller.utils.hooks import collect_all
import os

block_cipher = None

datas = [('yhkt.ico', '.')]
binaries = []
# 内嵌 ffmpeg / ffprobe（由 fetch_ffmpeg.py 提前准备好）
for _bin in ('ffmpeg.exe', 'ffprobe.exe'):
    if os.path.isfile(_bin):
        binaries.append((_bin, '.'))

hiddenimports = [
    'customtkinter', 'PIL', '_tkinter',
    'requests', 'urllib3',
    'charset_normalizer', 'idna', 'certifi',
    'tkinter', 'tkinter.ttk', 'tkinter.messagebox', 'tkinter.filedialog',
    'queue', 'threading', 'concurrent.futures',
    'app_paths', 'utils', 'm3u8dl',
]
_d, _b, _h = collect_all('customtkinter')
datas += _d; binaries += _b; hiddenimports += _h

# 简易版必须显式排除重型依赖，避免 PyInstaller 通过传递依赖把它们拉进来
excludes = [
    'faster_whisper', 'torch', 'torchaudio', 'torchvision',
    'cv2', 'opencv-python', 'numpy.testing',
    'imagehash', 'pptx', 'tqdm',
    'ppt_extractor_gpu', 'audio_transcriber_gpu', 'batch_process',
    'pandas', 'matplotlib', 'scipy', 'sklearn',
    'tensorflow', 'transformers', 'huggingface_hub',
    'IPython', 'jupyter', 'notebook',
]

a = Analysis(
    ['app_simple.py'],
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
    name='延河课堂下载器-简易版',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['yhkt.ico'],
)
