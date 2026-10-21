"""2.5：超时拆分、流式状态、口径进提示词、硬伤/未验证分类、核对类型、作者验证未通过标记、候选值比对、
代码哈希、输出语言、速度测试、报告修正。"""

import json
import shutil
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from alreadythere import feed, report
from alreadythere.models import CallFailedPause, ModelHub, TransportError, MockProvider
from alreadythere.store import Project, code_hashes
from alreadythere.tasks import final_answer as T
from alreadythere.util import FormatError
from tests.test_v22 import Env, ITEMS, op
from tests.test_v23 import BEHAVE, SEEN, H, chunk

DONE = "data: [DONE]\n\n"
OK = chunk(content='{"a":1}', finish="stop")


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        SEEN.clear()
        self.dir = Path(tempfile.mkdtemp())
        self.proj = Project.create(self.dir / "x", {"project_id": "X", "question": "", "scope": "", "items": []})

    def tearDown(self):
        self.proj.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def hub(self, first=5, idle=5, retries=False, **params):
        from alreadythere.config import merge_params
        r = {"kind": "生产者", "provider": "openai_compat", "base_url": f"http://127.0.0.1:{self.port}",
             "key_name": "K", "model": "m", "series": "S"}
        p = {"first_packet_timeout_seconds": first, "stream_idle_timeout_seconds": idle,
             "retry_batch1": [0] if not retries else [0, 0], "retry_batch2": []}
        p.update(params)
        st = merge_params({"roles": {"P1": r}, "search": {"engine": "mock"}, "params": p})
        return ModelHub(self.proj, st, {"K": "k"}, sleep=lambda s: None)


class TestTimeouts(Base):
    def test_keepalive_resets_first_packet_timer(self):
        # 首包限 1 秒；总共等了 1.8 秒才出内容，但中间每 0.6 秒有保活 → 不算超时
        BEHAVE["f"] = lambda b: ("sse", [": ka\n\n", 0.6, ": ka\n\n", 0.6, ": ka\n\n", 0.6, OK, DONE])
        h = self.hub(first=1, idle=5)
        self.assertEqual(h.call_json("P1", "s", "u", call_key="t", round_=0), {"a": 1})
        row = self.proj.q("SELECT * FROM calls")[0]
        self.assertGreaterEqual(row["max_gap"], 0.5)      # 记录了最长无数据间隔
        self.assertGreaterEqual(row["first_byte"], 0.0)
        self.assertGreater(row["duration"], 1.5)

    def test_first_packet_timeout_message(self):
        BEHAVE["f"] = lambda b: ("sse", [2.5, OK, DONE])
        with self.assertRaises(CallFailedPause) as cm:
            self.hub(first=1, idle=30).call_json("P1", "s", "u", call_key="t", round_=0)
        self.assertIn("首包等待超时", cm.exception.reason)

    def test_stream_idle_uses_separate_limit(self):
        # 首包限 10 秒，流中断限 1 秒：出了第一块内容之后沉默 2.5 秒 → 流中断
        BEHAVE["f"] = lambda b: ("sse", [chunk(reasoning="想"), 2.5, OK, DONE])
        with self.assertRaises(CallFailedPause) as cm:
            self.hub(first=10, idle=1).call_json("P1", "s", "u", call_key="t", round_=0)
        self.assertIn("流中断", cm.exception.reason)

    def test_keepalive_resets_idle_timer(self):
        BEHAVE["f"] = lambda b: ("sse", [chunk(reasoning="想"), 0.6, ": ka\n\n", 0.6, ": ka\n\n", 0.6, ": ka\n\n", OK, DONE])
        self.assertEqual(self.hub(first=5, idle=1).call_json("P1", "s", "u", call_key="t", round_=0), {"a": 1})

    def test_per_role_timeout_overrides_panel(self):
        h = self.hub(first=300, idle=60)
        h.settings["roles"]["P1"]["first_packet_timeout_seconds"] = 660
        h.settings["roles"]["P1"]["stream_idle_timeout_seconds"] = 90
        self.assertEqual(h._timeouts(h.settings["roles"]["P1"]), (660.0, 90.0))

    def test_legacy_single_timeout_still_works(self):
        from alreadythere.config import merge_params
        st = merge_params({"roles": {}, "params": {"call_timeout_seconds": 42}})
        self.assertEqual((st["params"]["first_packet_timeout_seconds"], st["params"]["stream_idle_timeout_seconds"]), (42, 42))

    def test_defaults(self):
        from alreadythere.config import DEFAULT_PARAMS
        self.assertEqual((DEFAULT_PARAMS["first_packet_timeout_seconds"], DEFAULT_PARAMS["stream_idle_timeout_seconds"]), (300, 60))

    def test_duration_and_think_seconds_recorded(self):
        BEHAVE["f"] = lambda b: ("sse", [chunk(reasoning="想"), 0.7, chunk(content='{"a":1}', finish="stop"), DONE])
        self.hub().call_json("P1", "s", "u", call_key="t", round_=0)
        row = self.proj.q("SELECT duration, think_secs FROM calls")[0]
        self.assertGreater(row["duration"], 0.6)
        self.assertGreater(row["think_secs"], 0.5)
        ev = json.loads(self.proj.q("SELECT content FROM events WHERE type='模型调用'")[0]["content"])
        self.assertIn("seconds", ev)
        self.assertGreater(ev["think_seconds"], 0.5)

    def test_speed_test(self):
        BEHAVE["f"] = lambda b: ("sse", [chunk(reasoning="想"), 0.3, chunk(content="观者自在", finish="stop"),
                                         'data: {"choices":[],"usage":{"prompt_tokens":10,"completion_tokens":40}}\n\n', DONE])
        r = self.hub().speed_test("P1")
        self.assertEqual(r["tout"], 40)
        self.assertGreater(r["tps"], 0)
        self.assertIsNotNone(r["first_byte"])
        self.assertFalse(r["truncated"])


