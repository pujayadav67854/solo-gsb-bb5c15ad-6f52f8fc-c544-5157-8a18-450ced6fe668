"""跨版本对照：按事件持续身份配对，产出两个版本之间的差异视图。

配对只依赖快照中不可变的事件 identity（保存/恢复时生成并沿用，
改名、修改时间范围、恢复旧版本都不会改变它），绝不按名称或
会随保存重建的行号猜配。缺可靠身份的旧快照不做逐事件差值，
由调用方原样展示各版本内容（pairable=False）。

对照是纯函数：输入为 db.get_version 的回看内容，不写任何数据。
"""

WINDOW_FIELDS = ("earliest_start", "latest_start", "earliest_end", "latest_end")
INPUT_FIELDS = ("earliest_start", "latest_start", "duration_minutes", "note")


def _row_key(event):
    """快照事件 key（id_<当时行号>）对应的推算结果键（行号字符串）。"""
    key = str(event.get("key") or "")
    return key[3:] if key.startswith("id_") else key


def has_reliable_identity(saved_input):
    """快照内全部事件都带有持续身份标识时才可跨版本配对。"""
    return all(e.get("identity") for e in saved_input.get("events") or [])


def _index(saved_input):
    """为一份快照输入建立索引：identity/行号/key 之间的映射与名称表。"""
    by_identity = {}
    ident_by_row = {}
    ident_by_key = {}
    name_by_identity = {}
    for e in saved_input.get("events") or []:
        ident = e.get("identity")
        by_identity[ident] = e
        ident_by_row[_row_key(e)] = ident
        ident_by_key[e.get("key")] = ident
        name_by_identity[ident] = e.get("name")
    return {
        "by_identity": by_identity,
        "ident_by_row": ident_by_row,
        "ident_by_key": ident_by_key,
        "name_by_identity": name_by_identity,
    }


def _event_view(e, result, ident_by_row):
    """单个事件在某版本中的视图：输入、推算可行窗口、是否暂存。"""
    row = _row_key(e)
    window = (result.get("windows") or {}).get(row)
    draft = any(str(d.get("id")) == row for d in result.get("drafts") or [])
    return {
        "identity": e.get("identity"),
        "name": e.get("name"),
        "input": {f: e.get(f) for f in INPUT_FIELDS},
        "window": window,
        "draft": draft,
    }


def _relation_views(saved_input, idx):
    """关系列表：端点解析为事件持续身份；无法解析的归入 unpaired（不猜配）。"""
    views, unpaired = [], []
    for r in saved_input.get("relations") or []:
        pa = idx["ident_by_key"].get(r.get("predecessor_key"))
        pb = idx["ident_by_key"].get(r.get("successor_key"))
        if not pa or not pb:
            unpaired.append({
                "predecessor_key": r.get("predecessor_key"),
                "successor_key": r.get("successor_key"),
                "min_gap_minutes": r.get("min_gap_minutes"),
                "max_gap_minutes": r.get("max_gap_minutes"),
            })
            continue
        views.append({
            "pair": (pa, pb),
            "predecessor": {"identity": pa, "name": idx["name_by_identity"].get(pa)},
            "successor": {"identity": pb, "name": idx["name_by_identity"].get(pb)},
            "min_gap_minutes": r.get("min_gap_minutes") or 0,
            "max_gap_minutes": r.get("max_gap_minutes"),
        })
    return views, unpaired


def _rel_public(v):
    return {"predecessor": v["predecessor"], "successor": v["successor"],
            "min_gap_minutes": v["min_gap_minutes"], "max_gap_minutes": v["max_gap_minutes"]}


def _conflict_views(result, idx):
    """冲突列表：event_ids（当时行号）映射为事件持续身份，供跨版本对照。"""
    views = []
    for c in result.get("conflicts") or []:
        idents = []
        for i in c.get("event_ids") or []:
            ident = idx["ident_by_row"].get(str(i))
            if ident:
                idents.append(ident)
        views.append({
            "type": c.get("type"),
            "message": c.get("message"),
            "events": [{"identity": i, "name": idx["name_by_identity"].get(i)}
                       for i in idents],
            "key": (c.get("type"), tuple(sorted(idents))),
        })
    return views


