"""桌面版打包脚本（窗口4）：PyInstaller 单目录（--onedir）打包。

产物：
    dist/弈友/弈友.exe        双击即可运行
    dist/弈友/_internal/      运行时依赖与数据（frontend/backend/engine/data）

打包内容：
    - backend/（**排除 config.yaml**——内含 API key，见下）
    - frontend/（页面与静态资源）
    - engine/（KataGo 引擎与模型，目录较大）
    - data/（SQLite 数据库与题库目录；**排除 config.yaml**）

⚠️ 分发前必读：`backend/config.yaml` 与 `data/config.yaml` 含付费 LLM 的
API key，**不能进安装包**——本脚本打包后会主动把这两个文件从产物里删掉并
扫描确认，但打**安装程序**（setup_installer.py）之前请先跑本脚本，不要手工
用旧产物重打包。

用法（项目根目录）：
    .venv\\Scripts\\python.exe scripts/build_desktop.py [--clean]
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_NAME = "弈友"
ENTRY = ROOT / "backend" / "desktop.py"
SEP = ";" if os.name == "nt" else ":"


def add_data(src: Path, dest: str) -> str:
    return f"{src}{SEP}{dest}"


def main() -> None:
    parser = argparse.ArgumentParser(description="打包桌面版（PyInstaller --onedir）")
    parser.add_argument("--clean", action="store_true", help="打包前清空 build/ 与 dist/弈友")
    parser.add_argument("--console", action="store_true", help="调试用：带控制台窗口打包（可看到报错）")
    args = parser.parse_args()

    dist_dir = ROOT / "dist" / APP_NAME
    if args.clean and dist_dir.exists():
        print(f"[build] 清空旧产物 {dist_dir}")
        shutil.rmtree(dist_dir, ignore_errors=True)
    (ROOT / "dist").mkdir(exist_ok=True)

    # 待打包数据（不存在则跳过）
    datas = [
        add_data(ROOT / "frontend", "frontend"),
        add_data(ROOT / "backend", "backend"),
        add_data(ROOT / "engine", "engine"),
        add_data(ROOT / "data", "data"),
    ]
    datas = [d for d in datas if Path(d.split(SEP)[0]).exists()]
    if not datas:
        print("[build] 未找到 frontend/backend 目录，请在项目根目录运行本脚本")
        sys.exit(2)

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--onedir",
    ]
    if not args.console:
        cmd += ["--windowed"]      # 无控制台窗口（--console 调试时保留）
    cmd += [
        "--name", APP_NAME,
        "--paths", str(ROOT),         # 保证 backend 包可被解析
        "--distpath", str(ROOT / "dist"),
        "--workpath", str(ROOT / "build"),
        "--specpath", str(ROOT / "build"),
        # uvicorn 动态加载的模块
        "--hidden-import", "uvicorn.logging",
        "--hidden-import", "uvicorn.loops.auto",
        "--hidden-import", "uvicorn.protocols.http.auto",
        "--hidden-import", "uvicorn.protocols.websockets.auto",
        "--hidden-import", "uvicorn.lifespan.on",
        # pywebview（WebView2 运行所需资源一并收集）
        "--collect-all", "pywebview",
    ]
    for d in datas:
        cmd += ["--add-data", d]
    cmd.append(str(ENTRY))

    print("[build] 开始打包（首次约需几分钟）…")
    print("[build] 命令：", " ".join(cmd))
    subprocess.run(cmd, cwd=ROOT, check=True)

    exe = dist_dir / f"{APP_NAME}.exe"

    # ---- 分发安全：把含 API key 的 config 从产物里剔除，并扫描确认 ----
    internal = dist_dir / "_internal"
    removed = []
    for rel in ("backend/config.yaml", "data/config.yaml"):
        for base in (internal, dist_dir):
            p = base / rel
            if p.exists():
                p.unlink()
                removed.append(str(p))
    if removed:
        print("[build][安全] 已从产物剔除含密钥的配置：" + "、".join(removed))
    key_hits = 0
    for base in (internal, dist_dir):
        if not base.exists():
            continue
        for p in base.rglob("*"):
            if not p.is_file() or p.suffix.lower() in (".exe", ".dll", ".bin", ".gz"):
                continue
            try:
                if b"sk-" in p.read_bytes()[:2_000_000]:
                    key_hits += 1
                    print(f"[build][安全] ⚠️ 产物内仍发现疑似密钥：{p}")
            except OSError:
                pass
    print(f"[build][安全] 密钥扫描：{'发现 ' + str(key_hits) + ' 处可疑文件，请人工确认！' if key_hits else '通过（产物内无 sk- 特征）'}")

    print()
    print(f"[build] 完成：{exe}")
    print("[build] 整个 dist/弈友 目录为绿色软件：双击 弈友.exe 即可使用；")
    print("[build] 复制整个 dist/弈友 目录到任意干净机器即可分发。")
    print("[build] 用户首次运行后在「设置」里填自己的 DeepSeek key（或用本地 Ollama）。")


if __name__ == "__main__":
    main()
