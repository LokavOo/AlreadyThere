"""本机只读查看页面：群聊式事件流、条目状态、块与审查、检索原文、模型调用原文。

只监听 127.0.0.1，别的电脑访问不到。页面只读，不能修改任何记录。
"""

from __future__ import annotations

import json
import sqlite3
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

PAGE = r"""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>观者自在 · 过程查看</title>
<style>
:root{--bg:#f6f6f4;--card:#fff;--ink:#1d1d1b;--mute:#6b6b66;--line:#e2e1dc;--acc:#2f5d8a;--ok:#2e7d4f;--warn:#b26a00;--bad:#b3261e}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--card:#20201e;--ink:#ecebe6;--mute:#9d9c95;--line:#34332f;--acc:#7fb0e0;--ok:#6cc08b;--warn:#e0a44a;--bad:#ef7b72}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.55 system-ui,"PingFang SC","Microsoft YaHei",sans-serif}
header{padding:14px 18px;border-bottom:1px solid var(--line);display:flex;gap:16px;align-items:center;flex-wrap:wrap}
header h1{font-size:16px;margin:0}.pill{padding:2px 8px;border-radius:99px;border:1px solid var(--line);font-size:12px}
main{display:grid;grid-template-columns:minmax(0,1.3fr) minmax(0,1fr);gap:14px;padding:14px}
@media (max-width:900px){main{grid-template-columns:1fr}}
section{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px;min-width:0}
h2{font-size:14px;margin:0 0 8px}.msg{border-left:3px solid var(--line);padding:4px 8px;margin:6px 0;cursor:pointer}
.msg:hover{background:rgba(127,127,127,.07)}.who{font-weight:600}.t{color:var(--mute);font-size:12px}
.P{border-color:var(--acc)}.R{border-color:var(--warn)}.程序{border-color:var(--line)}.用户{border-color:var(--ok)}
.hl{background:rgba(179,38,30,.08)}table{width:100%;border-collapse:collapse;font-size:13px}
td,th{border-bottom:1px solid var(--line);padding:4px 6px;text-align:left;vertical-align:top}
.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}
pre{white-space:pre-wrap;word-break:break-all;background:rgba(127,127,127,.08);padding:8px;border-radius:6px;max-height:420px;overflow:auto}
.round{margin-top:12px;font-weight:600;color:var(--mute)}#stream{max-height:75vh;overflow:auto}
</style></head><body>
<header><h1>观者自在 · 过程查看</h1><span id="chain" class="pill">校验中…</span><span id="q" class="t"></span></header>
<main><section><h2>过程（点击任一条查看详情）</h2><div id="stream"></div></section>
<div><section><h2>条目状态</h2><div id="items"></div></section>
<section style="margin-top:14px"><h2>详情</h2><div id="detail" class="t">点击左侧消息或条目查看。</div></section></div></main>
<script>
const J=u=>fetch(u).then(r=>r.json());const esc=s=>String(s??'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
const KEY=new Set(['拒收','调用失败暂停','意见提出','审查意见','配置一致性提示','迭代结束','重做未完成轮次']);
async function load(){
 const v=await J('/api/verify');const c=document.getElementById('chain');
 c.textContent=v.ok?`账本完整（${v.n} 条事件）`:`账本不一致：${v.bad}`;c.className='pill '+(v.ok?'ok':'bad');
 const info=await J('/api/info');document.getElementById('q').textContent=info.question;
 const ev=await J('/api/events');let h='',cur=null;
 for(const e of ev){if(e.round!==cur){cur=e.round;h+=`<div class="round">${cur==null?'项目':'第 '+cur+' 轮'}</div>`}
  const cls=(e.actor[0]==='P'?'P':e.actor[0]==='R'?'R':e.actor)+(KEY.has(e.type)?' hl':'');
  let brief='';try{const x=JSON.parse(e.content||'null');if(x){brief=esc(JSON.stringify(x)).slice(0,140)}}catch(_){}
  h+=`<div class="msg ${cls}" onclick="ev('${e.event_id}')"><span class="who">${esc(e.actor)}</span> · ${esc(e.type)} <span class="t">${esc(e.event_id)} ${esc(e.ref_id||'')}</span><div class="t">${brief}</div></div>`}
 document.getElementById('stream').innerHTML=h;
 const it=await J('/api/items');let t='<table><tr><th>条目</th><th>人物</th><th>验证</th><th>独立性</th><th>值</th></tr>';
 for(const i of it){const cl=i.st_verify==='已验证'?'ok':(i.st_verify==='待核验'?'warn':'bad');
  t+=`<tr onclick="item('${i.item_id}')" style="cursor:pointer"><td>${i.item_id}</td><td>${esc(i.name)}</td><td class="${cl}">${esc(i.st_verify)}</td><td>${esc(i.st_indep||'')}</td><td>${esc(i.value||'')}</td></tr>`}
 document.getElementById('items').innerHTML=t+'</table>'}
async function ev(id){const e=await J('/api/event/'+id);let h=`<pre>${esc(JSON.stringify(e.event,null,1))}</pre>`;
 if(e.call)h+=`<h2>模型实际看到的输入</h2><pre>${esc(e.call.request)}</pre><h2>原始返回</h2><pre>${esc(e.call.response)}</pre>`;
 if(e.archive)h+=`<h2>检索存档原文</h2><pre>${esc(e.archive)}</pre>`;
 if(e.block)h+=`<h2>块</h2><pre>${esc(JSON.stringify(e.block,null,1))}</pre>`;
 document.getElementById('detail').innerHTML=h}
async function item(id){const x=await J('/api/item/'+id);let h=`<pre>${esc(JSON.stringify(x.item,null,1))}</pre><h2>各方块</h2>`;
 for(const b of x.blocks)h+=`<pre>${esc(JSON.stringify(b,null,1))}</pre>`;
 h+='<h2>审查与意见</h2>';for(const r of x.reviews)h+=`<pre>${esc(JSON.stringify(r,null,1))}</pre>`;
 for(const o of x.opinions)h+=`<pre>${esc(JSON.stringify(o,null,1))}</pre>`;document.getElementById('detail').innerHTML=h}
load();
</script></body></html>"""


