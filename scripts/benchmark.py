"""KataGo 性能基准（窗口1）。

用同一 9 路 SGF，分别跑 OpenCL 与 EigenAVX2 版（visits=200），对比单手耗时；
输出推荐配置（哪个 exe 快就用哪个），回填 ``config.yaml`` 的
``katago.executable``。

- 默认用 ``kata-analyze`` 子命令跑整局（与复盘生产路径一致）；
  若该版本不支持该子命令，自动回退 analysis 引擎逐 turn 协议；
- OpenCL 报错自动回退 CPU 版并提示；
- 结果写 ``engine/benchmark_result.json``（含各版耗时、推荐项、备注）。

用法：
    python scripts/benchmark.py                     # 基准 + 回填 config
    python scripts/benchmark.py --no-write-config   # 只测不回填
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENGINE_DIR = PROJECT_ROOT / "engine"
sys.path.insert(0, str(PROJECT_ROOT))

from backend.common.settings import get_settings, save_settings  # noqa: E402
from backend.services.engine.engine import EngineError, KataGoEngine  # noqa: E402

# 9 路固定开局 20 手（合法序列，保证两版引擎分析同一局面）
BENCH_SGF = (
    "(;GM[1]FF[4]CA[UTF-8]SZ[9]KM[7.5]"
    "PB[bench-black]PW[bench-white]"
    ";B[cc];W[gg];B[gc];W[cg];B[ee];W[ec];B[ed];W[dc];B[fd];W[fe]"
    ";B[gd];W[ge];B[hd];W[he];B[ef];W[df];B[de];W[dd];B[cf];W[ce])"
)
VISITS = 200


def _engine_pair() -> dict[str, Path]:
    exes = {
        "opencl": ENGINE_DIR / "katago-opencl.exe",
        "eigenavx2": ENGINE_DIR / "katago-eigenavx2.exe",
    }
    missing = [k for k, p in exes.items() if not p.exists()]
    if missing:
        print(f"缺少引擎: {missing}，请先运行 scripts/download_katago.py", file=sys.stderr)
        sys.exit(2)
    return exes


def _model_cfg() -> tuple[Path, Path]:
    cfg = get_settings()
    model = Path(cfg["katago"]["model"])
    if not model.is_absolute():
        model = PROJECT_ROOT / model
    cfg_path = ENGINE_DIR / "analysis_example.cfg"
    return model, cfg_path


def bench_kata_analyze(exe: Path, model: Path, cfg_path: Path,
                       visits: int, turns: int = 20,
                       timeout: float = 1800.0) -> float:
    """用 kata-analyze 子命令跑整局，返回总耗时（秒）。

    注意 numAnalysisThreads=1（cfg numSearchThreads 已用满 CPU，避免争抢）。
    """
    sgf_path = ENGINE_DIR / "bench_9x9.sgf"
    out_path = ENGINE_DIR / "bench_9x9.json"
    sgf_path.write_text(BENCH_SGF, encoding="utf-8")
    out_path.unlink(missing_ok=True)
    cmd = [
        str(exe), "kata-analyze",
        str(sgf_path), str(out_path),
        "-config", str(cfg_path),
        "-model", str(model),
        "-analysis-threads", "1",
        "-override-config", f"maxVisits={visits}",
    ]
    t0 = time.perf_counter()
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace",
                          timeout=timeout, cwd=str(ENGINE_DIR))
    elapsed = time.perf_counter() - t0
    if proc.returncode != 0:
        raise EngineError(
            f"kata-analyze 退出码 {proc.returncode}\n"
            f"stderr: {proc.stderr[-2000:]}"
        )
    if not out_path.exists() or out_path.stat().st_size == 0:
        raise EngineError("kata-analyze 未产生输出文件")
    return elapsed


def bench_engine_protocol(exe: Path, model: Path, cfg_path: Path,
                          visits: int, turns: int = 20,
                          timeout: float = 1800.0) -> float:
    """analysis 引擎协议逐 turn 计时（总耗时秒）。"""
    from backend.common import sgf_io

    parsed = sgf_io.parse_sgf(BENCH_SGF)
    moves = [[c, "pass" if not p else p] for c, p in parsed.moves[:turns]]
    engine = KataGoEngine(exe, model, cfg_path, analysis_threads=1)
    engine.start()
    try:
        t0 = time.perf_counter()
        for t in range(len(moves)):
            engine.query(
                {
                    "moves": moves[:t],
                    "rules": "chinese",
                    "komi": 7.5,
                    "boardXSize": 9,
                    "boardYSize": 9,
                    "analyzeTurns": [t],
                    "maxVisits": visits,
                },
                timeout=timeout,
            )
        return time.perf_counter() - t0
    finally:
        engine.stop()


def main() -> int:
    parser = argparse.ArgumentParser(description="KataGo OpenCL vs EigenAVX2 基准")
    parser.add_argument("--no-write-config", action="store_true",
                        help="只测不回填 config.yaml")
    parser.add_argument("--turns", type=int, default=20, help="基准手数")
    parser.add_argument("--visits", type=int, default=VISITS)
    args = parser.parse_args()
    visits = args.visits

    exes = _engine_pair()
    model, cfg_path = _model_cfg()
    if not model.exists():
        print(f"模型不存在: {model}，请先运行 scripts/download_katago.py", file=sys.stderr)
        sys.exit(2)

    results: dict[str, dict] = {}
    for kind in ("opencl", "eigenavx2"):
        exe = exes[kind]
        print(f"\n== 基准 {kind}: {exe} ==")
        try:
            try:
                total = bench_kata_analyze(exe, model, cfg_path, visits,
                                           turns=args.turns)
                mode = "kata-analyze"
            except EngineError as exc:
                print(f"  kata-analyze 失败，回退引擎协议: {exc}")
                total = bench_engine_protocol(exe, model, cfg_path, visits,
                                              turns=args.turns)
                mode = "engine-protocol"
            per_move = total / args.turns
            results[kind] = {
                "exe": str(exe), "mode": mode,
                "total_seconds": round(total, 3),
                "seconds_per_move": round(per_move, 3),
                "ok": True,
            }
            print(f"  完成: 总 {total:.1f}s / 单手 {per_move:.2f}s（{mode}）")
        except (EngineError, subprocess.TimeoutExpired) as exc:
            results[kind] = {
                "exe": str(exe), "ok": False,
                "error": str(exc)[:2000],
            }
            print(f"  失败: {str(exc)[:300]}")

    # ---- 推荐与回填 ----
    oks = {k: v for k, v in results.items() if v.get("ok")}
    if not oks:
        print("\n两版引擎均失败，无法给出推荐。", file=sys.stderr)
        sys.exit(1)
    best_kind = min(oks, key=lambda k: oks[k]["seconds_per_move"])
    best = oks[best_kind]
    note = ""
    if best_kind == "opencl":
        note = "OpenCL（核显）快于 EigenAVX2"
    else:
        note = "EigenAVX2（纯 CPU）快于 OpenCL"
    rel_exe = str(Path(best["exe"]).relative_to(PROJECT_ROOT)).replace("\\", "/")

    print(f"\n== 推荐 ==\n  {best_kind}: {best['exe']}")
    print(f"  单手耗时 {best['seconds_per_move']:.2f}s（visits={visits}, 9 路）")
    print(f"  说明: {note}")

    if not args.no_write_config:
        save_settings({"katago": {"executable": rel_exe}})
        print(f"  已回填 config.yaml: katago.executable = {rel_exe}")

    out = ENGINE_DIR / "benchmark_result.json"
    out.write_text(
        json.dumps(
            {
                "visits": visits,
                "turns": args.turns,
                "results": results,
                "recommended": best_kind,
                "recommended_exe": rel_exe,
                "note": note,
                "config_written": not args.no_write_config,
            },
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )
    print(f"  基准结果已写入 {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
