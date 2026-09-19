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

**19 路局面的验收口径（v1.7.1 实测，务必先读）**：

任务书最初要求「正解胜率 > 0.95 且次优 < 0.3」。真实引擎实测（10 条定式链
× 10~12 候选点 × 逐层验题）：19 路**整盘**摆子局的胜负被「谁朝向空旷盘面」
主导（例：17 子角部实空只有 3 目时 KataGo 判落后 26 目），局部一手摆动不了
整盘胜率 → 全部丢光。为此提供三种口径（``chain_verify_mode``）：

- ``local_board``（默认）：把局部棋形裁成**小棋盘**（保留与角部两条边线的
  距离）再按契约阈值验题——小棋盘上局部攻杀就是全局内容，与 app 里 9 路
  整盘复盘题（正解胜率 0.99）同一尺度；
- ``local_death``：局部死活画像（与 v1.5.0 深度讲解同一套推演）——正解须
  达成目标（目标区归属 ≥ ``chain_own_min``）、死活/对杀须现在处理（脱先
  损失 ≥ ``chain_tenuki_min``）、次优点不得同样达成；
- ``winrate``：契约原文口径（19 路整盘 + allowMoves 局部聚焦）。

**实测结论（v1.7.1）**：三种口径在现有 10 条定式种子上都产出 0 题——定式
终局是两分局面，本来就没有「一手定生死」的棋串（正确判读）。补测古典死活题
（真死活）同样过不了阈值，原因是结构性的：**角部死活里防守方子力天然少于
围攻方**，活棋只多几目，胜率（哪怕在小棋盘上）也上不去；而「正解 > 0.95」
要求先手方本来就领先。因此要长出达标题，种子必须是**先手方明显领先且有一手
定生死**的局面（大龙对杀、打入被围的攻杀），而不是「白先活棋」型。改完种子
先用 ``scripts/check_chain_seeds.py`` 体检（会同时给出三种口径的读数）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Optional

from ...common import db as db_mod
from ...common import sgf_io
from ...common.settings import get_settings
from ..engine.analyze_sgf import _game_to_query, _resolve_paths
from ..engine.engine import KataGoEngine
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


def corner_board(
    kept: utils.Position, size: int, margin: int = 2, min_size: int = 5
) -> tuple[int, utils.Position, int, int]:
    """把角部棋形搬到小棋盘（保留与两条边线的距离）→ (sub_size, mapping)。

    只改棋盘大小（必要时平移），棋形本身不动 —— 角上「贴边」的气数关系因此
    原样保留，另外两侧留 ``margin`` 路空余。

    为什么需要它：19 路摆子局在**空棋盘**上的胜负由「谁朝向空旷盘面」决定
    （实测：17 子角部实空只有 3 目时判落后 26 目），局部死活的一手摆动不了
    整盘胜率。把局部裁成小棋盘后，局部攻杀就是全局内容——与 app 里 9 路
    整盘复盘题（正解胜率 0.99）同一个道理，也正是「只讲局部死活」的字面含义。
    """
    x0, y0, x1, y1 = utils.bbox_of(kept.keys())
    left = x0 <= size - 1 - x1
    bottom = y0 <= size - 1 - y1
    span_x = (x1 + 1) if left else (size - x0)
    span_y = (y1 + 1) if bottom else (size - y0)
    sub = max(span_x + margin, span_y + margin, int(min_size))
    dx = 0 if left else sub - size
    dy = 0 if bottom else sub - size
    mapping = {(x + dx, y + dy): c for (x, y), c in kept.items()}
    return sub, mapping, dx, dy


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
    local: dict = field(default_factory=dict)   # 局部死活画像（local_death 口径）
    verify_board: dict = field(default_factory=dict)  # 小棋盘验题信息（local_board）


# ---------------------------------------------------------------------------
# 局部死活画像（口径同 v1.5.0 深度讲解 explainer.py 与 scripts/classify_result.py）
# ---------------------------------------------------------------------------


def _seq_with(setup_sgf: str, coords: list[str]) -> list[list[str]]:
    """在题面基础上依次落子（行棋方由题面奇偶决定）→ 引擎 moves 序列。"""
    parsed = sgf_io.parse_sgf(setup_sgf)
    seq = [[c, "pass" if not p else p] for c, p in parsed.moves]
    color = "W" if len(seq) % 2 else "B"
    for cd in coords:
        c = utils.normalize_coord(cd)
        seq.append([color, "pass" if c == "pass" else c])
        color = "W" if color == "B" else "B"
    return seq


