#!/usr/bin/env python3
"""事故复盘时间线 —— HTTP 服务端（仅依赖 Python 标准库）。

路由：
  GET    /                        单页应用
  GET    /static/*                前端静态资源
  GET    /api/reviews             复盘列表
  POST   /api/reviews             新建复盘  {title}
  GET    /api/reviews/{id}        复盘快照（输入 + 最近一次推算结果）
  PUT    /api/reviews/{id}        整体保存并重新推算  {base_version, title, events, relations}
  POST   /api/reviews/{id}/finalize  定稿  {base_version}
  GET    /api/reviews/{id}/versions       可回看版本列表（含不可回看的旧库历史版本标记）
  GET    /api/reviews/{id}/versions/{v}   某版本回看：当时输入 + 推算依据 + 状态
  POST   /api/reviews/{id}/versions/{v}/restore  恢复为新草稿版本 {base_version}
  DELETE /api/reviews/{id}        删除复盘

并发控制：所有修改请求必须携带 base_version，与服务器当前版本一致才接受，
否则返回 409，客户端提示刷新。
"""

import json
import os
import re
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import db

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8000"))
DB_PATH = os.environ.get("DB_PATH", str(BASE_DIR.parent / "data" / "timeline.db"))

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
}


class ApiError(Exception):
    def __init__(self, status, message, **extra):
        super().__init__(message)
        self.status = status
        self.payload = {"error": message, **extra}


def validate_event(e):
    """校验并规范化单个事件输入，返回规范后的 dict。"""
    if not isinstance(e, dict):
        raise ApiError(400, "事件格式不正确")
    name = str(e.get("name") or "").strip()
    if not name:
        raise ApiError(400, "事件名称不能为空")
    if len(name) > 200:
        raise ApiError(400, "事件名称过长")

    def norm_time(value, label):
        if value in (None, ""):
            return None
        try:
            return datetime.fromisoformat(str(value)).isoformat(timespec="minutes")
        except ValueError:
            raise ApiError(400, "事件「{}」的{}格式不正确".format(name, label))

    duration = e.get("duration_minutes")
    if duration in (None, ""):
        duration = None
    else:
        try:
            duration = int(duration)
        except (TypeError, ValueError):
            raise ApiError(400, "事件「{}」的持续分钟数必须是整数".format(name))
        if not 0 <= duration <= 24 * 60 * 30:
            raise ApiError(400, "事件「{}」的持续分钟数超出合理范围".format(name))

    return {
        "key": str(e.get("key") or ""),
        "name": name,
        "earliest_start": norm_time(e.get("earliest_start"), "最早开始时间"),
        "latest_start": norm_time(e.get("latest_start"), "最晚开始时间"),
        "duration_minutes": duration,
        "note": str(e.get("note") or "")[:1000],
    }


def _norm_gap(value, label, rel_desc):
    """规范化“先于”关系的间隔分钟数。

    返回 None 表示未设置；否则为非负整数。非法输入（负数、非整数、
    超出合理范围）抛 ApiError(400)，在写库前拒绝，记录保持不变。
    """
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ApiError(400, "关系{}的{}必须是非负整数分钟".format(rel_desc, label))
    if isinstance(value, int):
        iv = value
    elif isinstance(value, float) and value.is_integer():
        iv = int(value)
    elif isinstance(value, str):
        try:
            iv = int(value.strip())
        except ValueError:
            raise ApiError(400, "关系{}的{}必须是非负整数分钟".format(rel_desc, label))
    else:
        raise ApiError(400, "关系{}的{}必须是非负整数分钟".format(rel_desc, label))
    if iv < 0:
        raise ApiError(400, "关系{}的{}必须是非负整数分钟".format(rel_desc, label))
    if iv > 24 * 60 * 30:
        raise ApiError(400, "关系{}的{}超出合理范围（0～{} 分钟）".format(
            rel_desc, label, 24 * 60 * 30))
    return iv


def validate_payload(body):
    if not isinstance(body, dict):
        raise ApiError(400, "请求体必须是 JSON 对象")
    if not isinstance(body.get("base_version"), int):
        raise ApiError(400, "缺少 base_version（用于并发版本校验）")
    title = str(body.get("title") or "").strip() or "未命名复盘"
    events = [validate_event(e) for e in body.get("events") or []]
    keys = [e["key"] for e in events]
    if any(not k for k in keys) or len(set(keys)) != len(keys):
        raise ApiError(400, "事件 key 缺失或重复")
    key_set = set(keys)
    name_by_key = {e["key"]: e["name"] for e in events}
    relations = []
    for r in body.get("relations") or []:
        if not isinstance(r, dict):
            raise ApiError(400, "“先于”关系格式不正确")
        a, b = str(r.get("predecessor_key") or ""), str(r.get("successor_key") or "")
        if a not in key_set or b not in key_set:
            raise ApiError(400, "“先于”关系引用了不存在的事件")
        desc = "「{}」先于「{}」".format(name_by_key[a], name_by_key[b])
        min_gap = _norm_gap(r.get("min_gap_minutes"), "最短等待", desc)
        if min_gap is None:
            min_gap = 0  # 未设置：沿用旧语义（前项结束不晚于后项开始）
        max_gap = _norm_gap(r.get("max_gap_minutes"), "最长等待", desc)
        if max_gap is not None and max_gap < min_gap:
            raise ApiError(400, "关系{}的最长等待不得短于最短等待".format(desc))
        relations.append({
            "predecessor_key": a,
            "successor_key": b,
            "min_gap_minutes": min_gap,
            "max_gap_minutes": max_gap,
        })
    return body["base_version"], title, events, relations


