# -*- coding: utf-8 -*-
"""死活题生长链条测试（v1.7.0）。

- 数据层：problem_chains 注册/查询/题数统计（幂等）；
- 纯逻辑：局部裁剪（完整棋串 + 超框丢弃）、候选枚举（空邻点/角部要点/pass）、
  目标归类（做活/杀棋/对杀/棋筋）；
- 生长（mock 验题）：步序、幂等（第二次新增 0 题）、链内回放一致性；
- 端到端（真实引擎，skipUnless 保护）：只长 1 条链，断言题面可解析、
  轮到方正确、包围盒 ≤ 9×9。
"""

from __future__ import annotations

import contextlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from backend.common import db as db_mod  # noqa: E402
from backend.common import sgf_io  # noqa: E402
from backend.services.engine.verify import VerifyResult  # noqa: E402
from backend.services.problems import chains, store, utils  # noqa: E402

ENGINE_EXE = PROJECT_ROOT / "engine" / "katago-eigenavx2.exe"
MODEL = PROJECT_ROOT / "engine" / "b10c128.bin.gz"
CHAINS_DIR = PROJECT_ROOT / "data" / "chains"

# 测试用定式：右下角小目 + 白小飞挂 + 黑托 + 白退（19 路，局部 ≤ 9×9）
ROOT_SGF = (
    "(;GM[1]FF[4]CA[UTF-8]SZ[19]KM[7.5]C[测试定式]"
    ";B[pq];W[qn];B[qr];W[pn];B[oo])"
)


def _fake_verify_candidates(setup_sgf, candidates, profile, urgent_max,
                            allow_moves=None):
    """假验题：首个候选点 0.97（正解），其余 0.10，pass 0.05（满足紧迫性）。

    正解 PV 的第二步取第二个候选点（也是空点，局部内），供链条向下一层生长。
    """
    others = [c for c in candidates if c != "pass"]
    reply = others[1] if len(others) > 1 else ""
    results = []
    for c in candidates:
        if c == "pass":
            results.append(VerifyResult(coord="pass", winrate=0.05, visits=600,
                                        pv=[], best_coord=""))
        else:
            wr = 0.97 if c == others[0] else 0.10
            pv = [c] + ([reply] if reply else [])
            results.append(VerifyResult(coord=c, winrate=wr, visits=600,
                                        pv=pv, best_coord=reply))
    valid = sorted(
        [r for r in results if r.coord != "pass" and r.winrate is not None],
        key=lambda r: r.winrate or 0.0, reverse=True,
    )
    best = valid[0] if valid else None
    second = valid[1] if len(valid) > 1 else None
    pass_ok = any(
        r.coord == "pass" and (r.winrate or 1.0) < urgent_max for r in results
    )
    return best, second, results, pass_ok


def _fake_death_profile(setup_sgf, size, solver, answer, pv, region, target_xy,
                        profile="fast", pv_len=6, with_tenuki=True):
    """假局部死活画像。

    正解那次：达成目标（归属 0.92）且不能脱先（损失 0.42，紧急）；
    次优点那次（``with_tenuki=False``，唯一性检查）：未达成 → 正解唯一。
    """
    if not with_tenuki:
        return {
            "own_pv": 0.25, "own_after": None, "tenuki_loss": None,
            "grade": None, "result_type": "未达成", "opp_winrate": 0.6,
            "answer": answer, "line": [answer],
        }
    return {
        "own_pv": 0.92, "own_after": 0.50, "tenuki_loss": 0.42,
        "grade": "紧急", "result_type": "净", "opp_winrate": 0.01,
        "answer": answer, "line": [answer],
    }


