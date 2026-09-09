"""生成 N 手自对弈 SGF 测试棋谱（窗口1 工具）。

用 KataGo GTP 引擎自对弈 N 手（低 visits），生成合法 SGF，供复盘耗时
验收与后续窗口测试使用。

用法：
    python scripts/gen_test_sgf.py [手数=100] [visits=10]

依赖：engine/katago-opencl.exe（或 config 中的 executable）、
engine/default_gtp.cfg（下载脚本解压保留）、engine/b10c128.bin.gz。
输出：data/tmp/gen19_100.sgf。
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from backend.common.sgf_io import coord_to_sgf  # noqa: E402

ENGINE = ROOT / "engine" / "katago-opencl.exe"
CFG = ROOT / "engine" / "default_gtp.cfg"   # 官方 GTP 配置（zip 内提取）
OUT = ROOT / "data" / "tmp" / "gen19_100.sgf"

N = int(sys.argv[1]) if len(sys.argv) > 1 else 100
VISITS = int(sys.argv[2]) if len(sys.argv) > 2 else 10

proc = subprocess.Popen(
    [str(ENGINE), "gtp", "-config", str(CFG), "-model",
     str(ROOT / "engine" / "b10c128.bin.gz"),
     "-override-config", f"maxVisits={VISITS}"],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    text=True, encoding="utf-8", errors="replace", bufsize=1,
    cwd=str(ENGINE.parent),
)


def gtp(cmd: str) -> str:
    proc.stdin.write(cmd + "\n")
    proc.stdin.flush()
    out_lines = []
    while True:
        line = proc.stdout.readline()
        if line == "":
            err = proc.stderr.read() if proc.stderr else ""
            raise RuntimeError(f"gtp 进程退出: {err[-500:]}")
        line = line.strip()
        if not line:
            break
        out_lines.append(line)
    return "\n".join(out_lines)


gtp("boardsize 19")
gtp("komi 7.5")
gtp("clear_board")

sgf = "(;GM[1]FF[4]CA[UTF-8]SZ[19]KM[7.5]PB[GenB]PW[GenW]"
try:
    for i in range(1, N + 1):
        color = "B" if i % 2 == 1 else "W"
        resp = gtp(f"genmove {color}")
        # 响应形如 "= Q16"
        move = resp.splitlines()[0].split(" ", 1)[1].strip().lower() if "=" in resp else ""
        if not move or move == "resign":
            break
        if move == "pass":
            move = "tt"
        else:
            # GTP 坐标（Q16）→ SGF 坐标（pd）
            move = coord_to_sgf(move.upper(), 19)
        sgf += f";{color}[{move}]"
except Exception as exc:
    print(f"genmove 失败于第 {len(sgf)//8} 手: {exc}", file=sys.stderr)
finally:
    gtp("quit")
    proc.stdin.close()
    proc.wait(timeout=10)

sgf += ")"
OUT.write_text(sgf, encoding="utf-8")
n_moves = sgf.count(";") - 1  # 第一个分号是根节点
print(f"已生成 {OUT}，共 {n_moves} 手")
