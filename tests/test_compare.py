"""跨版本对照测试：python tests/test_compare.py

覆盖：
- 事件按持续身份配对：改名、修改输入、恢复旧版本后仍识别为同一事件，
  不按名称或会重建的行号猜配；
- 新增/删除/改名/输入时间范围与推算可行窗口的变化；
- “先于”关系间隔、冲突与暂存状态的变化；
- 顺序无效、版本不存在、旧库缺快照的明确报错；
- 缺可靠身份的旧快照只展示各版本原貌（pairable=False），不虚构逐事件差值；
- 对照只读：不改变当前输入、版本号与定稿状态。
"""
import json
import pathlib
import sqlite3
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "app"))

import db  # noqa: E402


def ev(key, name, es, ls, dur):
    return {"key": key, "name": name, "earliest_start": es, "latest_start": ls,
            "duration_minutes": dur, "note": ""}


def rel(a, b, min_g=None, max_g=None):
    r = {"predecessor_key": a, "successor_key": b}
    if min_g is not None:
        r["min_gap_minutes"] = min_g
    if max_g is not None:
        r["max_gap_minutes"] = max_g
    return r


def setup_db():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db.init_db(tmp.name)
    return tmp.name


def snapshot_keys(p, rid, version):
    return [e["key"] for e in db.get_version(p, rid, version)["input"]["events"]]


def test_identity_stable_across_rename_and_save():
    p = setup_db()
    rid = db.create_review(p, "T")
    e1 = ev("a", "告警", "2026-10-01T10:00", "2026-10-01T10:30", 15)
    e2 = ev("b", "响应", "2026-10-01T10:00", "2026-10-01T12:00", 30)
    db.update_review(p, rid, 1, "T", [e1, e2], [rel("a", "b")])  # v2
    snap2 = db.get_version(p, rid, 2)["input"]["events"]
    keys = [e["key"] for e in snap2]

    # v3：两个事件都改名，告警最早开始 10:00 → 10:05（key 沿用 id_<旧行号>）
    e1b = ev(keys[0], "告警触发", "2026-10-01T10:05", "2026-10-01T10:30", 15)
    e2b = ev(keys[1], "值班响应", "2026-10-01T10:00", "2026-10-01T12:00", 30)
    db.update_review(p, rid, 2, "T", [e1b, e2b], [rel(keys[0], keys[1])])  # v3
    snap3 = db.get_version(p, rid, 3)["input"]["events"]
    # 行号已重建，但持续身份不变
    assert [e["key"] for e in snap3] != keys
    assert [e["identity"] for e in snap3] == [e["identity"] for e in snap2]

    cmp = db.compare_versions(p, rid, 2, 3)
    assert cmp["pairable"] is True
    assert cmp["events"]["added"] == [] and cmp["events"]["removed"] == []
    assert cmp["events"]["renamed"] == [
        {"identity": snap2[0]["identity"], "from_name": "告警", "to_name": "告警触发"},
        {"identity": snap2[1]["identity"], "from_name": "响应", "to_name": "值班响应"},
    ]
    changed = {c["identity"]: c for c in cmp["events"]["changed"]}
    c1 = changed[snap2[0]["identity"]]
    assert c1["input_changes"]["earliest_start"] == {
        "from": "2026-10-01T10:00", "to": "2026-10-01T10:05"}
    # 可行窗口联动：响应最早开始 10:15 → 10:20
    c2 = changed[snap2[1]["identity"]]
    assert c2["window_changes"]["earliest_start"] == {
        "from": "2026-10-01T10:15", "to": "2026-10-01T10:20"}


def test_added_removed_events_and_windows():
    p = setup_db()
    rid = db.create_review(p, "T")
    e1 = ev("a", "A", "2026-10-01T10:00", "2026-10-01T11:00", 10)
    e2 = ev("b", "B", "2026-10-01T10:00", "2026-10-01T12:00", 10)
    db.update_review(p, rid, 1, "T", [e1, e2], [])  # v2
    key_a = snapshot_keys(p, rid, 2)[0]
    e3 = ev("c", "C", "2026-10-01T13:00", "2026-10-01T14:00", 5)
    db.update_review(p, rid, 2, "T", [ev(key_a, "A", "2026-10-01T10:00",
                                         "2026-10-01T11:00", 10), e3], [])  # v3：删 B 增 C

    cmp = db.compare_versions(p, rid, 2, 3)
    assert [e["name"] for e in cmp["events"]["added"]] == ["C"]
    assert [e["name"] for e in cmp["events"]["removed"]] == ["B"]
    assert cmp["events"]["added"][0]["window"]["earliest_start"] == "2026-10-01T13:00"
    assert cmp["events"]["removed"][0]["window"]["latest_start"] == "2026-10-01T12:00"
    assert cmp["events"]["unchanged_count"] == 1
    assert cmp["events"]["renamed"] == [] and cmp["events"]["changed"] == []