class Handler(BaseHTTPRequestHandler):
    server_version = "RetroTimeline/1.0"
    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------ 工具
    def log_message(self, fmt, *args):
        pass  # 静默访问日志

    def _send_json(self, status, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path):
        try:
            data = path.read_bytes()
        except OSError:
            self._send_json(404, {"error": "not found"})
            return
        ctype = CONTENT_TYPES.get(path.suffix, "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 2 * 1024 * 1024:
            raise ApiError(400, "请求体为空或过大")
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ApiError(400, "请求体不是合法的 JSON")

    def _dispatch(self, method):
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        try:
            if method == "GET" and path == "/":
                return self._send_file(STATIC_DIR / "index.html")
            m = re.fullmatch(r"/static/([\w.\-]+)", path)
            if method == "GET" and m:
                target = (STATIC_DIR / m.group(1)).resolve()
                if target.is_file() and STATIC_DIR in target.parents:
                    return self._send_file(target)
                raise ApiError(404, "not found")

            if path == "/api/reviews":
                if method == "GET":
                    return self._send_json(200, {"reviews": db.list_reviews(DB_PATH)})
                if method == "POST":
                    body = self._read_json()
                    title = str(body.get("title") or "").strip() or "未命名复盘"
                    rid = db.create_review(DB_PATH, title)
                    return self._send_json(201, db.get_bundle(DB_PATH, rid))

            m = re.fullmatch(r"/api/reviews/(\d+)", path)
            if m:
                rid = int(m.group(1))
                if method == "GET":
                    return self._send_json(200, db.get_bundle(DB_PATH, rid))
                if method == "PUT":
                    base_version, title, events, relations = validate_payload(self._read_json())
                    db.update_review(DB_PATH, rid, base_version, title, events, relations)
                    return self._send_json(200, db.get_bundle(DB_PATH, rid))
                if method == "DELETE":
                    db.delete_review(DB_PATH, rid)
                    return self._send_json(200, {"ok": True})

            m = re.fullmatch(r"/api/reviews/(\d+)/finalize", path)
            if m and method == "POST":
                rid = int(m.group(1))
                body = self._read_json()
                if not isinstance(body.get("base_version"), int):
                    raise ApiError(400, "缺少 base_version")
                db.finalize_review(DB_PATH, rid, body["base_version"])
                return self._send_json(200, db.get_bundle(DB_PATH, rid))

            m = re.fullmatch(r"/api/reviews/(\d+)/versions", path)
            if m and method == "GET":
                rid = int(m.group(1))
                return self._send_json(200, {
                    "current_version": db.current_version(DB_PATH, rid),
                    "versions": db.list_versions(DB_PATH, rid),
                })

            m = re.fullmatch(r"/api/reviews/(\d+)/versions/(\d+)", path)
            if m and method == "GET":
                rid, ver = int(m.group(1)), int(m.group(2))
                return self._send_json(200, db.get_version(DB_PATH, rid, ver))

            m = re.fullmatch(r"/api/reviews/(\d+)/versions/(\d+)/restore", path)
            if m and method == "POST":
                rid, ver = int(m.group(1)), int(m.group(2))
                body = self._read_json()
                if not isinstance(body.get("base_version"), int):
                    raise ApiError(400, "缺少 base_version（用于并发版本校验）")
                db.restore_review(DB_PATH, rid, body["base_version"], ver)
                return self._send_json(200, db.get_bundle(DB_PATH, rid))

            raise ApiError(404, "not found")
        except ApiError as e:
            self._send_json(e.status, e.payload)
        except db.NotFound:
            self._send_json(404, {"error": "复盘不存在"})
        except db.VersionNotFound:
            self._send_json(404, {"error": "该版本不存在"})
        except db.LegacyVersion:
            self._send_json(422, {"error": db.LEGACY_REASON,
                                  "reviewable": False, "restorable": False})
        except db.VersionConflict as e:
            self._send_json(409, {
                "error": "该复盘已被其他成员保存了更新的版本，你的修改未保存，请刷新后重试",
                "current_version": e.current_version,
            })
        except db.FinalizeRejected as e:
            self._send_json(400, {"error": str(e)})

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_DELETE(self):
        self._dispatch("DELETE")


def main():
    db.init_db(DB_PATH)  # 启动时初始化数据库
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print("事故复盘时间线服务已启动: http://{}:{}  (数据库: {})".format(HOST, PORT, DB_PATH))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
