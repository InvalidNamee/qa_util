"""在当前系统构建独立客户端。"""

from pathlib import Path
import shutil
import subprocess
import sys

def main():
    root = Path(__file__).resolve().parent
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
                    "--distpath", str(root / "dist"), "--workpath", str(root / "build"), str(root / "qa_util.spec")],
                   cwd=root, check=True)
    directory = root / "dist" if sys.platform == "darwin" else root / "dist" / "qa_util"
    artifact = directory / "qa_util.app" if sys.platform == "darwin" else directory / ("qa_util.exe" if sys.platform == "win32" else "qa_util")
    shutil.copyfile(root / "client.example.json", directory / "client.example.json")
    print(f"构建完成：{artifact}")
    print(f"示例配置：{directory / 'client.example.json'}；真实配置和登录状态未打包。")


if __name__ == "__main__":
    main()