def test_relation_gap_changes_and_add_remove():
    p = setup_db()
    rid = db.create_review(p, "T")
    e1 = ev("a", "告警", "2026-10-01T10:00", "2026-10-01T10:30", 15)
    e2 = ev("b", "响应", "2026-10-01T10:00", "2026-10-01T12:00", 30)
    db.update_review(p, rid, 1, "T", [e1, e2], [rel("a", "b", min_g=10, max_g=60)])  # v2
    keys = snapshot_keys(p, rid, 2)
    db.update_review(p, rid, 2, "T",
                     [ev(keys[0], "告警", "2026-10-01T10:00", "2026-10-01T10:30", 15),
                      ev(keys[1], "响应", "2026-10-01T10:00", "2026-10-01T12:00", 30)],
                     [rel(keys[0], keys[1], min_g=20)])  # v3：间隔改 20/不限

    cmp = db.compare_versions(p, rid, 2, 3)
    gc = cmp["relations"]["gap_changed"]
    assert len(gc) == 1
    assert gc[0]["from"] == {"min_gap_minutes": 10, "max_gap_minutes": 60}
    assert gc[0]["to"] == {"min_gap_minutes": 20, "max_gap_minutes": None}
    assert gc[0]["predecessor"]["name"] == "告警" and gc[0]["successor"]["name"] == "响应"
    assert cmp["relations"]["added"] == [] and cmp["relations"]["removed"] == []

    # v4：删除关系 → removed；再对照 v2→v4 也体现间隔差异消失
    db.update_review(p, rid, 3, "T",
                     [ev(keys[0], "告警", "2026-10-01T10:00", "2026-10-01T10:30", 15),
                      ev(keys[1], "响应", "2026-10-01T10:00", "2026-10-01T12:00", 30)],
                     [])  # v4
    cmp = db.compare_versions(p, rid, 3, 4)
    assert len(cmp["relations"]["removed"]) == 1
    assert cmp["relations"]["removed"][0]["min_gap_minutes"] == 20
    assert cmp["relations"]["gap_changed"] == []


def test_restore_keeps_identity():
    p = setup_db()
    rid = db.create_review(p, "T")
    e1 = ev("a", "告警", "2026-10-01T10:00", "2026-10-01T10:30", 15)
    e2 = ev("b", "响应", "2026-10-01T10:00", "2026-10-01T12:00", 30)
    db.update_review(p, rid, 1, "T", [e1, e2], [rel("a", "b")])  # v2
    snap2 = db.get_version(p, rid, 2)["input"]["events"]
    keys = [e["key"] for e in snap2]
    # v3 大改：删掉响应、告警改名
    db.update_review(p, rid, 2, "T",
                     [ev(keys[0], "改名告警", "2026-10-01T10:00", "2026-10-01T10:30", 15)],
                     [])  # v3
    db.restore_review(p, rid, 3, 2)  # v4 恢复 v2

    snap4 = db.get_version(p, rid, 4)["input"]["events"]
    assert [e["identity"] for e in snap4] == [e["identity"] for e in snap2]
    # v2 与 v4 内容一致：对照无任何差异
    cmp = db.compare_versions(p, rid, 2, 4)
    assert cmp["pairable"] is True
    assert cmp["events"]["added"] == [] and cmp["events"]["removed"] == []
    assert cmp["events"]["renamed"] == [] and cmp["events"]["changed"] == []
    assert cmp["events"]["unchanged_count"] == 2
    # v3 → v4：响应“重新出现”，但身份与 v2 相同（恢复后仍识别为原事件）
    cmp34 = db.compare_versions(p, rid, 3, 4)
    assert [e["name"] for e in cmp34["events"]["added"]] == ["响应"]
    assert cmp34["events"]["added"][0]["identity"] == snap2[1]["identity"]
    assert cmp34["events"]["renamed"] == [
        {"identity": snap2[0]["identity"], "from_name": "改名告警", "to_name": "告警"}]


