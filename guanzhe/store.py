"""项目存储：一个项目 = 一个文件夹（9.9）。

文件夹结构：
  <项目>/project.db        SQLite：账本及各记录表
  <项目>/config.json       项目配置（问题、口径、角色、参数）
  <项目>/archive/          检索原文存档（按哈希命名）
  <项目>/roles/<角色编号>/  各角色产物（程序命名，9.6）

账本（事件表）只追加，每条事件包含上一条的哈希（9.1）。所有写入由程序完成，模型不接触存档（9.7）。
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from .util import canonical_json, now_iso, sha256_text

GENESIS = "0" * 64

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  seq INTEGER PRIMARY KEY,
  event_id TEXT UNIQUE NOT NULL,
  ts TEXT NOT NULL,
  prev_hash TEXT NOT NULL,
  hash TEXT NOT NULL,
  round INTEGER,
  phase TEXT,
  actor TEXT NOT NULL,
  type TEXT NOT NULL,
  ref_type TEXT,
  ref_id TEXT,
  content TEXT
);
CREATE TABLE IF NOT EXISTS calls (
  call_id TEXT PRIMARY KEY,
  event_id TEXT,
  role TEXT, session_id TEXT, provider TEXT, model TEXT, series TEXT,
  params TEXT, request TEXT, request_hash TEXT, response TEXT,
  input_tokens INTEGER, output_tokens INTEGER, context_ratio REAL,
  status TEXT, retry_batch INTEGER, retry_seq INTEGER, failure TEXT, ts TEXT
);
CREATE TABLE IF NOT EXISTS retrievals (
  retrieval_id TEXT PRIMARY KEY,
  requester TEXT, item_id TEXT, round INTEGER, ts TEXT,
  query TEXT, url TEXT, title TEXT,
  archive_path TEXT, content_hash TEXT, source TEXT
);
CREATE TABLE IF NOT EXISTS artifacts (
  artifact_id TEXT PRIMARY KEY,
  author TEXT, round INTEGER, type TEXT, filename TEXT,
  output_hash TEXT, input_hashes TEXT, base_hash TEXT,
  event_id TEXT, status TEXT
);
CREATE TABLE IF NOT EXISTS blocks (
  block_id TEXT PRIMARY KEY,
  artifact_id TEXT, author TEXT, round INTEGER, block_type TEXT, item_id TEXT,
  fields TEXT,
  st_check TEXT, st_verify TEXT, st_indep TEXT, st_valid TEXT,
  depends_on TEXT, flags TEXT
);
CREATE TABLE IF NOT EXISTS items (
  item_id TEXT PRIMARY KEY,
  name TEXT, type TEXT,
  st_check TEXT, st_verify TEXT, st_indep TEXT, st_valid TEXT,
  indep_count INTEGER DEFAULT 0,
  value TEXT, note TEXT, history TEXT
);
CREATE TABLE IF NOT EXISTS reviews (
  review_id TEXT PRIMARY KEY,
  reviewer TEXT, artifact_id TEXT, author TEXT, round INTEGER,
  same_model INTEGER, item_id TEXT, block_id TEXT,
  verdict TEXT, severity TEXT, method TEXT, evidence TEXT,
  reasoning TEXT, changes_state INTEGER, opinion_id TEXT
);
CREATE TABLE IF NOT EXISTS opinions (
  opinion_id TEXT PRIMARY KEY,
  source_role TEXT, review_id TEXT, target_author TEXT, item_id TEXT,
  content TEXT, status TEXT, closed_round INTEGER, evidence TEXT, kind TEXT
);
CREATE TABLE IF NOT EXISTS round_stats (
  round INTEGER PRIMARY KEY,
  new_errors INTEGER, open_opinions INTEGER, changed_blocks INTEGER,
  novelty REAL, low_novelty INTEGER, notes TEXT
);
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY,
  initiator TEXT, round INTEGER, run_type TEXT, purpose_items TEXT,
  code TEXT, code_hash TEXT, output TEXT, status TEXT, ts TEXT
);
"""


