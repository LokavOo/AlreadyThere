"""2.3：流式调用层测试（本机假 SSE 服务器，真实走 HTTP）。"""
import _winfix  # noqa: F401  Windows：测试结束时清理临时文件夹（见 _winfix.py）
import json, shutil, tempfile, threading, time, unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import alreadythere.models as M

from alreadythere.__main__ import load_settings_from_obj
from alreadythere.models import ModelHub, CallFailedPause
from alreadythere.store import Project

BEHAVE = {}
SEEN = []


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n))
        SEEN.append(body)
        beh = BEHAVE["f"](body)
        kind = beh[0]
        if kind == "400":
            self.send_response(400); self.end_headers(); self.wfile.write(beh[1].encode()); return
        if kind == "json":
            self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers()
            self.wfile.write(json.dumps(beh[1]).encode()); return
        self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()
        for ch in beh[1]:
            if isinstance(ch, (int, float)):
                time.sleep(ch); continue
            self.wfile.write(ch.encode()); self.wfile.flush()


def d(**k):
    return "data: " + json.dumps(k) + "\n\n"


def chunk(content=None, reasoning=None, finish=None, usage=None):
    delta = {}
    if content is not None: delta["content"] = content
    if reasoning is not None: delta["reasoning_content"] = reasoning
    o = {"choices": [{"delta": delta, "finish_reason": finish}]}
    if usage: o["usage"] = usage
    return d(**o)


