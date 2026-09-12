"""讲解 Prompt 模板（窗口2）。

- 系统提示词固定角色：耐心的围棋老师，面向 K 级到业余低段成人爱好者；
- 语言通俗、先结论后原因、每次只讲一个要点；讲解深度按请求者水平字段调节；
- 质量红线（任务书/契约 §4.2）：只解释给定变化、禁止编造着法、
  ``variation`` 仅允许来自传入的 KataGo PV；
- 输出严格 JSON，字段齐全（对应 docs/architecture.md §6）。

同时导出各输出的 schema_hint，供 ``llm.chat_json`` 做缺字段兜底。
"""
from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# 固定角色（任务书 §3.2）
# ---------------------------------------------------------------------------

SYSTEM_ROLE = (
    "你是一位讲棋经验丰富的围棋老师，面向 K 级到业余低段成人爱好者授课，"
    "风格类似视频网站上的围棋讲棋老师（明玥谈棋那种）："
    "像在棋盘边现场讲棋一样，自然使用标准围棋术语——"
    "布局位置：星位、小目、三三、高目、目外、大场、天王山；"
    "进攻着法：打入、分投、夹击、镇头、飞压、尖顶、靠压、逼、断、挖、"
    "冲、跨、刺、点、枷、征子、倒扑、滚打包收；"
    "防守联络：肩冲、扳粘、连扳、长、立、跳、飞、虎、渡过、做活、治孤、"
    "腾挪、整形、补断；"
    "形势判断：厚势、外势、薄味、实利、先手利、后手、急所、脱先、手筋、"
    "试应手、棋筋、愚形、凝形、转换、定型；"
    "定性评价：本手、俗手、缓手、随手、疑问手、命令手、胜负手、妙手、"
    "强手等，"
    "用术语时默认听众知道基础含义，个别生僻术语用几个字带过即可；"
    "讲解点睛时可以用棋谚（棋从断处生、金角银边草肚皮、二子头必扳、"
    "提子开花三十目、压强不压弱等），让要点更好记；"
    "先说结论再说原因；每次聚焦一个要点；口语化但不啰嗦；"
    "把胜负手讲出画面感（这一步是要干嘛、对手会怎么应、接下来会怎样）。"
    "死活教学专用要求（角部/边部局部死活必守）："
    "①必须讲清最终结果是净活、劫活、双活、净杀还是劫杀，并说明判断依据；"
    "②必须判断清楚「现在先手方能不能脱先」——局部告一段落可以脱先，还是这里欠着棋必须马上走；"
    "③绝不罗列 AI 全盘搜索的复杂变化——中腹的计算只给结论不给过程，不展示海量候选点；"
    "④局部变化只展示必要的应对与合理的交换，按人类棋手思路讲（先找急所、再讲次序），"
    "展示双方最自然的抵抗，不展示无理手硬撑的变化。"
)

# ---------------------------------------------------------------------------
# 输出 schema（字段名 -> 类型），供 chat_json 兜底缺字段
# ---------------------------------------------------------------------------

EXPLAIN_SCHEMA = {
    "problem": str,
    "reason": str,
    "recommendation": str,
    "necessity": str,
    "variation": list,
    "alternatives": list,
    "takeaway": str,
    "level_note": str,
    "segments": list,
    "result_type": str,
    "can_tenuki": str,
}

SUMMARY_SCHEMA = {
    "opening": str,
    "middle": str,
    "endgame": str,
    "strengths": list,
    "weaknesses": list,
    "suggestions": list,
}

DEEP_SCHEMA = {
    "title": str,
    "overview": str,
    "stages": list,
    "key_moves": list,
    "causality": str,
    "strengths": list,
    "weaknesses": list,
    "homework": list,
}

ANSWER_SCHEMA = {
    "conclusion": str,
    "reasoning": str,
    "variation": list,
    "kata_winrate": float,
}

PENALTY_SCHEMA = {
    "steps": list,
    "summary": str,
}


def _level_brief(level: str) -> str:
    """按水平给出讲解深度调节说明。"""
    return (
        f"讲解深度针对 {level} 水平调节："
        "12K 以下多用生活化比喻、只讲方向对错；"
        "8K~3K 可讲攻防要点与常见手筋；"
        "2K~2D 可涉及目数、厚薄与后续手段，但仍要通俗。"
    )


def _local_board_text(curve: list[dict], move_number: int, radius: int = 6) -> str:
    """该手前后若干手的文字描述（局部局面）。"""
    before = [m for m in curve if 0 < m.get("move", 0) < move_number][-radius:]
    after = [m for m in curve if m.get("move", 0) > move_number][: max(radius - 2, 2)]
    parts = [f"第{m['move']}手 {'黑' if m['color'] == 'B' else '白'}"
             f"{m['coord'] or '停一手'}" for m in before]
    parts.append(f"【第{move_number}手 本手】")
    parts += [f"第{m['move']}手 {'黑' if m['color'] == 'B' else '白'}"
              f"{m['coord'] or '停一手'}" for m in after]
    return "；".join(parts) if parts else "（无局部着法信息）"


