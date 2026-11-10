import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

root = Path(SPECPATH)

a = Analysis(
    [str(root / "packaging" / "launcher.py")],
    pathex=[str(root / "src")],
    binaries=collect_dynamic_libs("curl_cffi"),
    datas=collect_data_files("curl_cffi"),
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "PyInstaller"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="qa_util",
    debug=False,
    strip=False,
    upx=False,
    console=False,
    argv_emulation=False,
)
distribution = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="qa_util")
if sys.platform == "darwin":
    app = BUNDLE(
        distribution,
        name="qa_util.app",
        bundle_identifier="org.qa-util.client",
        info_plist={"CFBundleDisplayName": "qa_util", "NSHighResolutionCapable": True},
    )
