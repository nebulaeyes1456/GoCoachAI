"""整局 SGF 批量分析（窗口1）。

主路径：调用 KataGo **`kata-analyze` 子命令**对整局 SGF 批量分析：

    katago kata-analyze <sgf> <out.json> \
        -config engine/analysis_example.cfg -model engine/b10c128.bin.gz \
        -analysis-threads 12 -override-config maxVisits=200

解析输出 JSON 的 ``moveInfos``（字段：move/winrate/scoreLead/visits/pv/order
等，以实际输出为准）。KataGo 的胜率视角由 cfg 的
``reportAnalysisWinratesAs = SIDETOMOVE`` 固定为"当前行棋方"。

备用路径：若该版本 KataGo 无 kata-analyze 子命令（命令解析报错），自动
回退到 ``katago analysis`` 引擎协议：一条 query 携带 analyzeTurns=[0..n-1]，
按 turnNumber 收集逐局面响应，且可逐局面回调进度。

输出统一为 ``TurnInfo`` 列表（按 turn 排序），供 review 服务换算每手
before/after 胜率与关键手判定。
"""
from __future__ import annotations

import json
import re
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from ...common import sgf_io
from ...common.settings import BASE_DIR, get_settings
from .engine import KataGoEngine

ENGINE_DIR = BASE_DIR / "engine"
TMP_DIR = BASE_DIR / "data" / "tmp"

# 命令退出后若输出文件不存在/为空，视为"子命令不可用"的常见 stderr 特征
_KATA_ANALYZE_UNKNOWN_SIGNATURES = ("no subcommand", "unknown", "Usage:")

# kata-analyze 子命令支持检测缓存（v1.18.x 主程序已无该子命令，只有
# ``analysis`` 引擎；旧版本如 v1.15.x 有）
_katago_support_cache: dict[str, bool] = {}


def _has_kata_analyze(executable: str) -> bool:
    """检测主程序是否支持 kata-analyze 子命令（结果缓存）。"""
    if executable in _katago_support_cache:
        return _katago_support_cache[executable]
    ok = False
    try:
        proc = subprocess.run(
            [executable, "-help"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=60,
        )
        ok = proc.returncode == 0 and "kata-analyze" in (proc.stdout or "")
    except Exception:
        ok = False
    _katago_support_cache[executable] = ok
    return ok


@dataclass
class TurnInfo:
    """一个局面的分析结果（turn=t 表示第 t 手后的局面，0 基）。

    - ``move``：该局面"当前方即将落下的点"（即第 t+1 手，pass 记 "pass"）；
    - ``winrate``：当前方胜率 0~1（SIDETOMOVE 视角）；
    - ``score_lead``：当前方领先目数；
    - ``best_coord`` / ``pv`` / ``visits``：取自 order=0 的 moveInfo。
    """

    turn_number: int
    current_player: str = ""            # B/W
    winrate: Optional[float] = None
    score_lead: Optional[float] = None
    visits: Optional[int] = None
    best_coord: Optional[str] = None
    pv: list[str] = field(default_factory=list)
    move_infos: list[dict] = field(default_factory=list)
    score_stdev: Optional[float] = None      # 目差不确定度（复杂度指标）
    ownership: list[float] = field(default_factory=list)  # 全盘目数归属 [-1,1]

    def __post_init__(self) -> None:
        if not self.move_infos:
            return
        best = next((m for m in self.move_infos if m.get("order") == 0), None)
        if best is None:
            best = self.move_infos[0]
        if self.winrate is None:
            self.winrate = best.get("winrate")
        if self.score_lead is None:
            self.score_lead = best.get("scoreLead")
        if self.visits is None:
            self.visits = best.get("visits")
        if self.best_coord is None:
            move = str(best.get("move", ""))
            self.best_coord = "" if move.lower() == "pass" else move
        if not self.pv:
            pv = best.get("pv") or []
            self.pv = [
                ("" if str(m).lower() == "pass" else str(m)) for m in pv
            ]


ProgressFn = Callable[[float], None]


def _resolve_paths() -> tuple[str, str, str]:
    """经引擎后端工厂选择后端（T1），返回 (executable, model, cfg_path)。

    后端选择规则见 backend.py：auto 探测 OpenCL 失败自动回退 CPU。
    签名保持 3 元组不变，verify.py 等调用方不受影响。
    """
    from . import backend as engine_backend

    executable, model, _backend_name = engine_backend.resolve_paths()
    cfg_path = str(ENGINE_DIR / "analysis_example.cfg")
    return executable, model, cfg_path


def _max_visits(profile: str) -> int:
    cfg = get_settings()
    visits = cfg.get("katago", {}).get("max_visits", {})
    if isinstance(visits, dict) and profile in visits:
        return int(visits[profile])
    if isinstance(visits, int):
        return visits
    return 200


def _game_to_query(sgf_text: str, profile: str, analyze_turns: list[int]) -> dict:
    """SGF 主线 → analysis 引擎 query（含 AB/AW 摆子）。"""
    parsed = sgf_io.parse_sgf(sgf_text)
    size = parsed.board_size
    moves: list[list[str]] = []
    stones: list[list[str]] = []
    # 摆子/让子：转为 initialStones（AB[aa][bb]... 连续多摆子逐枚提取）
    for prop, color in (("AB", "B"), ("AW", "W")):
        for block in re.finditer(
            rf"{prop}((?:\[[a-zA-Z]*\])+)", sgf_text or "", re.IGNORECASE
        ):
            for m in re.finditer(r"\[([a-zA-Z]*)\]", block.group(1)):
                coord = sgf_io.sgf_to_coord(m.group(1), size)
                if coord:
                    stones.append([color, coord])
    for color, coord in parsed.moves:
        moves.append([color, "pass" if not coord else coord])
    komi = 7.5
    m = re.search(r"KM\s*\[\s*([-\d.]+)", sgf_text or "", re.IGNORECASE)
    if m:
        komi = float(m.group(1))
    return {
        "initialStones": stones,
        "moves": moves,
        "rules": "chinese",
        "komi": komi,
        "boardXSize": size,
        "boardYSize": size,
        "analyzeTurns": analyze_turns,
        "maxVisits": _max_visits(profile),
        "analysisPVLen": 25,
        "includeOwnership": True,  # 全盘目数归属（热图）
    }


def run_kata_analyze_subcommand(
    sgf_path: Path,
    out_path: Path,
    profile: str = "fast",
    timeout: float = 3600.0,
) -> subprocess.CompletedProcess:
    """运行 ``katago kata-analyze`` 子命令，结果写入 out_path。

    -analysis-threads 取 config 的 katago.analysis_threads（默认 1=顺序）。
    """
    executable, model, cfg_path = _resolve_paths()
    visits = _max_visits(profile)
    analysis_threads = int(
        get_settings().get("katago", {}).get("analysis_threads", 1)
    )
    cmd = [
        executable, "kata-analyze",
        str(sgf_path), str(out_path),
        "-config", cfg_path,
        "-model", model,
        "-analysis-threads", str(analysis_threads),
        "-override-config", f"maxVisits={visits}",
    ]
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        cwd=str(ENGINE_DIR),
    )