def _candidates_line(candidates: list[dict]) -> str:
    """候选点一行文字（讲解备选与舒适区变化的数据来源）。"""
    if not candidates:
        return "KataGo 候选点：无"
    parts = []
    for c in candidates[:5]:
        coord = c.get("move") or "?"
        wr = c.get("winrate")
        vis = c.get("visits")
        text = coord
        if wr is not None:
            text += f"（胜率 {wr:.3f}"
            if vis is not None:
                text += f"，计算量 {vis}"
            text += "）"
        else:
            text += "（胜率 无）"
        parts.append(text)
    return "KataGo 候选点：" + "；".join(parts)


def _category_cn(category: str) -> str:
    return {"blunder": "坏手", "question": "疑问手", "good": "好手",
            "normal": "普通手"}.get(category, category or "普通手")


PLAY_TIP_SCHEMA = {
    "problem": str,
    "reason": str,
    "recommendation": str,
    "proverb": str,
}


def build_play_tip_messages(size: int, moves: list[list[str]], coord: str,
                            best: str, delta: float | None, level: str) -> list[dict]:
    """对弈中坏手/疑问手的即时讲解（新手板块）：错在哪、常规下法、原理、口诀。"""
    move_number = len(moves)
    color_cn = "黑" if moves[-1][0] == "B" else "白"
    seq_lines = [
        f"第{idx}手 {'黑' if c == 'B' else '白'} {m}"
        for idx, (c, m) in enumerate(moves[-12:], start=max(1, move_number - 11))
    ]
    level_cn = {"bad": "坏手", "question": "疑问手"}.get(level, "坏手")
    delta_text = f"胜率损失约 {abs(delta) * 100:.0f} 个百分点" if delta is not None else "胜率损失未知"
    data_lines = [
        f"棋盘：{size} 路；学生对弈 AI，目前共 {move_number} 手",
        f"学生刚下：第 {move_number} 手，{color_cn}方 {coord}，判定为{level_cn}（{delta_text}）",
        f"常规应下：{best}",
        "解释落点位置时优先使用围棋术语（如「在二线小尖」「四线长一个」「虎住」「威胁对方断点」），不要只给坐标字母数字，让不懂棋的新手也能听懂。",
        "最近行棋序列：",
        *seq_lines,
        "注意：学生执黑。上面序列中「黑」的手都是学生下的，「白」的手都是 AI 应 的；"
        "被判定为坏手的就是上面标注的学生最后一步，讲解时务必围绕这一步展开，"
        "不要混淆成其他手。",
    ]
    user = "\n".join(
        [
            "学生是刚入门的新手（懂死活规则，不懂套路），正在和 AI 对弈。"
            "他刚下出了一手坏棋，请即时讲解，帮他当场学会常规下法。",
            "",
            "【输入数据】",
            *data_lines,
            "",
            "【输出要求】",
            "只输出一个 JSON 对象，字段如下（全部必填）：",
            "- problem: 用一两句话点出这手棋的问题；",
            "- reason: 通俗解释为什么这手下坏了（先结论后原因，讲清道理）；",
            "- recommendation: 常规下法 {best} 好在哪、怎么理解这一步的思路"
            "（一两句话，让新手能记住）；",
            "- proverb: 一句可迁移的口诀或棋谚（如「棋从断处生」「二子头必扳」，"
            "没有合适的可以自己概括一句，不超过 12 个字）。",
            "",
            "【硬性规则】",
            "1. 坐标只允许使用上面给出的坐标，禁止编造着法；",
            "2. 面向完全不懂套路的新手：术语要解释、比喻要生活化、讲清为什么；",
            "3. 只输出 JSON，不要输出任何解释性文字。",
        ]
    )
    return [
        {"role": "system", "content": SYSTEM_ROLE},
        {"role": "user", "content": user},
    ]


# ---------------------------------------------------------------------------
# PENALTY（惩罚变化多手连讲，任务书 §3.2 扩展）
# ---------------------------------------------------------------------------

