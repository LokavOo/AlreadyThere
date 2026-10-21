"""2.10：时间显示（气泡时间、开始/结束、总用时只算真正在跑的时间）、解说者、时间戳接口（只留接口）。"""

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

from alreadythere import explainer, feed, interject as I, report, timestamp
from alreadythere.app import App
from alreadythere.store import Project

from test_v27 import Env27


def env(t, **kw):
    kw.setdefault("flow_version", "2.9")
    return Env27(t, **kw)


def set_ts(p, etype, when, nth=0):
    rows = p.q("SELECT seq FROM events WHERE type=? ORDER BY seq", (etype,))
    p.exec("UPDATE events SET ts=? WHERE seq=?", (when.isoformat(), rows[nth]["seq"]))


class TestTiming(unittest.TestCase):
    def test_run_time_excludes_stops(self):
        with tempfile.TemporaryDirectory() as t:
            p = Project.create(Path(t) / "x", {"project_id": "X", "question": "", "scope": "", "items": []})
            T0 = dt.datetime(2026, 10, 21, 1, 0, tzinfo=dt.timezone.utc)
            seq = [("运行开始", 0), ("已暂停", 10),            # 跑 10 分钟，打断暂停
                   ("运行开始", 15), ("直接打断", 20), ("已暂停", 20),  # 停 5 分钟后再跑 5 分钟，直接打断
                   ("运行开始", 30), ("用户停止运行", 31),       # 停 10 分钟，跑 1 分钟，停止
                   ("运行开始", 40), ("调用失败暂停", 42),       # 停 9 分钟，跑 2 分钟，调用失败
                   ("运行开始", 50), ("迭代结束", 53)]           # 停 8 分钟，跑 3 分钟，结束
            cnt = {}
            for ty, m in seq:
                p.log("程序", ty, content={})
                set_ts(p, ty, T0 + dt.timedelta(minutes=m), cnt.get(ty, 0))
                cnt[ty] = cnt.get(ty, 0) + 1
            tm = feed.timing(p.root, False)
            self.assertEqual(tm["run_seconds"], (10 + 5 + 1 + 2 + 3) * 60)
            self.assertEqual(tm["stops"], {"打断后的暂停": 300, "直接打断": 600, "停止运行": 540, "调用失败暂停": 480})
            self.assertTrue(tm["start"].startswith("2026-10-21T01:00"))
            self.assertTrue(tm["end"].startswith("2026-10-21T01:53"))

    def test_old_project_without_run_markers(self):
        with tempfile.TemporaryDirectory() as t:
            e = env(t)
            e.flow().run()  # 直接调流程，不经过命令行，没有"运行开始"
            tm = feed.timing(e.p.root)
            self.assertTrue(tm["old"])
            self.assertIsNone(tm["run_seconds"])

    def test_run_start_does_not_hide_pause(self):
        with tempfile.TemporaryDirectory() as t:
            e = env(t)
            I.post(e.p.root, "补充")
            self.assertTrue(e.flow().run().startswith("已暂停"))
            e.p.log("程序", "运行开始", content={})  # 命令行在继续运行前会记这一条
            self.assertIsNotNone(I.last_pause(e.p))
            self.assertEqual(e.flow().run(), "完成")
            self.assertTrue(e.ev("继续运行"))

    def test_feed_messages_carry_ts(self):
        with tempfile.TemporaryDirectory() as t:
            e = env(t)
            e.flow().run()
            msgs = feed.messages(e.p.root)["messages"]
            self.assertTrue(msgs and all(m.get("ts") for m in msgs))


class _Fake(timestamp.TimestampService):
    name = "fake"

    def stamp(self, digest_hex):
        return {"receipt": b"R:" + digest_hex.encode(), "service": "测试服务", "time": "2026-10-21T00:00:00Z", "note": ""}


