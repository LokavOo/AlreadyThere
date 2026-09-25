"""模型调用：统一接口、两批重试（0.3）、全部留痕（9.2）。

支持三类接入：
- anthropic：Anthropic 官方接口；
- openai_compat：兼容 OpenAI 格式的接口（DeepSeek、通义千问、豆包等国内厂商多数提供）；
- mock：模拟模型，由测试脚本给出固定回复，不联网、不花钱。

恢复机制：每次调用带一个"调用键"（轮次+角色+步骤）。暂停后重新运行时，已成功的调用直接读取
存档的原始返回，不重复调用；失败的调用以存档的原请求重新发送（0.3 第 7 条）。
"""

from __future__ import annotations

import http.client
import json
import ssl
import time
import urllib.error
import urllib.request

from .util import FormatError, canonical_json, extract_json, now_iso, sha256_text


class FatalCallError(Exception):
    """鉴权失败、余额不足、请求无效：不重试，直接暂停（0.3 第 2 条）。"""


class TransportError(Exception):
    def __init__(self, msg, retry_after=None):
        super().__init__(msg)
        self.retry_after = retry_after


class CallFailedPause(Exception):
    """两批重试全部失败，流程进入"调用失败暂停"（0.3 第 5 条）。"""

    def __init__(self, role, call_key, reason):
        super().__init__(f"{role} 调用失败：{reason}")
        self.role, self.call_key, self.reason = role, call_key, reason


# ---------------- 各接入方式 ----------------

def _http_post(url, headers, payload, timeout):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:500]
        if e.code in (401, 403):
            raise FatalCallError(f"鉴权失败（HTTP {e.code}）：请检查密钥。{body}")
        if e.code == 402:
            raise FatalCallError(f"余额或额度不足（HTTP 402）。{body}")
        if e.code in (400, 404, 422):
            raise FatalCallError(f"请求被接口判为无效（HTTP {e.code}）：请检查模型名与接口地址。{body}")
        ra = e.headers.get("retry-after") if e.headers else None
        raise TransportError(f"HTTP {e.code}：{body}", retry_after=float(ra) if ra and ra.isdigit() else None)
    except urllib.error.URLError as e:
        if isinstance(e.reason, ssl.SSLCertVerificationError):
            raise FatalCallError("SSL 证书校验失败。Mac 上请在「应用程序 / Python 3.x」文件夹里双击运行 "
                                 "Install Certificates.command 后重试；公司网络可能需要配置代理。")
        raise TransportError(f"连接失败：{e.reason}")
    except (TimeoutError, OSError, http.client.HTTPException) as e:
        raise TransportError(f"连接中断或超时：{e}")
    try:
        return json.loads(raw)
    except ValueError:
        raise TransportError(f"接口返回的不是 JSON（可能是代理或网关页面）：{raw[:200]}")


class AnthropicProvider:
    def __init__(self, api_key, base_url="https://api.anthropic.com"):
        self.api_key, self.base_url = api_key, base_url.rstrip("/")

    def complete(self, model, system, user, max_tokens, timeout):
        payload = {"model": model, "max_tokens": max_tokens, "system": system,
                   "messages": [{"role": "user", "content": user}]}
        headers = {"x-api-key": self.api_key, "anthropic-version": "2023-06-01",
                   "content-type": "application/json"}
        r = _http_post(f"{self.base_url}/v1/messages", headers, payload, timeout)
        if not isinstance(r, dict) or "content" not in r:
            raise TransportError(f"接口返回缺少 content：{str(r)[:300]}")
        if r.get("stop_reason") == "max_tokens":
            raise FatalCallError("输出被截断（达到 max_tokens）：请减小 items_per_call 或调大输出上限")
        text = "".join(b.get("text", "") for b in r.get("content", []) if b.get("type") == "text")
        u = r.get("usage", {}) or {}
        return text, u.get("input_tokens"), u.get("output_tokens")


class OpenAICompatProvider:
    def __init__(self, api_key, base_url):
        self.api_key, self.base_url = api_key, base_url.rstrip("/")

    def complete(self, model, system, user, max_tokens, timeout, extra=None):
        payload = {"model": model, "max_tokens": max_tokens,
                   "messages": [{"role": "system", "content": system},
                                {"role": "user", "content": user}]}
        for k, v in (extra or {}).items():  # 设置里 extra 一栏的附加参数，例如关闭深度思考
            if k not in ("model", "messages"):
                payload[k] = v
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        r = _http_post(f"{self.base_url}/chat/completions", headers, payload, timeout)
        try:
            choice = r["choices"][0]
            text = choice["message"].get("content") or ""
        except (KeyError, IndexError, TypeError):
            raise TransportError(f"接口返回缺少 choices：{str(r)[:300]}")
        if choice.get("finish_reason") == "length":
            raise FatalCallError("输出被截断（达到 max_tokens）：请减小 items_per_call 或调大输出上限")
        u = r.get("usage", {}) or {}
        return text, u.get("prompt_tokens"), u.get("completion_tokens")


class MockProvider:
    """模拟模型：responder(role, system, user) -> 文本；也可以抛出异常模拟失败。"""

    def __init__(self, responder):
        self.responder = responder

    def complete(self, model, system, user, max_tokens, timeout):
        text = self.responder(model, system, user)
        return text, len(system + user) // 2, len(text) // 2


