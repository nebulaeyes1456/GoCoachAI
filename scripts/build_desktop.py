"""桌面版打包脚本（窗口4）：PyInstaller 单目录（--onedir）打包。

产物：
    dist/弈友/弈友.exe        双击即可运行
    dist/弈友/_internal/      运行时依赖与数据（frontend/backend/engine/data）

打包内容（都先过一遍暂存，见 stage_sources）：
    - backend/（**排除 config.yaml**——内含 API key）
    - frontend/（页面与静态资源）
    - engine/（KataGo 引擎与模型，目录较大）
    - data/（**只带 chains/ 题链种子与 config.example.yaml**）

⚠️ 分发前必读：**绝不能**直接 `--add-data` 整个 `data/` 或 `backend/`——
`backend/config.yaml`、`data/config.yaml` 含付费 LLM 的 API key；
`data/goapp.db`、`data/app.log`、`data/sgfs/` 含本机用户的复盘、题库与棋谱。
本脚本因此先做干净暂存副本，并在产物上跑一次密钥正则扫描再报告结果。

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


STAGE = ROOT / "build" / "_stage"

# 打进安装包时排除的东西（含隐私与密钥，见文件头警告）
DATA_EXCLUDE_DIRS = ("tmp", "sgfs", "library", "backups")
DATA_EXCLUDE_FILES = ("config.yaml", "app.db", "goapp.db", "app.log")


def stage_sources() -> tuple[Path, Path]:
    """做一份干净的暂存副本：backend 去 config.yaml，data 只留种子与示例配置。

    直接 --add-data 整个 data/ 会把 goapp.db（用户复盘与题库）、app.log、
    sgfs/（用户棋谱）一起打包分发——那是隐私泄露；backend/config.yaml 则是
    付费 LLM 的 API key。
    """
    backend_stage = STAGE / "backend"
    data_stage = STAGE / "data"
    for d in (backend_stage, data_stage):
        shutil.rmtree(d, ignore_errors=True)
        d.mkdir(parents=True, exist_ok=True)
    # backend/：排除 config.yaml
    for item in (ROOT / "backend").iterdir():
        if item.name == "config.yaml" or item.name == "__pycache__":
            continue
        if item.is_dir():
            shutil.copytree(item, backend_stage / item.name,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            shutil.copy2(item, backend_stage / item.name)
    # data/：只带 chains/ 与 config.example.yaml
    (data_stage / "chains").mkdir(exist_ok=True)
    for f in (ROOT / "data" / "chains").glob("*"):
        if f.is_file() and f.suffix.lower() in (".sgf", ".json"):
            shutil.copy2(f, data_stage / "chains" / f.name)
    example = ROOT / "data" / "config.example.yaml"
    if example.is_file():
        shutil.copy2(example, data_stage / "config.example.yaml")
    print(f"[build][安全] 暂存副本：backend {len(list(backend_stage.rglob('*')))} 项、"
          f"data {len(list(data_stage.rglob('*')))} 项（已剔除 config/db/log/用户棋谱）")
    return backend_stage, data_stage


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
    backend_stage, data_stage = stage_sources()
    datas = [
        add_data(ROOT / "frontend", "frontend"),
        add_data(backend_stage, "backend"),
        add_data(ROOT / "engine", "engine"),
        add_data(data_stage, "data"),
    ]
    datas = [d for d in datas if Path(d.split(SEP)[0]).exists()]
    if not datas:
        print("[build] 未找到 frontend/backend 目录，请在项目根目录运行本脚本")
        sys.exit(2)

    # Anaconda 基础环境：PyInstaller 不会收集这些扩展模块的 conda 依赖，
    # 产物启动即 "ImportError: DLL load failed while importing _ctypes/_sqlite3"。
    # 实测只需这两个（**别**把整个 Library/bin 拷进产物：含 MKL/CUDA，
    # 会从 108MB 涨到 795MB）：
    #   _ctypes.pyd  → Library/bin/ffi-*.dll
    #   _sqlite3.pyd → Library/bin/sqlite3.dll
    conda_bin = Path(sys.base_prefix, "Library", "bin")
    ffi_bins = [p for p in sorted(conda_bin.glob("ffi*.dll"))]
    sqlite_dll = conda_bin / "sqlite3.dll"
    if sqlite_dll.is_file():
        ffi_bins.append(sqlite_dll)

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--onedir",
    ]
    for b in ffi_bins:
        cmd += ["--add-binary", f"{b}{SEP}."]
    if ffi_bins:
        print("[build] 附带 ffi 运行库：" + "、".join(b.name for b in ffi_bins))
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
    # Anaconda 基础环境：_ctypes/_sqlite3 等扩展模块的依赖（ffi-*.dll、
    # sqlite3.dll…）在 Library/bin 下，PyInstaller 的 bindepend 靠 PATH 解析
    # 依赖——不放进 PATH，产物启动即 "DLL load failed"。
    # 注意：**不要**把 Library/bin 整个拷进产物（含 MKL/CUDA，能到 800MB），
    # 让 bindepend 按依赖收集即可（实测产物 ~110MB）。
    env = dict(os.environ)
    conda_bin = Path(sys.base_prefix, "Library", "bin")
    if conda_bin.is_dir():
        env["PATH"] = str(conda_bin) + os.pathsep + env.get("PATH", "")
        print(f"[build] PATH 前置 conda 运行库目录：{conda_bin}")
    subprocess.run(cmd, cwd=ROOT, check=True, env=env)

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
    import re as _re
    key_re = _re.compile(rb"sk-[A-Za-z0-9]{20,}")
    key_hits = 0
    for base in (internal, dist_dir):
        if not base.exists():
            continue
        for p in base.rglob("*"):
            if not p.is_file() or p.suffix.lower() in (
                    ".exe", ".dll", ".pyd", ".bin", ".gz", ".png", ".jpg"):
                continue
            try:
                if key_re.search(p.read_bytes()[:3_000_000]):
                    key_hits += 1
                    print(f"[build][SEC] suspicious key-like string: {p}")
            except OSError:
                pass
    print("[build][SEC] key scan: " + (
        f"{key_hits} file(s) flagged, review them before shipping!"
        if key_hits else "clean (no sk-<20+ chars> pattern in the bundle)"))

    print()
    print(f"[build] 完成：{exe}")
    print("[build] 整个 dist/弈友 目录为绿色软件：双击 弈友.exe 即可使用；")
    print("[build] 复制整个 dist/弈友 目录到任意干净机器即可分发。")
    print("[build] 用户首次运行后在「设置」里填自己的 DeepSeek key（或用本地 Ollama）。")


if __name__ == "__main__":
    main()
