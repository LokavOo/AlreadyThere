"""联网检索与原文存档（8.1、9.4）。

每个检索结果登记为一条检索记录，网页原文按哈希存档。模型只能引用存档原文中的句子；
检索接口返回的全文是接口自己从网页提取的正文，记为"接口提取正文"；只返回片段的记为"片段"，
片段不能作为引文来源。
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

from .util import now_iso


class TavilySearch:
    URL = "https://api.tavily.com/search"

    def __init__(self, api_key, timeout=60):
        self.api_key, self.timeout = api_key, timeout

    def search(self, query, max_results, include_domains=None):
        payload = {"query": query, "max_results": max_results, "include_raw_content": True,
                   "search_depth": "basic"}
        if include_domains:
            payload["include_domains"] = list(include_domains)
        req = urllib.request.Request(
            self.URL, data=json.dumps(payload).encode("utf-8"), method="POST",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
        out = []
        for it in data.get("results", []):
            raw = it.get("raw_content")
            out.append({"url": it.get("url"), "title": it.get("title"),
                        "text": raw if raw else (it.get("content") or ""),
                        "source": "接口提取正文" if raw else "片段"})
        return out


class MockSearch:
    """模拟检索：pages = [{url,title,text,keywords:[...]}]，按关键词命中。"""

    def __init__(self, pages):
        self.pages = pages

    def search(self, query, max_results, include_domains=None):
        from urllib.parse import urlparse
        hits = [p for p in self.pages if any(k in query for k in p["keywords"])]
        if include_domains:
            hits = [p for p in hits if any(urlparse(p["url"]).netloc.endswith(d) for d in include_domains)]
        else:
            hits = [p for p in hits if not p.get("hidden")]
        return [{"url": p["url"], "title": p["title"], "text": p["text"], "source": "接口提取正文"}
                for p in hits[:max_results]]


class Retriever:
    def __init__(self, project, engine, sleep=None, max_concurrency=3):
        import time
        self.project, self.engine = project, engine
        self.sleep = sleep or time.sleep
        # 2.0 并行：不管配了几个角色，检索用的是同一个账户（比如同一把 Tavily 密钥），
        # 所以整个 Retriever 只用一把信号量，所有角色的检索请求都在这把锁下排队。
        self._sem = threading.Semaphore(max(1, int(max_concurrency)))

    def search(self, requester, round_, query, max_results, item_id=None, include_domains=None):
        """执行一次检索；返回登记后的结果列表 [{retrieval_id,url,title,source}]。

        鉴权失败、额度用完直接暂停；其他失败短间隔重试 3 次，仍失败也暂停——
        不能把"检索没成功"当成"查不到资料"。
        """
        from .models import CallFailedPause
        last = None
        for attempt in range(4):
            try:
                with self._sem:  # 2.0：真正发出去、等回复的这一下才占名额
                    results = (self.engine.search(query, max_results, include_domains=include_domains)
                               if include_domains else self.engine.search(query, max_results))
                break
            except urllib.error.HTTPError as e:
                if e.code in (401, 403, 432, 433):
                    self.project.log("程序", "调用失败暂停", round_=round_, content={"检索": query, "HTTP": e.code})
                    raise CallFailedPause("检索", query, f"检索接口鉴权失败或额度用完（HTTP {e.code}），请检查检索密钥与额度")
                last = f"HTTP {e.code}"
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
                last = str(e)
            self.project.log("程序", "检索失败", round_=round_, content={"query": query, "error": last,
                                                                     "attempt": attempt + 1})
            self.sleep(5 * (attempt + 1))
        else:
            self.project.log("程序", "调用失败暂停", round_=round_, content={"检索": query, "原因": last})
            raise CallFailedPause("检索", query, f"检索连续失败：{last}")
        out = []
        # 2.0 并行：这一次检索的所有结果一起锁住登记，不会被别的角色的记录插进来打断。
        with self.project.batch():
            for res in results:
                rid = self.project.next_id("retrievals", "S", "retrieval_id")
                path, h = self.project.archive_text(res["text"])
                self.project.exec(
                    "INSERT INTO retrievals (retrieval_id,requester,item_id,round,ts,query,url,title,archive_path,"
                    "content_hash,source) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (rid, requester, item_id, round_, now_iso(), query, res["url"], res["title"], path, h, res["source"]))
                c = {"query": query, "url": res["url"], "content_hash": h, "source": res["source"],
                     "requester": requester}
                if include_domains:
                    c["限定网站"] = list(include_domains)
                self.project.log("程序", "检索", round_=round_, ref_type="retrieval", ref_id=rid, content=c)
                out.append({"retrieval_id": rid, "url": res["url"], "title": res["title"],
                            "source": res["source"]})
        return out