def serve(project, port=8765, open_browser=True):
    db = str(project.db_path)
    root = project.root

    def rows(sql, args=()):
        c = sqlite3.connect(db)
        c.row_factory = sqlite3.Row
        out = [dict(r) for r in c.execute(sql, args).fetchall()]
        c.close()
        return out

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, obj, ctype="application/json"):
            body = obj.encode("utf-8") if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = unquote(urlparse(self.path).path)
            if path == "/":
                return self._send(PAGE, "text/html")
            if path == "/api/info":
                return self._send({"question": project.config.get("question")})
            if path == "/api/verify":
                pj = project.__class__(root).open()
                ok, bad, n = pj.verify_chain()
                pj.close()
                return self._send({"ok": ok, "bad": bad, "n": n})
            if path == "/api/events":
                return self._send(rows("SELECT event_id,round,actor,type,ref_id,content FROM events ORDER BY seq"))
            if path == "/api/items":
                return self._send(rows("SELECT * FROM items ORDER BY item_id"))
            if path.startswith("/api/event/"):
                e = rows("SELECT * FROM events WHERE event_id=?", (path.split("/")[-1],))
                if not e:
                    return self._send({})
                e = e[0]
                out = {"event": e}
                if e["ref_type"] == "call":
                    c = rows("SELECT * FROM calls WHERE call_id=?", (e["ref_id"],))
                    out["call"] = c[0] if c else None
                if e["ref_type"] == "retrieval":
                    r = rows("SELECT archive_path FROM retrievals WHERE retrieval_id=?", (e["ref_id"],))
                    if r:
                        out["archive"] = (root / str(r[0]["archive_path"]).replace("\\", "/")).read_text(encoding="utf-8-sig")
                if e["ref_type"] == "block":
                    b = rows("SELECT * FROM blocks WHERE block_id=?", (e["ref_id"],))
                    out["block"] = b[0] if b else None
                return self._send(out)
            if path.startswith("/api/item/"):
                iid = path.split("/")[-1]
                return self._send({
                    "item": (rows("SELECT * FROM items WHERE item_id=?", (iid,)) or [{}])[0],
                    "blocks": rows("SELECT * FROM blocks WHERE item_id=? ORDER BY round,author", (iid,)),
                    "reviews": rows("SELECT * FROM reviews WHERE item_id=? ORDER BY round", (iid,)),
                    "opinions": rows("SELECT * FROM opinions WHERE item_id=?", (iid,))})
            self.send_response(404)
            self.end_headers()

    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    url = f"http://127.0.0.1:{port}/"
    print(f"查看页面：{url}  （按 Ctrl+C 退出）")
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
