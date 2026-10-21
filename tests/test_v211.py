"""2.11：实时状态每次调用一条；思考循环检测（掐断→续写→从头重试一次→失败）；停止／直接打断时存下未完成调用已收到的内容。"""

import _winfix  # noqa: F401  Windows：测试结束时清理临时文件夹（见 _winfix.py）
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from alreadythere import feed, loopguard as G
from alreadythere.config import DEFAULT_PARAMS, PARAM_SCHEMA
from alreadythere.models import CallFailedPause, MockProvider, ModelHub, _thinking_off
from alreadythere.store import Project

LOOP = "OK.\n Let me write. \nWriting.\n"
GOOD_THINK = "".join(f"第{i}步：先整理条件，再代入数值计算，得到中间结果并检查边界情况是否成立。\n" for i in range(80))  # 约 3 千字、不重复


def proj(t):
    return Project.create(Path(t) / "x", {"project_id": "X", "question": "", "scope": "", "items": []})


def hub(p, responder, base_url="", params=None, model="m"):
    st = {"roles": {"P1": {"kind": "生产者", "provider": "mock", "model": model, "series": "S", "base_url": base_url,
                           "extra": {"thinking": {"type": "enabled"}, "reasoning_effort": "max"}}},
          "params": dict(DEFAULT_PARAMS, retry_batch1=[0, 0, 0], retry_batch2=[], **(params or {}))}
    return ModelHub(p, st, {}, providers={f"mock|{base_url}|": MockProvider(responder)}, sleep=lambda s: None)


class TestDetector(unittest.TestCase):
    POS = {
        "观察到的原样（三句一组）": "思路已经成型：先写出换元步骤，再代入端点。\n" + LOOP * 120,
        "中文两句组": "好的，我现在开始写。\n正在写。\n" * 100,
        "单句不换行": "Let me reconsider this. " * 80,
        "单词复读": "the answer is " + "wait " * 300,
        "单字复读": "前文\n" + "嗯" * 1200,
        "五句段落整体重复": "先看第一个约束。它要求函数在闭区间上连续。由连续性，最值必然存在。再看第二个约束。它给出了两个端点值。" * 20,
        "两组交替": "嗯，等等。不对。\n好的，继续。\n" * 80,
        "大小写空白微变": "".join(("OK.\nLet me write.\n" if i % 2 else "ok.\n  let me   WRITE.\n") for i in range(120)),
    }
    NEG = {
        "逐项检查（数字递增、内容不同）": "".join(f"检查第 {i} 项：结果 {i * i}，与预期一致。\n" for i in range(1, 80)),
        "表格（数值各异）": "| a | b |\n|---|---|\n" + "".join(f"| {i} | {i * 3} |\n" for i in range(60)),
        "代码相似行": "".join(f"total += arr[{i}]\n" for i in range(60)),
        "零矩阵（有限重复后继续）": "[\n" + "  0,\n" * 25 + "  0\n]\n" + GOOD_THINK[:600],
        "相同的打印语句 30 行后继续": "前文。\n" + 'print("-" * 40)\n' * 30 + GOOD_THINK[:800],
        "叠句不连续": "风起了。山在远处。风起了。水在近处。风起了。我们回家。" + GOOD_THINK[:400],
        "分隔线": "前文。\n" + "-" * 300 + "\n" + GOOD_THINK[:400],
        "正常推导": GOOD_THINK,
        "自我纠正三遍后走出": "等等，不对，重新算。" * 3 + GOOD_THINK[:600],
    }

    def test_positive(self):
        for name, txt in self.POS.items():
            for ch in (1, 7, 16, 64):
                hit, _ = G.scan(txt, "thinking", ch)
                self.assertTrue(hit, f"{name} chunk={ch}")

    def test_negative(self):
        for name, txt in self.NEG.items():
            for ch in (1, 7, 16, 64):
                hit, _ = G.scan(txt, "thinking", ch)
                self.assertFalse(hit, f"{name} chunk={ch}：{hit}")

    def test_numbers_only_change_is_weak_not_abort(self):
        txt = "".join(f"重试第 {i} 次，失败。\n" for i in range(1, 300))
        hit, weak = G.scan(txt, "thinking", 16)
        self.assertIsNone(hit)
        self.assertIn("只有数字在变的重复", [w["rule"] for w in weak])

    def test_answer_channel_has_higher_bar(self):
        txt = "前文。\n" + "相同的一行内容。\n" * 40  # 约 360 字的重复：思考里会掐，回答里不掐
        self.assertTrue(G.scan(txt + "相同的一行内容。\n" * 40, "thinking", 16)[0])
        self.assertFalse(G.scan(txt + "后面换了内容，正常结束。", "answer", 16)[0])

    def test_start_is_first_occurrence_and_trim(self):
        head = "前面的推导结论是 x=3。\n"
        txt = head + LOOP * 60
        hit, _ = G.scan(txt, "thinking", 16)
        self.assertEqual(hit["start"], len(head))
        self.assertEqual(G.trim(txt, hit["start"]), head.rstrip())

    def test_trim_backs_off_to_sentence_end(self):
        txt = "第一句。第二句的一半" + "Writing.\n" * 100
        self.assertEqual(G.trim(txt, len("第一句。第二句的一半")), "第一句。")

    def test_off_switch(self):
        g = G.StreamGuard({"loop_guard": False})
        for i in range(0, 3000, 16):
            g.on_think((LOOP * 200)[i:i + 16])  # 不抛异常