class TestChainCrud(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "chains.db"
        db_mod.init_db(self.db)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_register_get_list_idempotent(self):
        chain = {
            "id": "chain-test", "name": "测试定式", "theme": "mixed",
            "root_sgf": ROOT_SGF, "description": "测试用",
        }
        self.assertTrue(chains.register_chain(chain, self.db))
        self.assertFalse(chains.register_chain(chain, self.db))  # 幂等
        got = chains.get_chain("chain-test", self.db)
        self.assertEqual(got["name"], "测试定式")
        self.assertEqual(got["status"], "draft")
        # 重复注册不覆盖 status / created_at
        chains.update_chain("chain-test", {"status": "active"}, self.db)
        chains.register_chain(chain, self.db)
        self.assertEqual(
            chains.get_chain("chain-test", self.db)["status"], "active"
        )
        listed = chains.list_chains(self.db)
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["problems_count"], 0)
        self.assertEqual(listed[0]["themes"], [])

    def test_register_updates_content_only(self):
        chain = {"id": "c1", "name": "旧名", "root_sgf": ROOT_SGF}
        chains.register_chain(chain, self.db)
        chains.register_chain(
            {"id": "c1", "name": "新名", "root_sgf": ROOT_SGF + " "}, self.db
        )
        got = chains.get_chain("c1", self.db)
        self.assertEqual(got["name"], "新名")

    def test_list_counts_and_themes(self):
        chains.register_chain(
            {"id": "c1", "name": "链一", "root_sgf": ROOT_SGF}, self.db
        )
        for i, theme in enumerate(["life_death", "life_death", "capturing_race"]):
            store.insert_problem({
                "id": f"p{i:016x}", "source": "chain", "theme": theme,
                "setup_sgf": ROOT_SGF, "answer": "Q6", "branches": "{}",
                "verdict": "", "hint": "", "chain_id": "c1", "chain_step": i + 1,
            }, self.db)
        listed = {c["id"]: c for c in chains.list_chains(self.db)}
        self.assertEqual(listed["c1"]["problems_count"], 3)
        self.assertEqual(listed["c1"]["themes"], ["life_death", "capturing_race"])
        items = store.list_chain_problems("c1", self.db)
        self.assertEqual([p["chain_step"] for p in items], [1, 2, 3])


class TestCropAndCandidates(unittest.TestCase):
    def test_crop_bbox_keeps_whole_groups(self):
        # 一串 3 子横跨包围盒边界：整串都要保留
        pos = {(2, 2): "B", (3, 2): "B", (4, 2): "B", (8, 8): "W"}
        kept = utils.crop_bbox(pos, 2, 2, 3, 3, 19)
        self.assertIn((4, 2), kept)      # 整串补全（在框外）
        self.assertNotIn((8, 8), kept)   # 无关棋串丢弃

    def test_crop_local_and_max_bbox(self):
        pos = {(0, 0): "B", (5, 5): "W"}
        kept = chains.crop_local(pos, 19, pad=1, max_bbox=9)
        self.assertEqual(kept, pos)
        self.assertIsNone(chains.crop_local(pos, 19, pad=1, max_bbox=4))
        self.assertIsNone(chains.crop_local({}, 19))

    def test_build_candidates_local_and_corner_and_pass(self):
        kept = {(3, 2): "B", (3, 3): "B", (4, 3): "W"}   # D3 / D4 / E4
        cands = chains.build_candidates(kept, 19, max_candidates=12)
        self.assertEqual(cands[-1], "pass")          # pass 始终保留
        self.assertEqual(len(cands), len(set(cands)))  # 去重
        for c in cands[:-1]:
            self.assertNotIn(utils.coord_to_xy(c), kept)  # 已占点被过滤
        # 左下角（局部所在角）的角部要点进入候选
        self.assertIn("B2", cands)   # 2-2
        self.assertIn("C3", cands)   # 3-3
        self.assertNotIn("D4", cands)   # 已占点不进候选
        self.assertNotIn("S18", cands)  # 无关角不送候选点

    def test_build_candidates_cap(self):
        kept = {(x, y): "B" for x in range(6) for y in range(6)}
        cands = chains.build_candidates(kept, 19, max_candidates=5)
        self.assertEqual(len(cands), 6)  # 5 + pass
        self.assertEqual(cands[-1], "pass")

    def test_classify_goal_rules(self):
        # 白两子仅剩 1 气、黑无危机 → 杀棋（黑先）
        pos = {(2, 2): "W", (3, 2): "W", (1, 2): "B", (2, 1): "B", (3, 1): "B",
               (4, 2): "B", (2, 3): "B"}
        self.assertEqual(
            chains.classify_goal("life_death", pos, 19, "B"), "杀棋"
        )
        self.assertEqual(
            chains.classify_goal("capturing_race", pos, 19, "B"), "对杀"
        )
        self.assertEqual(
            chains.classify_goal("middle", pos, 19, "B"), "中盘要点"
        )
        self.assertEqual(
            chains.classify_goal("endgame", pos, 19, "B"), "收官最大"
        )

    def test_region_of_covers_bbox_with_pad(self):
        kept = {(5, 5): "B"}
        region = chains.region_of(kept, 19, pad=2)
        self.assertEqual(len(region), 25)  # (2..8)²
        self.assertIn("G7", region)        # (6,6) → G7
        self.assertNotIn("A1", region)


