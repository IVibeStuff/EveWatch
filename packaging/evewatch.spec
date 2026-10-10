# -*- mode: python ; coding: utf-8 -*-
# PyInstaller recipe for both Windows and Linux:  pyinstaller packaging/evewatch.spec
import os, sys
from PyInstaller.utils.hooks import collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))

a = Analysis(
    [os.path.join(ROOT, "evewatch.py")],
    pathex=[ROOT],
    datas=[(os.path.join(ROOT, "assets", "icon.png"), "assets")],
    hiddenimports=["evewatch_tray"] + collect_submodules("pystray"),
    excludes=["tkinter", "unittest", "pydoc"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="EveWatch",
    console=(sys.platform != "win32"),      # no black window on Windows
    icon=os.path.join(ROOT, "assets", "icon.ico") if sys.platform == "win32" else None,
)
coll = COLLECT(exe, a.binaries, a.datas, name="EveWatch")
