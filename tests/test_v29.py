"""2.9：主窗口输入框（建题）、插话（等这一步跑完后暂停，暂停中可连着补充，继续后所有角色都收到）、立刻停止不变。"""

import json
import os
import tempfile
import unittest
from pathlib import Path

from alreadythere import feed, interject as I
from alreadythere.app import LOCK, App
from alreadythere.store import Project
from alreadythere.tasks import stage27 as S
from alreadythere.tasks.final_answer import SCOPE_IN_PROBLEM, scope_block

from test_v27 import Env27

BLOCK = "【用户补充】"


def env(t, **kw):
    kw.setdefault("flow_version", "2.9")
    return Env27(t, **kw)


def solve_prompts(e, role=None):
    return [(r, u) for r, s, u in e.prompts if "【生产者】" in s and "不做题" not in s and (role is None or r == role)]


class TestPauseAndInstructions(unittest.TestCase):
    def test_pause_after_step_then_all_roles_get_it(self):
        with tempfile.TemporaryDirectory() as t:
            e = env(t)
            I.post(e.p.root, "口径补充：只要最终式子")  # 运行开始时就有暂停请求
            r = e.flow().run()
            self.assertTrue(r.startswith("已暂停"), r)
            ins = e.ev("用户指令")
            # 第 1 轮第一步之前还在"读存档/还没调用过模型"，不处理；生产者交完后（审查前）送达并暂停
            self.assertEqual(ins[0]["位置"], [1, 2])
            self.assertEqual(e.ev("已暂停")[0]["位置"], [1, 2])
            self.assertFalse(I.pending(e.p.root))
            self.assertFalse(I.pause_requested(e.p.root))
            self.assertTrue(all(BLOCK not in u for _, u in solve_prompts(e)))  # 第 1 轮生产者没看到
            # 暂停中连着补充
            I.say_while_paused(e.p, "第二条补充")
            self.assertEqual(e.ev("用户指令")[1]["位置"], [1, 2])
            n_before = len(e.prompts)
            self.assertEqual(e.flow().run(), "完成")
            self.assertTrue(e.ev("继续运行"))
            later = e.prompts[n_before:]
            reviews = [u for r, s, u in later if s == S.SYSTEM_REVIEW_ONE_RAW]
            self.assertTrue(reviews and all("只要最终式子" in u and "第二条补充" in u for u in reviews))
            ffs = [u for r, s, u in later if s == S.SYSTEM_FAULTFINDER]
            self.assertTrue(ffs and all(BLOCK in u for u in ffs))
            # 2.9.1：收到补充后先进审查者——生产者刚交完，就由审查者按"原题＋补充"审；没有问题就闭合，生产者不必返工
            f = e.ev("最终状态")[0]
            self.assertEqual(f["轮数"], 1)
            self.assertFalse([u for r, s, u in later if "【生产者】" in s and "不做题" not in s])
            self.assertEqual(f["用户补充后重审"]["用户指令"], ["U1", "U2"])
            self.assertEqual(e.ev("转折点")[0]["先审的一轮"], 1)
            self.assertFalse(e.ev("存档答复不适用"))

    def test_before_start_applies_from_round_one(self):
        with tempfile.TemporaryDirectory() as t:
            e = env(t)
            I.log_instruction(e.p, "题目里的 k 指正整数", "2026-10-21T10:00:00", (1, 1), "开始运行前补充")
            self.assertEqual(e.flow().run(), "完成")
            sp = solve_prompts(e)
            self.assertTrue(sp and all("题目里的 k 指正整数" in u for _, u in sp))
            self.assertEqual(e.ev("最终状态")[0]["轮数"], 1)  # 第 1 轮开始前就生效，不额外加一轮

    def test_pause_mid_review_step(self):
        """审查进行中插话：等审查全部返回再暂停；继续后不改变找茬者出场（重放不走岔）。"""
        fired = []

        def one(role, n, user, lab):
            if not fired:
                fired.append(1)
                I.post(e.p.root, "审查时补充")
        with tempfile.TemporaryDirectory() as t:
            e = env(t, hooks={"one": one})
            r = e.flow().run()
            self.assertTrue(r.startswith("已暂停"))
            self.assertEqual(e.ev("已暂停")[0]["位置"], [1, 3])
            self.assertEqual(len(e.calls(S.SYSTEM_REVIEW_ONE_RAW)), 2)  # 两份答卷都审完了才停
            n0 = len(e.prompts)
            self.assertEqual(e.flow().run(), "完成")
            self.assertFalse(e.ev("存档答复不适用"))
            # 审查者刚审完：这一轮余下的不做，下一轮先审（生产者沿用已有答案，不调用）
            self.assertEqual(e.ev("转折点")[0]["先审的一轮"], 2)
            later = e.prompts[n0:]
            self.assertFalse([u for r, s, u in later if "【生产者】" in s and "不做题" not in s])
            self.assertTrue(all(BLOCK in u for r, s, u in later if s == S.SYSTEM_REVIEW_ONE_RAW))
            self.assertEqual(e.ev("最终状态")[0]["轮数"], 2)
            self.assertFalse([x for x in e.ev("轮次小结") if x["round"] == 1])  # 第 1 轮余下的步骤没做

    def test_undelivered_when_flow_already_ended(self):
        with tempfile.TemporaryDirectory() as t:
            e = env(t)
            self.assertEqual(e.flow().run(), "完成")
            # 流程结束后才到的话（窗口会拦；这里模拟竞争：文件已经写下）
            e2 = env(Path(t) / "b")

            def solve(role, n, user):
                I.post(e2.p.root, "太晚了", request_pause=False)
            e2.hooks["solve"] = solve
            self.assertEqual(e2.flow().run(), "完成")
            # 生产者那一步写下的话，在下一个检查点（审查前）送达，不暂停
            self.assertTrue(e2.ev("用户指令"))
            self.assertFalse(e2.ev("已暂停"))

    def test_pending_left_at_end_is_recorded(self):
        with tempfile.TemporaryDirectory() as t:
            e = env(t)

            def compare(role, n, user):
                return None
            fired = []

            def dec_hook(*a):
                pass
            f = e.flow()
            orig = f._decide

            def decide(st, r):
                orig(st, r)
                if not fired:
                    fired.append(1)
                    I.post(e.p.root, "最后一刻", request_pause=False)
            f._decide = decide
            self.assertEqual(f.run(), "完成")
            # 题已经闭合，补充仍然送达：已闭合的题重新交审查者按"原题＋补充"重审，没问题就维持闭合
            self.assertFalse(e.ev("插话未送达"))
            self.assertEqual(e.ev("用户指令")[0]["位置"], [2, 1])
            fin = e.ev("最终状态")[0]
            self.assertEqual(fin["状态"], "闭合")
            self.assertEqual(fin["用户补充后重审"]["之前的结论"], "闭合")
            self.assertFalse(I.pending(e.p.root))

    def test_feed_renders(self):
        with tempfile.TemporaryDirectory() as t:
            e = env(t)
            I.post(e.p.root, "补充一句")
            e.flow().run()
            msgs = feed.messages(e.p.root)["messages"]
            self.assertTrue(any(m["kind"] == "pause" for m in msgs))
            self.assertTrue(any(m["role"] == "用户" and m["text"] == "补充一句" for m in msgs))
            e.flow().run()
            msgs = feed.messages(e.p.root)["messages"]
            self.assertTrue(any("继续运行" in m["text"] for m in msgs))


