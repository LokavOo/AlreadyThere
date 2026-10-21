"""2.10：权威时间戳——只留接口，不带任何实现（用户 2026-10-21 定）。

用户定的规则：
- 可选项，默认不打。用哪种方式、连哪家服务由用户自己选，程序不替用户选、不内置任何一家的地址。
- 现在只留接口；具体实现（RFC 3161 时间戳服务、OpenTimestamps、国内收费的时间戳／存证服务等）以后再做，或由用户按接口自己接。
- 什么时候打：**每次流程结束时打一次**（用户点「再运行一次」又有新内容时再打一次）。因为账本是哈希链，
  给最后一条的哈希打一次时间戳，前面所有记录都一起被证明了，不需要逐条打；用户用付费服务时心里好算：
  跑完几次，就是几次。
- 能证明什么：这份完整的记录在那个时间点之前就已经存在、之后没被改过。不能证明记录里的内容是对的。

接口：在 REGISTRY 里登记一个类（继承 TimestampService），设置文件里写
    "timestamp": {"service": "<登记名>", ...这家服务需要的参数...}
密钥照旧写在 密钥.env，这里只写密钥名。
"""

from __future__ import annotations

import base64
import json
from pathlib import Path


class TimestampService:
    """一种时间戳方式。实现时只需要这三个方法；交出去的永远只是一个哈希（十六进制字符串），不是账本内容。"""

    name = ""        # 登记名
    title = ""       # 给人看的名字
    needs_upgrade = False  # 像 OpenTimestamps 那样要过一段时间再"升级"成完整凭证的，写 True

    def __init__(self, options: dict, secrets: dict):
        self.options, self.secrets = options or {}, secrets or {}

    def stamp(self, digest_hex: str) -> dict:
        """打时间戳。返回 {"receipt": bytes（服务返回的凭证原样）, "service": 服务地址或名称, "time": 服务给出的时间（字符串，
        拿不到就空）, "note": 说明}。失败就抛异常，程序记"失败"并写明原因，不影响流程本身。"""
        raise NotImplementedError

    def upgrade(self, record: dict) -> dict | None:
        """（可选）把还不完整的凭证升级成完整凭证；不需要或还没好就返回 None。"""
        return None

    def verify(self, record: dict, digest_hex: str) -> dict:
        """（可选）核对凭证是否对应这个哈希。返回 {"ok": True/False/None, "note": 说明}。"""
        return {"ok": None, "note": "这种方式还没有实现核对"}


REGISTRY: dict[str, type[TimestampService]] = {}  # 登记名 -> 类；2.10 一个实现都没有


def config_of(settings: dict) -> dict:
    ts = settings.get("timestamp") or {}
    return ts if isinstance(ts, dict) else {}


def enabled(settings: dict) -> bool:
    return bool(config_of(settings).get("service"))


def stamp_project(project, settings: dict, secrets: dict) -> dict | None:
    """流程结束时调用：没启用就什么都不做（返回 None）；启用了就对账本最后一条的哈希打一次，结果记进账本，凭证存进项目文件夹。"""
    cfg = config_of(settings)
    name = cfg.get("service")
    if not name:
        return None
    last = project.q("SELECT event_id, hash FROM events ORDER BY seq DESC LIMIT 1")
    if not last:
        return None
    digest, upto = last[0]["hash"], last[0]["event_id"]
    n = len(project.q("SELECT 1 FROM events WHERE type='时间戳'")) + 1
    base = {"第几次": n, "方式": name, "打的哈希": digest, "截止到事件": upto,
            "说明": "证明截止到这条事件的全部记录在这个时间点之前已经存在、之后没被改过；不证明内容正确"}
    cls = REGISTRY.get(name)
    if cls is None:
        project.log("程序", "时间戳", content=dict(base, 结果="失败", 原因=f"这个版本没有「{name}」的实现（2.10 只留了接口）"))
        return {"ok": False}
    try:
        res = cls(cfg, secrets).stamp(digest)
    except Exception as e:  # noqa: BLE001  打时间戳失败不影响流程和结论
        project.log("程序", "时间戳", content=dict(base, 结果="失败", 原因=str(e)[:300]))
        return {"ok": False}
    folder = Path(project.root) / "时间戳"
    folder.mkdir(exist_ok=True)
    rec = {"service": res.get("service", ""), "time": res.get("time", ""), "note": res.get("note", ""),
           "digest": digest, "upto": upto, "needs_upgrade": cls.needs_upgrade,
           "receipt_b64": base64.b64encode(res.get("receipt") or b"").decode("ascii")}
    (folder / f"{n:03d}.json").write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    project.log("程序", "时间戳", content=dict(base, 结果="成功", 服务=rec["service"], 服务给出的时间=rec["time"],
                                             凭证文件=f"时间戳/{n:03d}.json", 还要升级=cls.needs_upgrade))
    return {"ok": True}


def summary(project) -> dict:
    rows = [json.loads(r["content"]) for r in project.q("SELECT content FROM events WHERE type='时间戳' ORDER BY seq")]
    return {"次数": sum(1 for r in rows if r.get("结果") == "成功"), "失败": sum(1 for r in rows if r.get("结果") != "成功"),
            "记录": rows}
