# -*- mode: python ; coding: utf-8 -*-
"""
延河课堂下载器 - 完整版
包含下载 + 课件提取 (FFmpeg scene) + 音频转录 (faster-whisper)。
体积约 500MB，不内置 Whisper 模型，首次转录时模型会下载到本机缓存。
打包命令：pyinstaller --noconfirm --clean 延河课堂下载器-完整版.spec
"""
from PyInstaller.utils.hooks import (
    collect_all,
    collect_data_files,
    collect_dynamic_libs,
    collect_submodules,
)
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
    'app_paths', 'utils', 'm3u8dl', 'history',
    'ppt_extractor_gpu', 'audio_transcriber_gpu', 'batch_process',
    'cv2', 'numpy', 'imagehash', 'pptx',
    'faster_whisper', 'ctranslate2', 'tokenizers', 'huggingface_hub',
    'tqdm',
]

_d, _b, _h = collect_all('customtkinter')
datas += _d
binaries += _b
hiddenimports += _h

for _pkg in ('faster_whisper', 'ctranslate2', 'tokenizers'):
    try:
        datas += collect_data_files(_pkg)
        binaries += collect_dynamic_libs(_pkg)
        hiddenimports += collect_submodules(_pkg)
    except Exception as _e:
        print(f"[spec] collect {_pkg!r} failed: {_e}")

# Avoid collecting huggingface_hub CLI/inference extras; faster-whisper only needs
# model-cache/download helpers.
hiddenimports += [
    'huggingface_hub',
    'huggingface_hub.constants',
    'huggingface_hub.errors',
    'huggingface_hub.file_download',
    'huggingface_hub.hf_api',
    'huggingface_hub.utils',
    'huggingface_hub.utils._auth',
    'huggingface_hub.utils._cache_manager',
    'huggingface_hub.utils._headers',
    'huggingface_hub.utils._http',
    'huggingface_hub.utils._runtime',
    'huggingface_hub.utils._validators',
]

excludes = [
    'torch', 'torchaudio', 'torchvision',
    'tensorflow', 'transformers', 'onnxruntime',
    'pandas', 'matplotlib', 'scipy', 'sklearn', 'skimage',
    'bokeh', 'panel', 'plotly', 'dask', 'distributed', 'xarray',
    'selenium', 'sqlalchemy', 'h5py', 'nltk', 'spacy',
    'numba', 'llvmlite', 'astropy',
    'PyQt5', 'qtpy', 'PySide6', 'PySide2',
    'pytest', 'sphinx', 'nbconvert', 'nbformat',
    'IPython', 'jupyter', 'notebook',
    'boto3', 'botocore', 's3transfer',
    'fastapi', 'uvicorn', 'gradio',
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
