"""2.6：代码运行设置中途修改入账（bug 修复）、接口实际返回的模型名、国产 3-2-1 阵容配置、界面复制与宽度分档。"""

import _winfix  # noqa: F401  Windows：测试结束时清理临时文件夹（见 _winfix.py）
import json
import re
import tempfile
import unittest
from pathlib import Path

from alreadythere import feed, report
from alreadythere.config import load_settings
from alreadythere.flow_review import ReviewFlow
from alreadythere.models import ModelHub, _same_model
from alreadythere.store import Project
from tests.test_v22 import Env
from tests.test_v23 import BEHAVE, d
from tests.test_v25 import Base, DONE

ROOT = Path(__file__).resolve().parent.parent


class TestCodeRunSettingChange(unittest.TestCase):
    def test_change_after_stop_is_logged(self):
        with tempfile.TemporaryDirectory() as t:
            e = Env(t, allow_code=False)
            e.flow()._setup_env()
            e.flow()._setup_env()  # 设置没变：不重复记
            rows = e.p.q("SELECT content FROM events WHERE type='代码运行设置'")
            self.assertEqual(len(rows), 1)
            e.st["params"]["allow_code_run"] = True  # 用户停止后在参数面板里勾上，再继续
            e.flow()._setup_env()
            rows = e.p.q("SELECT ref_id, content FROM events WHERE type='代码运行设置' ORDER BY seq")
            self.assertEqual(len(rows), 2)
            last = json.loads(rows[-1]["content"])
            self.assertTrue(last["允许运行代码"])
            self.assertIn("中途", last["说明"])
            self.assertNotEqual(rows[0]["ref_id"], rows[1]["ref_id"])

    def test_report_follows_actual_code_runs(self):
        # 复现 2.5 实跑 Q2 的情形：开头记的是"关"，继续时改成"开"并真的运行了代码
        with tempfile.TemporaryDirectory() as t:
            e = Env(t, allow_code=False)
            e.flow()._setup_env()
            e.st["params"]["allow_code_run"] = True
            e.flow().run()
            runs = e.p.q("SELECT COUNT(*) c FROM events WHERE type='代码运行'")[0]["c"]
            self.assertGreater(runs, 0)
            rep = report.build(e.p)
            notes = "\n".join(rep["说明"])
            self.assertNotIn("本次没有运行任何代码", notes)
            self.assertIn(f"实际运行了 {runs} 段代码", notes)
            self.assertIn("中途改过", notes)

    def test_no_code_still_says_none(self):
        with tempfile.TemporaryDirectory() as t:
            e = Env(t, allow_code=False)
            e.flow().run()
            notes = "\n".join(report.build(e.p)["说明"])
            self.assertIn("本次没有运行任何代码", notes)

    def test_feed_shows_mid_run_change(self):
        with tempfile.TemporaryDirectory() as t:
            e = Env(t, allow_code=False)
            e.flow()._setup_env()
            e.st["params"]["allow_code_run"] = True
            e.flow()._setup_env()
            e.p.log("用户", "参数变更", content={"allow_code_run": {"旧": False, "新": True}})
            texts = [m["text"] for m in feed.messages(e.p.root)["messages"]]
            self.assertTrue(any("运行中途改过设置" in x and "允许运行" in x for x in texts))
            self.assertTrue(any("allow_code_run：False → True" in x for x in texts))


class TestServedModel(Base):
    def _stream(self, served):
        o = {"model": served, "choices": [{"delta": {"content": '{"a":1}'}, "finish_reason": "stop"}]}
        BEHAVE["f"] = lambda b: ("sse", [d(**o), DONE])

    def test_served_model_recorded(self):
        self._stream("m-2026-09-02")
        h = self.hub()
        h.call_json("P1", "s", "u", call_key="t", round_=0)
        self.assertEqual(self.proj.q("SELECT served_model FROM calls")[0]["served_model"], "m-2026-09-02")
        self.assertFalse(self.proj.q("SELECT 1 FROM events WHERE type='实际模型不一致'"))  # 只多了日期后缀，不算不一致

    def test_mismatch_flagged_once(self):
        self._stream("other-model")
        h = self.hub()
        h.call_json("P1", "s", "u", call_key="t1", round_=0)
        h.call_json("P1", "s", "u", call_key="t2", round_=0)
        rows = self.proj.q("SELECT content FROM events WHERE type='实际模型不一致'")
        self.assertEqual(len(rows), 1)
        c = json.loads(rows[0]["content"])
        self.assertEqual((c["请求"], c["实际"]), ("m", "other-model"))
        texts = [m["text"] for m in feed.messages(self.proj.root)["messages"]]
        self.assertTrue(any("other-model" in x for x in texts))

    def test_speed_test_reports_served_model(self):
        self._stream("other-model")
        r = self.hub().speed_test("P1")
        self.assertEqual(r["served_model"], "other-model")
        self.assertTrue(r["model_mismatch"])

    def test_same_model_rules(self):
        self.assertTrue(_same_model("qwen3.8-max", "qwen3.8-max-2026-09-02"))
        self.assertTrue(_same_model("glm-5.3", "GLM-5.3"))
        self.assertTrue(_same_model("kimi-k3", "moonshot/kimi-k3"))
        self.assertFalse(_same_model("deepseek-v4-pro", "deepseek-v4.1-flash"))
        self.assertTrue(_same_model("m", None))


