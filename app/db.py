"""SQLite 持久化层。

原始输入（reviews / events / relations）与推算结果（computations）
分表保存；每次成功修改都会写入一条新的 computations 记录。
通过 reviews.version 实现乐观锁：携带旧版本号的提交会被拒绝。

版本回看：每次保存/定稿/恢复成功时，除 computations 外另写一条不可变的
snapshots 记录，留存当时的标题、事件、“先于”关系输入，与该版本的推算结果
一一对应。旧库中只有 computations、没有 snapshots 的历史版本视为不可回看、
不可恢复，列表中明确标记，绝不用当前输入冒充。
"""

import json
import os
import sqlite3
from datetime import datetime

import compute

LEGACY_REASON = "历史版本缺少完整输入快照，不可回看、不可恢复"

SCHEMA = """
CREATE TABLE IF NOT EXISTS reviews (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    title      TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'draft',      -- draft | finalized
    version    INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    review_id       INTEGER NOT NULL REFERENCES reviews(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    earliest_start  TEXT,                          -- ISO 分钟精度，可空（暂存）
    latest_start    TEXT,
    duration_minutes INTEGER,
    note            TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS relations (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    review_id      INTEGER NOT NULL REFERENCES reviews(id) ON DELETE CASCADE,
    predecessor_id INTEGER NOT NULL,
    successor_id   INTEGER NOT NULL,
    min_gap_minutes INTEGER NOT NULL DEFAULT 0,    -- 前项结束到后项开始的最短等待
    max_gap_minutes INTEGER                        -- 最长等待，NULL = 不限
);
CREATE TABLE IF NOT EXISTS computations (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    review_id      INTEGER NOT NULL REFERENCES reviews(id) ON DELETE CASCADE,
    review_version INTEGER NOT NULL,
    status         TEXT NOT NULL,                  -- ok | conflict
    result_json    TEXT NOT NULL,
    computed_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshots (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    review_id      INTEGER NOT NULL REFERENCES reviews(id) ON DELETE CASCADE,
    review_version INTEGER NOT NULL,
    kind           TEXT NOT NULL,                  -- save | finalize | restore
    title          TEXT NOT NULL,                 -- 不可变的标题快照
    input_json     TEXT NOT NULL,                 -- 不可变输入：{events, relations}
    source_version INTEGER,                       -- restore/finalize 沿用的来源版本
    created_at     TEXT NOT NULL,
    UNIQUE(review_id, review_version)
);
"""


class NotFound(Exception):
    pass


class VersionNotFound(Exception):
    """指定的版本号在 computations 中不存在。"""


class LegacyVersion(Exception):
    """旧库历史版本：有推算记录但缺少完整输入快照，不可回看/恢复。"""


class VersionConflict(Exception):
    def __init__(self, current_version):
        super().__init__("stale version")
        self.current_version = current_version


class FinalizeRejected(Exception):
    pass


def _now():
    return datetime.now().isoformat(timespec="seconds")


def init_db(db_path):
    """启动时初始化数据库（建目录、建表、老库结构迁移）。"""
    directory = os.path.dirname(os.path.abspath(db_path))
    os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(SCHEMA)
        _migrate(conn)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.commit()
    finally:
        conn.close()