class TestStream(unittest.TestCase):
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
        self.proj.close(); shutil.rmtree(self.dir, ignore_errors=True)

    def hub(self, timeout=5, **role):
        r = {"kind": "生产者", "provider": "openai_compat", "base_url": f"http://127.0.0.1:{self.port}",
             "key_name": "K", "model": "m", "series": "S"}
        r.update(role)
        st = load_settings_from_obj({"roles": {"P1": r}, "search": {"engine": "mock"},
                                     "params": {"call_timeout_seconds": timeout, "retry_batch1": [0, 0, 0],
                                                "retry_batch2": [0, 0, 0]}})
        return ModelHub(self.proj, st, {"K": "k"}, sleep=lambda s: None)

    def test_reasoning_usage_keepalive_and_extra(self):
        BEHAVE["f"] = lambda b: ("sse", [": ping\n\n", chunk(reasoning="想一想"), chunk(reasoning="再想"),
                                         chunk(content='{"ok":'), chunk(content=' true}', finish="stop"),
                                         d(choices=[], usage={"prompt_tokens": 11, "completion_tokens": 22}),
                                         "data: [DONE]\n\n"])
        h = self.hub(max_tokens=16000, extra={"thinking": {"type": "disabled"}, "model": "bad"})
        self.assertEqual(h.call_json("P1", "s", "u", call_key="t", round_=0, max_tokens=300), {"ok": True})
        b = SEEN[0]
        self.assertTrue(b["stream"]); self.assertEqual(b["max_tokens"], 16000); self.assertEqual(b["model"], "m")
        self.assertEqual(b["thinking"], {"type": "disabled"})
        row = self.proj.q("SELECT * FROM calls")[0]
        self.assertEqual(row["reasoning"], "想一想再想")
        self.assertEqual((row["input_tokens"], row["output_tokens"]), (11, 22))
        self.assertIn("disabled", row["params"])

    def test_zero_cap_omits_max_tokens(self):
        BEHAVE["f"] = lambda b: ("sse", [chunk(content='{"a":1}', finish="stop"), "data: [DONE]\n\n"])
        self.hub().call_json("P1", "s", "u", call_key="t", round_=0, max_tokens=0)
        self.assertNotIn("max_tokens", SEEN[0])

    def test_truncation_keeps_partial(self):
        BEHAVE["f"] = lambda b: ("sse", [chunk(reasoning="想"), chunk(content='{"a":', finish="length"),
                                         "data: [DONE]\n\n"])
        with self.assertRaises(CallFailedPause):
            self.hub().call_json("P1", "s", "u", call_key="t", round_=0, max_tokens=100)
        row = self.proj.q("SELECT * FROM calls")[0]
        self.assertEqual(row["status"], "失败"); self.assertEqual(row["response"], '{"a":')
        self.assertEqual(row["reasoning"], "想"); self.assertEqual(len(self.proj.q("SELECT * FROM calls")), 1)

    def test_idle_timeout_retries(self):
        n = {"i": 0}
        def f(b):
            n["i"] += 1
            if n["i"] == 1:
                return ("sse", [chunk(reasoning="x"), 3])  # 之后 3 秒没动静
            return ("sse", [chunk(content='{"a":1}', finish="stop"), "data: [DONE]\n\n"])
        BEHAVE["f"] = f
        self.assertEqual(self.hub(timeout=1).call_json("P1", "s", "u", call_key="t", round_=0), {"a": 1})
        self.assertEqual(len(self.proj.q("SELECT * FROM calls")), 2)

    def test_no_finish_is_transport_error(self):
        n = {"i": 0}
        def f(b):
            n["i"] += 1
            return ("sse", [chunk(content='{"a":')]) if n["i"] == 1 else \
                   ("sse", [chunk(content='{"a":1}', finish="stop"), "data: [DONE]\n\n"])
        BEHAVE["f"] = f
        self.assertEqual(self.hub().call_json("P1", "s", "u", call_key="t", round_=0), {"a": 1})

    def test_stream_options_rejected_fallback(self):
        def f(b):
            if "stream_options" in b:
                return ("400", '{"error":"unknown field stream_options"}')
            return ("sse", [chunk(content='{"a":1}', finish="stop"), "data: [DONE]\n\n"])
        BEHAVE["f"] = f
        h = self.hub()
        self.assertEqual(h.call_json("P1", "s", "u", call_key="t", round_=0), {"a": 1})
        self.assertEqual(len(self.proj.q("SELECT * FROM calls")), 1)

    def test_cap_ladder(self):
        def f(b):
            if b.get("max_tokens", 0) > 16384:
                return ("400", '{"error":"max_tokens must be <= 16384"}')
            return ("sse", [chunk(content='{"a":1}', finish="stop"), "data: [DONE]\n\n"])
        BEHAVE["f"] = f
        h = self.hub()
        self.assertEqual(h.call_json("P1", "s", "u", call_key="t", round_=0, max_tokens=65536), {"a": 1})
        self.assertEqual([x["max_tokens"] for x in SEEN], [65536, 32768, 16384])
        SEEN.clear()
        h.call_json("P1", "s", "u", call_key="t2", round_=0, max_tokens=65536)
        self.assertEqual(SEEN[0]["max_tokens"], 16384)  # 已记住
        self.assertTrue(self.proj.q("SELECT * FROM events WHERE type='输出上限自动降级'"))

    def test_cap_field_configurable(self):
        def f(b):
            if "max_tokens" in b:
                return ("400", '{"error":"unknown field max_tokens, use max_completion_tokens"}')
            return ("sse", [chunk(content='{"a":1}', finish="stop"), "data: [DONE]\n\n"])
        BEHAVE["f"] = f
        h = self.hub(max_tokens_field="max_completion_tokens", max_tokens=131072)
        self.assertEqual(h.call_json("P1", "s", "u", call_key="t", round_=0, max_tokens=65536), {"a": 1})
        self.assertEqual(len(SEEN), 1)
        self.assertEqual(SEEN[0]["max_completion_tokens"], 131072)
        self.assertNotIn("max_tokens", SEEN[0])

    def test_wrong_field_not_laddered(self):
        # 接口点名拒绝的不是我们发的字段：不该瞎降级，直接按不可重试暂停
        BEHAVE["f"] = lambda b: ("400", '{"error":"bad max_completion_tokens"}')
        with self.assertRaises(CallFailedPause):
            self.hub().call_json("P1", "s", "u", call_key="t", round_=0, max_tokens=65536)
        self.assertEqual(len(SEEN), 1)

    def test_json_fallback(self):
        BEHAVE["f"] = lambda b: ("json", {"choices": [{"message": {"content": '{"a":1}',
                                  "reasoning_content": "r"}, "finish_reason": "stop"}], "usage": {}})
        self.assertEqual(self.hub().call_json("P1", "s", "u", call_key="t", round_=0), {"a": 1})
        self.assertEqual(self.proj.q("SELECT reasoning FROM calls")[0]["reasoning"], "r")

    def test_live_progress(self):
        BEHAVE["f"] = lambda b: ("sse", [chunk(reasoning="x" * 50), 1.3, chunk(reasoning="y" * 5),
                                         chunk(content='{"a":1}', finish="stop"), "data: [DONE]\n\n"])
        seen = []
        orig = self.proj.set_live_progress
        self.proj.set_live_progress = lambda r, t, o, *a, **k: (seen.append((t, o)), orig(r, t, o, *a, **k))
        self.hub().call_json("P1", "s", "u", call_key="t", round_=0)
        self.assertTrue(any(t >= 50 for t, o in seen))


if __name__ == "__main__":
    unittest.main()