def _own_of(
    own: Optional[list],
    size: int,
    target_xy: tuple[int, int],
    solver: str,
    half: int = 2,
) -> Optional[float]:
    """目标区（点周围 ±half）平均归属，换算成**先手方视角**（正=目标达成）。

    注意两套纵向口径：界面坐标 y=0 在下（``utils.coord_to_xy``），而 KataGo
    的 ownership 数组是 ``y * size + x``、y=0 在**上**（SGF 顺序，与
    ``explainer.parse_setup`` / ``classify_result.py`` 一致）——这里必须换算，
    否则读的是上下镜像的区域（v1.7.1 修）。
    ownership 正 = 黑方地盘；先手为白时取负 → 正负号统一表示「这块地方归先手方」，
    活棋/杀棋成功时都为正。
    """
    if not own or len(own) != size * size:
        return None
    x0, y0_bottom = target_xy
    y0 = size - 1 - y0_bottom          # 界面 y → ownership 行号（自上而下）
    vals = [
        own[y * size + x]
        for dy in range(-half, half + 1)
        for dx in range(-half, half + 1)
        for x, y in [(x0 + dx, y0 + dy)]
        if 0 <= x < size and 0 <= y < size
    ]
    if not vals:
        return None
    avg = sum(vals) / len(vals)
    return round(avg if solver.upper() == "B" else -avg, 4)


def local_death_profile(
    setup_sgf: str,
    size: int,
    solver: str,
    answer: str,
    pv: list[str],
    region: list[str],
    target_xy: tuple[int, int],
    profile: str = "fast",
    pv_len: int = 6,
    with_tenuki: bool = True,
    engine: Optional[KataGoEngine] = None,
) -> dict:
    """局部死活画像：一手棋是否**达成局部死活目标**、以及**能否脱先**。

    这是 v1.5.0 练习深度讲解（``explainer.py`` 局部推演）与原型脚本
    ``scripts/classify_result.py`` 用的同一套口径，只是取量化值：

    - ``allowMoves`` 锁定局部区域 + ``includeOwnership``；
    - ``own_pv``：正解 + PV 走完后，目标区归属（先手方视角，正 = 目标达成
      ——先手方的棋活了 / 对方的棋杀了）；
    - ``own_after`` / ``tenuki_loss``：先手方脱先、对手抢到要点后的同一区域
      归属与损失；损失越大越「必须现在处理」；
    - ``result_type``：净（own_pv ≥ 0.85 且对手翻盘胜率 ≤ 5%）/ 劫或双活
      （0.5~0.85，或对手仍有翻盘手段）/ 未达成（< 0.5）。

    与整盘胜率相比，这套口径在 19 路角部局面上是**有意义**的：它只看局部
    死活结果，不受「空棋盘上谁朝向空旷盘面」影响（实测见模块 docstring）。

    ``engine``：可传入已启动的常驻引擎复用（同一层的正解/次优点两次画像
    共用一个进程，省一次模型加载）；缺省自建自停。
    """
    own_engine = engine is None
    if own_engine:
        executable, model, cfg_path = _resolve_paths()
        engine = KataGoEngine(executable, model, cfg_path, analysis_threads=1)
    assert engine is not None
    out: dict = {
        "own_pv": None, "own_after": None, "tenuki_loss": None,
        "grade": None, "result_type": None, "opp_winrate": None,
        "answer": utils.normalize_coord(answer), "line": [],
    }
    if not region:
        return out

    def run(seq: list[list[str]]) -> dict:
        q = _game_to_query(setup_sgf, profile, [len(seq)])
        q["moves"] = seq
        q["allowMoves"] = [
            {"player": "B", "moves": region, "untilDepth": 100},
            {"player": "W", "moves": region, "untilDepth": 100},
        ]
        q["includeOwnership"] = True
        return engine.query(q) or {}

    ans = utils.normalize_coord(answer)
    line = [ans]
    # PV 首手与正解重复时去重（防 Illegal move），其余按序走到 pv_len
    for i, m in enumerate(list(pv or [])[: pv_len + 1]):
        c = utils.normalize_coord(m)
        if i == 0 and c == ans:
            continue
        if c == "pass" or c in line:
            continue
        line.append(c)
    opp = "W" if solver.upper() == "B" else "B"
    try:
        engine.start()
        r_solve = run(_seq_with(setup_sgf, line))
        own_arr = (r_solve or {}).get("ownership")
        out["own_pv"] = _own_of(own_arr, size, target_xy, solver)
        opp_wr = ((r_solve or {}).get("rootInfo") or {}).get("winrate")
        out["opp_winrate"] = None if opp_wr is None else round(float(opp_wr), 4)
        out["line"] = line
        if with_tenuki:
            r_pass = run(_seq_with(setup_sgf, ["pass"]))
            infos = (r_pass or {}).get("moveInfos") or []
            best_opp = next((x for x in infos if x.get("order") == 0), None)
            opp_move = utils.normalize_coord((best_opp or {}).get("move") or "")
            if opp_move and opp_move != "pass":
                r_after = run(_seq_with(setup_sgf, ["pass", opp_move]))
                out["own_after"] = _own_of(
                    (r_after or {}).get("ownership"), size, target_xy, solver
                )
                out["tenuki_reply"] = opp_move
    except Exception:  # noqa: BLE001 —— 引擎异常不该让整条链生长失败
        pass
    finally:
        if own_engine:
            engine.stop()

    if out["own_pv"] is not None and out["own_after"] is not None:
        loss = round(out["own_pv"] - out["own_after"], 4)
        out["tenuki_loss"] = loss
        out["grade"] = (
            "紧急" if loss >= 0.4 else ("半紧急" if loss >= 0.15 else "可脱先")
        )
    if out["own_pv"] is not None:
        if out["own_pv"] < 0.5:
            out["result_type"] = "未达成"
        elif out["own_pv"] < 0.85 or (out["opp_winrate"] or 0.0) > 0.05:
            out["result_type"] = "劫/双活"
        else:
            out["result_type"] = "净"
    return out