class TestLiveCallsPerCall(unittest.TestCase):
    def test_two_calls_same_role(self):
        with tempfile.TemporaryDirectory() as t:
            p = proj(t)
            p.set_live_call("R2", "rv1-R2-A13-甲-r1", 1, 7)
            p.set_live_call("R2", "rv1-R2-A13-乙-r1", 1, 7)
            p.set_live_progress("rv1-R2-A13-甲-r1", 100, 5, "甲的思考", True)
            p.set_live_progress("rv1-R2-A13-乙-r1", 50, 0, "乙的思考", True)
            s = feed.role_states(p.root, True)["R2"]
            self.assertEqual((s["state"], s["n"], s["think"], s["out"]), ("工作中", 2, 150, 5))
            self.assertEqual([c["label"] for c in s["calls"]], ["逐份评审 A13 甲 第1轮", "逐份评审 A13 乙 第1轮"])
            lt = feed.live_thinking(p.root)
            self.assertEqual(set(lt), {"rv1-R2-A13-甲-r1", "rv1-R2-A13-乙-r1"})
            p.clear_live_call("rv1-R2-A13-甲-r1")  # 先审完的一份结束：另一份还在，角色仍是工作中（2.10 的毛病）
            s = feed.role_states(p.root, True)["R2"]
            self.assertEqual((s["state"], s["n"]), ("工作中", 1))
            p.close()

    def test_problem_only_when_all_calls_have_problem(self):
        with tempfile.TemporaryDirectory() as t:
            p = proj(t)
            p.set_live_call("P1", "p-P1-A1-r1", 1, 7)
            p.set_live_call("P1", "p-P1-A2-r1", 2, 7)
            p.set_live_problem("p-P1-A2-r1", "连接中断")
            self.assertEqual(feed.role_states(p.root, True)["P1"]["state"], "工作中")
            p.set_live_problem("p-P1-A1-r1", "连接中断")
            s = feed.role_states(p.root, True)["P1"]
            self.assertEqual((s["state"], s["retry"]), ("响应中", 1))
            p.close()

    def test_old_role_keyed_table_is_rebuilt(self):
        with tempfile.TemporaryDirectory() as t:
            p = proj(t)
            root = p.root
            p.close()
            c = sqlite3.connect(root / "project.db")
            c.execute("DROP TABLE live_calls")
            c.execute("CREATE TABLE live_calls (role TEXT PRIMARY KEY, call_key TEXT, attempt INTEGER, of_total INTEGER, "
                      "started_ts TEXT, think_chars INTEGER)")
            c.execute("INSERT INTO live_calls (role, call_key) VALUES ('P1','k')")
            c.commit()
            c.close()
            p = Project(root).open()
            pk = [r[1] for r in p.conn.execute("PRAGMA table_info(live_calls)") if r[5]]
            self.assertEqual(pk, ["call_key"])
            p.set_live_call("P1", "a", 1, 7)
            p.set_live_call("P1", "b", 1, 7)
            self.assertEqual(len(p.q("SELECT * FROM live_calls")), 2)
            p.close()


