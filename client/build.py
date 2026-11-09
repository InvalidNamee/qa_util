"""在当前系统构建带 Chromium 的独立客户端。"""

import os
from pathlib import Path
import shutil
import subprocess
import sys

from playwright.sync_api import sync_playwright


def main():
    root = Path(__file__).resolve().parent
    environment = os.environ.copy()
    # 使用标准缓存中的匹配版本，打包后再原样复制浏览器。
    environment.pop("PLAYWRIGHT_BROWSERS_PATH", None)
    environment["PLAYWRIGHT_SKIP_BROWSER_GC"] = "1"
    print("准备随客户端分发的 Chromium…", flush=True)
    subprocess.run([sys.executable, "-m", "playwright", "install", "chromium", "--no-shell"],
                   cwd=root, env=environment, check=True)
    previous_browser_path = os.environ.pop("PLAYWRIGHT_BROWSERS_PATH", None)
    try:
        with sync_playwright() as playwright:
            executable = Path(playwright.chromium.executable_path)
    finally:
        if previous_browser_path is not None:
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = previous_browser_path
    browser = next(parent for parent in executable.parents if parent.name.startswith("chromium-"))
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
                    "--distpath", str(root / "dist"), "--workpath", str(root / "build"), str(root / "qa_util.spec")],
                   cwd=root, env=environment, check=True)
    directory = root / "dist" if sys.platform == "darwin" else root / "dist" / "qa_util"
    artifact = directory / "qa_util.app" if sys.platform == "darwin" else directory / ("qa_util.exe" if sys.platform == "win32" else "qa_util")
    resources = artifact / "Contents" / "Frameworks" if sys.platform == "darwin" else directory / "_internal"
    destination = resources / "playwright" / "driver" / "package" / ".local-browsers" / browser.name
    print("复制 Chromium…", flush=True)
    shutil.copytree(browser, destination, symlinks=True)
    if sys.platform == "darwin":
        subprocess.run(["/usr/bin/codesign", "--force", "--sign", "-", str(artifact)], check=True)
    shutil.copyfile(root / "client.example.json", directory / "client.example.json")
    print(f"构建完成：{artifact}")
    print(f"示例配置：{directory / 'client.example.json'}；真实配置和登录状态未打包。")


if __name__ == "__main__":
    main()