def build_penalty_messages(review: dict, move_number: int, penalty_seq: list[list[str]],
                           punisher_winrate: float | None, level: str = "-5") -> list[dict]:
    """惩罚变化分步讲解：把对手的惩罚序列当作一串棋连着讲。

    - penalty_seq: [[color, coord], ...]，第 1 步为惩罚方（对手）；
    - 要求 LLM 对每一步标注角色：先手 / 好手 / 俗手 / 应对，并逐手解说；
    - 最后给 summary：这串惩罚下来学生亏在哪、正确下法本该如何。
    """
    curve = review.get("winrate_curve") or []
    move = next((m for m in curve if m.get("move") == move_number), None)
    if move is None:
        raise ValueError(f"复盘数据中不存在第 {move_number} 手")
    color_cn = "黑" if move.get("color") == "B" else "白"
    winrate = move.get("winrate")
    delta = move.get("delta")
    seq_lines = [
        f"第{idx}步：{'黑' if c == 'B' else '白'} {coord}"
        for idx, (c, coord) in enumerate(penalty_seq, start=1)
    ]
    data_lines = [
        f"学生坏手：第 {move_number} 手，{color_cn}方落子 {move.get('coord') or '停一手'}",
        f"坏手性质：{_category_cn(move.get('category'))}",
        f"坏手后 {color_cn}方胜率：{winrate:.3f}"
        if winrate is not None else "坏手后胜率：无",
        f"胜率变化（{color_cn}方）：{delta:+.3f}"
        if delta is not None else "胜率变化：无",
        f"KataGo 最佳点：{move.get('best_coord') or '无'}",
        "KataGo 局部厮杀视角下的对手惩罚变化（连续着法，第 1 步为对手惩罚点）：",
        *seq_lines,
        f"惩罚变化后 行棋方（惩罚方）胜率：{punisher_winrate:.3f}"
        if punisher_winrate is not None else "惩罚变化后胜率：无",
        f"局部局面：{_local_board_text(curve, move_number)}",
        f"棋手水平：约 {level}",
    ]
    user = "\n".join(
        [
            "学生下出了坏手，KataGo 把视野锁定在坏手附近（局部厮杀，不管全盘大场）"
            "算出了对手的惩罚变化。请把这串棋当作一个整体、连着讲。",
            "",
            "【输入数据】",
            *data_lines,
            "",
            "【输出要求】",
            "只输出一个 JSON 对象，字段如下（全部必填）：",
            "- steps: 数组，对惩罚变化的每一步给一个对象 {step, role, text}：",
            "  step 为惩罚变化中的序号（1 基，与上面编号一致，每一步都要有）；",
            "  role 只允许取以下四种之一：",
            "  先手：这一步是对方必须立即应的先手（不应对会损失惨重）；",
            "  好手：严厉的惩罚/进攻要点，是这串变化的要害；",
            "  俗手：俗手交换（帮对方补强、自损、或不必走的交换，讲清为什么亏）；",
            "  应对：被逼的必然应对，讲清为什么只能这么走；",
            "  text 用一两句话讲这一步要干什么、对方会怎么应、威胁在哪里"
            "（先结论后原因，讲棋术语风格，像讲棋老师现场摆变化）；",
            "- summary: 2~3 句话总结这串惩罚：学生（下坏手的一方）被抓住了什么毛病、"
            "这串交换下来亏了多少、正确的下法本该如何、有什么可迁移的口诀。",
            "",
            "【硬性规则】",
            "1. 只能讲解上面给出的惩罚变化，禁止编造任何坐标或着法；",
            "2. steps 必须覆盖惩罚变化的每一步，一步一解说，不许跳步；",
            "3. 把「先手」「好手」「俗手」讲明白，不要只会报胜率数字；",
            "4. 只输出 JSON，不要输出任何解释性文字。",
            "",
            _level_brief(level),
        ]
    )
    return [
        {"role": "system", "content": SYSTEM_ROLE},
        {"role": "user", "content": user},
    ]


# ---------------------------------------------------------------------------
# EXPLAIN（单手讲解，任务书 §3.2）
# ---------------------------------------------------------------------------

