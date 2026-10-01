# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build for Agentboard.

Produces a single windowed executable that launches with a double-click,
needs no Python installation, and shows no console window.

    pyinstaller agentboard.spec --noconfirm

The web assets and the icons are not importable modules, so they are
declared as data and located at runtime through ``sys._MEIPASS``; see
``agentboard.paths`` and ``agentboard.api.WEB_DIR``.
"""

import sys
from pathlib import Path

# PyInstaller runs this file with exec(), so __file__ is not defined.
PROJECT = Path(SPECPATH).resolve()

datas = [
    (str(PROJECT / "agentboard" / "web"), "agentboard/web"),
    (str(PROJECT / "assets" / "icon.png"), "assets"),
    (str(PROJECT / "assets" / "icon.ico"), "assets"),
    (str(PROJECT / "assets" / "icon.icns"), "assets"),
    (str(PROJECT / "examples"), "examples"),
]

# Uvicorn and watchdog load parts of themselves by name, so the analysis
# cannot see them; the platform webview backends are the same.
hiddenimports = [
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    "watchdog.observers",
    "watchdog.observers.polling",
    "agentboard.actions",
    "agentboard.content",
    "agentboard.exporters",
    "agentboard.watcher",
    "agentboard.detect",
    # Provider adapters are imported by name from the registry.
    "agentboard.providers.claude",
    "agentboard.providers.codex",
    "agentboard.providers.gemini",
    "agentboard.providers.generic",
]
if sys.platform == "win32":
    hiddenimports += ["webview.platforms.edgechromium", "clr_loader", "pythonnet"]
elif sys.platform == "darwin":
    hiddenimports += ["webview.platforms.cocoa"]
else:
    hiddenimports += ["webview.platforms.gtk", "gi"]

# Nothing here talks to the network or a database; leaving these out keeps
# the executable substantially smaller.
excludes = [
    "tkinter", "unittest", "pydoc_data", "test", "distutils",
    "numpy", "pandas", "matplotlib", "PyQt5", "PyQt6", "PySide2", "PySide6",
    "IPython", "notebook", "setuptools", "pip",
]

# PyInstaller's PyGObject hook collects every icon theme, cursor theme and
# GTK theme installed on the build machine. On a desktop Linux box that is
# hundreds of megabytes of artwork the dashboard never draws. Narrow it to
# the one theme GTK actually falls back to.
hooksconfig = {
    "gi": {
        "icons": ["Adwaita"],
        "themes": ["Adwaita"],
        "languages": ["en", "en_US"],
    },
}

a = Analysis(
    ["run_app.py"],
    pathex=[str(PROJECT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig=hooksconfig,
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)

icon = str(PROJECT / "assets" / (
    "icon.ico" if sys.platform == "win32"
    else "icon.icns" if sys.platform == "darwin"
    else "icon.png"
))

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="Agentboard",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    # windowed: no console window on Windows or macOS.
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=icon,
)

if sys.platform == "darwin":
    app = BUNDLE(
        exe,
        name="Agentboard.app",
        icon=str(PROJECT / "assets" / "icon.icns"),
        bundle_identifier="local.agentboard",
        info_plist={
            "CFBundleShortVersionString": "2.0.0",
            "NSHighResolutionCapable": True,
            # The app only ever talks to 127.0.0.1.
            "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},
        },
    )