class TestGrowMocked(unittest.TestCase):
    """生长逻辑（mock 验题，不启引擎）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "grow.db"
        db_mod.init_db(self.db)
        chains.register_chain(
            {"id": "chain-t", "name": "测试定式", "theme": "mixed",
             "root_sgf": ROOT_SGF},
            self.db,
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _grow(self, **kw):
        with mock.patch.object(chains, "verify_candidates",
                               side_effect=_fake_verify_candidates), \
             mock.patch.object(chains, "local_death_profile",
                               side_effect=_fake_death_profile):
            return chains.grow_chain("chain-t", db_path=self.db, **kw)

    def test_grow_creates_steps_and_metadata(self):
        result = self._grow(max_depth=3)
        self.assertEqual(result["added"], 3)
        self.assertEqual(result["steps"], 3)
        items = store.list_chain_problems("chain-t", self.db)
        self.assertEqual([p["chain_step"] for p in items], [1, 2, 3])
        for p in items:
            self.assertEqual(p["source"], "chain")
            self.assertRegex(p["id"], r"^p[0-9a-f]{16}$")
            self.assertIsNotNone(p["hint"])
            self.assertIsNone(p["explanation"])
            self.assertIn(p["theme"], utils.VALID_THEMES)
            branches = json.loads(p["branches"])
            self.assertEqual(branches["answer"]["coord"], p["answer"])
            self.assertEqual(branches["chain"]["id"], "chain-t")
            self.assertIn("allow_moves", branches)
        # 链状态转为 active
        self.assertEqual(chains.get_chain("chain-t", self.db)["status"], "active")

    def test_grow_idempotent(self):
        first = self._grow(max_depth=3)
        snapshot = [
            (p["id"], p["chain_step"]) for p in
            store.list_chain_problems("chain-t", self.db)
        ]
        second = self._grow(max_depth=3)
        self.assertEqual(first["added"], 3)
        self.assertEqual(second["added"], 0)
        self.assertEqual(
            [(p["id"], p["chain_step"]) for p in
             store.list_chain_problems("chain-t", self.db)],
            snapshot,
        )

    def test_grow_replay_invariant(self):
        """第 n 题题面可由第 n-1 题的正解 + 对手应手重放得到（链内一致性）。"""
        self._grow(max_depth=3)
        items = store.list_chain_problems("chain-t", self.db)
        parsed = sgf_io.parse_sgf(ROOT_SGF)
        size = int(parsed.board_size)
        base = chains.setup_stones(parsed, size)
        root_moves = list(parsed.moves)

        lines = [json.loads(p["branches"])["chain"]["moves"] for p in items]
        self.assertEqual(lines[0], [[c, m] for c, m in root_moves])  # 首题=定式起点
        for i in range(1, len(items)):
            prev_branches = json.loads(items[i - 1]["branches"])
            cur_branches = json.loads(items[i]["branches"])
            solver = prev_branches["solver"]
            opp = "W" if solver == "B" else "B"
            expected = (
                lines[i - 1]
                + [[solver, items[i - 1]["answer"]]]
                + [[opp, cur_branches["chain"]["reply"]]]
            )
            self.assertEqual(lines[i], expected)
            self.assertEqual(cur_branches["chain"]["parent_id"], items[i - 1]["id"])
            self.assertEqual(cur_branches["chain"]["step"], i + 1)

        for p, line in zip(items, lines):
            branches = json.loads(p["branches"])
            position = utils.apply_moves(
                dict(base), [(c, m) for c, m in line], size
            )
            kept = chains.crop_local(position, size, pad=1, max_bbox=9)
            self.assertEqual(
                utils.setup_sgf(kept, branches["solver"], size), p["setup_sgf"]
            )

    def test_grow_discards_and_stops(self):
        def weak(setup_sgf, candidates, profile, urgent_max, allow_moves=None):
            results = [
                VerifyResult(coord=c, winrate=0.5 if i == 0 else 0.2,
                             visits=100, pv=[c])
                for i, c in enumerate(candidates)
            ]
            return results[0], results[1], results, True

        with mock.patch.object(chains, "verify_candidates", side_effect=weak):
            result = chains.grow_chain("chain-t", db_path=self.db, max_depth=3)
        self.assertEqual(result["added"], 0)
        self.assertEqual(result["steps"], 0)
        self.assertGreaterEqual(result["discarded"], 1)
        self.assertEqual(store.list_chain_problems("chain-t", self.db), [])

    def test_grow_unknown_chain(self):
        with self.assertRaises(chains.ChainNotFoundError):
            chains.grow_chain("missing", db_path=self.db)


class TestLocalDeathGate(unittest.TestCase):
    """局部死活口径（chain_verify_mode=local_death，默认）的验收门。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "death.db"
        db_mod.init_db(self.db)
        chains.register_chain(
            {"id": "chain-d", "name": "死活链", "root_sgf": ROOT_SGF}, self.db,
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _grow(self, profile_side_effect=None, theme=None, **kw):
        kw.setdefault("verify_mode", "local_death")
        patches = [
            mock.patch.object(chains, "verify_candidates",
                              side_effect=_fake_verify_candidates),
            mock.patch.object(chains, "local_death_profile",
                              side_effect=profile_side_effect
                              or _fake_death_profile),
        ]
        if theme:   # 紧迫性（脱先损失）只对死活/对杀题生效
            patches.append(
                mock.patch.object(utils, "classify_theme", return_value=theme)
            )
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            return chains.grow_chain("chain-d", db_path=self.db, **kw)

    def test_accepts_when_goal_reached_and_urgent(self):
        result = self._grow(max_depth=2, theme="life_death")
        self.assertEqual(result["added"], 2)
        items = store.list_chain_problems("chain-d", self.db)
        branches = json.loads(items[0]["branches"])
        self.assertEqual(branches["local"]["own_pv"], 0.92)
        self.assertEqual(branches["local"]["grade"], "紧急")
        # 结论写进 verdict / hint
        self.assertIn("净活", items[0]["verdict"])
        self.assertIn("脱先损失", items[0]["verdict"])
        self.assertIn("目标：", items[0]["hint"])

    def test_rejects_when_goal_not_reached(self):
        def weak(*a, **kw):
            prof = _fake_death_profile(*a, **kw)
            prof["own_pv"] = 0.4          # 目标区没拿下
            prof["result_type"] = "未达成"
            return prof

        result = self._grow(profile_side_effect=weak, max_depth=2)
        self.assertEqual(result["added"], 0)
        self.assertGreaterEqual(result["discarded"], 1)

    def test_rejects_when_can_tenuki(self):
        def calm(*a, **kw):
            prof = _fake_death_profile(*a, **kw)
            prof["tenuki_loss"] = 0.05    # 可脱先 → 不是死活/对杀题
            prof["grade"] = "可脱先"
            return prof

        result = self._grow(profile_side_effect=calm, max_depth=2,
                            theme="life_death")
        self.assertEqual(result["added"], 0)

    def test_rejects_when_second_move_also_works(self):
        """次优点同样达成目标 → 不是唯一急所，丢弃。"""
        def two_ways(*a, with_tenuki=True, **kw):
            prof = _fake_death_profile(*a, **kw)
            if not with_tenuki:           # 次优点那次调用
                prof["own_pv"] = 0.90     # 换个点也能达成
            return prof

        result = self._grow(profile_side_effect=two_ways, max_depth=1)
        self.assertEqual(result["added"], 0)

    def test_goal_text_mapping(self):
        self.assertEqual(chains.goal_text("做活", "净", "B"), "黑方净活")
        self.assertEqual(chains.goal_text("杀棋", "劫/双活", "W"), "白方劫杀")
        self.assertEqual(chains.goal_text("对杀", "净", "B"), "黑方净杀")
        self.assertEqual(chains.goal_text("做活", "未达成", "B"), "")
        self.assertEqual(chains.goal_text("做活", None, "B"), "")

    def test_seq_with_parity(self):
        """题面奇偶决定行棋方：白先题面（含 B[tt]）之后轮到白。"""
        sgf_w = "(;GM[1]FF[4]CA[UTF-8]SZ[9]AB[cc]AW[dd];B[tt])"
        seq = chains._seq_with(sgf_w, ["E5", "F5"])
        self.assertEqual(seq[0], ["B", "pass"])
        self.assertEqual(seq[1], ["W", "E5"])
        self.assertEqual(seq[2], ["B", "F5"])


class TestGrowMockedHelpers(unittest.TestCase):
    """生长流程的边界（沿用 TestGrowMocked 的假验题）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "grow2.db"
        db_mod.init_db(self.db)
        chains.register_chain(
            {"id": "chain-t", "name": "测试定式", "theme": "mixed",
             "root_sgf": ROOT_SGF},
            self.db,
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _grow(self, **kw):
        with mock.patch.object(chains, "verify_candidates",
                               side_effect=_fake_verify_candidates), \
             mock.patch.object(chains, "local_death_profile",
                               side_effect=_fake_death_profile):
            return chains.grow_chain("chain-t", db_path=self.db, **kw)

    def test_grow_bad_sgf_raises(self):
        chains.register_chain(
            {"id": "chain-bad", "name": "坏链", "root_sgf":
             "(;GM[1]FF[4]SZ[19];B[pq];B[pq])"}, self.db,
        )
        with self.assertRaises(chains.ChainSgfError):
            chains.grow_chain("chain-bad", db_path=self.db)

    def test_grow_respects_max_depth_and_level(self):
        result = self._grow(max_depth=1, max_per_level=1)
        self.assertEqual(result["added"], 1)
        result2 = chains.grow_chain("chain-t", db_path=self.db, max_depth=0)
        self.assertEqual(result2["added"], 0)


@unittest.skipUnless(ENGINE_EXE.exists() and MODEL.exists(), "需要引擎与模型")
class TestGrowEndToEnd(unittest.TestCase):
    """真实引擎：只长 1 条链（首轮 1 层，控时）。

    注意：19 路定式终局是两分局面，契约阈值（正解 >0.95 且次优 <0.3）下
    实测产出为 0（见 chains.py 模块注释与 scripts/check_chain_seeds.py）。
    因此这里断言的是**流程与题面性质**：grow 跑通、返回结构合法、丢弃计数
    合理；一旦产出题目，则题面必须可解析、轮到方正确、包围盒 ≤ 9×9、
    且可从链起点重放。想要「必然出题」的 e2e，请把种子换成带生死的局面。
    """

    @classmethod
    def setUpClass(cls) -> None:
        seeds = sorted(CHAINS_DIR.glob("*.sgf")) if CHAINS_DIR.exists() else []
        if not seeds:
            raise unittest.SkipTest("data/chains/ 没有定式种子")
        cls.seed = seeds[0]

    def test_e2e_grow_one_chain(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "e2e.db"
            db_mod.init_db(db)
            sgf_text = self.seed.read_text(encoding="utf-8")
            chain_id = f"chain-{self.seed.stem}"
            chains.register_chain(
                {"id": chain_id, "name": self.seed.stem, "theme": "mixed",
                 "root_sgf": sgf_text},
                db,
            )
            result = chains.grow_chain(
                chain_id, max_depth=1, max_per_level=1, profile="standard",
                db_path=db,
            )
            self.assertEqual(result["chain_id"], chain_id)
            self.assertIsInstance(result["added"], int)
            self.assertIsInstance(result["discarded"], int)
            # 没长出题 = 至少尝试过并丢弃（证明验题真的跑了）
            if result["added"] == 0:
                self.assertGreaterEqual(result["discarded"], 1)
            for p in result["problems"]:
                parsed = sgf_io.parse_sgf(p["setup_sgf"])
                self.assertEqual(parsed.board_size, 19)
                stored = store.get_problem(p["id"], db)
                self.assertIsNotNone(stored)
                self.assertEqual(stored["source"], "chain")
                self.assertEqual(stored["chain_step"], 1)
                xs, ys = [], []
                for _color, coord in parsed.setup:
                    x, y = utils.coord_to_xy(coord)
                    xs.append(x)
                    ys.append(y)
                self.assertTrue(xs and ys)
                self.assertLessEqual(max(xs) - min(xs) + 1, 9)
                self.assertLessEqual(max(ys) - min(ys) + 1, 9)
                branches = json.loads(stored["branches"])
                # 轮到方正确：白先题面前置一手 B[tt] 修正奇偶
                root_moves = list(sgf_io.parse_sgf(sgf_text).moves)
                self.assertEqual(
                    branches["solver"], chains._next_color(root_moves)
                )
                self.assertIn("allow_moves", branches)  # 局部聚焦口径随题保存
                self.assertEqual(
                    branches["chain"]["moves"],
                    [[c, m] for c, m in root_moves],
                )


if __name__ == "__main__":
    unittest.main()