def test_conflicts_and_drafts_changes():
    p = setup_db()
    rid = db.create_review(p, "T")
    bad = ev("x", "X", "2026-10-01T11:00", "2026-10-01T10:00", 5)   # 范围自相矛盾
    draft = ev("y", "Y", None, None, None)                          # 暂存
    db.update_review(p, rid, 1, "T", [bad, draft], [])              # v2：冲突 + 暂存
    keys = snapshot_keys(p, rid, 2)
    good = ev(keys[0], "X", "2026-10-01T10:00", "2026-10-01T11:00", 5)
    full = ev(keys[1], "Y", "2026-10-01T12:00", "2026-10-01T13:00", 10)
    db.update_review(p, rid, 2, "T", [good, full], [])              # v3：全部修复

    cmp = db.compare_versions(p, rid, 2, 3)
    assert cmp["status"] == {"from": "conflict", "to": "ok", "changed": True}
    assert len(cmp["conflicts"]["from"]) == 1 and cmp["conflicts"]["to"] == []
    assert len(cmp["conflicts"]["resolved"]) == 1
    assert cmp["conflicts"]["resolved"][0]["type"] == "invalid_range"
    assert cmp["conflicts"]["resolved"][0]["events"][0]["name"] == "X"
    assert cmp["conflicts"]["new"] == []
    # 暂存：Y 解除暂存；draft_change 落在同一身份的事件上
    assert [d["name"] for d in cmp["drafts"]["resolved"]] == ["Y"]
    assert cmp["drafts"]["new"] == []
    y_ident = cmp["drafts"]["resolved"][0]["identity"]
    changed = {c["identity"]: c for c in cmp["events"]["changed"]}
    assert changed[y_ident]["draft_change"] == {"from": True, "to": False}
    assert changed[y_ident]["window_changes"]["earliest_start"] == {
        "from": None, "to": "2026-10-01T12:00"}


def test_compare_errors():
    p = setup_db()
    rid = db.create_review(p, "T")
    e1 = ev("a", "A", "2026-10-01T10:00", "2026-10-01T11:00", 10)
    db.update_review(p, rid, 1, "T", [e1], [])  # v2

    # 顺序无效：from >= to（即使 to 不存在也先报顺序错误）
    for a, b in ((2, 2), (3, 2)):
        try:
            db.compare_versions(p, rid, a, b)
            assert False, "应当抛 InvalidVersionOrder"
        except db.InvalidVersionOrder:
            pass
    # 版本不存在
    try:
        db.compare_versions(p, rid, 1, 99)
        assert False, "应当抛 VersionNotFound"
    except db.VersionNotFound:
        pass
    # 复盘不存在
    try:
        db.compare_versions(p, 999, 1, 2)
        assert False, "应当抛 NotFound"
    except db.NotFound:
        pass
    # 旧库缺快照的版本：说明无法比较
    conn = sqlite3.connect(p)
    conn.execute("DELETE FROM snapshots WHERE review_id=? AND review_version=1", (rid,))
    conn.commit()
    conn.close()
    try:
        db.compare_versions(p, rid, 1, 2)
        assert False, "应当抛 VersionNotComparable"
    except db.VersionNotComparable as e:
        assert e.version == 1


def test_snapshot_without_identity_shows_raw_only():
    p = setup_db()
    rid = db.create_review(p, "T")
    e1 = ev("a", "A", "2026-10-01T10:00", "2026-10-01T11:00", 10)
    db.update_review(p, rid, 1, "T", [e1], [])  # v2
    key_a = snapshot_keys(p, rid, 2)[0]
    db.update_review(p, rid, 2, "T2",
                     [ev(key_a, "A2", "2026-10-01T10:00", "2026-10-01T11:00", 10)], [])  # v3

    # 模拟升级前的旧快照：事件没有 identity 字段
    conn = sqlite3.connect(p)
    data = json.loads(conn.execute(
        "SELECT input_json FROM snapshots WHERE review_id=? AND review_version=2",
        (rid,)).fetchone()[0])
    for e in data["events"]:
        e.pop("identity", None)
    conn.execute("UPDATE snapshots SET input_json=? WHERE review_id=? AND review_version=2",
                 (json.dumps(data, ensure_ascii=False), rid))
    conn.commit()
    conn.close()

    cmp = db.compare_versions(p, rid, 2, 3)
    assert cmp["pairable"] is False
    assert "缺少可靠的事件身份标识" in cmp["reason"]
    assert "events" not in cmp  # 不虚构逐事件差值
    # 各版本原貌仍可展示
    assert cmp["from_snapshot"]["input"]["events"][0]["name"] == "A"
    assert cmp["to_snapshot"]["input"]["events"][0]["name"] == "A2"
    assert cmp["title"] == {"from": "T", "to": "T2", "changed": True}
    # 对照 v3→v4（两侧快照都有身份）则正常配对
    key_a3 = snapshot_keys(p, rid, 3)[0]
    db.update_review(p, rid, 3, "T2",
                     [ev(key_a3, "A3", "2026-10-01T10:00", "2026-10-01T11:00", 10)], [])  # v4
    cmp2 = db.compare_versions(p, rid, 3, 4)
    assert cmp2["pairable"] is True
    assert cmp2["events"]["renamed"][0]["to_name"] == "A3"


