import sys
from pathlib import Path

root = Path(SPECPATH)

a = Analysis(
    [str(root / "packaging" / "launcher.py")],
    pathex=[str(root / "src")],
    binaries=[],
    datas=[],
    hiddenimports=["qa_util.browser"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "PyInstaller"],
    noarchive=False,
)
# 浏览器由 build.py 在最后原样复制，避免打包器改写其签名和 Framework 结构。
browser_prefix = "playwright/driver/package/.local-browsers/"
a.datas = [entry for entry in a.datas if not entry[0].replace("\\", "/").startswith(browser_prefix)]
a.binaries = [entry for entry in a.binaries if not entry[0].replace("\\", "/").startswith(browser_prefix)]
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