def build_explain_messages(review: dict, move_number: int, level: str = "-5") -> list[dict]:
    curve = review.get("winrate_curve") or []
    move = next((m for m in curve if m.get("move") == move_number), None)
    if move is None:
        raise ValueError(f"复盘数据中不存在第 {move_number} 手")
    color_cn = "黑" if move.get("color") == "B" else "白"
    pv = move.get("pv") or []
    winrate = move.get("winrate")
    delta = move.get("delta")
    data_lines = [
        f"本手：第 {move_number} 手，{color_cn}方落子 {move.get('coord') or '停一手'}",
        f"本手性质：{_category_cn(move.get('category'))}",
        f"本手后 {color_cn}方胜率：{winrate:.3f}"
        if winrate is not None else "本手后胜率：无",
        f"本手前 {color_cn}方胜率：{winrate - delta:.3f}"
        if winrate is not None and delta is not None else "本手前胜率：无",
        f"胜率变化（{color_cn}方）：{delta:+.3f}"
        if delta is not None else "胜率变化：无",
        f"KataGo 最佳点：{move.get('best_coord') or '无'}",
        f"KataGo PV：{'、'.join(pv) if pv else '无'}",
        _candidates_line(move.get("candidates") or []),
        f"局部局面：{_local_board_text(curve, move_number)}",
        f"棋手水平：约 {level}",
    ]
    user = "\n".join(
        [
            "请讲解下面这一手棋（单手讲解）。",
            "",
            "【输入数据】",
            *data_lines,
            "",
            "【输出要求】",
            "只输出一个 JSON 对象，字段如下（全部必填）：",
            "- problem: 用一两句话点出这手棋的问题或看点（普通手/好手则说清它在做什么）；",
            "- reason: 为什么会这样（先结论后原因，通俗解释）；",
            "- recommendation: 推荐的下法及理由（若本手是最佳点请说明其作用）；",
            "- variation: 变化序列字符串数组，只允许从上面给出的 KataGo PV 中选取，"
            "不得自创坐标；若 PV 为空则给空数组；",
            "- alternatives: 字符串数组，讲解「舒适区替代下法」。"
            "查看候选点里与最佳点胜率差距在 3 个百分点以内的点，"
            "每条用一两句话讲：这个点差在哪里、为什么也算可行、"
            "适合什么样的人（如：不想走复杂一选时，下 X 更简明稳妥，"
            "胜率只低 1%，思路是……）。"
            "若没有接近的候选点（如本手是坏手、候选点差距都很大），给空数组；"
            "坐标只允许使用候选点中出现的坐标，不得编造；",
            "- takeaway: 一句可迁移的口诀或提醒；",
            "- level_note: 针对本手水平的补充说明，不超过两句。",
            "- segments: 同步讲棋分段数组，3~5 段，每项 {\"text\", \"variation\"}。\n"
            "  * text：本段讲解文字（口语化，一两句话，像讲棋老师边说边摆棋）；\n"
            "  * variation：本段要摆在棋盘上的变化，从**本手落下前的局面**开始"
            "（首手是" + color_cn + "方，即「这一手本该下在哪里」），双方交替；"
            "每步用 [\"B\"或\"W\", 坐标] 数组表示；\n"
            "  * **只要 KataGo PV 非空，就必须把 PV 拆进各段 variation 里**"
            "（按顺序连续截取、不遗漏），第一段结论段可以不给变化，其余段禁止全给空数组；\n"
            "  * **变化必须从首手开始**：第一个带变化的段落，variation 第一步必须是本手的最佳下法"
            "（best_coord，本局为 " + str(move.get("best_coord") or "无") + "），不许把首手省略在文字里；\n"
            "  * 变化坐标只能来自 KataGo 最佳点/PV/候选点，禁止编造；\n"
            "  * 示例（本手是白方、PV 为 D4 D7 C7 C3）：\n"
            "    {\"text\":\"白12应该拆在D4，这是当前最大的大场。\",\"variation\":[[\"W\",\"D4\"]]}\n"
            "    {\"text\":\"黑若从D7挂角，白C7尖顶、黑C3长，白棋顺势走厚。\",\"variation\":[[\"B\",\"D7\"],[\"W\",\"C7\"],[\"B\",\"C3\"]]}\n"
            "  * 段与段按讲解逻辑顺序排列：结论 → 正解变化 → 后续手段；\n"
            "  * 【结论一致】若 result_type / can_tenuki 非空，第一段结论必须与这两个字段一致，"
            "段与段之间、段与字段之间不允许出现矛盾；\n"
            "- result_type: 若本手涉及局部死活/对杀，给出结果类型"
            "（净活/劫活/双活/净杀/劫杀 之一），无关则给空字符串；\n"
            "- can_tenuki: 若本手涉及局部死活/对杀，判断该处现在能不能脱先"
            "（能脱先/不能脱先/有条件 三选一），无关则给空字符串。",
            "",
            "【硬性规则】",
            "1. 只能引用上面给出的 KataGo 数据与变化，禁止编造任何着法或数据；",
            "2. variation 数组中的每个坐标必须出现在上面给出的 PV 里；",
            "3. 先结论后原因，每次只讲一个要点；",
            "4. 使用围棋讲棋术语（肩冲、点三三、拆边、打入、厚势、急所等），"
            "像讲棋老师一样自然表达，不要只会报胜率数字；",
            "5. 只输出 JSON，不要输出任何解释性文字。",
            "",
            _level_brief(level),
        ]
    )
    return [
        {"role": "system", "content": SYSTEM_ROLE},
        {"role": "user", "content": user},
    ]


# ---------------------------------------------------------------------------
# SUMMARY（全局总结，任务书 §3.2）
# ---------------------------------------------------------------------------