def goal_text(goal: str, result_type: Optional[str], solver: str) -> str:
    """目标词 + 局部结果类型 → 讲解口径的死活结论（净活/劫活/净杀/劫杀…）。"""
    if result_type in (None, "未达成"):
        return ""
    c = utils.COLOR_CN.get(solver.upper(), solver)
    ko = "劫" if result_type == "劫/双活" else "净"
    if goal in ("做活", "逃棋筋"):
        return f"{c}方{ko}活"
    if goal in ("杀棋", "吃棋筋", "对杀"):
        return f"{c}方{ko}杀"
    return f"{ko}（{result_type}）"


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
    mode: str = "local_board",
    own_min: float = 0.75,
    tenuki_min: float = 0.15,
    death_profile: str = "fast",
    pv_len: int = 6,
    board_margin: int = 2,
    board_min_size: int = 5,
    rel_gap: float = 0.5,
    rel_urgency: float = 0.3,
) -> Optional[_Outcome]:
    """对一个节点裁剪局部 + 验题；不达标返回 None。

    三种验收口径（config ``problems.chain_verify_mode``）：

    - ``local_board``（默认，**19 路推荐**）：把局部棋形裁成小棋盘（保留与
      角部两条边线的距离），在小棋盘上按契约阈值验题（正解 > 0.95、
      次优 < 0.3、死活/对杀 pass < 0.3）。小棋盘上局部攻杀就是全局内容，
      阈值才有意义——与 app 里 9 路整盘复盘题同一尺度；
    - ``local_death``：局部死活画像口径——正解须达成局部死活目标
      （目标区归属 ≥ ``own_min``）、死活/对杀须现在处理（脱先损失 ≥
      ``tenuki_min``）、次优点不得同样达成目标，再叠加契约胜率线；
    - ``relative``：相对口径——正解比次优强 ≥ ``chain_relative_gap``（默认 0.5）
      且比脱先强 ≥ ``chain_relative_urgency``（默认 0.3），不看绝对胜率，
      因此不受空盘面稀释（判的是「唯一急所 + 必须现在处理」）；
    - ``winrate``：契约原文口径，在 19 路整盘（+ allowMoves 局部聚焦）上验
      ——正解 > ``answer_min``、次优 < ``second_max``、死活/对杀另需
      pass 后胜率 < ``urgent_max``（实测 19 路定式局面几乎不可能达标）。
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

    # 验题局面/棋盘：local_board 模式把局部裁成小棋盘再验（见 corner_board）
    v_setup, v_size, v_cands, v_allow = setup, size, candidates, allow
    board_info: dict = {}
    b_dx = b_dy = 0
    if mode == "local_board":
        sub, mapping, b_dx, b_dy = corner_board(
            kept, size, board_margin, board_min_size
        )
        if sub < size:
            v_size = sub
            v_setup = utils.setup_sgf(mapping, solver, sub)
            v_cands = build_candidates(mapping, sub, max_candidates)
            v_allow = region_of(mapping, sub, int(cfg_region_pad))
            board_info = {
                "size": sub, "stones": len(mapping), "dx": b_dx, "dy": b_dy,
            }

    best, second, results, pass_ok = verify_candidates(
        v_setup, v_cands, prof, urgent_max, allow_moves=v_allow
    )
    if best is None:
        return None
    target_xy = utils.coord_to_xy(best.coord)
    if v_size != size:
        # 小棋盘上的坐标 → 19 路界面坐标（题面/答案/变化一律用界面坐标）
        def _back(cd: str) -> str:
            try:
                x, y = utils.coord_to_xy(cd)
            except ValueError:
                return cd
            return utils.xy_to_coord(x - b_dx, y - b_dy, size)
        best = replace(best, coord=_back(best.coord),
                       pv=[_back(m) for m in (best.pv or [])],
                       best_coord=(_back(best.best_coord)
                                   if best.best_coord else best.best_coord))
        if second is not None:
            second = replace(
                second, coord=_back(second.coord),
                pv=[_back(m) for m in (second.pv or [])],
                best_coord=(_back(second.best_coord)
                            if second.best_coord else second.best_coord))
        results = [
            replace(r, coord=_back(r.coord),
                    pv=[_back(m) for m in (r.pv or [])])
            for r in results
        ]

    if mode in ("local_death", "local_board"):
        # 局部死活口径：正解必须**达成**局部死活目标，且（死活/对杀）必须
        # 现在处理；次优点不得同样达成目标（正解唯一）——见 local_death_profile。
        # 次优点的画像只在正解已过其他门槛后才跑（多数候选到不了这一步）
        local = local_death_profile(
            v_setup, v_size, solver, best.coord, list(best.pv or []), v_allow,
            target_xy, profile=death_profile, pv_len=pv_len,
        )
        own_pv = local.get("own_pv")
        if mode == "local_death":
            if own_pv is None or own_pv < float(own_min):
                return None
            if urgent:
                loss = local.get("tenuki_loss")
                if loss is None or loss < float(tenuki_min):
                    return None
            if second is not None:
                alt = local_death_profile(
                    v_setup, v_size, solver, second.coord,
                    list(second.pv or []), v_allow, target_xy,
                    profile=death_profile, pv_len=pv_len, with_tenuki=False,
                )
                if (alt.get("own_pv") is not None
                        and alt["own_pv"] >= float(own_min)):
                    return None   # 换个点也能达成目标 → 不是唯一急所
        # 两种局部口径都叠加契约胜率线（local_board 下这是小棋盘上的胜率，
        # 与 9 路整盘题同一尺度；local_death 下默认 0 = 不叠加）
        if (best.winrate or 0.0) <= float(answer_min):
            return None
        if second is not None and (second.winrate or 0.0) >= float(second_max):
            return None
        if urgent and not pass_ok and mode == "local_death":
            return None
    elif mode == "relative":
        # 相对口径：不看绝对胜率，只看「正解比次优强多少、比脱先强多少」
        # ——不受空盘面稀释，判的是「唯一急所 + 必须现在处理」，与死活题
        #   的本意一致（绝对线 0.95 是 9 路整盘尺度下的经验值）。
        best_wr = best.winrate or 0.0
        second_wr = second.winrate if second and second.winrate is not None else 0.0
        pass_wr = next(
            (r.winrate for r in results
             if r.coord == "pass" and r.error is None and r.winrate is not None),
            None,
        )
        local = {"gap_second": round(best_wr - second_wr, 4),
                 "gap_pass": None if pass_wr is None
                 else round(best_wr - pass_wr, 4)}
        if best_wr - second_wr < float(rel_gap):
            return None                       # 要点不唯一
        if pass_wr is not None and best_wr - pass_wr < float(rel_urgency):
            return None                       # 可以脱先 → 不是急所
        if (best.winrate or 0.0) <= float(answer_min):
            return None                       # 可选绝对下限（默认 0）
    else:
        if (best.winrate or 0.0) <= float(answer_min):
            return None
        if second is not None and (second.winrate or 0.0) >= float(second_max):
            return None
        if urgent and not pass_ok:
            return None
        local = {}
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
        local=local if isinstance(local, dict) else {},
        verify_board=board_info,
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
        # 局部死活画像（local_death 口径）：目标区归属 / 脱先损失 / 结果类型，
        # 深度讲解（explainer.py）与前端"本题目标"共用
        "local": outcome.local,
        # local_board 口径：验题所用的小棋盘（size/子数）；题面仍是 19 路
        # 摆子局（界面坐标不变），验题在只含局部的等距小棋盘上做
        "verify_board": outcome.verify_board,
        # relative 口径：正解−次优 / 正解−脱先 的胜率差（判「唯一急所」）
        "gaps": {k: v for k, v in (outcome.local or {}).items()
                 if k in ("gap_second", "gap_pass")},
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
    local = outcome.local or {}
    verdict = utils.verdict_text(
        outcome.theme,
        outcome.solver,
        best.coord,
        best.winrate or 0.0,
        second.coord if second else None,
        second.winrate if second else None,
    )
    death = goal_text(outcome.goal, local.get("result_type"), outcome.solver)
    if death:
        extra = f"局部结论：{death}。"
        if local.get("grade"):
            loss = local.get("tenuki_loss")
            extra += (
                f"脱先损失 {loss:.2f}（{local['grade']}）——"
                + ("必须现在处理。" if local["grade"] == "紧急" else
                   ("最好现在处理。" if local["grade"] == "半紧急" else "可以脱先。"))
            )
        verdict = f"{verdict} {extra}"
    hint = utils.hint_text(outcome.theme, outcome.solver, center, size)
    if death:
        hint = f"{hint}（目标：{death}）"
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
        "verdict": verdict,
        "hint": hint,
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
    verify_mode: Optional[str] = None,
) -> dict:
    """沿定式链生长题目（契约 §4.3 追加小节 POST /chains/{id}/grow）。

    返回 ``{"chain_id", "added", "discarded", "problems": [brief...],
    "steps": n}``：added = 本次新入库题数（重复 grow 为 0），
    discarded = 验题未通过/局部超框的丢弃次数。

    ``max_depth``：从定式终局算起的层数（默认 3 → 最多 3 道链上题）；
    ``max_per_level``：每层最多尝试的种子数（主变 + 备选对手应手）；
    ``profile``：验题档位，缺省按 config ``problems.verify_profile``
    （死活/对杀 fine，其余 fallback 到 "standard"）。
    ``verify_mode``：验收口径覆盖（``local_board``（默认）/ ``local_death`` /
    ``winrate``），缺省取 config ``problems.chain_verify_mode``。
    """
    cfg = _problems_cfg()
    pad = int(cfg.get("chain_crop_pad", 1))
    max_bbox = int(cfg.get("chain_max_bbox", 9))
    max_candidates = int(cfg.get("max_candidates", 12))
    urgent_max = float(cfg.get("urgency_max_winrate", 0.3))
    region_pad = int(cfg.get("chain_region_pad", 2))
    answer_min = float(cfg.get("chain_answer_min_winrate", 0.95))
    second_max = float(cfg.get("chain_second_max_winrate", 0.3))
    mode = str(verify_mode or cfg.get("chain_verify_mode", "local_board")).lower()
    if mode not in ("local_board", "local_death", "relative", "winrate"):
        mode = "local_board"
    own_min = float(cfg.get("chain_own_min", 0.75))
    tenuki_min = float(cfg.get("chain_tenuki_min", 0.15))
    death_profile = str(cfg.get("chain_death_profile", "fast"))
    pv_len = int(cfg.get("chain_death_pv_len", 6))
    rel_gap = float(cfg.get("chain_relative_gap", 0.5))
    rel_urgency = float(cfg.get("chain_relative_urgency", 0.3))
    if mode == "relative":
        # 相对口径默认不设绝对胜率下限（0.95 是 9 路整盘尺度的经验值）
        answer_min = float(cfg.get("chain_relative_floor", 0.0))
    board_margin = int(cfg.get("chain_board_margin", 2))
    board_min_size = int(cfg.get("chain_board_min_size", 5))
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
                mode, own_min, tenuki_min, death_profile, pv_len,
                board_margin, board_min_size,
                rel_gap, rel_urgency,
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
