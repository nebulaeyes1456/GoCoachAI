"""串行执行全部古典题导入（挂机批处理，用后删除）。

顺序：官子谱（答案）→ 忘忧清乐集（答案）→ 玄览（答案）
     → 玄玄棋经（auto）→ 碁经众妙（auto）→ 发阳论（auto）
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(r"c:\Users\31878\Documents\GoCoachAI2")
PY = ROOT / ".venv" / "Scripts" / "python.exe"
IMP = ROOT / "scripts" / "import_classic_answers.py"

JOBS = [
    ("官子谱(答案)", ROOT / "data" / "library" / "_guanzipu_extract" / "guanzipu",
     ["--label", "官子谱"]),
    ("忘忧清乐集(答案)", ROOT / "data" / "library" / "import_classics" / "wangyou",
     ["--label", "忘忧清乐集"]),
    ("玄览(答案)", ROOT / "data" / "library" / "_xuanlan_raw",
     ["--label", "玄览"]),
    ("玄玄棋经(auto)", ROOT / "data" / "library" / "import_classics" / "xxqj",
     ["--label", "玄玄棋经", "--auto"]),
    ("碁经众妙(auto)", ROOT / "data" / "library" / "import_classics" / "gokyoshumyo",
     ["--label", "碁经众妙", "--auto"]),
    ("发阳论(auto)", ROOT / "data" / "library" / "import_classics" / "hatsuyoron",
     ["--label", "发阳论", "--auto"]),
]


def main() -> int:
    for name, src, extra in JOBS:
        print(f"\n========== {name} 开始 ==========", flush=True)
        cmd = [str(PY), "-u", str(IMP), "--src", str(src),
               "--profile", "standard"] + extra
        r = subprocess.run(cmd, cwd=str(ROOT))
        print(f"========== {name} 结束 code={r.returncode} ==========", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