def _curve_summary(curve: list[dict]) -> list[str]:
    """胜率曲线摘要：总手数、黑白均胜率、分段趋势、最大波动。"""
    if not curve:
        return ["胜率曲线：无数据"]
    n = len(curve)
    black = [m for m in curve if m.get("color") == "B" and m.get("winrate") is not None]
    white = [m for m in curve if m.get("color") == "W" and m.get("winrate") is not None]
    lines = [f"总手数 {curve[-1].get('move', n)}"]

    def avg(ms: list[dict]) -> str:
        return f"{sum(m['winrate'] for m in ms) / len(ms):.3f}" if ms else "无"

    lines.append(f"黑方平均胜率 {avg(black)}，白方平均胜率 {avg(white)}")
    stages = [("布局(1~40)", 1, 40), ("中盘(41~180)", 41, 180), ("官子(181+)", 181, 10**9)]
    for name, lo, hi in stages:
        seg = [m for m in curve if lo <= m.get("move", 0) <= hi and m.get("winrate") is not None]
        if not seg:
            continue
        wr = [m["winrate"] for m in seg]
        lines.append(
            f"{name}：共 {len(seg)} 手，胜率区间 {min(wr):.3f}~{max(wr):.3f}，"
            f"期末 {wr[-1]:.3f}，波动幅度 {max(wr) - min(wr):.3f}"
        )
    biggest = max(
        (m for m in curve if m.get("delta") is not None),
        key=lambda m: abs(m["delta"]), default=None,
    )
    if biggest is not None:
        lines.append(
            f"最大波动：第 {biggest['move']} 手 "
            f"({'黑' if biggest['color'] == 'B' else '白'} {biggest['coord']})"
            f" 胜率变化 {biggest['delta']:+.3f}"
        )
    return lines


def build_summary_messages(review: dict, level: str = "-5") -> list[dict]:
    curve = review.get("winrate_curve") or []
    key_moves = review.get("key_moves") or []
    stats = review.get("stats") or {}
    lines = [
        f"黑白双方：{review.get('black') or '黑方'}（黑）对 {review.get('white') or '白方'}（白）",
        f"双方水平：约 {level}",
        "【胜率曲线摘要】",
        *_curve_summary(curve),
        "【关键手】",
    ]
    if key_moves:
        lines += [
            f"第{m['move']}手 {'黑' if m['color'] == 'B' else '白'} {m['coord']}"
            f"（{_category_cn(m.get('category'))}，胜率变化 {m.get('delta') or 0:+.3f}，"
            f"最佳点 {m.get('best_coord') or '无'}）"
            for m in key_moves
        ]
    else:
        lines.append("无")
    lines += [
        f"统计：坏手 {stats.get('blunders', 0)} 手，疑问手 {stats.get('questions', 0)} 手，"
        f"好手 {stats.get('good', 0)} 手",
    ]
    user = "\n".join(
        [
            "请对整局棋做全局总结（复盘总结）。",
            "",
            "【输入数据】",
            *lines,
            "",
            "【输出要求】",
            "只输出一个 JSON 对象，字段如下（全部必填）：",
            "- opening: 布局阶段总结（1~3 句）；",
            "- middle: 中盘阶段总结（1~3 句，重点说连续失误或亮点）；",
            "- endgame: 官子阶段总结（1~2 句，官子少则说明）；",
            "- strengths: 优点数组（2~3 条）；",
            "- weaknesses: 短板数组（2~3 条）；",
            "- suggestions: 训练建议数组（2~3 条，具体可执行，如“本周练习接触战断点 10 题”）。",
            "",
            "【硬性规则】",
            "1. 只能引用上面给出的胜率数据与关键手，禁止编造着法或数据；",
            "2. 建议具体、可执行，语气鼓励为主；",
            "3. 只输出 JSON，不要输出任何解释性文字。",
            "",
            _level_brief(level),
        ]
    )
    return [
        {"role": "system", "content": SYSTEM_ROLE},
        {"role": "user", "content": user},
    ]


# ---------------------------------------------------------------------------
# ASK（答疑，任务书 §3.2）
# ---------------------------------------------------------------------------

def build_ask_messages(
    sgf_text: str,
    question: str,
    level: str = "-5",
    kata_data: dict[str, Any] | None = None,
) -> list[dict]:
    """答疑：局面描述 + 用户问题 + （可选）KataGo 数据。

    ``kata_data`` 为开发期可选参数（窗口1 尚未提供实时分析，可不传）。
    """
    # SGF 可能很长，做保守截断（尾部保留局面信息）
    sgf = (sgf_text or "").strip()
    if len(sgf) > 4000:
        sgf = sgf[:1000] + "\n……（SGF 中段省略）……\n" + sgf[-3000:]
    kata_lines = []
    if kata_data:
        kata_lines = [
            "【KataGo 数据】",
            f"当前方胜率：{kata_data.get('winrate', '无')}",
            f"胜率变化：{kata_data.get('delta', '无')}",
            f"最佳点：{kata_data.get('best_coord', '无')}",
            f"PV：{kata_data.get('pv') or '无'}",
        ]
    else:
        kata_lines = [
            "【KataGo 数据】",
            "本局面暂无 KataGo 分析数据，请基于围棋常识回答；"
            "kata_winrate 字段填 null。",
        ]
    user = "\n".join(
        [
            "学生在复盘中提出了一个问题，请解答。",
            "",
            "【局面 SGF】",
            sgf,
            "",
            "【学生问题】",
            question,
            "",
            *kata_lines,
            "",
            "【输出要求】",
            "只输出一个 JSON 对象，字段如下（全部必填）：",
            "- conclusion: 结论先行，直接回答问题（一两句）；",
            "- reasoning: 原因与推演过程（通俗，3~5 句）；",
            "- variation: 关键变化序列字符串数组；"
            "若有 KataGo PV 则只允许从中选取，否则允许给出常识变化但每步坐标必须合法；",
            "- kata_winrate: 有 KataGo 数据时填数值，否则填 null。",
            "",
            "【坐标格式】",
            "所有坐标一律使用界面格式（列字母 A~T 跳过 I、行号 1~19，如 Q16、D15、P10），"
            "禁止使用 SGF 小写坐标（如 qe、pd）。",
            "",
            "【硬性规则】",
            "1. 结论先行，每次只讲一个要点；",
            "2. 不确定的地方明确说“不确定”，不要编造数据；",
            "3. 只输出 JSON，不要输出任何解释性文字。",
            "",
            _level_brief(level),
        ]
    )
    return [
        {"role": "system", "content": SYSTEM_ROLE},
        {"role": "user", "content": user},
    ]