def _migrate(conn):
    """老库结构升级（幂等）：relations 补充“先于”间隔设置列。

    升级前已存在的关系行由列默认值得到 最短等待=0、最长等待=NULL（不限），
    与旧语义“前项结束不晚于后项开始”完全一致。
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(relations)")}
    if "min_gap_minutes" not in cols:
        conn.execute(
            "ALTER TABLE relations ADD COLUMN min_gap_minutes INTEGER NOT NULL DEFAULT 0")
    if "max_gap_minutes" not in cols:
        conn.execute("ALTER TABLE relations ADD COLUMN max_gap_minutes INTEGER")


def connect(db_path):
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# ---------------------------------------------------------------------- 读取

def current_version(db_path, review_id):
    conn = connect(db_path)
    try:
        row = conn.execute("SELECT version FROM reviews WHERE id=?", (review_id,)).fetchone()
        if row is None:
            raise NotFound(review_id)
        return row["version"]
    finally:
        conn.close()


def list_reviews(db_path):
    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT id, title, status, version, updated_at FROM reviews ORDER BY updated_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_bundle(db_path, review_id):
    """返回复盘完整快照：元信息 + 原始输入 + 最近一次推算结果。"""
    conn = connect(db_path)
    try:
        review = conn.execute("SELECT * FROM reviews WHERE id=?", (review_id,)).fetchone()
        if review is None:
            raise NotFound(review_id)
        events = [dict(r) for r in conn.execute(
            "SELECT * FROM events WHERE review_id=? ORDER BY id", (review_id,)).fetchall()]
        relations = [dict(r) for r in conn.execute(
            "SELECT * FROM relations WHERE review_id=? ORDER BY id", (review_id,)).fetchall()]
        comp = conn.execute(
            "SELECT * FROM computations WHERE review_id=? ORDER BY id DESC LIMIT 1",
            (review_id,)).fetchone()
        computation = None
        if comp is not None:
            computation = {
                "review_version": comp["review_version"],
                "status": comp["status"],
                "computed_at": comp["computed_at"],
                "result": json.loads(comp["result_json"]),
            }
        return {
            "review": dict(review),
            "events": events,
            "relations": relations,
            "computation": computation,
        }
    finally:
        conn.close()


def list_versions(db_path, review_id):
    """列出某复盘可回看的版本（按版本号倒序）。

    以 computations 为准枚举每个版本；关联到 snapshots 的版本可回看，
    旧库中缺少完整输入快照的历史版本 reviewable/restorable 均为 false，
    并给出原因，不能也不会用当前输入冒充。
    """
    conn = connect(db_path)
    try:
        review = conn.execute("SELECT id FROM reviews WHERE id=?", (review_id,)).fetchone()
        if review is None:
            raise NotFound(review_id)
        comps = conn.execute(
            "SELECT * FROM computations WHERE review_id=? ORDER BY review_version DESC",
            (review_id,)).fetchall()
        snaps = {s["review_version"]: s for s in conn.execute(
            "SELECT * FROM snapshots WHERE review_id=?", (review_id,)).fetchall()}
        versions = []
        for c in comps:
            v = c["review_version"]
            snap = snaps.get(v)
            result = json.loads(c["result_json"])
            if snap is None:
                versions.append({
                    "version": v,
                    "state": "draft",
                    "kind": None,
                    "reviewable": False,
                    "restorable": False,
                    "status": c["status"],
                    "has_drafts": bool(result.get("drafts")),
                    "computed_at": c["computed_at"],
                    "reason": LEGACY_REASON,
                    "source_version": None,
                })
            else:
                versions.append({
                    "version": v,
                    "state": "finalized" if snap["kind"] == "finalize" else "draft",
                    "kind": snap["kind"],
                    "reviewable": True,
                    "restorable": True,
                    "status": c["status"],
                    "has_drafts": bool(result.get("drafts")),
                    "computed_at": c["computed_at"],
                    "created_at": snap["created_at"],
                    "title": snap["title"],
                    "source_version": snap["source_version"],
                })
        return versions
    finally:
        conn.close()


def get_version(db_path, review_id, version):
    """返回某版本的完整回看内容：不可变输入 + 当时推算依据 + 状态。

    旧库缺少输入快照的版本抛 LegacyVersion；版本不存在抛 VersionNotFound。
    """
    conn = connect(db_path)
    try:
        review = conn.execute("SELECT id FROM reviews WHERE id=?", (review_id,)).fetchone()
        if review is None:
            raise NotFound(review_id)
        comp = conn.execute(
            "SELECT * FROM computations WHERE review_id=? AND review_version=?",
            (review_id, version)).fetchone()
        if comp is None:
            raise VersionNotFound(version)
        snap = conn.execute(
            "SELECT * FROM snapshots WHERE review_id=? AND review_version=?",
            (review_id, version)).fetchone()
        if snap is None:
            raise LegacyVersion(version)
        saved_input = json.loads(snap["input_json"])
        return {
            "version": version,
            "state": "finalized" if snap["kind"] == "finalize" else "draft",
            "kind": snap["kind"],
            "reviewable": True,
            "restorable": True,
            "title": snap["title"],
            "source_version": snap["source_version"],
            "created_at": snap["created_at"],
            "status": comp["status"],
            "computed_at": comp["computed_at"],
            "input": saved_input,
            "result": json.loads(comp["result_json"]),
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------- 写入

def create_review(db_path, title):
    conn = connect(db_path)
    try:
        now = _now()
        cur = conn.execute(
            "INSERT INTO reviews (title, status, version, created_at, updated_at) VALUES (?,?,?,?,?)",
            (title, "draft", 1, now, now))
        review_id = cur.lastrowid
        _save_version(conn, review_id, 1, title, [], [], "save", now)
        conn.commit()
        return review_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _snapshot_relations(relations, key_by_id):
    """快照用的关系列表（key 引用 + 当时的间隔设置），供回看与恢复使用。"""
    return [{
        "predecessor_key": key_by_id.get(r["predecessor_id"], ""),
        "successor_key": key_by_id.get(r["successor_id"], ""),
        "min_gap_minutes": r.get("min_gap_minutes") or 0,
        "max_gap_minutes": r.get("max_gap_minutes"),
    } for r in relations]


def _current_input(conn, review_id):
    """读取当前 live 输入，事件按插入顺序生成 key，关系用 key 引用。"""
    events = [dict(r) for r in conn.execute(
        "SELECT * FROM events WHERE review_id=? ORDER BY id", (review_id,)).fetchall()]
    relations = [dict(r) for r in conn.execute(
        "SELECT * FROM relations WHERE review_id=? ORDER BY id", (review_id,)).fetchall()]
    key_by_id = {e["id"]: "id_" + str(e["id"]) for e in events}
    snapshot_events = [{
        "key": key_by_id[e["id"]],
        "name": e["name"],
        "earliest_start": e["earliest_start"],
        "latest_start": e["latest_start"],
        "duration_minutes": e["duration_minutes"],
        "note": e["note"],
    } for e in events]
    snapshot_relations = _snapshot_relations(relations, key_by_id)
    return events, relations, snapshot_events, snapshot_relations


def _save_version(conn, review_id, version, title, events, relations, kind, now=None,
                  source_version=None):
    """运行推算并写入该版本的 computations 与不可变 snapshots 记录。

    events/relations 为 compute 引擎所需的 live 行（含数字 id）；
    快照输入另外以 key 形式留存，供回看与恢复使用。
    """
    now = now or _now()
    result = compute.compute(events, relations)
    conn.execute(
        "INSERT INTO computations (review_id, review_version, status, result_json, computed_at)"
        " VALUES (?,?,?,?,?)",
        (review_id, version, result["status"], json.dumps(result, ensure_ascii=False), now))
    key_by_id = {e["id"]: "id_" + str(e["id"]) for e in events}
    snapshot_events = [{
        "key": key_by_id[e["id"]],
        "name": e["name"],
        "earliest_start": e["earliest_start"],
        "latest_start": e["latest_start"],
        "duration_minutes": e["duration_minutes"],
        "note": e["note"],
    } for e in events]
    snapshot_relations = _snapshot_relations(relations, key_by_id)
    input_json = json.dumps(
        {"events": snapshot_events, "relations": snapshot_relations}, ensure_ascii=False)
    conn.execute(
        "INSERT INTO snapshots (review_id, review_version, kind, title, input_json,"
        " source_version, created_at) VALUES (?,?,?,?,?,?,?)",
        (review_id, version, kind, title, input_json, source_version, now))
    return result


def _replace_input(conn, review_id, events, relations):
    """整体替换 live 输入表。events 用客户端 key，relations 用 key 引用。"""
    conn.execute("DELETE FROM events WHERE review_id=?", (review_id,))
    conn.execute("DELETE FROM relations WHERE review_id=?", (review_id,))
    key_to_id = {}
    for e in events:
        cur = conn.execute(
            "INSERT INTO events (review_id, name, earliest_start, latest_start,"
            " duration_minutes, note) VALUES (?,?,?,?,?,?)",
            (review_id, e["name"], e.get("earliest_start"), e.get("latest_start"),
             e.get("duration_minutes"), e.get("note", "")))
        key_to_id[e["key"]] = cur.lastrowid
    for r in relations:
        min_gap = r.get("min_gap_minutes")
        conn.execute(
            "INSERT INTO relations (review_id, predecessor_id, successor_id,"
            " min_gap_minutes, max_gap_minutes) VALUES (?,?,?,?,?)",
            (review_id, key_to_id[r["predecessor_key"]], key_to_id[r["successor_key"]],
             0 if min_gap is None else min_gap, r.get("max_gap_minutes")))


def update_review(db_path, review_id, base_version, title, events, relations):
    """整体替换某复盘的输入并重新推算。base_version 过期则抛 VersionConflict。"""
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT version FROM reviews WHERE id=?", (review_id,)).fetchone()
        if row is None:
            raise NotFound(review_id)
        if row["version"] != base_version:
            raise VersionConflict(row["version"])

        _replace_input(conn, review_id, events, relations)
        live_events, live_relations, _, _ = _current_input(conn, review_id)

        new_version = base_version + 1
        # 输入发生变化：已定稿状态失效，回到草稿
        now = _now()
        conn.execute(
            "UPDATE reviews SET title=?, version=?, status='draft', updated_at=? WHERE id=?",
            (title, new_version, now, review_id))
        _save_version(conn, review_id, new_version, title, live_events, live_relations,
                      "save", now)
        conn.commit()
        return new_version
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def restore_review(db_path, review_id, base_version, source_version):
    """把某历史版本的不可变输入恢复为当前复盘的新草稿版本，重新推算。

    旧版本及原版本都不被覆盖；版本过期抛 VersionConflict；来源版本缺少
    完整输入快照（旧库历史版本）抛 LegacyVersion。
    """
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT version FROM reviews WHERE id=?", (review_id,)).fetchone()
        if row is None:
            raise NotFound(review_id)
        if row["version"] != base_version:
            raise VersionConflict(row["version"])

        snap = conn.execute(
            "SELECT * FROM snapshots WHERE review_id=? AND review_version=?",
            (review_id, source_version)).fetchone()
        if snap is None:
            # 可能是版本号不存在，也可能是旧库缺快照的历史版本
            comp = conn.execute(
                "SELECT 1 FROM computations WHERE review_id=? AND review_version=?",
                (review_id, source_version)).fetchone()
            if comp is not None:
                raise LegacyVersion(source_version)
            raise VersionNotFound(source_version)

        saved_input = json.loads(snap["input_json"])
        _replace_input(conn, review_id, saved_input["events"], saved_input["relations"])
        live_events, live_relations, _, _ = _current_input(conn, review_id)

        new_version = base_version + 1
        title = snap["title"]
        now = _now()
        # 恢复产生的永远是新草稿：冲突或暂存输入允许恢复，但仍不得定稿
        conn.execute(
            "UPDATE reviews SET title=?, version=?, status='draft', updated_at=? WHERE id=?",
            (title, new_version, now, review_id))
        _save_version(conn, review_id, new_version, title, live_events, live_relations,
                      "restore", now, source_version=source_version)
        conn.commit()
        return new_version
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def finalize_review(db_path, review_id, base_version):
    """定稿。存在约束冲突或暂存事件时拒绝；版本过期抛 VersionConflict。

    定稿不改动输入，沿用同一输入重新留存一条记录（kind=finalize），
    以记录定稿状态与推算依据。
    """
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM reviews WHERE id=?", (review_id,)).fetchone()
        if row is None:
            raise NotFound(review_id)
        if row["version"] != base_version:
            raise VersionConflict(row["version"])
        comp = conn.execute(
            "SELECT * FROM computations WHERE review_id=? ORDER BY id DESC LIMIT 1",
            (review_id,)).fetchone()
        if comp is None or comp["status"] != "ok":
            raise FinalizeRejected("当前版本存在约束冲突，不得定稿")
        result = json.loads(comp["result_json"])
        if result.get("drafts"):
            raise FinalizeRejected("仍有 {} 个事件处于暂存状态（缺少时间范围），不得定稿".format(
                len(result["drafts"])))
        live_events, live_relations, _, _ = _current_input(conn, review_id)
        new_version = base_version + 1
        now = _now()
        conn.execute(
            "UPDATE reviews SET status='finalized', version=?, updated_at=? WHERE id=?",
            (new_version, now, review_id))
        _save_version(conn, review_id, new_version, row["title"], live_events, live_relations,
                      "finalize", now, source_version=base_version)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def delete_review(db_path, review_id):
    conn = connect(db_path)
    try:
        cur = conn.execute("DELETE FROM reviews WHERE id=?", (review_id,))
        conn.commit()
        if cur.rowcount == 0:
            raise NotFound(review_id)
    finally:
        conn.close()
