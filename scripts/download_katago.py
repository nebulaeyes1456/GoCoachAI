"""下载 KataGo 引擎与模型到 engine/（窗口1，Windows/Linux 通用）。

功能：
- 从 KataGo GitHub Releases（lightvector/KataGo）按当前平台下载：
  * Windows: OpenCL 版 -> engine/katago-opencl.exe；EigenAVX2 版 -> engine/katago-eigenavx2.exe
  * Linux  : OpenCL 版 -> engine/katago-opencl；EigenAVX2 版 -> engine/katago-eigenavx2
    （官方 Linux 包内是 `katago` 裸二进制，落盘时自动 chmod +x；
      后端会自动适配 config 里 .exe 后缀的默认路径）
  * macOS  : 官方 v1.14+ 已无 macOS 预编译包——本脚本直接报错并提示自行编译
- 下载小网络模型 b10c128（.bin.gz）-> engine/b10c128.bin.gz；
  备用 b6c96 -> engine/b6c96.bin.gz。
- 支持断点续传（Range 请求，.part 临时文件）、幂等（已存在且大小>0 则跳过）、
  解压与校验；结束后输出各产物路径（可用 --json-out 落盘供 benchmark 使用）。

下载地址（以实际页面为准，脚本运行时动态解析）：
- Releases API : https://api.github.com/repos/lightvector/KataGo/releases/latest
  资产示例（v1.18.1）:
    katago-v1.18.1-opencl-windows-x64.zip / katago-v1.18.1-eigenavx2-windows-x64.zip
    katago-v1.18.1-opencl-linux-x64.zip  / katago-v1.18.1-eigenavx2-linux-x64.zip
  ⚠️ 最新 release 可能只有 CUDA 资产（如 v1.18.2）：本脚本自动向前翻找
    最近的含 opencl/eigen 资产的正式 release。
  （github.com 下载走 releases/download；无法访问时可设 GITHUB_MIRROR 环境变量，
    如 https://ghproxy.net/https://github.com/... 之类镜像前缀。）
- 模型列表    : https://katagoarchive.org/g170/neuralnets/index.html
  模型直链    :
    https://katagoarchive.org/g170/neuralnets/g170e-b10c128-s1141046784-d204142634.bin.gz
    https://katagoarchive.org/g170/neuralnets/g170-b6c96-s175395328-d26788732.bin.gz
  （katagotraining.org/media.katagotraining.org 模型直链在国内网络下通常 403，
    katagoarchive.org 为 GitHub Pages 镜像，国内可达；g170e-b10c128 与
    katagotraining 的 kata1-b10c128-s1141046784-d204142634 为同一模型。）

用法：
    python scripts/download_katago.py                 # 全部下载（按当前平台）
    python scripts/download_katago.py --platform linux   # 指定平台
    python scripts/download_katago.py --list          # 只解析打印资产地址
    python scripts/download_katago.py --skip-engine   # 只下载模型
    python scripts/download_katago.py --json-out engine/download_result.json
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENGINE_DIR = PROJECT_ROOT / "engine"

# 模型直链（备用源：katagoarchive.org，GitHub Pages，国内可达）
MODEL_B10C128_URL = (
    "https://katagoarchive.org/g170/neuralnets/"
    "g170e-b10c128-s1141046784-d204142634.bin.gz"
)
MODEL_B6C96_URL = (
    "https://katagoarchive.org/g170/neuralnets/"
    "g170-b6c96-s175395328-d26788732.bin.gz"
)

RELEASES_API = "https://api.github.com/repos/lightvector/KataGo/releases/latest"
RELEASES_LIST_API = (
    "https://api.github.com/repos/lightvector/KataGo/releases?per_page=10"
)
CHUNK = 1 << 16  # 64 KiB


def download(url: str, dest: Path, description: str) -> Path:
    """断点续传下载 url -> dest。

    - 已存在且非空则直接跳过（幂等）；
    - 下载过程中写入 dest.part，完成后原子改名；
    - 服务器不支持 Range 则从头重下。
    """
    if dest.exists() and dest.stat().st_size > 0:
        print(f"[skip] {description}: 已存在 {dest} ({dest.stat().st_size} 字节)")
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    done = part.stat().st_size if part.exists() else 0
    headers = {"User-Agent": "GoCoachAI-downloader"}
    if done > 0:
        headers["Range"] = f"bytes={done}-"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            total = resp.headers.get("content-length")
            total = int(total) if total else None
            mode = "ab" if done > 0 and resp.status == 206 else "wb"
            if mode == "wb":
                done = 0
            print(f"[dl ] {description}: 从 {done} 字节开始"
                  + (f" / 共 {total} 字节" if total else ""))
            last_pct = -1
            with part.open(mode) as f:
                while True:
                    chunk = resp.read(CHUNK)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    if total:
                        pct = done * 100 // total
                        if pct != last_pct and pct % 10 == 0:
                            print(f"      {pct}%")
                            last_pct = pct
    except Exception as exc:  # 保留 .part 以便下次续传
        print(f"[fail] {description}: {exc}", file=sys.stderr)
        print(f"       已下载 {done} 字节保留在 {part}，可重跑续传", file=sys.stderr)
        raise
    part.replace(dest)
    print(f"[ok  ] {description}: {dest} ({dest.stat().st_size} 字节)")
    return dest


def _json_from_url(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "GoCoachAI-downloader"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def detect_platform() -> str:
    """返回资产平台段：windows / linux；macOS（darwin）官方无预编译包。"""
    if os.name == "nt":
        return "windows"
    if sys.platform == "darwin":
        return "darwin"
    return "linux"


def _pick_assets(assets: list[dict], platform: str) -> dict[str, str]:
    """从资产列表里找该平台 x64 的 opencl / eigenavx2 zip 下载地址。"""
    found: dict[str, str] = {}
    for a in assets:
        name = a.get("name", "")
        dl = a.get("browser_download_url", "")
        if not name.lower().endswith(".zip") \
                or f"-{platform}-x64" not in name.lower():
            continue
        low = name.lower()
        if "+bs50" in low:
            continue  # 特制 batch-size-50 构建，标准模型用普通版
        if "opencl" in low and "opencl" not in found:
            found["opencl"] = dl
        elif "eigenavx2" in low and "eigenavx2" not in found:
            found["eigenavx2"] = dl
    return found


def find_release_assets(platform: str, tag: str | None) -> dict[str, str]:
    """从 Releases API 找指定平台的 opencl / eigenavx2 zip 下载地址。

    最新 release 可能只有 CUDA 资产（如 v1.18.2）——此时向前翻找最近的
    含 opencl/eigen 资产的正式 release。
    """
    if tag:
        data = _json_from_url(RELEASES_API.replace("/latest", f"/tags/{tag}"))
        return _pick_assets(data.get("assets", []), platform)
    found = _pick_assets(_json_from_url(RELEASES_API).get("assets", []),
                         platform)
    if found:
        return found
    for rel in _json_from_url(RELEASES_LIST_API):
        if rel.get("draft") or rel.get("prerelease"):
            continue
        found = _pick_assets(rel.get("assets", []), platform)
        if found:
            print(f"[info] 最新 release 无 {platform} opencl/eigen 资产，"
                  f"回退到 {rel.get('tag_name')}")
            return found
    return {}


def _apply_mirror(url: str) -> str:
    mirror = os.environ.get("GITHUB_MIRROR", "")
    return mirror.rstrip("/") + "/" + url if mirror else url


def extract_katago(zip_path: Path, dest: Path, binary_name: str) -> Path:
    """解压 zip：引擎二进制直接写 dest，其余顶层文件解到 engine/。

    - Windows：官方包内含 VC++ 运行库 DLL（msvcp140*.dll/vcruntime140*.dll
      以及 bz2/z/zip.dll），必须与 katago.exe 同目录，否则启动报
      0xC0000135（DLL 未找到）；binary_name 传 "katago.exe"。
    - Linux：官方包内是 `katago` 裸二进制，落盘后 chmod +x；
      binary_name 传 "katago"。
    """
    with zipfile.ZipFile(zip_path) as zf:
        names = [n for n in zf.namelist()
                 if Path(n).name.lower() == binary_name]
        if not names:
            raise FileNotFoundError(f"{zip_path} 中未找到 {binary_name}")
        for name in zf.namelist():
            member = Path(name)
            if member.is_dir() or member.name.lower() == binary_name:
                continue
            # 顶层运行库解压到 engine/（同平台两包内容相同，覆盖无害）；
            # 跳过 .txt 与 analysis_example.cfg：保留本仓库自建的
            # engine/analysis_example.cfg 不被官方版覆盖；
            # default_gtp.cfg 等官方配置保留（scripts/gen_test_sgf.py 使用）。
            if member.suffix.lower() == ".txt" \
                    or member.name.lower() == "analysis_example.cfg":
                continue
            out = ENGINE_DIR / member.name
            with zf.open(name) as src, out.open("wb") as dst:
                shutil.copyfileobj(src, dst)
        with zf.open(names[0]) as src, dest.open("wb") as dst:
            shutil.copyfileobj(src, dst)
        print(f"[unzip] {zip_path} -> {dest}")
    if os.name != "nt":
        os.chmod(dest, 0o755)
    return dest


def main() -> int:
    parser = argparse.ArgumentParser(description="下载 KataGo 引擎与模型到 engine/")
    parser.add_argument("--tag", default=None,
                        help="指定版本 tag，如 v1.18.1（默认取最新）")
    parser.add_argument("--platform", default=None, choices=("windows", "linux"),
                        help="资产平台（默认按当前系统自动检测）")
    parser.add_argument("--list", action="store_true",
                        help="只解析并打印资产下载地址，不下载")
    parser.add_argument("--skip-engine", action="store_true", help="跳过引擎下载")
    parser.add_argument("--skip-model", action="store_true", help="跳过模型下载")
    parser.add_argument("--json-out", default=None, help="把产物路径写入该 JSON 文件")
    args = parser.parse_args()

    platform = args.platform or detect_platform()
    if platform == "darwin":
        print("[X] 官方 KataGo 已不出 macOS 预编译包（v1.14+ 无 mac 资产），",
              file=sys.stderr)
        print("    请自行编译 KataGo（Metal/CPU 后端），或用 Linux 机器运行。",
              file=sys.stderr)
        return 1
    exe_suffix = ".exe" if platform == "windows" else ""
    binary_name = "katago.exe" if platform == "windows" else "katago"

    ENGINE_DIR.mkdir(parents=True, exist_ok=True)
    result: dict[str, str] = {}

    # ---- 1. 引擎 ----
    if not args.skip_engine:
        assets = find_release_assets(platform, args.tag)
        if args.list:
            print(f"平台: {platform}")
            for kind in ("opencl", "eigenavx2"):
                print(f"{kind}: {assets.get(kind) or '(未找到)'}")
            return 0 if assets else 1
        if not assets:
            print(f"未在 Releases 中找到 {platform} opencl/eigenavx2 资产，"
                  "检查网络或 tag。", file=sys.stderr)
        zip_map = {
            "opencl": (assets.get("opencl"),
                       ENGINE_DIR / f"katago-opencl{exe_suffix}"),
            "eigenavx2": (assets.get("eigenavx2"),
                          ENGINE_DIR / f"katago-eigenavx2{exe_suffix}"),
        }
        for kind, (url, dest_exe) in zip_map.items():
            if not url:
                continue
            if dest_exe.exists() and dest_exe.stat().st_size > 0:
                print(f"[skip] {kind}: 已存在 {dest_exe}")
                result[kind] = str(dest_exe)
                continue
            zip_path = ENGINE_DIR / f"katago-{kind}-{platform}.zip"
            download(_apply_mirror(url), zip_path, f"引擎 {kind} 版")
            extract_katago(zip_path, dest_exe, binary_name)
            if not dest_exe.exists() or dest_exe.stat().st_size == 0:
                raise RuntimeError(f"引擎校验失败: {dest_exe}")
            result[kind] = str(dest_exe)
        if platform != "windows" and result:
            print("[info] Linux：config.yaml 的引擎路径用默认值即可（后端会"
                  "自动适配 .exe 后缀），也可手动改为：")
            print(f"       executable: engine/katago-opencl")
            print(f"       executable_cpu: engine/katago-eigenavx2")

    # ---- 2. 模型 ----
    if not args.skip_model:
        model_map = {
            "model": (MODEL_B10C128_URL, ENGINE_DIR / "b10c128.bin.gz"),
            "backup_model": (MODEL_B6C96_URL, ENGINE_DIR / "b6c96.bin.gz"),
        }
        for key, (url, dest) in model_map.items():
            download(url, dest, f"模型 {dest.name}")
            if not dest.exists() or dest.stat().st_size == 0:
                raise RuntimeError(f"模型校验失败: {dest}")
            result[key] = str(dest)

    # ---- 3. 输出 ----
    print("\n== 下载结果 ==")
    for k, v in result.items():
        print(f"  {k}: {v}")
    if args.json_out:
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        print(f"已写入 {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