# ---------------------------------------------------------------------------
# DEEP（整盘深度分析报告，付费功能：一次性生成整篇、上下文关联）
# ---------------------------------------------------------------------------

def build_deep_messages(review: dict, level: str = "-5") -> list[dict]:
    """整盘棋深度报告：分段详评 + 关键手串讲 + 失误因果链。"""
    curve = review.get("winrate_curve") or []
    key_moves = review.get("key_moves") or []
    stats = review.get("stats") or {}
    lines = [
        f"黑白双方：{review.get('black') or '黑方'}（黑）对 "
        f"{review.get('white') or '白方'}（白）",
        f"双方水平：约 {level}",
        "【胜率曲线摘要】",
        *_curve_summary(curve),
        "【关键手清单】",
    ]
    if key_moves:
        for m in key_moves:
            cands = m.get("candidates") or []
            cand_txt = "；".join(
                f"{c.get('move') or '?'}({c.get('winrate'):.3f})"
                for c in cands[:3] if c.get("winrate") is not None
            ) or "无"
            lines.append(
                f"第{m['move']}手 {'黑' if m.get('color') == 'B' else '白'} "
                f"{m.get('coord')}（{_category_cn(m.get('category'))}，"
                f"胜率变化 {m.get('delta') or 0:+.3f}，"
                f"最佳点 {m.get('best_coord') or '无'}，"
                f"候选 {cand_txt}）"
            )
    else:
        lines.append("无")
    lines.append(
        f"统计：坏手 {stats.get('blunders', 0)}，疑问手 "
        f"{stats.get('questions', 0)}，好手 {stats.get('good', 0)}"
    )
    user = "\n".join(
        [
            "请为这整盘棋写一篇深度分析报告（付费精品内容）。",
            "",
            "【输入数据】",
            *lines,
            "",
            "【输出要求】只输出一个 JSON 对象，字段如下（全部必填）：",
            "- title: 报告标题，一句有画面感的总结（如「一着缓手引发的中盘雪崩」）；",
            "- overview: 全局总评，150 字左右：双方棋风、胜负转折点、一句话结论；",
            "- stages: 字符串数组，按布局/中盘/官子分阶段评述，每段 2~4 句，"
            "点出该阶段双方意图、关键争夺与得失；",
            "- key_moves: 对象数组，逐个串讲关键手：每个对象含 move（整数）、"
            "analysis（该手的问题/亮点、正确下法与后续影响，2~3 句），"
            "按手数升序，最多讲 12 手；",
            "- causality: 上下文关联分析，120 字左右：把若干手失误串成因果链，"
            "如「第 7 手的缓手让出先机，第 12 手为抢回主动冒进，反而暴露断点，"
            "最终第 19 手不得不弃子止损」；",
            "- strengths: 字符串数组，双方各 1~2 条亮点；",
            "- weaknesses: 字符串数组，2~4 条主要短板（具体到棋的层面，"
            "如「战斗中的断点意识薄弱」）；",
            "- homework: 字符串数组，3~5 条针对性训练建议（具体可执行）。",
            "",
            "【硬性规则】",
            "1. 只能引用上面给出的 KataGo 数据，禁止编造任何着法坐标或胜率；",
            "2. 使用讲棋老师风格的围棋术语（外势、实利、急所、消长、定型等）；",
            "3. 报告整体连贯，前后有逻辑呼应，不是孤立点评的堆砌；",
            "4. 只输出 JSON，不要输出任何解释性文字。",
            "",
            _level_brief(level),
        ]
    )
    return [
        {"role": "system", "content": SYSTEM_ROLE},
        {"role": "user", "content": user},
    ]


# ---------------------------------------------------------------------------
# PROGRESS_ADVICE（成长视图：棋力画像 → 针对性提高建议）
# ---------------------------------------------------------------------------