class TestSaveUnfinished(unittest.TestCase):
    def test_saved_to_ledger_and_viewable(self):
        with tempfile.TemporaryDirectory() as t:
            p = proj(t)
            p.set_live_call("P3", "p-P3-A15-r1", 1, 7)
            p.set_live_progress("p-P3-A15-r1", 120000, 30, "最近的思考内容", True, "回答开头")
            n = p.save_unfinished("用户停止运行")
            self.assertEqual(n, 1)
            self.assertEqual(p.q("SELECT COUNT(*) c FROM live_calls")[0]["c"], 0)
            ev = p.q("SELECT seq, actor, content FROM events WHERE type='未完成调用'")[0]
            c = json.loads(ev["content"])
            self.assertEqual((ev["actor"], c["call_key"], c["思考字数"], c["思考（最近一段）"], c["回答（最近一段）"]),
                             ("P3", "p-P3-A15-r1", 120000, "最近的思考内容", "回答开头"))
            self.assertIn("只存了最近一段", c["说明"])
            self.assertEqual(feed.call_thinking(p.root, f"unfinished:{ev['seq']}"), "最近的思考内容")
            m = [x for x in feed.messages(p.root, 0)["messages"] if "没有完成" in x.get("text", "")]
            self.assertTrue(m and m[0]["think"]["call"] == f"unfinished:{ev['seq']}")
            self.assertIn("做题 A15 第1轮", m[0]["text"])
            p.close()


