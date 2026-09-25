"""命令行入口：python -m guanzhe <命令>

  demo    <项目文件夹>                               用模拟模型跑一遍演示项目（不联网、不花钱）
  new     <项目文件夹> --project 问题.json            新建项目（问题、口径、条目清单）
  run     <项目文件夹> --settings 设置.json --secrets 密钥.env   运行或从暂停处继续
  report  <项目文件夹>                               重新生成结论
  verify  <项目文件夹>                               校验账本哈希链
  serve   <项目文件夹> [--port 8765]                 在浏览器中查看过程
  ping    --settings 设置.json --secrets 密钥.env    逐个测试模型与检索接口能否连通（会产生极少费用）
  app                                                打开窗口版（1.3）
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from . import demo, report
from .config import load_secrets, load_settings
from .flow import Flow
from .models import ModelHub
from .retrieval import MockSearch, Retriever, TavilySearch
from .store import Project


def _open(folder):
    p = Project(folder)
    if not p.db_path.exists():
        sys.exit(f"找不到项目：{folder}")
    return p.open()


def _engine(settings, secrets):
    s = settings.get("search", {})
    if s.get("engine") == "tavily":
        key = secrets.get(s.get("key_name", "TAVILY_API_KEY"))
        if not key:
            sys.exit("密钥文件中没有 TAVILY_API_KEY")
        return TavilySearch(key)
    if s.get("engine") == "mock":
        return MockSearch(demo.PAGES)
    sys.exit("设置中 search.engine 只能是 tavily 或 mock")


def cmd_demo(a):
    folder = Path(a.folder)
    if (folder / "project.db").exists():
        sys.exit(f"{folder} 已存在项目；请换一个文件夹名")
    p = Project.create(folder, demo.project_config())
    st = load_settings_from_obj(demo.settings())
    (folder / "settings.snapshot.json").write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
    hub = ModelHub(p, st, {}, providers=demo.providers(), sleep=lambda s: None)
    res = Flow(p, st, hub, Retriever(p, MockSearch(demo.PAGES))).run()
    c = report.build(p)
    print(f"演示运行：{res}")
    print((folder / "outputs" / "结论.md").read_text(encoding="utf-8-sig"))
    print(f"\n用浏览器查看过程：python -m guanzhe serve {folder}")


def load_settings_from_obj(obj):
    from .config import DEFAULT_PARAMS
    params = dict(DEFAULT_PARAMS)
    params.update(obj.get("params", {}))
    obj["params"] = params
    return obj


def _read_json(path, what):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        sys.exit(f"找不到{what}文件：{path}")
    except UnicodeDecodeError:
        sys.exit(f"{what}文件不是 UTF-8 编码：请用记事本「另存为」时选择 UTF-8。")
    except json.JSONDecodeError as e:
        sys.exit(f"{what}文件格式有误（第 {e.lineno} 行附近）：{e.msg}。常见原因：少了逗号或引号、用了中文标点。")


NO_TRUSTED = ("警告：这个项目的题目文件里没有可信网站名单（trusted_sites）。审查者的独立核验不会启动，"
              "任何条目都不可能判为\"查到\"。如果不是有意这样，请停下，换用带名单的题目文件（如 明清皇帝生肖.json）新建项目。")

SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


def cmd_new(a):
    cfg = _read_json(a.project, "问题")
    for k in ("project_id", "question", "scope", "items"):
        if k not in cfg:
            sys.exit(f"问题文件缺少字段：{k}")
    if not SAFE_ID.match(str(cfg["project_id"])):
        sys.exit("project_id 只能用英文字母、数字、下划线或短横线")
    Project.create(a.folder, cfg)
    print(f"已创建项目：{a.folder}")


def cmd_run(a):
    p = _open(a.folder)
    _read_json(a.settings, "设置")
    st = load_settings(a.settings)
    for role in st["roles"]:
        if not SAFE_ID.match(role):
            sys.exit(f"角色名 {role} 只能用英文字母、数字、下划线或短横线")
    if not Path(a.secrets).exists():
        sys.exit(f"找不到密钥文件：{a.secrets}")
    sec = load_secrets(a.secrets)
    snap = Path(a.folder) / "settings.snapshot.json"
    roles_view = {r: {k: v for k, v in s.items() if k != "key_name"} for r, s in st["roles"].items()}
    if not snap.exists():  # 记录本项目使用的设置（不含密钥）
        snap.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
        p.log("用户", "配置", content={"roles": roles_view})
    else:
        old = json.loads(snap.read_text(encoding="utf-8-sig"))
        old_view = {r: {k: v for k, v in s.items() if k != "key_name"} for r, s in old["roles"].items()}
        if old_view != roles_view:
            print("注意：角色设置与本项目上次运行时不同。已完成的调用会沿用存档答复，新调用使用新设置。")
            p.log("用户", "配置变更", content={"旧": old_view, "新": roles_view})
            snap.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
    hub = ModelHub(p, st, sec)
    flow = Flow(p, st, hub, Retriever(p, _engine(st, sec)))
    if not flow.trusted:  # 1.5：缺可信网站名单时醒目提示
        print("!" * 60)
        print(NO_TRUSTED)
        print("!" * 60, flush=True)
        p.log("程序", "缺可信网站名单", content={"说明": NO_TRUSTED})
    res = flow.run()
    print(f"运行结果：{res}")
    if not res.startswith("调用失败暂停"):
        report.build(p)
        print((Path(a.folder) / "outputs" / "结论.md").read_text(encoding="utf-8-sig"))


def cmd_report(a):
    p = _open(a.folder)
    report.build(p)
    print((Path(a.folder) / "outputs" / "结论.md").read_text(encoding="utf-8-sig"))


def cmd_verify(a):
    p = _open(a.folder)
    ok, bad, n = p.verify_chain()
    print(f"账本共 {n} 条事件：" + ("哈希链完整。" if ok else f"从事件 {bad} 起不一致，记录可能被改动过。"))
    fbad = p.verify_files()
    print("检索存档与产物文件：" + ("与登记的哈希一致。" if not fbad else f"{len(fbad)} 个文件与登记哈希不符：{fbad[:5]}"))
    last = p.q("SELECT event_id, hash FROM events ORDER BY seq DESC LIMIT 1")
    if last:
        print(f"最后一条事件：{last[0]['event_id']}，哈希 {last[0]['hash']}")
        print("（哈希链能发现中间被改动；要发现末尾被删除，请把这条哈希另外记下，下次对照。）")
    sys.exit(0 if ok and not fbad else 1)


def cmd_serve(a):
    from .web import serve
    serve(_open(a.folder), a.port)


def cmd_app(a):
    from . import app
    app.main(None, port=a.port, open_it=not a.no_window, idle_exit=not a.no_window)


def cmd_ping(a):
    st = load_settings(a.settings)
    sec = load_secrets(a.secrets)
    import tempfile
    tmp = Project.create(Path(tempfile.mkdtemp()) / "ping", {"project_id": "PING", "question": "", "scope": "",
                                                            "items": []})
    st["params"]["retry_batch1"] = [2]
    st["params"]["retry_batch2"] = []
    hub = ModelHub(tmp, st, sec)
    for role in st["roles"]:
        try:
            hub.call_json(role, "只输出 JSON。", '请输出 {"ok": true}', call_key=f"ping-{role}", round_=0,
                          max_tokens=300)
            print(f"{role}：连通")
        except Exception as e:  # noqa: BLE001
            print(f"{role}：失败 —— {e}")
    try:
        r = _engine(st, sec).search("test", 1)
        print(f"检索：连通（返回 {len(r)} 条）")
    except Exception as e:  # noqa: BLE001
        print(f"检索：失败 —— {e}")


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):  # Windows 控制台编码不是 UTF-8 时也不崩溃
        try:
            stream.reconfigure(errors="replace")
        except Exception:  # noqa: BLE001
            pass
    ap = argparse.ArgumentParser(prog="guanzhe", description="观者自在 (AlreadyThere) · 原型第一期")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("demo"); s.add_argument("folder"); s.set_defaults(f=cmd_demo)
    s = sub.add_parser("new"); s.add_argument("folder"); s.add_argument("--project", required=True); s.set_defaults(f=cmd_new)
    s = sub.add_parser("run"); s.add_argument("folder"); s.add_argument("--settings", required=True)
    s.add_argument("--secrets", default="secrets.env"); s.set_defaults(f=cmd_run)
    s = sub.add_parser("report"); s.add_argument("folder"); s.set_defaults(f=cmd_report)
    s = sub.add_parser("verify"); s.add_argument("folder"); s.set_defaults(f=cmd_verify)
    s = sub.add_parser("serve"); s.add_argument("folder"); s.add_argument("--port", type=int, default=8765)
    s.set_defaults(f=cmd_serve)
    s = sub.add_parser("app"); s.add_argument("--port", type=int, default=0); s.add_argument("--no-window", action="store_true")
    s.set_defaults(f=cmd_app)
    s = sub.add_parser("ping"); s.add_argument("--settings", required=True); s.add_argument("--secrets", default="secrets.env")
    s.set_defaults(f=cmd_ping)
    a = ap.parse_args(argv)
    a.f(a)


if __name__ == "__main__":
    main()