def test_compare_is_read_only():
    p = setup_db()
    rid = db.create_review(p, "T")
    e1 = ev("a", "A", "2026-10-01T10:00", "2026-10-01T11:00", 10)
    db.update_review(p, rid, 1, "T", [e1], [])  # v2
    db.finalize_review(p, rid, 2)              # v3 定稿

    before = db.get_bundle(p, rid)
    cmp = db.compare_versions(p, rid, 2, 3)
    after = db.get_bundle(p, rid)
    # 当前输入、版本号、定稿状态、推算结果均未变
    assert before == after
    assert after["review"]["status"] == "finalized" and after["review"]["version"] == 3
    assert cmp["pairable"] is True
    assert cmp["current_version"] == 3
    assert cmp["from"]["state"] == "draft" and cmp["to"]["state"] == "finalized"
    # 版本列表也未新增记录
    assert [v["version"] for v in db.list_versions(p, rid)] == [3, 2, 1]


def test_existing_db_rows_get_identity_on_migrate():
    # 旧库（events 无 identity 列）启动迁移后：存量行补发身份，后续保存可配对
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    conn = sqlite3.connect(tmp.name)
    conn.executescript(
        "CREATE TABLE reviews (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,"
        " status TEXT NOT NULL DEFAULT 'draft', version INTEGER NOT NULL DEFAULT 1,"
        " created_at TEXT, updated_at TEXT);"
        "CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, review_id INTEGER,"
        " name TEXT, earliest_start TEXT, latest_start TEXT, duration_minutes INTEGER,"
        " note TEXT DEFAULT '');"
        "CREATE TABLE relations (id INTEGER PRIMARY KEY AUTOINCREMENT, review_id INTEGER,"
        " predecessor_id INTEGER, successor_id INTEGER);"
        "CREATE TABLE computations (id INTEGER PRIMARY KEY AUTOINCREMENT, review_id INTEGER,"
        " review_version INTEGER, status TEXT, result_json TEXT, computed_at TEXT);"
        "INSERT INTO reviews VALUES (1,'旧','draft',1,'t','t');"
        "INSERT INTO events VALUES (1,1,'A','2026-10-01T10:00','2026-10-01T11:00',10,'');")
    conn.commit()
    conn.close()

    db.init_db(tmp.name)  # 迁移：补 identity 列并补发身份
    bundle = db.get_bundle(tmp.name, 1)
    assert bundle["events"][0]["identity"]
    # 后续保存继承身份：v1（旧库，无快照不可对照）之后的 v2→v3 可正常配对
    key = "id_" + str(bundle["events"][0]["id"])
    db.update_review(tmp.name, 1, 1, "旧", [ev(key, "A", "2026-10-01T10:00",
                                              "2026-10-01T11:00", 10)], [])  # v2
    key2 = snapshot_keys(tmp.name, 1, 2)[0]
    db.update_review(tmp.name, 1, 2, "旧", [ev(key2, "A改", "2026-10-01T10:00",
                                               "2026-10-01T11:00", 10)], [])  # v3
    cmp = db.compare_versions(tmp.name, 1, 2, 3)
    assert cmp["pairable"] is True
    assert cmp["events"]["renamed"][0]["to_name"] == "A改"


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]

if __name__ == "__main__":
    failed = 0
    for t in TESTS:
        try:
            t()
            print("PASS", t.__name__)
        except AssertionError as e:
            failed += 1
            print("FAIL", t.__name__, "->", e)
    sys.exit(1 if failed else 0)