class TestTurnsAndDirectInterrupt(unittest.TestCase):
    def _kill_at(self, hook_name, role_pred, e_holder):
        """模拟进程被掐断：某次调用时抛出 SystemExit（不会被流程当成调用失败）。"""
        def h(*a):
            role = a[0]
            if role_pred(role, *a[1:]):
                raise SystemExit("killed")
        return h

    def test_closed_item_rereviewed_and_reopened_on_hard_flaw(self):
        q2 = {"id": "Q2", "name": "q2", "problem": "第二题"}
        flaw = []

        def one(role, n, user, lab):
            # Q2 第 1 轮给硬伤，让它进入第 2 轮；用户补充之后，Q1 重审时给硬伤（新前提推翻了已闭合的结论）
            if "第二题" in user and "【用户补充】" not in user and lab == "甲" and not flaw:
                flaw.append(1)
                return {"overall": "有问题", "requirements": [{"req": "原题", "verdict": "不满足"}],
                        "opinions": [{"severity": "硬伤", "basis": "计算错误", "requirement": "原题", "problem": "算错"}]}
            if "【用户补充】" in user and "第二题" not in user and lab == "甲" and len(flaw) == 1:
                flaw.append(2)
                return {"overall": "有问题", "requirements": [{"req": "原题", "verdict": "不满足"}],
                        "opinions": [{"severity": "硬伤", "basis": "与口径冲突", "requirement": "原题", "problem": "补充说 k 为奇数"}]}
        with tempfile.TemporaryDirectory() as t:
            from test_v27 import ITEM
            e = env(t, items=(ITEM, q2), hooks={"one": one})
            f = e.flow()
            orig = f._decide

            def decide(st, r):  # 第 1 轮小结后（Q1 已闭合）用户插话并暂停
                orig(st, r)
                if r == 1 and st.id == "Q2":
                    I.post(e.p.root, "k 是奇数")
            f._decide = decide
            self.assertTrue(f.run().startswith("已暂停"))
            self.assertEqual(e.ev("已暂停")[0]["位置"], [2, 1])
            self.assertEqual(e.flow().run(), "完成")
            fin = {x["item"] if "item" in x else None: x for x in e.ev("最终状态")}
            q1 = [x for x in e.ev("最终状态") if x.get("用户补充后重审", {}).get("之前的结论") == "闭合"]
            self.assertTrue(q1)  # Q1 原来闭合，被重新打开
            self.assertGreaterEqual(q1[0]["轮数"], 3)  # 第 2 轮重审挑出硬伤 → 第 3 轮返工
            r3 = [u for r, s, u in e.prompts if "【生产者】" in s and "不做题" not in s and "补充说 k 为奇数" in u]
            self.assertTrue(r3)  # 生产者按审查意见返工

    def test_direct_interrupt_in_producer_step_round1(self):
        with tempfile.TemporaryDirectory() as t:
            e = env(t)

            def solve(role, n, user):
                if role == "P2" and n == 1:
                    raise SystemExit("killed")
            e.hooks["solve"] = solve
            with self.assertRaises(SystemExit):
                e.flow().run()
            self.assertTrue(e.p.q("SELECT 1 FROM calls WHERE call_id LIKE 'p-P1-Q1-r1#%' AND status='成功'"))
            r = I.direct_interrupt(e.p, e.p.root, ["p-P2-Q1-r1"])
            self.assertEqual(r["位置"], (1, 1))
            I.say_while_paused(e.p, "k 是正整数")
            del e.hooks["solve"]
            n0 = len(e.prompts)
            self.assertEqual(e.flow().run(), "完成")
            later = e.prompts[n0:]
            sp = [(r_, u) for r_, s, u in later if "【生产者】" in s and "不做题" not in s]
            # P1 已存档的答案照用、不重做；P2 手里没有答案，直接作答（另起编号，看得到补充）；被作废的调用不补
            self.assertEqual([r_ for r_, _ in sp], ["P2"])
            self.assertIn("k 是正整数", sp[0][1])
            self.assertTrue(e.p.q("SELECT 1 FROM calls WHERE call_id LIKE 'p-P2-Q1-r1-u#%' AND status='成功'"))
            self.assertFalse(e.p.q("SELECT 1 FROM calls WHERE call_id LIKE 'p-P2-Q1-r1#%' AND status='成功'"))
            self.assertEqual(e.ev("最终状态")[0]["轮数"], 1)

    def test_direct_interrupt_in_review_step(self):
        with tempfile.TemporaryDirectory() as t:
            e = env(t)
            cnt = []

            def one(role, n, user, lab):
                cnt.append(lab)
                if len(cnt) == 2:
                    raise SystemExit("killed")
            e.hooks["one"] = one
            with self.assertRaises(SystemExit):
                e.flow().run()
            killed = cnt[1]
            r = I.direct_interrupt(e.p, e.p.root, [f"rv1-R-Q1-{killed}-r1"])
            self.assertEqual(r["位置"], (1, 3))
            del e.hooks["one"]
            n0 = len(e.prompts)
            self.assertEqual(e.flow().run(), "完成")
            later = e.prompts[n0:]
            # 第 1 轮被切断的那份审查不补；第 1 轮余下的不做；第 2 轮先审（生产者不调用）
            self.assertFalse(e.p.q(f"SELECT 1 FROM calls WHERE call_id LIKE 'rv1-R-Q1-{killed}-r1#%' AND status='成功'"))
            self.assertFalse([u for r_, s, u in later if "【生产者】" in s and "不做题" not in s])
            self.assertEqual(e.ev("最终状态")[0]["轮数"], 2)
            self.assertFalse(e.ev("存档答复不适用"))

    def test_step_of_key(self):
        self.assertEqual(I.step_of_key("p-P1-Q1-r3"), (3, 1))
        self.assertEqual(I.step_of_key("p-P1-Q1-r3-u"), (3, 1))
        self.assertEqual(I.step_of_key("rv1-R-Q1-甲-r2"), (2, 2))
        self.assertEqual(I.step_of_key("rc-R-Q1-r2"), (2, 3))
        self.assertIsNone(I.step_of_key("fr-R-Q1-r2"))
        self.assertEqual(I.cut_position((2, 1)), (2, 1))
        self.assertEqual(I.cut_position((2, 2)), (2, 3))