PROGRESS_ADVICE_SCHEMA = {
    "summary": str,
    "strengths": list,
    "weaknesses": list,
    "plan": list,
    "homework": list,
}

_PHASE_CN = {"layout": "布局", "middle": "中盘", "endgame": "官子"}


def build_progress_advice_messages(name: str, insight: dict,
                                   samples: list[dict]) -> list[dict]:
    """把画像特征与典型坏手样本交给 LLM，生成针对性提高建议。"""
    f = insight or {}
    rates = f.get("rates") or {}
    phases = f.get("phases") or {}
    phase_lines = []
    for key, cn in _PHASE_CN.items():
        p = phases.get(key) or {}
        bl = p.get("blunder_rate")
        line = (
            f"- {cn}：平均每手损失 {p.get('avg_loss', 0):.3f}，"
            f"坏手率 {bl:.1%}（共 {p.get('moves', 0)} 手）"
            if isinstance(bl, (int, float))
            else f"- {cn}：平均每手损失 {p.get('avg_loss', 0):.3f}"
        )
        phase_lines.append(line)

    sample_lines = []
    for s in samples or []:
        ph = _PHASE_CN.get(s.get("phase"), "")
        sample_lines.append(
            f"  {ph} 第{s.get('move_number')}手 下了 {s.get('coord')}，"
            f"胜率损失 {abs(s.get('delta') or 0):.1%}，"
            f"一选应为 {s.get('best_coord') or '未知'}"
        )

    trend_cn = {"improving": "进步中", "declining": "有所退步", "flat": "平稳"}.get(
        f.get("trend"), "平稳")
    data_lines = [
        f"棋手：{name}",
        f"样本：{f.get('n_games', 0)} 局，平均每局 {f.get('avg_moves', 0)} 手",
        f"平均每手失误：{f.get('avg_loss_per_move', 0):.3f}（胜率损失绝对值）",
        f"参考棋力区间：{f.get('rank_estimate') or '未知'}（基于样本的粗略估计）",
        f"坏手率 {rates.get('blunder', 0):.1%}，疑问手率 "
        f"{rates.get('question', 0):.1%}，好手率 {rates.get('good', 0):.1%}",
        "分阶段画像：",
        *phase_lines,
        f"最弱环节：{f.get('weakest_phase') or '未知'}",
        f"近期趋势：{trend_cn}"
        + (f"（近期平均损失 {f.get('recent_loss', 0):.3f} vs 早期 "
           f"{f.get('earlier_loss', 0):.3f}）"
           if f.get('recent_loss') is not None else ""),
    ]
    if f.get("calc_blunder_share") is not None:
        data_lines.append(
            f"复杂局面（高不确定性）中的坏手占比："
            f"{f['calc_blunder_share']:.0%}（高=计算力偏弱）"
        )
    if f.get("direction_blunder_share") is not None:
        data_lines.append(
            f"方向性失误占比（一选距离本手 ≥3 格的坏手）："
            f"{f['direction_blunder_share']:.0%}（高=大局观/方向感偏弱）"
        )
    if sample_lines:
        data_lines.append("典型坏手样本：")
        data_lines.extend(sample_lines)

    user = "\n".join(
        [
            "你是一名围棋教练，正在给一位学生做阶段性评估。"
            "下面是这位学生的棋力画像数据（全部来自 KataGo 对局分析统计）。",
            "",
            "【输入数据】",
            *data_lines,
            "",
            "【输出要求】只输出一个 JSON 对象，字段如下（全部必填）：",
            "- summary: 3~5 句总评：先肯定优点，再点出最影响进步的一两个问题，"
            "语气像教练当面谈话，不堆砌数据；",
            "- strengths: 字符串数组，2~3 条优势（具体到棋的层面）；",
            "- weaknesses: 字符串数组，2~4 条短板（按影响排序，"
            "优先讲最弱环节，结合典型坏手样本）；",
            "- plan: 训练计划数组，3~5 条，每条 {\"focus\", \"why\", \"practice\", \"theme\"}：",
            "  * focus：训练重点（如「官子收束」）；",
            "  * why：为什么针对它（结合画像数据，一两句）；",
            "  * practice：具体怎么练（每天练多少、练什么，可执行）；",
            "  * theme：对应题库主题（只允许：做活/杀棋/对杀/吃棋筋/逃棋筋/收官最大/中盘要点，"
            "选最贴近的一个）；",
            "- homework: 字符串数组，1~2 条本周可完成的小任务（具体、有数量）。",
            "",
            "【硬性规则】",
            "1. 只依据上面给出的数据，禁止编造胜率或棋谱；",
            "2. 面向 K 级到业余低段爱好者，建议要具体可执行，不要空话；",
            "3. 不假装精确段位——引用棋力区间时加上「参考」二字；",
            "4. 只输出 JSON。",
        ]
    )
    return [
        {"role": "system", "content": SYSTEM_ROLE},
        {"role": "user", "content": user},
    ]

