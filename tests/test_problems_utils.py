"""题目系统工具单测（窗口3，不依赖引擎）。

覆盖 utils.py：坐标换算、重放提子、局部裁剪、题面 SGF 奇偶修正、
主题归类、难度分级、规则模板。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from backend.services.problems import utils  # noqa: E402


class TestCoord(unittest.TestCase):
    def test_roundtrip(self):
        for size in (9, 19):
            for x in range(size):
                for y in range(size):
                    c = utils.xy_to_coord(x, y, size)
                    self.assertEqual(utils.coord_to_xy(c), (x, y))

    def test_invalid(self):
        with self.assertRaises(ValueError):
            utils.coord_to_xy("")
        with self.assertRaises(ValueError):
            utils.coord_to_xy("I5")  # I 不在列字母表
        with self.assertRaises(ValueError):
            utils.xy_to_coord(20, 0, 9)

    def test_normalize(self):
        self.assertEqual(utils.normalize_coord(" d15 "), "D15")
        self.assertEqual(utils.normalize_coord("pass"), "pass")
        self.assertEqual(utils.normalize_coord(""), "pass")


class TestReplay(unittest.TestCase):
    def test_capture(self):
        # D6 单子被白 C6/E6/D7/D5 围杀（9 路）
        moves = [
            ("B", "D6"), ("W", "C6"), ("B", "A9"), ("W", "E6"),
            ("B", "B9"), ("W", "D7"), ("B", "C9"), ("W", "D5"),
        ]
        pos = utils.replay_position(moves, 9)
        self.assertNotIn((3, 5), pos)          # D6 被提
        self.assertEqual(pos.get((2, 5)), "W")  # C6
        self.assertEqual(pos.get((3, 4)), "W")  # D5

    def test_suicide_rejected(self):
        # 黑下在四面包围的点上：自杀
        moves = [("W", "C4"), ("W", "D3"), ("W", "E4"), ("W", "D5")]
        with self.assertRaises(ValueError):
            utils.replay_position(moves + [("B", "D4")], 9)

    def test_pass_ignored(self):
        pos = utils.replay_position([("B", "A9"), ("W", "")], 9)
        self.assertEqual(pos, {(0, 8): "B"})


class TestCropAndSgf(unittest.TestCase):
    def test_crop_keeps_complete_group(self):
        # E5-E6-E7 竖串穿出半径 1 的框 → 整串保留
        pos = {
            utils.coord_to_xy("E5"): "B",
            utils.coord_to_xy("E6"): "B",
            utils.coord_to_xy("E7"): "B",
            utils.coord_to_xy("A1"): "W",
        }
        kept = utils.crop_stones(pos, utils.coord_to_xy("E5"), 1, 9)
        self.assertIn(utils.coord_to_xy("E7"), kept)
        self.assertNotIn(utils.coord_to_xy("A1"), kept)

    def test_setup_sgf_parity(self):
        from backend.common import sgf_io

        pos = {utils.coord_to_xy("E5"): "B", utils.coord_to_xy("D4"): "W"}
        sgf_b = utils.setup_sgf(pos, "B", 9)
        parsed = sgf_io.parse_sgf(sgf_b)
        self.assertEqual(parsed.board_size, 9)
        self.assertEqual(len(parsed.moves), 0)  # 黑先无需修正 pass
        self.assertIn("AB", sgf_b)
        self.assertIn("AW", sgf_b)

        sgf_w = utils.setup_sgf(pos, "W", 9)
        parsed_w = sgf_io.parse_sgf(sgf_w)
        self.assertEqual(len(parsed_w.moves), 1)
        self.assertEqual(parsed_w.moves[0], ("B", ""))  # 白先：前置一手黑 pass

    def test_setup_sgf_verify_roundtrip(self):
        # 题面解析回摆子后应还原 kept 局面（奇偶修正 pass 不影响局面）
        import re

        from backend.common import sgf_io

        pos = {utils.coord_to_xy("E5"): "B", utils.coord_to_xy("D4"): "W"}
        for solver in ("B", "W"):
            sgf = utils.setup_sgf(pos, solver, 9)
            rebuilt = {}
            for prop, color in (("AB", "B"), ("AW", "W")):
                for block in re.finditer(rf"{prop}((?:\[[a-zA-Z]*\])+)", sgf):
                    for m in re.finditer(r"\[([a-zA-Z]*)\]", block.group(1)):
                        coord = sgf_io.sgf_to_coord(m.group(1), 9)
                        if coord:
                            rebuilt[utils.coord_to_xy(coord)] = color
            self.assertEqual(rebuilt, pos)


class TestClassifyTheme(unittest.TestCase):
    def test_endgame(self):
        pos = {(0, 0): "B"}
        self.assertEqual(
            utils.classify_theme(pos, pos, 9, 18, 20), "endgame"
        )
        self.assertEqual(
            utils.classify_theme(pos, pos, 9, 10, 20), "middle"
        )

    def test_capturing_race(self):
        # 黑白两条棋串互相紧气（各 2~3 气）
        pos = {
            (0, 1): "B", (0, 2): "B", (2, 1): "B",
            (1, 1): "W", (1, 2): "W",
        }
        self.assertEqual(
            utils.classify_theme(pos, pos, 9, 5, 20), "capturing_race"
        )

    def test_life_death(self):
        # 黑两子串仅 2 气，被白包围
        pos = {
            (3, 3): "B", (3, 4): "B",
            (2, 3): "W", (4, 3): "W", (3, 2): "W", (3, 5): "W",
        }
        self.assertEqual(
            utils.classify_theme(pos, pos, 9, 5, 20), "life_death"
        )

    def test_middle(self):
        pos = {(0, 0): "B", (8, 8): "W"}
        self.assertEqual(
            utils.classify_theme(pos, pos, 9, 5, 20), "middle"
        )


class TestDifficultyAndTemplates(unittest.TestCase):
    def test_difficulty_bands(self):
        d1, lo1, hi1 = utils.difficulty_from_gap(0.92, -5)
        self.assertEqual(d1, 1)
        self.assertEqual(lo1, -9)
        self.assertEqual(hi1, -5)
        d5, lo5, hi5 = utils.difficulty_from_gap(0.74, -5)
        self.assertEqual(d5, 5)
        self.assertEqual(lo5, -5)
        self.assertEqual(hi5, -1)

    def test_region_label(self):
        self.assertEqual(utils.region_label("A1", 19), "左下")
        self.assertEqual(utils.region_label("T19", 19), "右上")
        self.assertEqual(utils.region_label("K10", 19), "中央")  # 天元

    def test_hint_and_verdict(self):
        for theme in utils.VALID_THEMES:
            hint = utils.hint_text(theme, "B", "E5", 9)
            self.assertIn("黑先", hint)
            verdict = utils.verdict_text(theme, "B", "E5", 0.97, "D4", 0.2)
            self.assertIn("97%", verdict)
            self.assertIn("E5", verdict)

    def test_default_rank(self):
        self.assertEqual(utils.default_rank_range("life_death"), (-15, 0))
        self.assertEqual(utils.default_rank_range("middle"), (-10, 3))

    def test_problem_id(self):
        pid = utils.problem_id("(;SZ[9])", "life_death")
        self.assertEqual(len(pid), 17)
        self.assertEqual(pid, utils.problem_id("(;SZ[9])", "life_death"))
        self.assertNotEqual(pid, utils.problem_id("(;SZ[9])", "middle"))


if __name__ == "__main__":
    unittest.main()