class TestContinuation(unittest.TestCase):
    def _calls(self, p):
        return p.q("SELECT call_id, status, failure, request, params, reasoning FROM calls ORDER BY rowid")

    def test_loop_then_continuation_succeeds(self):
        with tempfile.TemporaryDirectory() as t:
            p = proj(t)
            n = {"i": 0}

            def resp(model, system, user):
                n["i"] += 1
                if n["i"] == 1:
                    return {"think": GOOD_THINK + LOOP * 200, "text": ""}
                return {"think": "", "text": '{"a": 1}'}
            h = hub(p, resp, base_url="https://api.deepseek.com")
            self.assertEqual(h.call_json("P1", "系统", "原题", call_key="p-P1-A1-r1", round_=1), {"a": 1})
            cs = self._calls(p)
            self.assertEqual([c["status"] for c in cs], ["失败", "成功"])
            self.assertIn("疑似循环，已掐断", cs[0]["failure"])
            self.assertTrue(cs[0]["reasoning"].startswith(GOOD_THINK))         # 已收到的思考全部留档
            req = json.loads(cs[1]["request"])["user"]
            self.assertTrue(req.startswith("原题"))
            self.assertIn(GOOD_THINK.strip()[-50:], req)                        # 交还了重复之前的思考
            self.assertNotIn("Let me write", req)                               # 重复部分截掉了
            self.assertEqual(json.loads(cs[1]["params"])["extra"]["thinking"], {"type": "disabled"})  # DeepSeek 续写关思考
            evs = {e["type"]: json.loads(e["content"]) for e in p.q("SELECT type, content FROM events")}
            self.assertIn("续写", evs)
            last = [json.loads(e["content"]) for e in p.q("SELECT content FROM events WHERE type='模型调用' ORDER BY seq")]
            self.assertIn("循环", last[0])
            self.assertEqual(last[1]["续写"]["续写自"], "p-P1-A1-r1#1")
            self.assertEqual(p.q("SELECT COUNT(*) c FROM live_calls")[0]["c"], 0)
            p.close()

    def test_continuation_fails_then_fresh_retry(self):
        with tempfile.TemporaryDirectory() as t:
            p = proj(t)
            n = {"i": 0}

            def resp(model, system, user):
                n["i"] += 1
                if n["i"] == 1:
                    return {"think": GOOD_THINK + LOOP * 200, "text": ""}
                if n["i"] == 2:
                    return {"think": "", "text": "不是 JSON"}                     # 续写格式不对
                assert "思考记录" not in user                                      # 从头重试用原请求
                return {"think": GOOD_THINK, "text": '{"a": 2}'}
            h = hub(p, resp)
            self.assertEqual(h.call_json("P1", "s", "原题", call_key="k", round_=1), {"a": 2})
            cs = self._calls(p)
            self.assertEqual([c["status"] for c in cs], ["失败", "失败", "成功"])
            self.assertTrue(cs[1]["failure"].startswith("续写没成功"))
            p.close()

    def test_second_loop_fails_the_call(self):
        with tempfile.TemporaryDirectory() as t:
            p = proj(t)
            h = hub(p, lambda m, s, u: {"think": GOOD_THINK + LOOP * 200, "text": ""} if "思考记录" not in u
                    else {"think": "", "text": "坏"})
            with self.assertRaises(CallFailedPause) as cm:
                h.call_json("P1", "s", "原题", call_key="k", round_=1)
            self.assertIn("疑似循环", cm.exception.reason)
            self.assertEqual(len(self._calls(p)), 3)                              # 原调用、续写、从头重试
            p.close()

    def test_too_little_kept_skips_continuation(self):
        with tempfile.TemporaryDirectory() as t:
            p = proj(t)
            n = {"i": 0}

            def resp(model, system, user):
                n["i"] += 1
                return {"think": "很短。" + LOOP * 200, "text": ""} if n["i"] == 1 else {"think": "", "text": '{"a": 3}'}
            h = hub(p, resp)
            self.assertEqual(h.call_json("P1", "s", "原题", call_key="k", round_=1), {"a": 3})
            self.assertIn("跳过续写", [e["type"] for e in p.q("SELECT type FROM events")])
            self.assertNotIn("思考记录", json.loads(self._calls(p)[1]["request"])["user"])
            p.close()

    def test_answer_loop_retries_without_continuation(self):
        with tempfile.TemporaryDirectory() as t:
            p = proj(t)
            n = {"i": 0}

            def resp(model, system, user):
                n["i"] += 1
                return {"think": GOOD_THINK, "text": '{"a": [' + '{"k": "v"}, ' * 300} if n["i"] == 1 \
                    else {"think": "", "text": '{"a": 4}'}
            h = hub(p, resp)
            self.assertEqual(h.call_json("P1", "s", "原题", call_key="k", round_=1), {"a": 4})
            sk = [json.loads(e["content"]) for e in p.q("SELECT content FROM events WHERE type='跳过续写'")]
            self.assertIn("回答里", sk[0]["原因"])
            p.close()

    def test_guard_off(self):
        with tempfile.TemporaryDirectory() as t:
            p = proj(t)
            h = hub(p, lambda m, s, u: {"think": GOOD_THINK + LOOP * 200, "text": '{"a": 5}'}, params={"loop_guard": False})
            self.assertEqual(h.call_json("P1", "s", "u", call_key="k", round_=1), {"a": 5})
            p.close()

    def test_weak_signal_recorded_not_aborted(self):
        with tempfile.TemporaryDirectory() as t:
            p = proj(t)
            think = "".join(f"重试第 {i} 次，失败。\n" for i in range(1, 300))
            h = hub(p, lambda m, s, u: {"think": think, "text": '{"a": 6}'})
            self.assertEqual(h.call_json("P1", "s", "u", call_key="k", round_=1), {"a": 6})
            ev = json.loads(p.q("SELECT content FROM events WHERE type='模型调用'")[0]["content"])
            self.assertEqual(ev["status"], "成功")
            self.assertEqual(ev["疑似循环（未掐断）"][0]["rule"], "只有数字在变的重复")
            p.close()

    def test_thinking_off_rules(self):
        self.assertEqual(_thinking_off({"base_url": "https://api.deepseek.com", "extra": {"reasoning_effort": "max"}}),
                         ({"thinking": {"type": "disabled"}}, True))
        self.assertEqual(_thinking_off({"base_url": "https://dashscope.aliyuncs.com/x", "extra": {"enable_thinking": True}}),
                         ({"enable_thinking": False}, True))
        self.assertFalse(_thinking_off({"base_url": "https://api.moonshot.cn/v1", "model": "kimi-k3", "extra": {}})[1])
        self.assertFalse(_thinking_off({"base_url": "https://open.bigmodel.cn", "model": "glm-5.3", "extra": {}})[1])
        self.assertFalse(_thinking_off({"base_url": "https://tokenhub.tencentmaas.com/v1", "model": "hy4-preview",
                                        "extra": {"reasoning_effort": "high"}})[1])

    def test_continuation_prompt_is_mechanical(self):
        from alreadythere.models import ModelHub as H
        txt = H.CONT_HEAD + H.CONT_TAIL
        for w in ("信任", "可以相信", "很好", "正确的", "你上一次", "请务必"):
            self.assertNotIn(w, txt)