# ---------------- 统一调用 ----------------

class ModelHub:
    def __init__(self, project, settings, secrets, providers=None, sleep=time.sleep):
        self.project = project
        self.settings = settings
        self.params = settings["params"]
        self.sleep = sleep
        self.providers = providers or {}
        self.secrets = secrets

    def _provider(self, spec):
        key = spec["provider"] + "|" + spec.get("base_url", "")
        if key in self.providers:
            return self.providers[key]
        kind = spec["provider"]
        if kind == "anthropic":
            p = AnthropicProvider(self.secrets.get(spec.get("key_name", "ANTHROPIC_API_KEY"), ""),
                                  spec.get("base_url", "https://api.anthropic.com"))
        elif kind == "openai_compat":
            p = OpenAICompatProvider(self.secrets.get(spec["key_name"], ""), spec["base_url"])
        else:
            raise ValueError(f"未知的接入方式：{kind}（mock 须由测试脚本注入）")
        self.providers[key] = p
        return p

    def call_json(self, role, system, user, *, call_key, round_, session_id=None,
                  validator=None, max_tokens=4000):
        """调用模型并解析为 JSON。validator(obj) 不通过时抛 FormatError，按格式类失败重试。"""
        spec = self.settings["roles"][role]
        base_request = {"system": system, "user": user}
        # 已成功的调用：直接读存档（恢复时不重复调用）
        done = self.project.q("SELECT response FROM calls WHERE call_id LIKE ? AND status='成功' "
                              "ORDER BY ts DESC LIMIT 1", (f"{call_key}#%",))
        if done:
            try:
                obj = extract_json(done[0]["response"])
                if validator:
                    validator(obj)
                return obj
            except (FormatError, AttributeError, TypeError) as e:  # 存档答复与当前状态不符：记录后重新调用
                self.project.log("程序", "存档答复不适用", round_=round_, ref_type="call_key", ref_id=call_key,
                                 content={"原因": str(e)})

        schedule = [(1, 0)] + [(1, s) for s in self.params["retry_batch1"]] + \
                   [(2, s) for s in self.params["retry_batch2"]]
        note = None
        last_reason = None
        attempt = self.project.q("SELECT COUNT(*) c FROM calls WHERE call_id LIKE ?", (f"{call_key}#%",))[0]["c"]
        first = attempt
        for batch, wait in schedule:
            if attempt > first and wait:
                self.sleep(wait)
            attempt += 1
            req = dict(base_request)
            if note:  # 格式类失败：新请求 = 原请求 + 错误说明（0.3 第 4 条）
                req["user"] = user + "\n\n【格式错误说明】上一次返回不合规定格式：" + note + \
                              "\n请严格按要求只输出一个 JSON 对象。"
            call_id = f"{call_key}#{attempt}"
            status, text, tin, tout, failure = "失败", None, None, None, None
            try:
                prov = self._provider(spec)
                mt = max(max_tokens, int(spec.get("max_tokens", 0) or 0))  # 角色可单独调高输出上限
                if spec.get("extra") and isinstance(prov, OpenAICompatProvider):
                    text, tin, tout = prov.complete(spec["model"], req["system"], req["user"], mt,
                                                    self.params["call_timeout_seconds"], extra=spec["extra"])
                else:
                    text, tin, tout = prov.complete(spec["model"], req["system"], req["user"], mt,
                                                    self.params["call_timeout_seconds"])
                obj = extract_json(text)
                if validator:
                    validator(obj)
                status = "成功"
            except (FormatError, AttributeError, TypeError) as e:
                failure = f"格式：{e}"
                note = str(e) if isinstance(e, FormatError) else f"返回的 JSON 结构不合规：{e}"
            except TransportError as e:
                failure = f"传输：{e}"
                if e.retry_after:
                    self.sleep(max(0, e.retry_after - wait))
            except FatalCallError as e:
                failure = f"不可重试：{e}"
            ratio = None
            if tin and spec.get("context_window"):
                ratio = tin / spec["context_window"]
            self.project.exec(
                "INSERT INTO calls (call_id,event_id,role,session_id,provider,model,series,params,request,"
                "request_hash,response,input_tokens,output_tokens,context_ratio,status,retry_batch,retry_seq,"
                "failure,ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (call_id, None, role, session_id or role, spec["provider"], spec["model"],
                 spec.get("series", ""), canonical_json({"max_tokens": max(max_tokens, int(spec.get("max_tokens", 0) or 0)),
                                 "extra": spec.get("extra") or {}}),
                 canonical_json(req), sha256_text(canonical_json(req)), text, tin, tout, ratio,
                 status, batch, attempt, failure, now_iso()))
            self.project.log(role, "模型调用", round_=round_, ref_type="call", ref_id=call_id,
                             content={"status": status, "failure": failure,
                                      "request_hash": sha256_text(canonical_json(req)),
                                      "response_hash": sha256_text(text) if text is not None else None})
            if status == "成功":
                return obj
            last_reason = failure
            if failure and failure.startswith("不可重试"):
                break
        self.project.log("程序", "调用失败暂停", round_=round_, ref_type="call_key", ref_id=call_key,
                         content={"role": role, "reason": last_reason})
        raise CallFailedPause(role, call_key, last_reason)