class TestScope(unittest.TestCase):
    def test_scope_in_problem_block(self):
        b = scope_block(SCOPE_IN_PROBLEM)
        self.assertIn("口径写在题面里", b)
        self.assertNotIn("题目没有写口径", b)


class TestApp(unittest.TestCase):
    def _app(self, t):
        a = App.__new__(App)
        a.base = Path(t)
        return a

    def test_say_states(self):
        with tempfile.TemporaryDirectory() as t:
            e = env(t)
            a = self._app(t)
            name = e.p.root.name
            # 还没开始：直接记入，从第 1 轮第一步起生效
            r = a.say(name, "开始前补充")
            self.assertTrue(r["ok"], r)
            self.assertEqual(e.ev("用户指令")[0]["位置"], [1, 1])
            # 运行中（用本进程的 pid 假装在跑）：写待送达并请求暂停
            (e.p.root / LOCK).write_text(f"{os.getpid()} run\n", encoding="utf-8")
            r = a.say(name, "运行中插话")
            self.assertTrue(r["ok"], r)
            self.assertTrue(I.pause_requested(e.p.root))
            self.assertTrue(a.talk_state(e.p.root, True)["pausing"])
            # 裸题对照时不能插话
            (e.p.root / LOCK).write_text(f"{os.getpid()} bare\n", encoding="utf-8")
            self.assertFalse(a.say(name, "x")["ok"])
            (e.p.root / LOCK).unlink()
            # 被停止（有调用、不在暂停中）：不收，提示先继续运行
            (e.p.root / I.FLAG).unlink()
            for m in I.pending(e.p.root):
                (e.p.root / I.INBOX / m["file"]).unlink()
            e.flow().run()  # 跑完（没有暂停请求）
            self.assertFalse(a.say(name, "结束后")["ok"])  # 已结束

    def test_paused_state_and_say(self):
        with tempfile.TemporaryDirectory() as t:
            e = env(t)
            a = self._app(t)
            I.post(e.p.root, "插话")
            e.flow().run()
            st = a.talk_state(e.p.root, False)
            self.assertTrue(st["paused"])
            r = a.say(e.p.root.name, "暂停中再补一句")
            self.assertTrue(r["ok"], r)
            self.assertEqual(len(e.ev("用户指令")), 2)
            self.assertTrue(a.talk_state(e.p.root, False)["paused"])  # 补充不改变暂停状态

    def test_stopped_refuses(self):
        with tempfile.TemporaryDirectory() as t:
            e = env(t, params={"review_rounds": 3})
            a = self._app(t)
            e.p.log("程序", "代码运行设置", ref_type="key", ref_id="设置", content={})
            e.p.exec("INSERT INTO calls (call_id, role, status, ts) VALUES ('x#1','P1','失败','t')")
            r = a.say(e.p.root.name, "停止后")
            self.assertFalse(r["ok"])
            self.assertIn("继续运行", r["msg"])

    def test_quick_new(self):
        import shutil
        with tempfile.TemporaryDirectory() as t:
            a = App(Path(t))
            r = a.quick_new("求 1+1。只要最终答案。")
            self.assertTrue(r["ok"], r)
            cfg = json.loads((Path(t) / r["name"] / "config.json").read_text(encoding="utf-8"))
            self.assertEqual(cfg["scope"], SCOPE_IN_PROBLEM)
            self.assertEqual(cfg["flow_version"], "2.9")
            self.assertEqual(cfg["items"][0]["problem"], "求 1+1。只要最终答案。")
            self.assertNotIn("gold", cfg["items"][0])
            self.assertFalse(a.quick_new("   ")["ok"])
            shutil.rmtree(Path(t) / r["name"], ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
