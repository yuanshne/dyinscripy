# -*- mode: python ; coding: utf-8 -*-
# 抖音工具箱 便携版打包配置（PyInstaller onedir）
#
# 构建：
#   python -m PyInstaller douyin_toolbox.spec --noconfirm
# 之后手动把 Chromium 内核放进 dist/抖音工具箱/runtime/ms-playwright
# （见 build_portable.bat）

import os
from PyInstaller.utils.hooks import collect_data_files

a = Analysis(
    ["douyin_ui.py"],
    pathex=[],
    # 三个子脚本由 exe 以 --task 参数二次启动时动态 import，需显式声明
    hiddenimports=[
        "douyin_spider",
        "douyin_live_recorder",
        "douyin_login",
        "imageio_ffmpeg",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "pandas", "PIL"],
    noarchive=False,
)

# imageio-ffmpeg 内置的 ffmpeg.exe（转封装兜底，机器没装 ffmpeg 时用）
# collect_data_files 返回 (源文件, 目标目录)，需拼上文件名组成
# PyInstaller 6 的三元组 (目标相对路径, 源文件, "DATA")
a.datas += [
    (os.path.join(dest, os.path.basename(src)), src, "DATA")
    for (src, dest) in collect_data_files("imageio_ffmpeg")
]
# 包内使用说明（轻量版/完整版通用）
a.datas += [("使用说明.txt", "包内使用说明.txt", "DATA")]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="抖音工具箱",
    console=True,          # 保留控制台窗口显示启动日志
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name="抖音工具箱",
)