class TestParams(unittest.TestCase):
    def test_loop_params_explained(self):
        keys = {x[0]: x for x in PARAM_SCHEMA}
        for k in ("loop_guard", "loop_confirm_chars", "loop_confirm_chars_answer", "loop_min_keep_chars",
                  "loop_min_keep_percent"):
            self.assertIn(k, DEFAULT_PARAMS)
            self.assertIn("代价", keys[k][5])
        self.assertTrue(DEFAULT_PARAMS["loop_guard"])


class TestOverHttp(unittest.TestCase):
    """真实走 HTTP（本机假 SSE 服务器）：思考循环被掐断后连接关闭，服务器写不下去；续写请求关掉思考。"""

    def test_stream_cut_and_continue(self):
        import threading
        import time
        from http.server import ThreadingHTTPServer
        import test_v23 as T
        from alreadythere.__main__ import load_settings_from_obj
        sent = {"n": 0, "broken": False}

        def behave(body):
            if body.get("thinking") == {"type": "disabled"}:
                return ("sse", [T.chunk(content='{"ok": true}', finish="stop"), "data: [DONE]\n\n"])
            parts = [T.chunk(reasoning=GOOD_THINK)]
            for _ in range(3000):
                parts.append(T.chunk(reasoning=LOOP))
            return ("sse", parts)
        T.BEHAVE["f"] = behave
        T.SEEN.clear()
        srv = ThreadingHTTPServer(("127.0.0.1", 0), T.H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            with tempfile.TemporaryDirectory() as t:
                p = proj(t)
                r = {"kind": "生产者", "provider": "openai_compat", "base_url": f"http://127.0.0.1:{srv.server_address[1]}/deepseek",
                     "key_name": "K", "model": "m", "series": "S", "extra": {"thinking": {"type": "enabled"}}}
                st = load_settings_from_obj({"roles": {"P1": r}, "search": {"engine": "mock"},
                                             "params": {"call_timeout_seconds": 5, "retry_batch1": [0], "retry_batch2": []}})
                h = ModelHub(p, st, {"K": "k"}, sleep=lambda s: None)
                t0 = time.monotonic()
                self.assertEqual(h.call_json("P1", "s", "原题", call_key="k", round_=1), {"ok": True})
                self.assertLess(time.monotonic() - t0, 20)
                cs = p.q("SELECT status, reasoning FROM calls ORDER BY rowid")
                self.assertEqual([c["status"] for c in cs], ["失败", "成功"])
                self.assertLess(len(cs[0]["reasoning"]), len(GOOD_THINK) + 3000 * len(LOOP) // 2)  # 没等对方发完就掐了
                self.assertEqual(T.SEEN[1].get("thinking"), {"type": "disabled"})
                self.assertNotIn("reasoning_effort", T.SEEN[1])
                p.close()
        finally:
            srv.shutdown()


class TestIcon(unittest.TestCase):
    def test_icon_served_and_linked(self):
        import threading
        import urllib.request
        from http.server import ThreadingHTTPServer
        from alreadythere.app import App, make_handler
        with tempfile.TemporaryDirectory() as t:
            app = App(Path(t))
            srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            try:
                base = f"http://127.0.0.1:{srv.server_address[1]}"
                ico = urllib.request.urlopen(base + "/favicon.ico").read()
                png = urllib.request.urlopen(base + "/icon.png").read()
                page = urllib.request.urlopen(base + "/").read().decode("utf-8")
            finally:
                srv.shutdown()
        self.assertEqual(ico[:4], b"\x00\x00\x01\x00")      # ICO 文件头
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
        self.assertIn('href="/favicon.ico"', page)
        self.assertIn('src="/icon.png"', page)


class TestRename(unittest.TestCase):
    def test_old_program_folder_still_recognized(self):
        from alreadythere.app import _is_program
        with tempfile.TemporaryDirectory() as t:
            old = Path(t) / "观者自在 (AlreadyThere)_v2.10"
            (old / "guanzhe").mkdir(parents=True)
            (old / "guanzhe" / "__init__.py").write_text("", encoding="utf-8")
            new = Path(t) / "观者自在 (AlreadyThere)_v2.11"
            (new / "alreadythere").mkdir(parents=True)
            (new / "alreadythere" / "__init__.py").write_text("", encoding="utf-8")
            self.assertTrue(_is_program(old))
            self.assertTrue(_is_program(new))
            self.assertFalse(_is_program(Path(t)))


if __name__ == "__main__":
    unittest.main()