def parse_analysis_output(raw_text: str) -> list[dict]:
    """解析 kata-analyze 输出文本 → 逐局面响应对象列表。

    兼容两种格式：整文件一个 JSON 数组 / 多个 JSON 对象拼接（逐行或流式）。
    """
    raw = (raw_text or "").strip()
    if not raw:
        return []
    # 1) 整个文件是 JSON 数组
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return [d for d in data if isinstance(d, dict)]
        if isinstance(data, dict):
            return [data]
    except json.JSONDecodeError:
        pass
    # 2) 多个 JSON 值拼接：用 raw_decode 扫描
    results: list[dict] = []
    decoder = json.JSONDecoder()
    idx = 0
    n = len(raw)
    while idx < n:
        while idx < n and raw[idx] in " \t\r\n":
            idx += 1
        if idx >= n:
            break
        try:
            obj, end = decoder.raw_decode(raw, idx)
        except json.JSONDecodeError:
            idx += 1
            continue
        if isinstance(obj, dict):
            results.append(obj)
        idx = end
    return results


def _turn_infos_from_responses(responses: list[dict]) -> list[TurnInfo]:
    infos: list[TurnInfo] = []
    for resp in responses:
        if resp.get("isDuringSearch") or "moveInfos" not in resp:
            continue
        root = resp.get("rootInfo") or {}
        infos.append(
            TurnInfo(
                turn_number=int(resp.get("turnNumber", 0)),
                current_player=str(root.get("currentPlayer", "")),
                winrate=root.get("winrate"),
                score_lead=root.get("scoreLead"),
                visits=root.get("visits"),
                score_stdev=root.get("scoreStdev"),
                ownership=resp.get("ownership") or [],
                move_infos=resp.get("moveInfos") or [],
            )
        )
    infos.sort(key=lambda t: t.turn_number)
    return infos


@dataclass
class AnalysisResult:
    """analyze_sgf 的返回值。"""

    turns: list[TurnInfo] = field(default_factory=list)   # turn 0..n-1
    final_turn: Optional[TurnInfo] = None                 # turn n（最后一手后）


