"""成长视图：档案存储 + 画像引擎单元测试（不依赖引擎）。"""
import json
import os
import tempfile
import unittest
from pathlib import Path

from backend.common import db as db_mod
from backend.services.progress import analyzer, service, store


def _fake_game(rid, moves_spec, created_at):
    rows = []
    for mn, delta, cat, stdev in moves_spec:
        rows.append({
            "move_number": mn,
            "coord": "D4",
            "delta": delta,
            "category": cat,
            "best_coord": "D4" if cat != "blunder" else "Q16",
            "score_stdev": stdev,
        })
    return {"review_id": rid, "moves_count": len(moves_spec),
            "created_at": created_at, "move_rows": rows}


class AnalyzerTest(unittest.TestCase):
    def test_insight_features(self):
        g1 = _fake_game("r1", [(1, -0.01, "normal", 1.0)] * 5 +
                        [(6, -0.15, "blunder", 4.0)] * 2, "2026-09-01")
        g2 = _fake_game("r2", [(1, -0.02, "normal", 1.0)] * 7, "2026-09-10")
        ins = analyzer.build_insight([g1, g2])
        self.assertEqual(ins["n_games"], 2)
        self.assertGreater(ins["avg_loss_per_move"], 0)
        self.assertIn("blunder", ins["rates"])
        self.assertIn(ins["weakest_phase"], ("布局", "中盘", "官子"))
        self.assertTrue(ins["rank_estimate"].startswith("约"))
        self.assertIn(ins["trend"], ("improving", "declining", "flat"))

    def test_insight_empty(self):
        ins = analyzer.build_insight([])
        self.assertEqual(ins["n_games"], 0)

    def test_phase_split(self):
        # 布局坏手集中：weakest 应为布局
        spec = [(i + 1, -0.2, "blunder", 1.0) for i in range(4)]
        spec += [(i + 5, -0.005, "normal", 1.0) for i in range(16)]
        g = _fake_game("r1", spec, "2026-09-01")
        ins = analyzer.build_insight([g])
        self.assertEqual(ins["weakest_phase"], "布局")

    def test_trend_declining(self):
        early = _fake_game("r1", [(i, -0.005, "normal", 1.0) for i in range(10)],
                           "2026-09-01")
        late = _fake_game("r2", [(i, -0.1, "blunder", 1.0) for i in range(10)],
                          "2026-09-10")
        ins = analyzer.build_insight([early, late])
        self.assertEqual(ins["trend"], "declining")


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "t.db"
        db_mod.init_db(self.db)

    def tearDown(self):
        # Windows 下杀软扫描可能短暂锁定 sqlite 文件，清理失败不影响测试结果
        import gc
        gc.collect()
        try:
            self.tmp.cleanup()
        except PermissionError:
            pass

    def test_profile_crud_and_attach(self):
        p = store.create_profile("小明", "初级", self.db)
        self.assertTrue(p["id"].startswith("pp"))
        self.assertEqual(len(store.list_profiles(self.db)), 1)
        # 插入一个复盘记录
        conn = db_mod.connect(self.db)
        conn.execute(
            "INSERT INTO reviews (id, sgf_path, board_size, status, created_at)"
            " VALUES ('rv1', 'rv1.sgf', 9, 'done', '2026-09-12T00:00:00')")
        conn.commit()
        conn.close()
        self.assertTrue(store.attach_review(p["id"], "rv1", self.db))
        games = store.profile_games(p["id"], self.db)
        self.assertEqual(len(games), 1)
        self.assertEqual(games[0]["review_id"], "rv1")
        # 画像缓存写入/读取
        store.save_insight(p["id"], {"n_games": 1, "rank_estimate": "约 10 级"},
                           self.db)
        ins = store.get_insight(p["id"], self.db)
        self.assertEqual(ins["features"]["n_games"], 1)
        # 删除档案：复盘解除关联但不删除
        store.delete_profile(p["id"], self.db)
        self.assertIsNone(store.get_profile(p["id"], self.db))
        conn = db_mod.connect(self.db)
        n = conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0]
        conn.close()
        self.assertEqual(n, 1)

    def test_service_insight_no_games(self):
        p = service.create("空档案", db_path=self.db)
        with self.assertRaises(service.NoGamesError):
            service.insight(p["id"], db_path=self.db)


if __name__ == "__main__":
    unittest.main()
