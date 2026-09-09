"""KataGo b10c384 模型断点续传下载器（多镜像源 + 自动重试）。"""
import os
import sys
import time
import urllib.request

DEST = r"c:\Users\31878\Documents\GoCoachAI2\engine\b10c384.bin.gz"
SOURCES = [
    # 官方 GitHub release 资产（多 tag 备用同一文件）
    "https://github.com/lightvector/KataGo/releases/download/v1.17.1/b10c384h6nbttflrs.bin.gz",
    "https://github.com/lightvector/KataGo/releases/download/v1.17.0/b10c384h6nbttflrs.bin.gz",
    # GitHub 加速镜像前缀（多个备用）
    "https://ghfast.top/https://github.com/lightvector/KataGo/releases/download/v1.17.1/b10c384h6nbttflrs.bin.gz",
    "https://gh-proxy.com/https://github.com/lightvector/KataGo/releases/download/v1.17.1/b10c384h6nbttflrs.bin.gz",
    "https://ghproxy.net/https://github.com/lightvector/KataGo/releases/download/v1.17.1/b10c384h6nbttflrs.bin.gz",
]
EXPECTED = 38.2 * 1e6  # 约 38.2 MB（允许 5% 误差）


def download(url: str) -> bool:
    got = os.path.getsize(DEST) if os.path.exists(DEST) else 0
    if got >= EXPECTED * 0.95:
        return True
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    })
    if got > 0:
        req.add_header("Range", f"bytes={got}-")
    for attempt in range(6):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                mode = "ab" if got > 0 else "wb"
                with open(DEST, mode) as f:
                    while True:
                        chunk = r.read(1 << 16)
                        if not chunk:
                            break
                        f.write(chunk)
                        got += len(chunk)
            if got >= EXPECTED * 0.95:
                print(f"[ok] {got/1e6:.1f} MB from {url}", flush=True)
                return True
            print(f"[partial] {got/1e6:.1f} MB, retry", flush=True)
        except Exception as e:
            print(f"[err] {type(e).__name__} from {url}: {e}", flush=True)
        time.sleep(3)
        # 更新 Range 重试
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        })
        if os.path.exists(DEST) and os.path.getsize(DEST) > 0:
            req.add_header("Range", f"bytes={os.path.getsize(DEST)}-")
    return False


for i, src in enumerate(SOURCES):
    print(f"[try {i+1}/{len(SOURCES)}] {src}", flush=True)
    if download(src):
        print("DONE", flush=True)
        sys.exit(0)
print("ALL FAILED", flush=True)
sys.exit(1)