def analyze_sgf(
    sgf_text: str,
    profile: str = "fast",
    progress_cb: Optional[ProgressFn] = None,
    timeout: float = 3600.0,
    include_final: bool = True,
) -> AnalysisResult:
    """分析整局 SGF，返回 AnalysisResult（含最后一局面，供最后一手 delta）。

    - 优先 ``kata-analyze`` 子命令（旧版本 KataGo）；输出文件缺失/为空且
      stderr 表明命令不被支持时，自动回退 ``katago analysis`` 引擎协议；
    - ``progress_cb``：kata-analyze 路径无法逐手回调，仅在解析完成后按批
      回调；引擎协议路径逐局面回调；
    - ``include_final``：额外分析最后一手后的局面（turn n），使最后一手
      也能计算 after 胜率。
    """
    executable, model, cfg_path = _resolve_paths()
    if not Path(executable).exists():
        raise FileNotFoundError(f"引擎不存在: {executable}（先运行 scripts/download_katago.py）")
    if not Path(model).exists():
        raise FileNotFoundError(f"模型不存在: {model}（先运行 scripts/download_katago.py）")

    # v1.18.x 无 kata-analyze 子命令：直接走 analysis 引擎协议（带逐局面进度）
    if not _has_kata_analyze(executable):
        return _analyze_via_engine_protocol(
            sgf_text, profile, progress_cb, timeout, include_final,
            fallback_reason="当前 KataGo 版本无 kata-analyze 子命令，使用 analysis 引擎协议",
        )

    TMP_DIR.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex[:12]
    sgf_path = TMP_DIR / f"analysis-{token}.sgf"
    out_path = TMP_DIR / f"analysis-{token}.json"
    sgf_path.write_text(sgf_text, encoding="utf-8")

    try:
        proc = run_kata_analyze_subcommand(sgf_path, out_path, profile, timeout)
        raw = out_path.read_text(encoding="utf-8", errors="replace") if out_path.exists() else ""
        responses = parse_analysis_output(raw)
        if responses or proc.returncode == 0:
            if not responses and proc.returncode == 0:
                raise RuntimeError(
                    f"kata-analyze 返回 0 但输出为空/不可解析，stderr:\n{proc.stderr[-2000:]}"
                )
            infos = _turn_infos_from_responses(responses)
            if progress_cb:
                progress_cb(1.0)
            # kata-analyze 不分析最后一局面；由调用方用 complement_final_turn 补
            return AnalysisResult(turns=infos, final_turn=None)
        # 命令不存在/未知 → 回退引擎协议
        raise RuntimeError(f"kata-analyze 退出码 {proc.returncode}，stderr:\n{proc.stderr[-2000:]}")
    except (subprocess.TimeoutExpired, RuntimeError, FileNotFoundError) as exc:
        if isinstance(exc, subprocess.TimeoutExpired):
            raise
        return _analyze_via_engine_protocol(
            sgf_text, profile, progress_cb, timeout, include_final, str(exc)
        )
    finally:
        sgf_path.unlink(missing_ok=True)
        out_path.unlink(missing_ok=True)


def _analyze_via_engine_protocol(
    sgf_text: str,
    profile: str,
    progress_cb: Optional[ProgressFn],
    timeout: float,
    include_final: bool,
    fallback_reason: str,
) -> AnalysisResult:
    """备用路径：analysis 引擎逐 turn 查询，逐局面回调进度。

    单 worker 顺序提交（numAnalysisThreads=1，numSearchThreads 由 cfg 控制），
    避免与复盘队列/其他调用争抢 CPU。
    """
    parsed = sgf_io.parse_sgf(sgf_text)
    total_turns = len(parsed.moves)
    if total_turns == 0:
        return AnalysisResult(turns=[], final_turn=None)
    executable, model, cfg_path = _resolve_paths()
    engine = KataGoEngine(executable, model, cfg_path, analysis_threads=1)
    results: dict[int, dict] = {}
    try:
        engine.start()
        last = total_turns if include_final else total_turns - 1
        q = _game_to_query(sgf_text, profile, [0])
        for t in range(0, last + 1):
            req = dict(q)
            req["id"] = f"batch-{uuid.uuid4().hex[:10]}-{t}"
            req["analyzeTurns"] = [t]
            resp = engine.query(req, timeout=timeout)
            if resp and "moveInfos" in resp:
                results[t] = resp
            if progress_cb:
                progress_cb((t + 1) / (last + 1))
    finally:
        engine.stop()
    infos = _turn_infos_from_responses(
        [results[t] for t in sorted(results) if t < total_turns]
    )
    final_turn = None
    if include_final and total_turns in results:
        final_turn = _turn_infos_from_responses([results[total_turns]])
        final_turn = final_turn[0] if final_turn else None
    return AnalysisResult(turns=infos, final_turn=final_turn)


def complement_final_turn(
    sgf_text: str,
    profile: str,
    timeout: float = 600.0,
) -> Optional[dict]:
    """补分析最后一手之后的局面。

    返回 {"winrate": 对手方胜率（SIDETOMOVE）, "score_lead": ...}，
    供 review 服务计算最后一手的 after 值：after = 1 - winrate。
    """
    parsed = sgf_io.parse_sgf(sgf_text)
    n = len(parsed.moves)
    if n == 0:
        return None
    executable, model, cfg_path = _resolve_paths()
    engine = KataGoEngine(executable, model, cfg_path, analysis_threads=1)
    try:
        engine.start()
        req = _game_to_query(sgf_text, profile, [n])
        resp = engine.query(req, timeout=timeout)
        root = (resp or {}).get("rootInfo") or {}
        if "winrate" not in root:
            return None
        return {"winrate": float(root["winrate"]), "score_lead": root.get("scoreLead")}
    finally:
        engine.stop()