class TestLineup(unittest.TestCase):
    def setUp(self):
        self.st = load_settings(ROOT / "examples" / "设置_最强配置.json")

    def test_three_two_one(self):
        kinds = [v["kind"] for v in self.st["roles"].values()]
        self.assertEqual((kinds.count("生产者"), kinds.count("审查者"), kinds.count("找茬者")), (3, 2, 1))
        series = [v["series"] for v in self.st["roles"].values()]
        self.assertEqual(len(set(series)), 6)  # 六格六个不同系列
        models = {v["series"]: v["model"] for v in self.st["roles"].values()}
        self.assertEqual(models["DeepSeek"], "deepseek-flash")  # 旧名 deepseek-v4-pro 已被官方转走

    def test_every_key_in_template(self):
        tpl = (ROOT / "examples" / "密钥.env.模板").read_text(encoding="utf-8")
        names = set(re.findall(r"^([A-Z_]+)=", tpl, re.M))
        for r, v in self.st["roles"].items():
            self.assertIn(v["key_name"], names, r)

    def test_review_flow_accepts_lineup(self):
        with tempfile.TemporaryDirectory() as t:
            p = Project.create(Path(t) / "p", {"project_id": "T", "question": "q", "scope": "s", "task": "final_answer",
                                               "items": [{"id": "Q1", "problem": "1+1", "answer": "2"}]})
            f = ReviewFlow(p, self.st, ModelHub(p, self.st, {}))
            self.assertEqual(f.producers, ["P1", "P2", "P3"])
            self.assertEqual(f.reviewers, ["R1", "R2"])  # 找茬者 2.7 才出场，2.6 不参与流程
            p.close()


class TestPage(unittest.TestCase):
    def setUp(self):
        self.h = (ROOT / "alreadythere" / "app_page.html").read_text(encoding="utf-8")

    def test_width_tiers(self):
        for w, b in (("1280px", "720px"), ("1600px", "880px"), ("1920px", "1040px")):
            self.assertIn(f"@media (min-width:{w}){{:root{{--bubw:{b}}}}}", self.h)
        self.assertNotIn("max-width:820px", self.h)
        self.assertIn(".tthink{margin:2px 0 6px 44px;max-width:var(--bubw)}", self.h)

    def test_copy_features(self):
        self.assertIn("data-bcopy", self.h)
        self.assertIn('id="btnCopyAll"', self.h)
        self.assertIn("ICO_CHECK", self.h)
        self.assertIn("function copiedMark", self.h)
        # 复制整个聊天记录时跳过思考框
        self.assertIn("n.classList.contains('tthink')", self.h)


if __name__ == "__main__":
    unittest.main()


class TestPanelThinkingNewVendors(unittest.TestCase):
    """2.6：参数面板保存思考开关时，千问用 enable_thinking、GLM-5.3 关不掉、混元和智谱能调强度。"""

    def test_lineup_round_trip(self):
        import shutil
        from alreadythere import app as A
        with tempfile.TemporaryDirectory() as t:
            base = Path(t)
            shutil.copy(ROOT / "examples" / "设置_最强配置.json", base / "设置.json")
            a = A.App(base, base)
            v = {r["role"]: r for r in a.params_view()["roles"]}
            self.assertTrue(v["R1"]["thinking"] and not v["R1"]["effort_ok"])     # 千问：开着，不用 reasoning_effort
            self.assertTrue(v["R2"]["thinking_fixed"] and v["R2"]["effort_ok"])   # GLM-5.3
            self.assertTrue(v["P3"]["effort_ok"])                                 # 混元
            r = a.set_params({}, None, {}, {"R1": {"on": False}, "R2": {"on": False, "effort": "high"},
                                            "P3": {"on": True, "effort": "low"}, "P1": {"on": True, "effort": "max"}})
            self.assertTrue(r["ok"], r)
            st = json.loads((base / "设置.json").read_text(encoding="utf-8"))
            self.assertEqual(st["roles"]["R1"]["extra"], {"enable_thinking": False})
            self.assertEqual(st["roles"]["R2"]["extra"], {"thinking": {"type": "enabled"}, "reasoning_effort": "high"})
            self.assertEqual(st["roles"]["P3"]["extra"], {"reasoning_effort": "low", "thinking": {"type": "enabled"}})
            self.assertEqual(st["roles"]["P1"]["extra"], {"reasoning_effort": "max"})  # Kimi K3 不传 thinking
