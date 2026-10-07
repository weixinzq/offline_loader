# -*- mode: python ; coding: utf-8 -*-

analysis = Analysis(
    ["backend_main.py"],
    pathex=[],
    binaries=[],
    datas=[
        ("scripts/__init__.py", "scripts"),
        ("scripts/mt250816_1.py", "scripts"),
        ("scripts/mt250816_2.py", "scripts"),
        ("scripts/mt250816_3.py", "scripts"),
        ("scripts/mt250816_4.py", "scripts"),
        ("scripts/auto_battle.py", "scripts"),
    ],
    hiddenimports=[
        "scripts.mt250816_1",
        "scripts.mt250816_2",
        "scripts.mt250816_3",
        "scripts.mt250816_4",
        "scripts.auto_battle",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "pydantic", "rich", "PIL", "numpy", "pygame"],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(analysis.pure)

exe = EXE(
    pyz,
    analysis.scripts,
    analysis.binaries,
    analysis.datas,
    [],
    name="AolaBackend",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
)
