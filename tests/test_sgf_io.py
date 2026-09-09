"""SGF 工具单元测试（坐标换算 + 解析最小用例）。

覆盖：SGF↔界面坐标互转（含跳过 I 列的边界用例）、pass、9/19 路、
主线手数提取（忽略分支）。
"""
import unittest

from backend.common.sgf_io import coord_to_sgf, parse_sgf, sgf_to_coord


class SgfCoordConversionTest(unittest.TestCase):
    """SGF 坐标 ↔ 界面坐标（19 路）。"""

    def test_top_left(self):
        self.assertEqual(sgf_to_coord("aa"), "A19")
        self.assertEqual(coord_to_sgf("A19"), "aa")

    def test_tengen(self):
        self.assertEqual(sgf_to_coord("pd"), "Q16")
        self.assertEqual(coord_to_sgf("Q16"), "pd")

    def test_komoku(self):
        self.assertEqual(sgf_to_coord("dd"), "D16")
        self.assertEqual(coord_to_sgf("D16"), "dd")

    def test_skip_i_column_boundary(self):
        # SGF 第 9 列（'i'）→ 界面跳过 I 后的 'J'
        self.assertEqual(sgf_to_coord("ia"), "J19")
        self.assertEqual(coord_to_sgf("J19"), "ia")
        # 'h' 是跳过 I 前的最后一列
        self.assertEqual(sgf_to_coord("ha"), "H19")
        self.assertEqual(coord_to_sgf("H19"), "ha")
        # 'j' 是跳过 I 后的下一列
        self.assertEqual(sgf_to_coord("ja"), "K19")
        self.assertEqual(coord_to_sgf("K19"), "ja")

    def test_row_boundaries(self):
        self.assertEqual(sgf_to_coord("sa"), "T19")   # 右上角（列 's'→'T'）
        self.assertEqual(coord_to_sgf("T19"), "sa")
        self.assertEqual(sgf_to_coord("ss"), "T1")    # 右下角
        self.assertEqual(coord_to_sgf("T1"), "ss")
        self.assertEqual(sgf_to_coord("as"), "A1")    # 左下角
        self.assertEqual(coord_to_sgf("A1"), "as")

    def test_pass(self):
        self.assertEqual(sgf_to_coord("tt"), "")
        self.assertEqual(sgf_to_coord(""), "")
        self.assertEqual(coord_to_sgf(""), "tt")
        self.assertEqual(coord_to_sgf("pass"), "tt")

    def test_out_of_range_is_pass(self):
        # 超出 19 路字母范围视为 pass
        self.assertEqual(sgf_to_coord("zz"), "")

    def test_nine_board(self):
        self.assertEqual(sgf_to_coord("aa", 9), "A9")
        self.assertEqual(coord_to_sgf("A9", 9), "aa")
        self.assertEqual(sgf_to_coord("ee", 9), "E5")   # 9 路天元
        self.assertEqual(coord_to_sgf("E5", 9), "ee")
        # 9 路 pass 记作 "jj"
        self.assertEqual(sgf_to_coord("jj", 9), "")
        self.assertEqual(coord_to_sgf("", 9), "jj")

    def test_invalid_coord_raises(self):
        with self.assertRaises(ValueError):
            coord_to_sgf("I19")   # 界面坐标不允许 I 列
        with self.assertRaises(ValueError):
            coord_to_sgf("A20")   # 行号超界
        with self.assertRaises(ValueError):
            coord_to_sgf("A0")


class SgfParseTest(unittest.TestCase):
    """SGF 文本解析最小用例。"""

    def test_basic_game(self):
        sgf = "(;GM[1]FF[4]SZ[19]PB[Black]PW[White];B[pd];W[dp];B[dd];W[pp];B[tt];W[])"
        parsed = parse_sgf(sgf)
        self.assertEqual(parsed.board_size, 19)
        self.assertEqual(parsed.black, "Black")
        self.assertEqual(parsed.white, "White")
        self.assertEqual(
            parsed.moves,
            [("B", "Q16"), ("W", "D4"), ("B", "D16"), ("W", "Q4"), ("B", ""), ("W", "")],
        )

    def test_variations_ignored(self):
        # 分支内着法不计入主线
        sgf = "(;GM[1]FF[4]SZ[19];B[pd](;W[dp];B[dd])(;W[dd]))"
        parsed = parse_sgf(sgf)
        self.assertEqual(parsed.moves, [("B", "Q16")])

    def test_board_size_detection(self):
        sgf = "(;GM[1]FF[4]SZ[9];B[ee])"
        parsed = parse_sgf(sgf)
        self.assertEqual(parsed.board_size, 9)
        self.assertEqual(parsed.moves, [("B", "E5")])


if __name__ == "__main__":
    unittest.main()