PROBLEM_EXPLAIN_SCHEMA = {
    "result_type": str,
    "can_tenuki": str,
    "segments": list,
    "takeaway": str,
}


def build_problem_explain_messages(
    size: int,
    goal: str,
    solver: str,
    answer: str,
    answer_wr: float | None,
    board_grid: str,
    own_solve: str,
    resist_line: str,
    res_wr: float | None,
    ko_line: str,
    tenuki_line: str,
    own_pass: str,
    level: str = "15K",
) -> list[dict]:
    """练习深度讲解：把死活的结论/手段/脱先判断讲成同步讲棋分段。

    数据全部来自 KataGo 局部推演（正解/抵抗/脱先三变化 + 局部归属网格），
    LLM 只做"把变化讲成段"的翻译工作，不自行计算。
    """
    solver_cn = "黑" if solver == "B" else "白"
    opp_cn = "白" if solver == "W" else "黑"
    wr_line = (
        f"正解：{solver_cn}{answer}（KataGo 验证胜率 {answer_wr:.1%}）。"
        if answer_wr is not None
        else f"正解：{solver_cn}{answer}。"
    )
    res_wr_line = (
        f"正解走完后，{opp_cn}方剩余胜率约 {res_wr:.0%}。"
        f"接近 0 说明{solver_cn}方已净杀/净活；"
        f"若在 10%~40% 则通常还有劫争或顽抗空间。"
        if res_wr is not None
        else ""
    )
    data_lines = [
        f"棋盘：{size} 路。本题目标：{goal}。先手方：{solver_cn}。",
        wr_line,
        "",
        "题面局部（X=黑 O=白 .=空点）：",
        board_grid,
        "",
        "正解后，正解点周围 5x5 终局归属（X=归黑 O=归白 .=均势/不确定）：",
        own_solve,
        "",
        f"正解后，{opp_cn}方的最强抵抗变化：{resist_line}",
        res_wr_line,
        ko_line,
        "",
        f"若{solver_cn}方脱先（该处停一手），{opp_cn}方的局部最佳应对变化：{tenuki_line}",
        "脱先后同样区域的终局归属：",
        own_pass,
        "",
        f"棋手水平：约 {level}",
    ]
    user = "\n".join(
        [
            "你是一名死活题教练。上面是一道围棋死活的题目数据、正解与推演变化。",
            "请按「同步讲棋」格式输出：把讲解拆成段，每段绑定一段要摆在棋盘上的变化，"
            "让前端可以像讲棋视频一样边说边摆棋。",
            "",
            "【输入数据】",
            *data_lines,
            "",
            "【输出要求】只输出一个 JSON 对象，字段如下（全部必填）：",
            "- result_type: 最终结果是净活/劫活/双活/净杀/劫杀/官子（只能选一个）。"
            "判劫的标准：变化中有重复落子坐标、或正解后对手剩余胜率在 10%~40%、"
            "或归属网格中有较多均势格，则判劫活/劫杀而不是净活/净杀；",
            "- can_tenuki: 能脱先/不能脱先/有条件（三选一），"
            "依据「脱先变化与脱先后归属」判断；",
            "- segments: 同步讲棋分段数组，3~5 段，每项 {\"text\", \"variation\"}：",
            "  * text：本段讲解文字（口语化，一两句话，像讲棋老师边说边摆棋）；",
            "  * variation：本段要摆在棋盘上的变化——从上面给出的坐标中"
            "截取连续子序列，每步用 [\"B\"或\"W\", 坐标] 数组表示（如 [\"W\",\"D19\"]），"
            "双方交替、保持顺序；本段不涉及摆棋变化则给空数组；",
            "  * 首手颜色：正解/抵抗段从{solver_cn}方（题面先手方）开始，"
            "脱先后果段从{opp_cn}方开始；",
            "  * 段序：先结论（结果类型+能否脱先，可无变化），"
            "再讲正解手段（变化=正解+对方抵抗），再讲脱先后果（变化=脱先后的对方应对）；"
            "  * 只展示人类会下的合理交换，不要放无理手硬撑的变化；",
            "  * 坐标只能来自上面出现过的坐标，禁止编造；",
            "  * 【结论一致】第一段的结论必须与 result_type 和 can_tenuki 两个字段完全一致，"
            "段与段之间、段与字段之间不允许出现矛盾（如字段写劫杀、段里却写净死）。",
            "- takeaway: 一句可迁移的口诀（12 字内）。",
            "",
            "【硬性规则】",
            "1. 坐标只用上面出现过的坐标，禁止编造着法；",
            "2. 不讲中腹/全盘的复杂计算，聚焦局部死活；",
            "3. 变化只讲必要应对与合理交换；",
            "4. 只输出 JSON，不要输出任何解释性文字。",
        ]
    )
    return [
        {"role": "system", "content": SYSTEM_ROLE},
        {"role": "user", "content": user},
    ]
