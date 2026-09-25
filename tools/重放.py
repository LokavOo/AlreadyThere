"""离线整体重放（1.7，开发用，不花钱）：用一个实跑项目存档里的模型答复和检索原文，把整个流程从头再跑一遍。

用法（在程序文件夹里）：
    python tools/重放.py <原项目文件夹> <输出文件夹>

做法：
- 新建一个同题目的项目，把原项目 calls 表里的调用记录原样拷进去。程序本来就会"已成功的调用直接读存档"，
  所以模型调用全部走存档，不联网；存档里没有的调用（改了代码后流程走向不同）会当作调用失败、暂停。
- 检索按检索词依次返回原项目当时存档的原文，不联网。
- 跑完生成结论，打印与原项目结论逐条对比的结果。

用途：改了程序以后，确认同一份存档在新代码下每一条的结论是否与旧代码一致。
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from guanzhe import report  # noqa: E402
from guanzhe.__main__ import load_settings_from_obj  # noqa: E402
from guanzhe.flow import Flow  # noqa: E402
from guanzhe.models import FatalCallError, ModelHub  # noqa: E402
from guanzhe.retrieval import Retriever  # noqa: E402
from guanzhe.store import Project  # noqa: E402


class NoCall:
    """存档里没有的调用：不联网，直接按调用失败处理。"""

    def complete(self, *a, **k):
        raise FatalCallError("重放：存档里没有这次调用的答复")


class ReplaySearch:
    """按检索词依次返回原项目存档的检索结果。"""

    def __init__(self, orig: Project):
        self.batches = defaultdict(list)
        rows = orig.q("SELECT * FROM retrievals ORDER BY retrieval_id")
        cur_key, cur = None, []
        for r in rows:
            key = (r["requester"], r["round"], r["query"], r["item_id"])
            if key != cur_key and cur:
                self.batches[cur_key[2]].append(cur)
                cur = []
            cur_key = key
            cur.append({"url": r["url"], "title": r["title"], "source": r["source"],
                        "text": orig.read_archive(r["retrieval_id"]) or ""})
        if cur:
            self.batches[cur_key[2]].append(cur)
        self.misses = []

    def search(self, query, max_results, include_domains=None):
        if self.batches.get(query):
            return self.batches[query].pop(0)[:max_results]
        self.misses.append(query)
        return []


def replay(orig_dir, out_dir):
    orig_dir, out_dir = Path(orig_dir), Path(out_dir)
    if out_dir.exists():
        shutil.rmtree(out_dir)
    orig = Project(orig_dir).open()
    cfg = json.loads((orig_dir / "config.json").read_text(encoding="utf-8-sig"))
    st = load_settings_from_obj(json.loads((orig_dir / "settings.snapshot.json").read_text(encoding="utf-8-sig")))
    p = Project.create(out_dir, cfg)
    (out_dir / "settings.snapshot.json").write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
    cols = [r[1] for r in orig.conn.execute("PRAGMA table_info(calls)")]
    for row in orig.conn.execute("SELECT * FROM calls"):
        p.conn.execute(f"INSERT INTO calls ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", tuple(row))
    p.conn.commit()
    keys = {s["provider"] + "|" + s.get("base_url", "") for s in st["roles"].values()}
    hub = ModelHub(p, st, {}, providers={k: NoCall() for k in keys}, sleep=lambda s: None)
    prompts = {}  # 记下每次调用发给模型的提示词，用来和旧代码的重放、以及原项目逐字比对
    orig_call = hub.call_json

    def recording(role, system, user, *, call_key, **kw):
        prompts[call_key] = {"system": system, "user": user}
        return orig_call(role, system, user, call_key=call_key, **kw)
    hub.call_json = recording
    eng = ReplaySearch(orig)
    res = Flow(p, st, hub, Retriever(p, eng, sleep=lambda s: None)).run()
    report.build(p)
    (out_dir / "重放_提示词.json").write_text(json.dumps(prompts, ensure_ascii=False, indent=0), encoding="utf-8")
    return res, eng.misses


def compare_prompts(a_dir, b_dir=None, orig=None):
    """两次重放记下的提示词逐字比对；给 orig 时与原项目 calls 表里存的请求比对。返回不同的调用编号。"""
    b = json.loads((Path(b_dir) / "重放_提示词.json").read_text(encoding="utf-8"))
    if orig:
        a = {}
        for cid, req in sqlite3.connect(Path(orig) / "project.db").execute(
                "SELECT call_id, request FROM calls WHERE status='成功'"):
            q = json.loads(req)
            a[cid.split("#")[0]] = {"system": q.get("system"), "user": q.get("user")}
        return sorted(k for k in b if k in a and a[k] != b[k])
    a = json.loads((Path(a_dir) / "重放_提示词.json").read_text(encoding="utf-8"))
    return sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))


def items_of(folder):
    j = json.loads((Path(folder) / "outputs" / "结论.json").read_text(encoding="utf-8-sig"))
    return {r["条目"]: r for r in j["逐条结果"]}


def compare(a_dir, b_dir, keys=("结论", "出生日期", "农历年干支", "独立验证次数", "生肖", "备注")):
    a, b = items_of(a_dir), items_of(b_dir)
    diff = []
    for iid in sorted(set(a) | set(b)):
        x, y = a.get(iid, {}), b.get(iid, {})
        d = {k: (x.get(k), y.get(k)) for k in keys if x.get(k) != y.get(k)}
        if d:
            diff.append((iid, d))
    return diff


def compare_blocks(a_dir, b_dir):
    """逐个条目块比对：提交检查的结果（字段、标记、查证状态）和审查结果（等价、独立核验、独立来源）。"""
    out = []
    for sql in ("SELECT block_id, fields, st_check, flags FROM blocks ORDER BY block_id",
                "SELECT block_id, reviewer, verdict, eq_check, indep_check, indep_source FROM reviews ORDER BY block_id, reviewer",
                "SELECT item_id, st_check, st_verify, indep_count, value, note, ganzhi FROM items ORDER BY item_id",
                "SELECT opinion_id, target_author, item_id, kind, status FROM opinions ORDER BY opinion_id"):
        a = [tuple(r) for r in sqlite3.connect(Path(a_dir) / "project.db").execute(sql)]
        b = [tuple(r) for r in sqlite3.connect(Path(b_dir) / "project.db").execute(sql)]
        if a != b:
            sa, sb = set(a), set(b)
            out.append((sql.split()[-4] if "ORDER" in sql else sql, len(sa - sb), len(sb - sa),
                        sorted(sa - sb)[:3], sorted(sb - sa)[:3]))
    return out


if __name__ == "__main__":
    src, out = sys.argv[1], sys.argv[2]
    res, misses = replay(src, out)
    print(f"重放结果：{res}；检索未命中 {len(misses)} 次")
    diff = compare(src, out)
    print("与原项目结论逐条一致" if not diff else f"与原项目结论不同的条目 {len(diff)} 条：")
    for iid, d in diff:
        print(" ", iid, json.dumps(d, ensure_ascii=False))
    if len(sys.argv) > 3:  # 再给一个文件夹：与它逐块比对（用于新旧代码各重放一次后对比）
        bd = compare_blocks(sys.argv[3], out)
        print("与对照重放逐块一致" if not bd else f"与对照重放逐块不同：{bd}")
        pd = compare_prompts(sys.argv[3], out)
        print("与对照重放的提示词逐字一致" if not pd else f"与对照重放提示词不同的调用：{pd}")