class TestTimestamp(unittest.TestCase):
    def test_disabled_by_default(self):
        with tempfile.TemporaryDirectory() as t:
            e = env(t)
            e.flow().run()
            self.assertIsNone(timestamp.stamp_project(e.p, {"roles": {}}, {}))
            self.assertFalse(e.ev("时间戳"))
            report.build(e.p)
            md = (e.p.root / "outputs" / "结论.md").read_text(encoding="utf-8")
            self.assertIn("未启用", md)

    def test_unknown_service_logged_as_failed(self):
        with tempfile.TemporaryDirectory() as t:
            e = env(t)
            e.flow().run()
            timestamp.stamp_project(e.p, {"timestamp": {"service": "rfc3161"}}, {})
            ev = e.ev("时间戳")[0]
            self.assertEqual(ev["结果"], "失败")
            self.assertIn("只留了接口", ev["原因"])

    def test_registered_service_stamps_last_hash_once(self):
        with tempfile.TemporaryDirectory() as t:
            e = env(t)
            e.flow().run()
            timestamp.REGISTRY["fake"] = _Fake
            try:
                last = e.p.q("SELECT hash, event_id FROM events ORDER BY seq DESC LIMIT 1")[0]
                timestamp.stamp_project(e.p, {"timestamp": {"service": "fake"}}, {})
            finally:
                del timestamp.REGISTRY["fake"]
            ev = e.ev("时间戳")[0]
            self.assertEqual((ev["结果"], ev["打的哈希"], ev["截止到事件"]), ("成功", last["hash"], last["event_id"]))
            rec = json.loads((e.p.root / "时间戳" / "001.json").read_text(encoding="utf-8"))
            self.assertEqual(rec["digest"], last["hash"])
            report.build(e.p)
            md = (e.p.root / "outputs" / "结论.md").read_text(encoding="utf-8")
            self.assertIn("共打了 1 次", md)
            self.assertTrue(e.p.verify_chain()[0])


class _Hub:
    def __init__(self):
        self.seen = []

    def chat_once(self, role, system, user, max_tokens=4000):
        self.seen.append((role, system, user))
        return "现在审查者在核对第 1 轮的答卷（#12）。", {"seconds": 1, "tin": 10, "tout": 10}


class TestExplainer(unittest.TestCase):
    ST = {"roles": {"X1": {"kind": "解说者", "provider": "mock", "model": "m", "context_window": 20000}}, "params": {}}

    def test_not_configured(self):
        with tempfile.TemporaryDirectory() as t:
            r = explainer.ask(Path(t), None, "你好", {"roles": {}}, {}, False)
            self.assertFalse(r["ok"])
            self.assertIn("便宜的模型", r["msg"])

    def test_ask_records_chain_and_reads_project(self):
        with tempfile.TemporaryDirectory() as t:
            e = env(t)
            e.flow().run()
            h = _Hub()
            r = explainer.ask(e.p.root.parent, e.p.root.name, "现在在做什么？", self.ST, {}, False, hub=h)
            self.assertTrue(r["ok"])
            role, system, user = h.seen[0]
            self.assertEqual(role, "X1")
            self.assertIn("What is the exact value", user)   # 看得到题目
            self.assertIn("【项目记录", user)                 # 看得到过程
            self.assertIn("现在在做什么？", user)
            hist = explainer.history(e.p.root.parent, e.p.root.name)
            self.assertEqual([x["who"] for x in hist], ["用户", "解说者"])
            self.assertEqual(hist[1]["prev"], hist[0]["hash"])
            self.assertFalse(e.p.q("SELECT 1 FROM events WHERE content LIKE '%现在在做什么%'"))  # 不进项目账本

    def test_budget_drops_oldest_records_keeps_newest(self):
        with tempfile.TemporaryDirectory() as t:
            e = env(t)
            for i in range(400):
                e.p.log("用户", "用户指令", ref_type="key", ref_id=f"U{i}", content={"编号": f"U{i}", "内容": f"第{i}条" + "字" * 60,
                                                                                   "位置": [1, 1], "生效": "x"})
            prompt, info = explainer.build_prompt(e.p.root.parent, e.p.root.name, "问题", 12000, False)
            self.assertGreater(info["省略的记录条数"], 0)
            self.assertIn("第399条", prompt)
            self.assertNotIn("第0条字", prompt)
            self.assertIn("更早的记录已省略", prompt)

    def test_prompt_principle(self):
        for w in ("像一位", "老师", "你的价值", "认真"):
            self.assertNotIn(w, explainer.SYSTEM)

    def test_no_project_conversation(self):
        with tempfile.TemporaryDirectory() as t:
            h = _Hub()
            r = explainer.ask(Path(t), None, "这道题怎么问清楚？", self.ST, {}, False, hub=h)
            self.assertTrue(r["ok"])
            self.assertTrue((Path(t) / "解说者_未建题对话.jsonl").exists())

    def test_app_view(self):
        with tempfile.TemporaryDirectory() as t:
            (Path(t) / "设置.json").write_text(json.dumps(self.ST), encoding="utf-8")
            a = App(Path(t))
            v = a.explain_view("")
            self.assertTrue(v["configured"])
            self.assertEqual(v["history"], [])

    def test_examples_have_explainer(self):
        root = Path(__file__).resolve().parent.parent
        for f in ("设置.json", "设置_最强配置.json"):
            st = json.loads((root / "examples" / f).read_text(encoding="utf-8"))
            r, spec = explainer.find_role(st)
            self.assertTrue(r)
            self.assertIn("便宜", spec["_说明"])


if __name__ == "__main__":
    unittest.main()