class Project:
    def __init__(self, root: str | os.PathLike):
        self.root = Path(root)
        self.db_path = self.root / "project.db"
        self.archive_dir = self.root / "archive"
        self.roles_dir = self.root / "roles"
        self.conn = None

    # ---------- 生命周期 ----------
    @classmethod
    def create(cls, root, config: dict) -> "Project":
        p = cls(root)
        if p.db_path.exists():
            raise FileExistsError(f"项目已存在：{p.root}")
        p.root.mkdir(parents=True, exist_ok=True)
        p.archive_dir.mkdir(exist_ok=True)
        p.roles_dir.mkdir(exist_ok=True)
        (p.root / "config.json").write_text(
            json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        p.open()
        p.log("程序", "项目创建", content={"config_hash": sha256_text(canonical_json(config))})
        return p

    def open(self) -> "Project":
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()
        return self

    MIGRATIONS = {  # 1.4 新增的列；旧项目打开时自动补上（只加列，不改已有数据）
        "reviews": ["eq_check TEXT", "indep_check TEXT", "indep_source TEXT", "indep_dates TEXT"],
        "opinions": ["content_isolated TEXT"],
        "items": ["ganzhi TEXT"],
    }

    def _migrate(self):
        for table, cols in self.MIGRATIONS.items():
            have = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            for c in cols:
                if c.split()[0] not in have:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {c}")

    @property
    def config(self) -> dict:
        return json.loads((self.root / "config.json").read_text(encoding="utf-8-sig"))

    def close(self):
        if self.conn:
            self.conn.close()
            self.conn = None

    # ---------- 编号 ----------
    def next_id(self, table: str, prefix: str, col: str) -> str:
        n = self.conn.execute(
            f"SELECT MAX(CAST(SUBSTR({col}, {len(prefix) + 1}) AS INTEGER)) FROM {table} "
            f"WHERE {col} LIKE ?", (prefix + "%",)).fetchone()[0] or 0
        return f"{prefix}{n + 1:05d}"

    # ---------- 账本 ----------
    def log(self, actor: str, etype: str, *, round_=None, phase=None,
            ref_type=None, ref_id=None, content=None) -> str:
        """追加一条事件；返回事件编号。哈希覆盖除 hash 以外的全部字段。"""
        cur = self.conn.execute("SELECT hash, seq FROM events ORDER BY seq DESC LIMIT 1").fetchone()
        prev = cur["hash"] if cur else GENESIS
        seq = (cur["seq"] + 1) if cur else 1
        event_id = f"E{seq:06d}"
        body = {
            "seq": seq, "event_id": event_id, "ts": now_iso(), "prev_hash": prev,
            "round": round_, "phase": phase, "actor": actor, "type": etype,
            "ref_type": ref_type, "ref_id": ref_id,
            "content": canonical_json(content) if content is not None else None,
        }
        h = sha256_text(canonical_json(body))
        self.conn.execute(
            "INSERT INTO events (seq,event_id,ts,prev_hash,hash,round,phase,actor,type,ref_type,ref_id,content)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (seq, event_id, body["ts"], prev, h, round_, phase, actor, etype, ref_type, ref_id, body["content"]))
        self.conn.commit()
        return event_id

    def verify_chain(self):
        """逐条重算哈希链；返回 (是否完整, 第一个不一致的事件编号或 None, 事件总数)。"""
        prev = GENESIS
        rows = self.conn.execute("SELECT * FROM events ORDER BY seq").fetchall()
        for r in rows:
            body = {k: r[k] for k in ("seq", "event_id", "ts", "prev_hash", "round", "phase",
                                      "actor", "type", "ref_type", "ref_id", "content")}
            if r["prev_hash"] != prev or sha256_text(canonical_json(body)) != r["hash"]:
                return False, r["event_id"], len(rows)
            prev = r["hash"]
        return True, None, len(rows)

    def verify_files(self):
        """重算检索存档与产物文件的哈希，返回不一致的文件列表。"""
        bad = []
        for r in self.q("SELECT archive_path, content_hash FROM retrievals"):
            f = self.path_of(r["archive_path"])
            if not f.exists() or sha256_text(f.read_text(encoding="utf-8")) != r["content_hash"]:
                bad.append(r["archive_path"])
        for r in self.q("SELECT filename, output_hash FROM artifacts"):
            f = self.path_of(r["filename"])
            if not f.exists() or sha256_text(f.read_text(encoding="utf-8")) != r["output_hash"]:
                bad.append(r["filename"])
        return bad

    # ---------- 文件 ----------
    def save_artifact_file(self, role: str, round_: int, kind: str, text: str) -> tuple[str, str]:
        """按 9.6 程序命名保存产物文件；返回 (相对路径, 哈希)。"""
        h = sha256_text(text)
        d = self.roles_dir / role
        d.mkdir(parents=True, exist_ok=True)
        name = f"{self.config.get('project_id', 'P')}_R{round_:02d}_{role}_{kind}_{h[:8]}.json"
        (d / name).write_text(text, encoding="utf-8")
        return (d / name).relative_to(self.root).as_posix(), h

    def archive_text(self, text: str) -> tuple[str, str]:
        h = sha256_text(text)
        path = self.archive_dir / f"{h}.txt"
        if not path.exists():  # 相同内容按哈希只存一份（9.8）
            path.write_text(text, encoding="utf-8")
        return path.relative_to(self.root).as_posix(), h

    def path_of(self, rel: str) -> Path:
        """账本里记的相对路径 -> 本机路径（1.4：Windows 上建的项目在别的系统上也能读，反斜杠统一换成 /）。"""
        return self.root / str(rel).replace("\\", "/")

    def read_archive(self, retrieval_id: str) -> str | None:
        r = self.conn.execute("SELECT archive_path FROM retrievals WHERE retrieval_id=?",
                              (retrieval_id,)).fetchone()
        if not r:
            return None
        return self.path_of(r["archive_path"]).read_text(encoding="utf-8-sig")

    # ---------- 便捷查询 ----------
    def q(self, sql: str, args=()):
        return self.conn.execute(sql, args).fetchall()

    def exec(self, sql: str, args=()):
        self.conn.execute(sql, args)
        self.conn.commit()