class TestTruncationMessage(Base):
    def test_message_names_effective_cap_and_no_zero_advice(self):
        BEHAVE["f"] = lambda b: ("sse", [chunk(reasoning="想"), chunk(content="{", finish="length"), DONE])
        with self.assertRaises(CallFailedPause) as cm:
            self.hub().call_json("P1", "s", "u", call_key="t", round_=0, max_tokens=4000)
        m = cm.exception.reason
        self.assertIn("4000", m)
        self.assertIn("较大的一个", m)
        self.assertIn("官方文档", m)  # 2.7：截断提示改成通用写法（用户定），不再写死"调到 131072 以上"
        self.assertIn("200000", m)
        self.assertNotIn("设为 0", m)

    def test_role_cap_takes_larger(self):
        BEHAVE["f"] = lambda b: ("sse", [chunk(content="{", finish="length"), DONE])
        h = self.hub()
        h.settings["roles"]["P1"]["max_tokens"] = 9000
        with self.assertRaises(CallFailedPause) as cm:
            h.call_json("P1", "s", "u", call_key="t", round_=0, max_tokens=4000)
        self.assertIn("9000", cm.exception.reason)


class TestAvatarState(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.p = Project.create(self.dir / "x", {"project_id": "X", "question": "", "scope": "", "items": []})

    def tearDown(self):
        self.p.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def st(self, role="P1"):
        return feed.role_states(self.p.root, True).get(role)

    # 2.11：实时状态按调用键记（每次调用一条），set_live_progress / set_live_problem 的第一个参数改成调用键
    def test_streaming_is_working_however_long(self):
        self.p.set_live_call("P1", "k", 1, 7)
        self.p.exec("UPDATE live_calls SET started_ts='2000-01-01T00:00:00+00:00' WHERE role='P1'")  # 已经跑了很久
        self.p.set_live_progress("k", 10, 0, "想", True)
        s = self.st()
        self.assertEqual(s["state"], "工作中")
        self.assertNotIn("retry", s)

    def test_keepalive_only_is_still_working(self):
        self.p.set_live_call("P1", "k", 1, 7)
        self.p.set_live_progress("k", 0, 0, None, False)
        self.assertEqual(self.st()["state"], "工作中")

    def test_confirmed_problem_is_responding_with_retry_number(self):
        self.p.set_live_call("P1", "k", 1, 7)
        self.p.set_live_problem("k", "连接中断")
        self.p.set_live_call("P1", "k", 2, 7)  # 第二次尝试已经发出，问题标记保留
        s = self.st()
        self.assertEqual((s["state"], s["retry"]), ("响应中", 1))
        self.assertIn("连接中断", s["problem"])
        self.p.set_live_progress("k", 5, 0, "新内容", True)  # 收到真正的内容 → 回到工作中
        self.assertEqual(self.st()["state"], "工作中")

    def test_hub_marks_problem_after_failed_attempt(self):
        seen = []
        n = {"i": 0}

        def responder(model, system, user):
            n["i"] += 1
            seen.append(feed.role_states(self.p.root, True).get("P1"))
            if n["i"] == 1:
                raise TransportError("模拟断线")
            return '{"a":1}'
        st = {"roles": {"P1": {"kind": "生产者", "provider": "mock", "model": "m", "series": "S"}},
              "params": {"retry_batch1": [0, 0], "retry_batch2": [], "first_packet_timeout_seconds": 5}}
        h = ModelHub(self.p, st, {}, providers={"mock||": MockProvider(responder)}, sleep=lambda s: None)
        self.assertEqual(h.call_json("P1", "s", "u", call_key="t", round_=0), {"a": 1})
        self.assertEqual(seen[0]["state"], "工作中")           # 第一次尝试不显示重试
        self.assertEqual((seen[1]["state"], seen[1]["retry"]), ("响应中", 1))

    def test_offline_after_pause_lists_all_failed_roles(self):
        self.p.log("程序", "调用失败暂停", content={"role": "P1", "reason": "欠费", "失败角色": [
            {"role": "P1", "reason": "欠费"}, {"role": "P2", "reason": "密钥错误"}]})
        s = feed.role_states(self.p.root, False)
        self.assertEqual((s["P1"]["state"], s["P2"]["state"]), ("离线", "离线"))
        self.assertIn("密钥错误", s["P2"]["reason"])


class TestDeferredPause(unittest.TestCase):
    def test_pause_recorded_after_other_roles_finish(self):
        with tempfile.TemporaryDirectory() as d:
            e = Env(d)
            e.fail = lambda role, system, user: role == "P1"
            r = e.flow().run()
            self.assertTrue(r.startswith("调用失败暂停"))
            ev = e.p.q("SELECT seq, type, actor, content FROM events ORDER BY seq")
            types = [x["type"] for x in ev]
            self.assertIn("调用失败", types)
            fail_seq = next(x["seq"] for x in ev if x["type"] == "调用失败")
            pause = [x for x in ev if x["type"] == "调用失败暂停"]
            self.assertEqual(len(pause), 1)
            self.assertEqual(ev[-1]["type"], "调用失败暂停")        # 暂停是最后一条
            sub_p2 = [x["seq"] for x in ev if x["type"] == "提交" and x["actor"] == "P2"]
            self.assertTrue(sub_p2 and sub_p2[0] < pause[0]["seq"])  # P2 本步照常做完才暂停
            c = json.loads(pause[0]["content"])
            self.assertEqual([x["role"] for x in c["失败角色"]], ["P1"])
            fc = json.loads(next(x["content"] for x in ev if x["type"] == "调用失败"))
            self.assertEqual(fc["role"], "P1")
            self.assertIn("仍在运行", fc)
            m = feed.messages(e.p.root)["messages"]
            self.assertTrue(any("等它们完成本步" in x["text"] or "即将暂停" in x["text"] for x in m))

    def test_single_failure_message_is_not_final(self):
        with tempfile.TemporaryDirectory() as d:
            e = Env(d)
            e.fail = lambda role, system, user: role == "P1"
            e.flow().run()
            msgs = [x["text"] for x in feed.messages(e.p.root)["messages"]]
            first_fail = next(t for t in msgs if "没能拿到回应" in t and "已暂停" not in t)
            self.assertNotIn("已暂停", first_fail)


class TestPromptsStrictness(unittest.TestCase):
    ITEM = [dict(ITEMS[0], scope="只要最终答案，不要求证明")]

    def prompts(self, **kw):
        with tempfile.TemporaryDirectory() as d:
            e = Env(d, items=self.ITEM, allow_code=True, **kw)
            e.flow().run()
            return e.prompts

    def test_scope_reaches_producer_reviewer_verifier(self):
        ps = self.prompts()
        for role_kind, pred in (("producer", lambda s: "【生产者】" in s), ("reviewer", lambda s: "【审查者】" in s and "最后一步" not in s),
                                ("verifier", lambda s: "最后一步" in s)):
            us = [u for r, s, u in ps if pred(s)]
            self.assertTrue(us, role_kind)
            self.assertTrue(all("只要最终答案，不要求证明" in u and "【口径】" in u for u in us), role_kind)

    def test_project_scope_used_when_item_has_none(self):
        with tempfile.TemporaryDirectory() as d:
            e = Env(d)  # 项目口径是"测试"
            e.flow().run()
            self.assertTrue(all("【口径】（题目提出者规定的" in u for r, s, u in e.prompts))

    def test_gold_never_in_prompts_with_or_without_gold(self):
        with tempfile.TemporaryDirectory() as d:
            e = Env(d, items=[{"id": "Q1", "name": "n", "problem": "PROB-Q1 求值", "gold": "7771777"}])
            e.flow().run()
            self.assertFalse(any("7771777" in u or "7771777" in s for r, s, u in e.prompts))
        with tempfile.TemporaryDirectory() as d:  # 没有标准答案：流程照常，不因此改变行为
            e = Env(d, items=[{"id": "Q1", "name": "n", "problem": "PROB-Q1 求值"}])
            self.assertEqual(e.flow().run(), "完成")
            self.assertEqual(e.final()["状态"], "闭合")

    def test_status_semantics_in_system_prompts(self):
        self.assertIn("不看你自己的标准", T.SYSTEM_PRODUCER)
        self.assertIn("确实没有完整答案", T.SYSTEM_PRODUCER)
        self.assertIn("assert", T.SYSTEM_PRODUCER)
        self.assertIn("没有证明不等于错误", T.SYSTEM_REVIEWER)
        self.assertIn("不要因为你自己认为数学上应当更严格", T.SYSTEM_REVIEWER)
        self.assertIn("引用了定理或文献", T.SYSTEM_REVIEWER)

    def test_output_language_block(self):
        self.assertIn("一律用中文写", T.lang_block("中文"))
        self.assertIn("Output language: English", T.lang_block("English"))
        self.assertIn("固定取值", T.lang_block("English"))
        ps = self.prompts(params={"output_language": "English"})
        self.assertTrue(all("Output language: English" in u for r, s, u in ps))
        self.assertIn("English", __import__("alreadythere.config", fromlist=["x"]).PARAM_SCHEMA[-2][3])


class TestSeverityValidation(unittest.TestCase):
    L = ["甲", "乙"]

    def rv(self, **kw):
        o = {"overall": {"甲": "通过", "乙": "有问题"}, "opinions": [], "unverified_parts": [], "implementations": []}
        o.update(kw)
        T.validate_review(self.L)(o)
        return o

    def test_hard_flaw_requires_basis(self):
        with self.assertRaises(FormatError) as cm:
            self.rv(opinions=[{"target": "乙", "severity": "硬伤", "problem": "上界没证明"}])
        self.assertIn("basis", str(cm.exception))
        self.assertIn("unverified_parts", str(cm.exception))
        for b in T.BASES[:5]:
            o = self.rv(opinions=[{"target": "乙", "severity": "硬伤", "basis": b, "problem": "x"}])
            self.assertEqual(o["opinions"][0]["basis"], b)

    def test_minor_needs_no_basis(self):
        o = self.rv(overall={"甲": "通过", "乙": "通过"}, opinions=[{"target": "乙", "severity": "次要", "problem": "措辞"}])
        self.assertEqual(o["opinions"][0]["basis"], "")

    def test_unverified_part_requires_tried(self):
        with self.assertRaises(FormatError) as cm:
            self.rv(overall={"甲": "通过", "乙": "通过"}, unverified_parts=[{"target": "乙", "what": "引用了定理 X 没证明"}])
        self.assertIn("tried", str(cm.exception))
        o = self.rv(overall={"甲": "通过", "乙": "通过"},
                    unverified_parts=[{"target": "乙", "what": "引用了定理 X 没证明", "tried": "对 n≤8 枚举，没找到反例"}])
        self.assertEqual(o["unverified_parts"][0]["tried"], "对 n≤8 枚举，没找到反例")

    def test_unverified_part_does_not_block_closure(self):
        with tempfile.TemporaryDirectory() as d:
            e = Env(d, rev=lambda *a: {"unverified_parts": [{"target": "甲", "what": "上界没证明", "tried": "枚举到 n=10，无反例"}]})
            e.flow().run()
            f = e.final()
            self.assertEqual(f["状态"], "闭合")
            ups = [x for x in f["未验证的部分"] if x["来源"].startswith("审查者")]
            self.assertTrue(ups and ups[0]["试过"] == "枚举到 n=10，无反例")
            # 进账本
            ev = json.loads(e.p.q("SELECT content FROM events WHERE type='评审' LIMIT 1")[0]["content"])
            self.assertEqual(ev["未验证的部分"][0]["what"], "上界没证明")

    def test_hard_flaw_blocks_closure(self):
        with tempfile.TemporaryDirectory() as d:
            def rev(role, iid, rnd, user, labels):
                if rnd == 1:
                    return {"overall": {"甲": "有问题", "乙": "通过"}, "opinions": [dict(op("甲", "第 3 步算错"), basis="计算错误")]}
            e = Env(d, rev=rev)
            e.flow().run()
            self.assertGreaterEqual(e.final()["轮数"], 2)

    def test_check_type_required_on_implementations(self):
        base = {"overall": {"甲": "通过", "乙": "通过"}, "opinions": []}
        impl = {"target": "甲", "explain": "e", "code": "print(1)"}
        with self.assertRaises(FormatError) as cm:
            T.validate_review(self.L)(dict(base, implementations=[dict(impl)]))
        self.assertIn("check_type", str(cm.exception))
        with self.assertRaises(FormatError):
            T.validate_review(self.L)(dict(base, implementations=[dict(impl, check_type="另一方法")]))
        with self.assertRaises(FormatError):
            T.validate_review(self.L)(dict(base, implementations=[dict(impl, check_type="未能换方法")]))
        o = dict(base, implementations=[dict(impl, check_type="另一方法", check_note="用符号推导代替枚举")])
        T.validate_review(self.L)(o)
        self.assertEqual(o["implementations"][0]["check_type"], "另一方法")
        o = dict(base, implementations=[dict(impl, check_type="同路线复现")])
        T.validate_review(self.L)(o)  # 同路线复现不要求 note

    def test_verifier_hard_opinion_defaults_basis(self):
        o = {"verifications": [{"target": "甲", "result": "不一致", "diagnosis": "真有错", "reason": "x"},
                               {"target": "乙", "result": "一致", "diagnosis": "无", "reason": "y"}],
             "opinions": [{"target": "甲", "severity": "硬伤", "problem": "两边输出不一样"}], "unverified": []}
        T.validate_verify(self.L, self.L)(o)
        self.assertEqual(o["opinions"][0]["basis"], "验证结果不一致")


class TestCheckTypeReport(unittest.TestCase):
    def test_same_route_vs_other_method_in_report(self):
        with tempfile.TemporaryDirectory() as d:
            def rev(role, iid, rnd, user, labels):
                return {"implementations": [
                    {"target": "甲", "check_type": "另一方法", "check_note": "用符号推导代替枚举", "explain": "e", "code": "print(1)"},
                    {"target": "乙", "check_type": "同路线复现", "check_note": "", "explain": "e", "code": "print(2)"}]}
            e = Env(d, allow_code=True, rev=rev)
            e.flow().run()
            f = e.final()
            by = {v["生产者"]: v for v in f["验证概况"]}
            self.assertEqual(by["P1"]["核对类型"], "另一方法")
            self.assertEqual(by["P2"]["核对类型"], "同路线复现")
            rep = report.build(e.p)
            md = (e.p.root / "outputs" / "结论.md").read_text(encoding="utf-8")
            self.assertIn("另一方法核对（用符号推导代替枚举）", md)
            self.assertIn("仅同路线复现，只核对了实现，未核对模型假设", md)
            # 程序自己补的：同路线复现 → 未验证项里有"仅核对实现，未核对模型假设"
            self.assertTrue(any("仅核对实现，未核对模型假设" in x["内容"] for x in f["未验证的部分"]))
            self.assertFalse(any("乙" == x["内容"][:1] and "另一方法" in x["内容"] for x in f["未验证的部分"]))

    def test_producer_limits_listed_as_source(self):
        with tempfile.TemporaryDirectory() as d:
            e = Env(d, prod=lambda *a: {"limits": "只验证了 n≤6"})
            e.flow().run()
            srcs = [x["来源"] for x in e.final()["未验证的部分"]]
            self.assertTrue(any("自述的限制" in s for s in srcs))


class TestAuthorCheck(unittest.TestCase):
    def test_author_assert_failure_flagged(self):
        with tempfile.TemporaryDirectory() as d:
            e = Env(d, allow_code=True, prod=lambda role, iid, rnd, user, ids: {"code": "assert 3 == 4, '预期 3 实得 4'"} if role == "P1" else {})
            e.flow().run()
            f = e.final()
            self.assertEqual([x["生产者"] for x in f["作者验证未通过却提交"]], ["P1"])
            self.assertIn("预期 3 实得 4", f["作者验证未通过却提交"][0]["摘要"])
            rep = report.build(e.p)
            md = (e.p.root / "outputs" / "结论.md").read_text(encoding="utf-8")
            self.assertIn("作者自己的验证没有通过却提交了答案", md)
            row = rep["逐题结果"][0]
            self.assertTrue(row["提示"])
            m = [x["text"] for x in feed.messages(e.p.root)["messages"]]
            self.assertTrue(any("作者自己的验证没有通过" in t for t in m))

    def test_passing_author_code_not_flagged(self):
        with tempfile.TemporaryDirectory() as d:
            e = Env(d, allow_code=True, prod=lambda *a: {"code": "assert 1 + 1 == 2\nprint('ok')"})
            e.flow().run()
            self.assertEqual(e.final()["作者验证未通过却提交"], [])

    def test_no_code_run_is_not_a_failure(self):
        r = T.author_check("得出", None)
        self.assertFalse(r["failed"])
        self.assertFalse(T.author_check("未得出", {"status": "出错", "stderr": "x"})["failed"])
        self.assertTrue(T.author_check("得出", {"status": "超时", "stderr": ""})["failed"])


class TestCandidateVsGold(unittest.TestCase):
    UNK = {"tried": "试了代入小情形与递推", "stuck": "一般情形的上界没有证明", "missing": "一个引理"}

    def run_env(self, ans):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        e = Env(d, prod=lambda *a: {"status": "部分得出", "answer": ans, "unknown": self.UNK})
        e.flow().run()
        return e

    def test_partial_with_correct_candidate_counted(self):
        e = self.run_env("7771777")  # 与标准答案相同
        rep = report.build(e.p)
        k = "正确率（基线=各生产者首轮独立作答；最终=评审闭合后的共识）"
        s = rep[k]["最终"]
        self.assertEqual((s["部分得出"], s["部分得出中候选值与标准答案一致的"], s["对"]), (1, 1, 0))
        c = json.loads(e.p.q("SELECT content FROM events WHERE type='对照标准答案' AND ref_id='Q1|最终'")[0]["content"])
        self.assertEqual(c["候选值与标准答案"], "自动一致")
        md = (e.p.root / "outputs" / "结论.md").read_text(encoding="utf-8")
        self.assertIn("其中候选值与标准答案一致 1", md)
        self.assertIn("漏报", md)

    def test_partial_with_wrong_candidate_not_counted(self):
        e = self.run_env("5")
        rep = report.build(e.p)
        s = rep["正确率（基线=各生产者首轮独立作答；最终=评审闭合后的共识）"]["最终"]
        self.assertEqual((s["部分得出"], s["部分得出中候选值与标准答案一致的"]), (1, 0))

    def test_none_has_no_candidate(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        e = Env(d, prod=lambda *a: {"status": "未得出", "answer": "", "unknown": self.UNK})
        e.flow().run()
        s = report.build(e.p)["正确率（基线=各生产者首轮独立作答；最终=评审闭合后的共识）"]["最终"]
        self.assertEqual((s["未得出"], s["未得出中候选值与标准答案一致的"]), (1, 0))

    def test_right_but_with_unverified_flagged_separately(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        items = [{"id": "Q1", "name": "n", "problem": "PROB-Q1 求值", "gold": "142857"}]
        e = Env(d, items=items, rev=lambda *a: {"unverified_parts": [{"target": "甲", "what": "引用定理未核对出处", "tried": "无法联网核对"}]})
        e.flow().run()
        s = report.build(e.p)["正确率（基线=各生产者首轮独立作答；最终=评审闭合后的共识）"]["最终"]
        self.assertEqual((s["对"], s["对且带未验证部分或作者验证未通过的"]), (1, 1))


class TestReportFixes(unittest.TestCase):
    def test_ledger_count_wording_and_value(self):
        with tempfile.TemporaryDirectory() as d:
            e = Env(d)
            e.flow().run()
            rep = report.build(e.p)
            total = e.p.q("SELECT COUNT(*) c FROM events")[0]["c"]
            self.assertEqual(rep["账本完整性"]["事件数"], total - 1)  # 不含随后记入的"输出结论"
            self.assertIn("不含", rep["账本完整性"]["说明"])
            md = (e.p.root / "outputs" / "结论.md").read_text(encoding="utf-8")
            self.assertIn("不含随后记入的「输出结论」", md)

    def test_per_role_timing_in_report(self):
        with tempfile.TemporaryDirectory() as d:
            e = Env(d)
            e.flow().run()
            rep = report.build(e.p)
            roles = {t["角色"] for t in rep["各角色用时"]}
            self.assertEqual(roles, {"P1", "P2", "R"})
            md = (e.p.root / "outputs" / "结论.md").read_text(encoding="utf-8")
            self.assertIn("各角色用时", md)
            self.assertIn("tokens/秒", md)


class TestCodeHash(unittest.TestCase):
    def test_hash_recorded_at_creation(self):
        with tempfile.TemporaryDirectory() as d:
            p = Project.create(Path(d) / "p", {"project_id": "X", "question": "", "scope": "", "items": []})
            c = json.loads(p.q("SELECT content FROM events WHERE type='项目创建'")[0]["content"])
            from alreadythere import __version__
            self.assertEqual(c["版本"], __version__)
            now = code_hashes()
            self.assertEqual(c["代码总哈希"], now["__total__"])
            self.assertIn("alreadythere/models.py", c["代码哈希"])
            self.assertIn("entry.py", c["代码哈希"])
            self.assertIn("alreadythere/tasks/final_answer.py", c["代码哈希"])
            self.assertNotIn("__total__", c["代码哈希"])
            self.assertTrue(p.verify_chain()[0])
            m = feed.messages(p.root)["messages"][0]
            self.assertEqual(m["codehash"]["total"], c["代码总哈希"])

    def test_hash_changes_with_content(self):
        a = code_hashes()
        f = Path(__import__("alreadythere").__file__).parent / "util.py"
        orig = f.read_text(encoding="utf-8")
        try:
            f.write_text(orig + "\n# x\n", encoding="utf-8")
            b = code_hashes()
        finally:
            f.write_text(orig, encoding="utf-8")
        self.assertNotEqual(a["__total__"], b["__total__"])


class TestParams(unittest.TestCase):
    def app(self, d):
        from alreadythere import app as A
        base = Path(d)
        (base / "设置.json").write_text(json.dumps({"roles": {"P1": {"kind": "生产者", "provider": "mock", "model": "m"}},
                                                  "params": {}}), encoding="utf-8")
        return A.App(base, base)

    def test_output_language_choice(self):
        with tempfile.TemporaryDirectory() as d:
            a = self.app(d)
            self.assertTrue(a.set_params({"output_language": "English"}, None, {})["ok"])
            self.assertEqual(json.loads((Path(d) / "设置.json").read_text(encoding="utf-8"))["params"]["output_language"], "English")
            self.assertFalse(a.set_params({"output_language": "Klingon"}, None, {})["ok"])
            v = a.params_view()
            sch = {x["key"]: x for x in v["schema"]}
            self.assertEqual(sch["output_language"]["type"], "choice")
            self.assertEqual(sch["output_language"]["min"][0], "中文")
            self.assertEqual(v["values"]["output_language"], "English")

    def test_every_param_explained_with_cost(self):
        from alreadythere.config import PARAM_SCHEMA
        for key, label, typ, lo, hi, desc, live in PARAM_SCHEMA:
            if not live:
                continue
            self.assertIn("意思", desc, key)
            self.assertTrue("代价" in desc, key)
            self.assertNotIn("实测", desc, key)
            self.assertNotIn("保守取", desc, key)

    def test_answer_max_tokens_text(self):
        from alreadythere.config import PARAM_SCHEMA
        d = next(x for x in PARAM_SCHEMA if x[0] == "answer_max_tokens")[5]
        self.assertIn("较大的一个", d)
        self.assertIn("131072", d)
        self.assertNotIn("设为 0", d)


if __name__ == "__main__":
    unittest.main()