def _draft_views(result, idx):
    return [{
        "identity": idx["ident_by_row"].get(str(d.get("id"))),
        "name": d.get("name"),
        "reason": d.get("reason"),
    } for d in result.get("drafts") or []]


def _key_diff(from_views, to_views):
    """按配对键比较两组视图，返回 (新增, 消除)；返回项不含内部键。"""
    from_keys = [v["key"] for v in from_views]
    to_keys = [v["key"] for v in to_views]
    new = [{k: v[k] for k in v if k != "key"} for v in to_views if v["key"] not in from_keys]
    resolved = [{k: v[k] for k in v if k != "key"} for v in from_views if v["key"] not in to_keys]
    return new, resolved


def diff_versions(from_version, to_version):
    """对照两个版本的回看内容（db.get_version 的返回结构）。

    返回 dict：
      pairable   是否可按事件持续身份逐事件配对
      title      {"from", "to", "changed"}
      status     {"from", "to", "changed"}     推算状态 ok/conflict
      可配对时另有 events/relations/conflicts/drafts 四组差异；
      不可配对时另有 reason 与 from_snapshot/to_snapshot 各版本原貌。
    """
    from_input = from_version.get("input") or {}
    to_input = to_version.get("input") or {}
    out = {
        "title": {"from": from_version.get("title"), "to": to_version.get("title"),
                  "changed": from_version.get("title") != to_version.get("title")},
        "status": {"from": from_version.get("status"), "to": to_version.get("status"),
                   "changed": from_version.get("status") != to_version.get("status")},
    }
    from_ok = has_reliable_identity(from_input)
    to_ok = has_reliable_identity(to_input)
    if not (from_ok and to_ok):
        lacking = []
        if not from_ok:
            lacking.append("v{}".format(from_version.get("version")))
        if not to_ok:
            lacking.append("v{}".format(to_version.get("version")))
        out.update({
            "pairable": False,
            "reason": "版本 {} 的输入快照缺少可靠的事件身份标识（升级前保存的旧快照），"
                      "无法按事件持续身份配对；以下仅展示各版本原貌，不提供逐事件差值。".format(
                          "、".join(lacking)),
            "from_snapshot": {"input": from_input, "result": from_version.get("result") or {}},
            "to_snapshot": {"input": to_input, "result": to_version.get("result") or {}},
        })
        return out

    from_idx = _index(from_input)
    to_idx = _index(to_input)
    from_events = from_idx["by_identity"]
    to_events = to_idx["by_identity"]
    from_result = from_version.get("result") or {}
    to_result = to_version.get("result") or {}

    # ---- 事件：新增 / 删除 / 改名 / 输入与可行窗口变化 --------------------
    added, removed, renamed, changed = [], [], [], []
    unchanged = 0
    for ident, te in to_events.items():
        if ident not in from_events:
            added.append(_event_view(te, to_result, to_idx["ident_by_row"]))
    for ident, fe in from_events.items():
        if ident not in to_events:
            removed.append(_event_view(fe, from_result, from_idx["ident_by_row"]))
    for ident, fe in from_events.items():
        te = to_events.get(ident)
        if te is None:
            continue
        fv = _event_view(fe, from_result, from_idx["ident_by_row"])
        tv = _event_view(te, to_result, to_idx["ident_by_row"])
        is_renamed = fe.get("name") != te.get("name")
        if is_renamed:
            renamed.append({"identity": ident, "from_name": fe.get("name"),
                            "to_name": te.get("name")})
        input_changes = {f: {"from": fv["input"].get(f), "to": tv["input"].get(f)}
                         for f in INPUT_FIELDS
                         if fv["input"].get(f) != tv["input"].get(f)}
        fw, tw = fv["window"], tv["window"]
        window_changes = {}
        for f in WINDOW_FIELDS:
            a = fw.get(f) if fw else None   # None = 该版本暂存，无可行窗口
            b = tw.get(f) if tw else None
            if a != b:
                window_changes[f] = {"from": a, "to": b}
        draft_change = None
        if fv["draft"] != tv["draft"]:
            draft_change = {"from": fv["draft"], "to": tv["draft"]}
        if input_changes or window_changes or draft_change:
            changed.append({
                "identity": ident,
                "from_name": fe.get("name"),
                "to_name": te.get("name"),
                "renamed": is_renamed,
                "input_changes": input_changes,
                "window_changes": window_changes,
                "draft_change": draft_change,
                "from_window": fw,
                "to_window": tw,
            })
        elif not is_renamed:
            unchanged += 1

    # ---- “先于”关系：按（前项身份, 后项身份）配对，比较间隔设置 -------------
    from_rels, from_unpaired = _relation_views(from_input, from_idx)
    to_rels, to_unpaired = _relation_views(to_input, to_idx)

    def group(views):
        g = {}
        for v in views:
            g.setdefault(v["pair"], []).append(v)
        return g

    from_groups, to_groups = group(from_rels), group(to_rels)
    pair_order = list(from_groups) + [p for p in to_groups if p not in from_groups]
    rel_added, rel_removed, rel_gap_changed = [], [], []
    for pair in pair_order:
        fl = from_groups.get(pair, [])
        tl = to_groups.get(pair, [])
        for i in range(max(len(fl), len(tl))):
            f = fl[i] if i < len(fl) else None
            t = tl[i] if i < len(tl) else None
            if f is None:
                rel_added.append(_rel_public(t))
            elif t is None:
                rel_removed.append(_rel_public(f))
            elif (f["min_gap_minutes"], f["max_gap_minutes"]) != \
                    (t["min_gap_minutes"], t["max_gap_minutes"]):
                rel_gap_changed.append({
                    "predecessor": t["predecessor"],
                    "successor": t["successor"],
                    "from": {"min_gap_minutes": f["min_gap_minutes"],
                             "max_gap_minutes": f["max_gap_minutes"]},
                    "to": {"min_gap_minutes": t["min_gap_minutes"],
                           "max_gap_minutes": t["max_gap_minutes"]},
                })

    # ---- 冲突与暂存状态变化 ------------------------------------------------
    from_conflicts = _conflict_views(from_result, from_idx)
    to_conflicts = _conflict_views(to_result, to_idx)
    conf_new, conf_resolved = _key_diff(from_conflicts, to_conflicts)
    from_drafts = _draft_views(from_result, from_idx)
    to_drafts = _draft_views(to_result, to_idx)
    from_draft_idents = {v["identity"] for v in from_drafts}
    to_draft_idents = {v["identity"] for v in to_drafts}
    # 解除暂存 = 原本暂存、新版本中仍存在且不再暂存；新增暂存同理
    drafts_resolved = [v for v in from_drafts
                       if v["identity"] not in to_draft_idents
                       and v["identity"] in to_events]
    drafts_new = [v for v in to_drafts
                  if v["identity"] not in from_draft_idents
                  and v["identity"] in from_events]

    out.update({
        "pairable": True,
        "events": {
            "added": added,
            "removed": removed,
            "renamed": renamed,
            "changed": changed,
            "unchanged_count": unchanged,
        },
        "relations": {
            "added": rel_added,
            "removed": rel_removed,
            "gap_changed": rel_gap_changed,
            "unpaired": {"from": from_unpaired, "to": to_unpaired},
        },
        "conflicts": {
            "from": [{k: v[k] for k in v if k != "key"} for v in from_conflicts],
            "to": [{k: v[k] for k in v if k != "key"} for v in to_conflicts],
            "new": conf_new,
            "resolved": conf_resolved,
        },
        "drafts": {
            "from": from_drafts,
            "to": to_drafts,
            "new": drafts_new,
            "resolved": drafts_resolved,
        },
    })
    return out
