# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

# Collect all pedalboard files (C++ DLLs, data files)
pb_datas, pb_binaries, pb_hiddenimports = collect_all("pedalboard")

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=pb_binaries,
    datas=pb_datas,
    hiddenimports=pb_hiddenimports + [
        "sounddevice",
        "numpy",
        "tkinter",
        "tkinter.ttk",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="VoiceChanger",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,   # no terminal window
    disable_windowed_traceback=False,
    argv_emulation=False,
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="VoiceChanger",
)
