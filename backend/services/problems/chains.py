"""死活题生长链条（Growth Chains，v1.7.0）。

把题库从「平面题集」升级为「有来源、有脉络的成长树」：从布局定式出发，
长出后续的死活/对杀变化题，做题者沿链条练习能看清「这个死活是从哪里来的」。

数据模型（``docs/architecture.md`` §3 / db 迁移 v10-v11）：

- ``problem_chains``：定式链（root_sgf = 定式起点 SGF，含全部手顺）；
- ``problems.chain_id`` / ``problems.chain_step``：题目挂在链上的位置（步序）。

生长流程 ``grow_chain``（契约 §4.3 追加小节）：

1. 读链 ``root_sgf`` → ``utils.replay_position`` 重放全部手顺 → 得当前局面；
2. **局部裁剪**：以局面棋子包围盒外扩 ``chain_crop_pad``（默认 1 格）——
   复用 ``utils.crop_bbox``（与 generator 的中心裁剪同一份「保留完整棋串」
   逻辑）；裁剪后包围盒超过 ``chain_max_bbox``（默认 9×9）则丢弃该分支，
   保证题面始终是角部局部题；题面用 ``utils.setup_sgf`` 生成摆子局
   （白先时前置 ``;B[tt]`` 修正奇偶）；
3. **候选枚举** ``build_candidates``：局部空邻点（按与局部重心的距离排序）
   + 角部要点（涉及的角之 2-1/2-2/2-3/3-3 一带）+ ``pass``（紧迫性检验）；
4. **验题**：复用 ``generator.verify_candidates``（一次 verify_position 批量
   验证，档位取 config ``problems.verify_profile``，死活/对杀为 fine）：
   正解 > 0.95、次优 < 0.3；死活/对杀还要求 ``pass`` 后胜率 <
   ``urgency_max_winrate``（必须现在处理），不达标丢弃该分支；
5. **入库**：``source='chain'``、theme/goal 由 ``utils.classify_theme`` +
   ``classify_goal`` 规则归类、``chain_step`` 写步序、hint/verdict 用规则
   模板、explanation 置 null；题 id 仍为 ``utils.problem_id``（题面哈希），
   ``INSERT OR IGNORE`` 保证重复 grow 幂等、不改动既有步序；
6. **递归生长**：正解落子 + PV 中对手最强应手 → 下一层种子；
   深度 ≤ ``max_depth``，每层最多尝试 ``max_per_level`` 个种子
   （主变 + 上一手 PV 里其余对手应手作为备选，主变不达标时依次重试），
   因此链是一条**主线**：链上第 n 题的题面可由第 n-1 题的「正解 + 对手应手」
   重放得到（每题的 branches.chain.moves 记录完整手顺，测试据此断言）。

幂等性：题 id 由题面 SGF 哈希决定，重复 grow 只会命中 ``INSERT OR IGNORE``
（返回 created=False），第二次新增 0 题、既有 chain_step 不变。

**19 路定式局面的实测口径（v1.7.0 交付实测，务必先读）**：

任务书要求「正解 > 0.95 且次优 < 0.3」（战术题合格线）。在 19 路棋盘上
用真实引擎实测（10 条定式链 × 10~12 候选点 × 逐层验题）：
定式终局局面是**两分**局面——双方都没有「生死攸关」的棋串，且摆子局在
空棋盘上的评估被「谁的子朝向空旷盘面」主导（实测：17 子角部实空只有 3 目
时，KataGo 判该方落后 26 目）。因此 19 路定式局面几乎不可能同时满足
「正解 > 0.95 且次优 < 0.3 且 pass < 0.3」——该口径是为 9 路整盘复盘题
（局部即全局）与古典死活题（allowMoves 局部聚焦）设计的。

为此本模块：

1. 默认仍按契约阈值验题（``chain_answer_min_winrate=0.95`` /
   ``chain_second_max_winrate=0.3``），并把局部聚焦坐标写进
   ``branches.allow_moves``，判题补查沿用同一口径；
2. 阈值可经 config 调整（``problems.chain_answer_min_winrate`` /
   ``chain_second_max_winrate``）——想让定式链立即产出题目，实测把合格线
   放宽到 0.80/0.35 量级即可（同一条链可产出数道「局部要点」题，
   但题目锐度低于古典死活题）；
3. 想要「正统死活题」质量的链，请把种子 SGF 换成**局面本身带生死**的
   定式片段（如雪崩/妖刀走完、角上大龙未活），此时契约阈值即可达标。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ...common import db as db_mod
from ...common import sgf_io
from ...common.settings import get_settings
from ..engine.verify import VerifyResult
from . import store, utils
from .generator import result_dict, verify_candidates

# 链上题的默认目标级位（无对局信息，取与 generator 相同的默认 -5K 中心）
DEFAULT_TARGET_RANK = -5

CHAIN_COLUMNS = "id, name, theme, root_sgf, seed_sgf, description, status, created_at"

# 角部要点偏移（列, 行）：2-1/2-2/2-3/3-3 所在的 3×3 角部方框（1 基）
_CORNER_OFFSETS = tuple(
    (dx, dy) for dx in (1, 2, 3) for dy in (1, 2, 3)
)


class ChainNotFoundError(RuntimeError):
    """链不存在（路由层转 HTTP 404）。"""


class ChainSgfError(RuntimeError):
    """链的 root_sgf 无法解析/重放（路由层转 422）。"""


def _problems_cfg() -> dict:
    cfg = get_settings().get("problems", {})
    return cfg if isinstance(cfg, dict) else {}


# ---------------------------------------------------------------------------
# 数据层：problem_chains CRUD
# ---------------------------------------------------------------------------


def _row_to_chain(row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "theme": row["theme"],
        "root_sgf": row["root_sgf"],
        "seed_sgf": row["seed_sgf"],
        "description": row["description"],
        "status": row["status"],
        "created_at": row["created_at"],
    }


def register_chain(chain: dict, db_path: str | Path | None = None) -> bool:
    """注册/更新一条定式链（幂等）：返回是否新建。

    已存在同 id 时只覆盖内容字段（name/theme/root_sgf/seed_sgf/description），
    保留 created_at 与 status —— 种子脚本可反复执行，改 SGF 后重跑即同步。
    """
    conn = db_mod.connect(db_path)
    try:
        exists = conn.execute(
            "SELECT 1 FROM problem_chains WHERE id=?", (chain["id"],)
        ).fetchone()
        if exists:
            conn.execute(
                "UPDATE problem_chains SET name=?, theme=?, root_sgf=?,"
                " seed_sgf=?, description=? WHERE id=?",
                (
                    chain["name"],
                    chain.get("theme"),
                    chain["root_sgf"],
                    chain.get("seed_sgf"),
                    chain.get("description"),
                    chain["id"],
                ),
            )
        else:
            conn.execute(
                "INSERT INTO problem_chains"
                " (id, name, theme, root_sgf, seed_sgf, description,"
                "  status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    chain["id"],
                    chain["name"],
                    chain.get("theme"),
                    chain["root_sgf"],
                    chain.get("seed_sgf"),
                    chain.get("description"),
                    chain.get("status") or "draft",
                    chain.get("created_at") or store.utcnow(),
                ),
            )
        conn.commit()
        return not exists
    finally:
        conn.close()


def get_chain(
    chain_id: str, db_path: str | Path | None = None
) -> Optional[dict]:
    """按 id 取链；不存在返回 None。"""
    conn = db_mod.connect(db_path)
    try:
        row = conn.execute(
            f"SELECT {CHAIN_COLUMNS} FROM problem_chains WHERE id=?",
            (chain_id,),
        ).fetchone()
    finally:
        conn.close()
    return _row_to_chain(row) if row else None


def update_chain(
    chain_id: str, fields: dict, db_path: str | Path | None = None
) -> None:
    """更新链的部分字段（白名单列，防注入）。"""
    allowed = {"name", "theme", "root_sgf", "seed_sgf", "description", "status"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return
    sets = ", ".join(f"{k}=?" for k in updates)
    conn = db_mod.connect(db_path)
    try:
        conn.execute(
            f"UPDATE problem_chains SET {sets} WHERE id=?",
            (*updates.values(), chain_id),
        )
        conn.commit()
    finally:
        conn.close()


def list_chains(db_path: str | Path | None = None) -> list[dict]:
    """链列表（含题数、实际涉及的主题），按创建时间排序。"""
    conn = db_mod.connect(db_path)
    try:
        rows = conn.execute(
            f"SELECT {CHAIN_COLUMNS} FROM problem_chains"
            " ORDER BY created_at ASC, id ASC"
        ).fetchall()
        counts: dict[str, int] = {}
        themes: dict[str, list[str]] = {}
        for r in conn.execute(
            "SELECT chain_id, theme FROM problems"
            " WHERE chain_id IS NOT NULL AND status='active'"
            " ORDER BY chain_id, COALESCE(chain_step, 9999), id"
        ).fetchall():
            cid = r["chain_id"]
            counts[cid] = counts.get(cid, 0) + 1
            lst = themes.setdefault(cid, [])
            if r["theme"] and r["theme"] not in lst:
                lst.append(r["theme"])
    finally:
        conn.close()
    out = []
    for r in rows:
        item = _row_to_chain(r)
        item["problems_count"] = counts.get(r["id"], 0)
        item["themes"] = themes.get(r["id"], [])
        out.append(item)
    return out


# ---------------------------------------------------------------------------
# 局部裁剪与候选枚举
# ---------------------------------------------------------------------------


def _next_color(moves: list[tuple[str, str]]) -> str:
    """定式手顺之后轮到谁走（无手顺时黑先）。"""
    if not moves:
        return "B"
    return "W" if moves[-1][0].upper() == "B" else "B"


def setup_stones(
    parsed: sgf_io.ParsedSGF, size: int
) -> utils.Position:
    """SGF 的 AB/AW 摆子 → Position（非法坐标跳过）。"""
    out: utils.Position = {}
    for color, coord in parsed.setup:
        try:
            x, y = utils.coord_to_xy(coord)
        except ValueError:
            continue
        if 0 <= x < size and 0 <= y < size:
            out[(x, y)] = color.upper()
    return out


def local_bbox(
    position: utils.Position, size: int, pad: int = 1
) -> tuple[int, int, int, int]:
    """局面棋子包围盒外扩 pad 格（收敛到棋盘内）。"""
    x0, y0, x1, y1 = utils.bbox_of(position.keys())
    return (
        max(0, x0 - pad),
        max(0, y0 - pad),
        min(size - 1, x1 + pad),
        min(size - 1, y1 + pad),
    )


def crop_local(
    position: utils.Position,
    size: int,
    pad: int = 1,
    max_bbox: int = 9,
) -> Optional[utils.Position]:
    """按局面包围盒外扩 pad 裁剪局部；保留完整棋串。

    裁剪后（含被补全的整串棋）包围盒超过 ``max_bbox`` → 返回 None
    （题面必须是角部局部题，超出即丢弃该分支）；局面为空同样返回 None。
    """
    if not position:
        return None
    x0, y0, x1, y1 = local_bbox(position, size, pad)
    kept = utils.crop_bbox(position, x0, y0, x1, y1, size)
    if not kept:
        return None
    kx0, ky0, kx1, ky1 = utils.bbox_of(kept.keys())
    if (kx1 - kx0 + 1) > max_bbox or (ky1 - ky0 + 1) > max_bbox:
        return None
    return kept


def _corner_points(kept: utils.Position, size: int) -> list[str]:
    """与局部区域（外扩 2 格）相交的角之角部要点（界面坐标）。"""
    x0, y0, x1, y1 = utils.bbox_of(kept.keys())
    out: list[str] = []
    corners = (
        (0, 0, 1, 1),                    # 左下 (dx 向右, dy 向上)
        (size - 1, 0, -1, 1),            # 右下
        (0, size - 1, 1, -1),            # 左上
        (size - 1, size - 1, -1, -1),    # 右上
    )
    for cx, cy, sx, sy in corners:
        pts = [
            (cx + sx * dx, cy + sy * dy) for dx, dy in _CORNER_OFFSETS
        ]
        # 只保留与局部区域（外扩 2 格）相交的角，避免给无关角送候选点
        if not any(
            x0 - 2 <= px <= x1 + 2 and y0 - 2 <= py <= y1 + 2
            for px, py in pts
        ):
            continue
        for px, py in pts:
            if 0 <= px < size and 0 <= py < size and (px, py) not in kept:
                out.append(utils.xy_to_coord(px, py, size))
    return out


def build_candidates(
    kept: utils.Position,
    size: int,
    max_candidates: int = 12,
) -> list[str]:
    """候选点 = 局部空邻点 + 角部要点 + pass（去重、已占点过滤）。

    空邻点按「到局部重心距离」排序（近的优先），上限 ``max_candidates``；
    ``pass`` 始终保留在末尾（generator.build_candidates 同样处理），
    死活/对杀题据此检验紧迫性。
    """
    cands: list[str] = []
    seen: set[str] = set()

    def add(coord: str) -> None:
        n = utils.normalize_coord(coord)
        if n == "pass" or n in seen:
            return
        try:
            xy = utils.coord_to_xy(n)
        except ValueError:
            return
        if xy in kept:
            return
        seen.add(n)
        cands.append(n)

    xs = [p[0] for p in kept]
    ys = [p[1] for p in kept]
    cx = sum(xs) / len(xs) if xs else 0.0
    cy = sum(ys) / len(ys) if ys else 0.0
    for x, y in sorted(kept, key=lambda p: (abs(p[0] - cx) + abs(p[1] - cy))):
        for nx, ny in utils.neighbors(x, y, size):
            if (nx, ny) not in kept:
                add(utils.xy_to_coord(nx, ny, size))
    for coord in _corner_points(kept, size):
        add(coord)
    if len(cands) > int(max_candidates):
        cands = cands[: int(max_candidates)]
    cands.append("pass")
    return cands


# ---------------------------------------------------------------------------
# 主题 / 目标 归类（规则，参考 utils.classify_theme 与 classify_goals.py 的思路）
# ---------------------------------------------------------------------------


def _meaningful_groups(
    position: utils.Position, size: int, color: str
) -> list[tuple[set[tuple[int, int]], int]]:
    """color 方「有意义」的棋串及气数（长度≥2 或仅 1 气的单子）。"""
    out: list[tuple[set[tuple[int, int]], int]] = []
    visited: set[tuple[int, int]] = set()
    for pt in sorted(position):
        if pt in visited or position[pt] != color:
            continue
        g = utils.group_at(position, pt[0], pt[1], size)
        visited |= g
        libs = len(utils.group_liberties(g, position, size))
        if len(g) >= 2 or libs <= 1:
            out.append((g, libs))
    return out


def _tendon_count(position: utils.Position, size: int, color: str) -> int:
    """color 方棋筋数：同色四邻 ≥2、气数 ≤2，且移除本子后邻居不再连通。"""
    n = 0
    for (x, y), c in position.items():
        if c != color:
            continue
        nbrs = [
            p for p in utils.neighbors(x, y, size)
            if position.get(p) == color
        ]
        if len(nbrs) < 2:
            continue
        without = {k: v for k, v in position.items() if k != (x, y)}
        g = utils.group_at(without, nbrs[0][0], nbrs[0][1], size)
        if all(p in g for p in nbrs):
            continue  # 移除后仍连通，不是棋筋
        libs = len(
            utils.group_liberties(
                utils.group_at(position, x, y, size), position, size
            )
        )
        if libs <= 2:
            n += 1
    return n


def classify_goal(
    theme: str,
    position: utils.Position,
    size: int,
    solver: str,
) -> str:
    """题目标标（goal）归类：主题映射 + 死活题的棋筋/危机信号规则。

    取值与题库既有目标词表一致：做活/杀棋/对杀/逃棋筋/吃棋筋/收官最大/中盘要点。
    """
    if theme == "capturing_race":
        return "对杀"
    if theme == "endgame":
        return "收官最大"
    if theme == "middle":
        return "中盘要点"

    me = solver.upper()
    opp = "W" if me == "B" else "B"
    me_t = _tendon_count(position, size, me)
    opp_t = _tendon_count(position, size, opp)
    if me_t and not opp_t:
        return "逃棋筋"
    if opp_t and not me_t:
        return "吃棋筋"
    me_urgent = any(libs <= 3 for _g, libs in _meaningful_groups(position, size, me))
    opp_urgent = any(libs <= 3 for _g, libs in _meaningful_groups(position, size, opp))
    if me_urgent and opp_urgent:
        return "对杀"
    if opp_urgent:
        return "杀棋"
    return "做活"  # 仅己方危急或双方均不急（题面已由 classify_theme 收窄）


# ---------------------------------------------------------------------------
# 生长
# ---------------------------------------------------------------------------


@dataclass
class _Node:
    """一个待出题的生长节点：定式摆子 + 从定式起点算起的完整手顺。"""

    stones: utils.Position                 # 起点摆子（root_sgf/seed_sgf 的 AB/AW）
    moves: list[tuple[str, str]]           # 含定式与生长出的应手
    depth: int                             # 距定式终局的手数层级（0 = 定式终局）
    step: int                              # 父题步序；本题步序 = step + 1
    parent_id: Optional[str] = None        # 父题 id（链谱系）
    reply: Optional[str] = None            # 由父题题面走到本题题面的对手应手
    alt_replies: list[str] = field(default_factory=list)  # 备选对手应手（重试用）

    def position(self, size: int) -> utils.Position:
        """节点局面 = 起点摆子 + 手顺重放（含提子）。"""
        return utils.apply_moves(dict(self.stones), self.moves, size)


def region_of(
    kept: utils.Position, size: int, pad: int = 2
) -> list[str]:
    """局部聚焦区域 = 题面棋子包围盒外扩 pad 格（v0.9.8 古典题验题口径）。

    作为 verify_position 的 ``allow_moves``：19 路角部摆子局在空棋盘上
    全盘搜索时一选常是局外大场、胜率被子力多寡主导；局部聚焦后胜率才
    反映局部攻杀的成败（死活/对杀题因此能过「正解 > 0.95」的战术线）。
    """
    x0, y0, x1, y1 = utils.bbox_of(kept.keys())
    x0, y0 = max(0, x0 - pad), max(0, y0 - pad)
    x1, y1 = min(size - 1, x1 + pad), min(size - 1, y1 + pad)
    out: list[str] = []
    for x in range(x0, x1 + 1):
        for y in range(y0, y1 + 1):
            out.append(utils.xy_to_coord(x, y, size))
    return out


@dataclass
class _Outcome:
    """一次成功的验题结果。"""

    kept: utils.Position
    setup_sgf: str
    theme: str
    goal: str
    solver: str
    profile: str
    best: VerifyResult
    second: Optional[VerifyResult]
    results: list[VerifyResult]
    candidates: list[str]
    allow_moves: list[str]


def _reply_options(best: VerifyResult, max_replies: int) -> list[str]:
    """正解 PV 中对手的应手候选（pv[1], pv[3], ...），过滤 pass/空。"""
    out: list[str] = []
    pv = list(best.pv or [])
    for i in range(1, len(pv), 2):
        c = utils.normalize_coord(pv[i])
        if c == "pass" or c in out:
            continue
        out.append(c)
        if len(out) >= max_replies:
            break
    if not out and best.best_coord:
        c = utils.normalize_coord(best.best_coord)
        if c != "pass":
            out.append(c)
    return out


def _attempt(
    node: _Node,
    size: int,
    pad: int,
    max_bbox: int,
    max_candidates: int,
    profile: Optional[str],
    urgent_max: float,
    verify_profile: dict,
    cfg_region_pad: int = 2,
    answer_min: float = 0.95,
    second_max: float = 0.3,
) -> Optional[_Outcome]:
    """对一个节点裁剪局部 + 验题；不达标返回 None。

    合格线（契约 §4.3：正解 > ``answer_min``、次优 < ``second_max``；
    死活/对杀另需 pass 后胜率 < ``urgent_max``）默认即契约值，可经
    config ``problems.chain_answer_min_winrate`` /
    ``chain_second_max_winrate`` 调整（19 路定式局面的实测口径见 docstring）。
    """
    try:
        position = node.position(size)
    except ValueError:
        return None
    kept = crop_local(position, size, pad=pad, max_bbox=max_bbox)
    if kept is None:
        return None
    solver = _next_color(node.moves)
    # 定式阶段不属于官子：total_moves=0 → 不触发 endgame 归类
    theme = utils.classify_theme(kept, position, size, 1, 0)
    setup = utils.setup_sgf(kept, solver, size)
    candidates = build_candidates(kept, size, max_candidates=max_candidates)
    if not candidates:
        return None
    if profile:
        prof = profile
    else:
        prof = verify_profile.get(theme) or "standard"
    urgent = theme in ("life_death", "capturing_race")
    allow = region_of(kept, size, int(cfg_region_pad))
    best, second, results, pass_ok = verify_candidates(
        setup, candidates, prof, urgent_max, allow_moves=allow
    )
    if best is None or (best.winrate or 0.0) <= float(answer_min):
        return None
    if second is not None and (second.winrate or 0.0) >= float(second_max):
        return None
    if urgent and not pass_ok:
        return None
    return _Outcome(
        kept=kept,
        setup_sgf=setup,
        theme=theme,
        goal=classify_goal(theme, kept, size, solver),
        solver=solver,
        profile=prof,
        best=best,
        second=second,
        results=results,
        candidates=candidates,
        allow_moves=allow,
    )


def _problem_row(
    chain: dict,
    node: _Node,
    step: int,
    outcome: _Outcome,
    size: int,
    target_rank: int,
) -> dict:
    """验题结果 → problems 表行（branches 结构与 generator 一致，便于判题复用）。"""
    best, second = outcome.best, outcome.second
    gap = (best.winrate or 0.0) - (
        second.winrate if second and second.winrate is not None else 0.0
    )
    _, rank_min, rank_max = utils.difficulty_from_gap(gap, target_rank)
    xs = [p[0] for p in outcome.kept]
    ys = [p[1] for p in outcome.kept]
    center = utils.xy_to_coord(
        int(round(sum(xs) / len(xs))), int(round(sum(ys) / len(ys))), size
    )
    branches = {
        "solver": outcome.solver,
        "answer": result_dict(best),
        "candidates": [result_dict(r) for r in outcome.results],
        "profile": outcome.profile,
        # 局部聚焦区域（v1.7.0）：判题补查同一口径，避免全盘/局部两套胜率
        "allow_moves": outcome.allow_moves,
        "verified_at": store.utcnow(),
        # 链谱系：完整手顺 + 步序（测试据此回放断言「第 n 题题面可由
        # 第 n-1 题的正解 + 对手应手重放得到」）
        "chain": {
            "id": chain["id"],
            "name": chain["name"],
            "step": step,
            "parent_id": node.parent_id,
            "reply": node.reply,
            "moves": [[c, coord] for c, coord in node.moves],
        },
    }
    return {
        "id": utils.problem_id(outcome.setup_sgf, outcome.theme),
        "source": "chain",
        "review_id": None,
        "theme": outcome.theme,
        "goal": outcome.goal,
        "rank_min": rank_min,
        "rank_max": rank_max,
        "setup_sgf": outcome.setup_sgf,
        "answer": best.coord,
        "branches": json.dumps(branches, ensure_ascii=False),
        "verdict": utils.verdict_text(
            outcome.theme,
            outcome.solver,
            best.coord,
            best.winrate or 0.0,
            second.coord if second else None,
            second.winrate if second else None,
        ),
        "hint": utils.hint_text(outcome.theme, outcome.solver, center, size),
        "explanation": None,  # 讲解按需由 coach/explain 生成（question 表缓存）
        "status": "active",
        "chain_id": chain["id"],
        "chain_step": step,
        "created_at": store.utcnow(),
    }


def _child_node(
    node: _Node,
    outcome: _Outcome,
    step: int,
    problem_id: str,
    max_replies: int,
) -> Optional[_Node]:
    """下一层种子 = 正解 + 对手最强应手；无可用应手返回 None。"""
    opp = "W" if outcome.solver.upper() == "B" else "B"
    replies = _reply_options(outcome.best, max_replies)
    if not replies:
        return None
    moves = list(node.moves) + [
        (outcome.solver.upper(), outcome.best.coord),
        (opp, replies[0]),
    ]
    return _Node(
        stones=dict(node.stones),
        moves=moves,
        depth=node.depth + 1,
        step=step,
        parent_id=problem_id,
        reply=replies[0],
        alt_replies=replies[1:],
    )


def _variants(node: _Node, size: int, max_per_level: int) -> list[_Node]:
    """本层的尝试顺序：主变优先，其后是上一手 PV 里其余对手应手。

    备选只换最后一手（对手应手），题面因此仍在同一条主线的邻近分支上。
    """
    out = [node]
    for alt in node.alt_replies[: max(0, int(max_per_level) - 1)]:
        moves = list(node.moves)
        if not moves:
            break
        color = "W" if moves[-1][0].upper() == "B" else "B"
        moves = moves[:-1] + [(color, alt)]
        out.append(
            _Node(
                stones=dict(node.stones),
                moves=moves,
                depth=node.depth,
                step=node.step,
                parent_id=node.parent_id,
                reply=alt,
            )
        )
    return out


def grow_chain(
    chain_id: str,
    max_depth: int = 3,
    max_per_level: int = 3,
    profile: Optional[str] = None,
    db_path: str | Path | None = None,
) -> dict:
    """沿定式链生长题目（契约 §4.3 追加小节 POST /chains/{id}/grow）。

    返回 ``{"chain_id", "added", "discarded", "problems": [brief...],
    "steps": n}``：added = 本次新入库题数（重复 grow 为 0），
    discarded = 验题未通过/局部超框的丢弃次数。

    ``max_depth``：从定式终局算起的层数（默认 3 → 最多 3 道链上题）；
    ``max_per_level``：每层最多尝试的种子数（主变 + 备选对手应手）；
    ``profile``：验题档位，缺省按 config ``problems.verify_profile``
    （死活/对杀 fine，其余 fallback 到 "standard"）。
    """
    cfg = _problems_cfg()
    pad = int(cfg.get("chain_crop_pad", 1))
    max_bbox = int(cfg.get("chain_max_bbox", 9))
    max_candidates = int(cfg.get("max_candidates", 12))
    urgent_max = float(cfg.get("urgency_max_winrate", 0.3))
    region_pad = int(cfg.get("chain_region_pad", 2))
    answer_min = float(cfg.get("chain_answer_min_winrate", 0.95))
    second_max = float(cfg.get("chain_second_max_winrate", 0.3))
    verify_profile = cfg.get("verify_profile", {}) or {}
    target_rank = int(cfg.get("chain_target_rank", DEFAULT_TARGET_RANK))

    chain = get_chain(chain_id, db_path)
    if chain is None:
        raise ChainNotFoundError(f"题链不存在: {chain_id}")
    # 起点：有 seed_sgf（定式完成棋形摆子局）就用它，否则从 root_sgf 重放
    seed_sgf = (chain.get("seed_sgf") or "").strip()
    parsed = sgf_io.parse_sgf(seed_sgf or chain["root_sgf"])
    size = int(parsed.board_size)
    stones = setup_stones(parsed, size)
    moves = list(parsed.moves)
    try:
        utils.apply_moves(dict(stones), moves, size)
    except ValueError as exc:
        raise ChainSgfError(f"链 {chain_id} 的定式 SGF 无法重放: {exc}") from exc

    node: Optional[_Node] = _Node(stones=stones, moves=moves, depth=0, step=0)
    added = 0
    discarded = 0
    problems: list[dict] = []
    last_step = 0
    while node is not None and node.depth < int(max_depth):
        picked: Optional[tuple[_Node, _Outcome]] = None
        for variant in _variants(node, size, max_per_level):
            outcome = _attempt(
                variant, size, pad, max_bbox, max_candidates, profile,
                urgent_max, verify_profile, region_pad, answer_min, second_max,
            )
            if outcome is None:
                discarded += 1
                continue
            picked = (variant, outcome)
            break
        if picked is None:
            break  # 本层长不出题，主线到此为止（其余分支不强行接续）
        variant, outcome = picked
        step = variant.step + 1
        row = _problem_row(chain, variant, step, outcome, size, target_rank)
        created = store.insert_problem(row, db_path)
        if created:
            added += 1
        last_step = step
        problems.append({
            "id": row["id"],
            "theme": row["theme"],
            "goal": row["goal"],
            "setup_sgf": row["setup_sgf"],
            "hint": row["hint"],
            "rank_min": row["rank_min"],
            "rank_max": row["rank_max"],
            "chain_id": row["chain_id"],
            "chain_step": row["chain_step"],
            "answer": row["answer"],
        })
        node = _child_node(variant, outcome, step, row["id"], max_per_level)

    if added and chain.get("status") != "active":
        update_chain(chain_id, {"status": "active"}, db_path)
    return {
        "chain_id": chain_id,
        "added": added,
        "discarded": discarded,
        "steps": last_step,
        "problems": problems,
    }
