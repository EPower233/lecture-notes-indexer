# -*- coding: utf-8 -*-
"""tools/capture_snapshot.py — 从本机 live seekdb API 抓取展示站快照数据。

Qoder Sites 的后端是 Supabase/Deno，跑不了 seekdb 的单 SQL 向量+ik 全文融合，
也装不了 Python bge 模型。故公开站采用「静态展示 + 真实快照」形态：本脚本把
live 栈的真实检索结果/笔记/术语抓成 JSON，供 site/ 前端离线演示（明确标注为
本机 seekdb 实测数据，非编造）。

隐私白名单：公开展示只保留 CMU 与 Mathematics 两类课程，其余笔记（含个人
笔记）一律不抓取、不进入快照。统计数也按白名单子集重新计算，避免泄露总量。

用法:
  py -3.10 tools/capture_snapshot.py            # 默认 http://127.0.0.1:8000
  py -3.10 tools/capture_snapshot.py --base http://127.0.0.1:8000
"""
import argparse
import json
import urllib.parse
import urllib.request
from pathlib import Path

import pymysql

# seekdb 连接（与 serve.py 一致；仅本机、root 空密码）
DB_CONF = dict(host="127.0.0.1", port=20881, user="root", password="",
               database="vault", charset="utf8mb4")

# 隐私白名单：只公开这两类课程前缀
ALLOW_PREFIXES = ("CMU", "Mathematics")


def allowed(course) -> bool:
    return bool(course) and any(course.startswith(p) for p in ALLOW_PREFIXES)


# 预置演示查询：仅覆盖 CMU15-445（数据库）与 Mathematics（概率统计），
# 均为 live 栈实测能命中好结果的真实问题
DEMO_QUERIES = [
    {"q": "两阶段锁的死锁怎么检测和处理", "mode": "hybrid", "k": 6},
    {"q": "MVCC 多版本并发控制如何避免回滚", "mode": "hybrid", "k": 6},
    {"q": "缓冲池管理 LRU 替换策略", "mode": "hybrid", "k": 6},
    {"q": "B+树索引的顺序扫描与范围查询", "mode": "hybrid", "k": 6},
    {"q": "大数定律与中心极限定理", "mode": "hybrid", "k": 6},
    {"q": "二维随机变量的联合分布函数", "mode": "hybrid", "k": 6},
    # 展示三种检索模式的差异（同一查询）
    {"q": "两阶段锁的死锁怎么检测和处理", "mode": "vector", "k": 6},
    {"q": "两阶段锁的死锁怎么检测和处理", "mode": "fts", "k": 6},
]

DEMO_TERMS = ["时间戳", "死锁", "MVCC", "B+ 树", "缓冲池", "事务"]

# 浏览页/结果页可离线打开的代表笔记（note_id），额外补充几篇讲次笔记
EXTRA_NOTE_IDS = [1, 2]


def get(base, path):
    url = base + path
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def dump(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"  wrote {path}  ({path.stat().st_size/1024:.1f} KB)")


def filtered_stats():
    """按白名单子集重新计算统计，避免泄露全库总量。"""
    conn = pymysql.connect(**DB_CONF)
    try:
        cur = conn.cursor()
        where = " OR ".join(["n.course LIKE %s"] * len(ALLOW_PREFIXES))
        like = [p + "%" for p in ALLOW_PREFIXES]

        cur.execute(f"SELECT COUNT(*) FROM notes n WHERE {where}", like)
        notes = cur.fetchone()[0]

        cur.execute(
            f"SELECT COUNT(*) FROM chunks c JOIN notes n ON n.note_id=c.note_id "
            f"WHERE {where}", like)
        chunks = cur.fetchone()[0]

        cur.execute(
            f"SELECT COUNT(*) FROM glossary g JOIN notes n ON n.note_id=g.note_id "
            f"WHERE {where}", like)
        glossary = cur.fetchone()[0]

        cur.execute(
            f"SELECT COUNT(*) FROM links l JOIN notes n ON n.note_id=l.src_note_id "
            f"WHERE {where}", like)
        links = cur.fetchone()[0]

        cur.execute(
            f"SELECT l.valid, COUNT(*) FROM links l JOIN notes n ON n.note_id=l.src_note_id "
            f"WHERE {where} GROUP BY l.valid", like)
        links_valid = {str(r[0]): r[1] for r in cur.fetchall()}

        return {"notes": notes, "chunks": chunks, "glossary": glossary,
                "links": links, "links_valid": links_valid}
    finally:
        conn.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--out", default="site/data")
    args = ap.parse_args()
    base, out = args.base, Path(args.out)

    print(f"capturing from {base} -> {out}/  (allowlist={ALLOW_PREFIXES})")

    # 1) 元信息：白名单子集统计 + 课程（仅白名单）
    courses_all = get(base, "/api/courses")["courses"]
    courses = [c for c in courses_all if allowed(c.get("course"))]
    meta = {"stats": filtered_stats(), "courses": courses}
    dump(out / "meta.json", meta)

    # 2) 笔记列表（仅白名单）
    notes_all = get(base, "/api/notes?limit=1000")["notes"]
    notes = [n for n in notes_all if allowed(n.get("course"))]
    dump(out / "notes.json", {"notes": notes})
    print(f"  notes kept {len(notes)}/{len(notes_all)}")

    # 3) 预置查询的真实结果（结果按白名单过滤）
    searches = []
    hit_note_ids = set()
    for dq in DEMO_QUERIES:
        qs = urllib.parse.urlencode({"q": dq["q"], "mode": dq["mode"], "k": dq["k"]})
        res = get(base, "/api/search?" + qs)
        rows = [r for r in res.get("results", []) if allowed(r.get("course"))]
        searches.append({"q": dq["q"], "mode": dq["mode"], "k": dq["k"],
                         "count": len(rows), "results": rows})
        for r in rows:
            if r.get("note_id") is not None:
                hit_note_ids.add(r["note_id"])
    dump(out / "searches.json", {"searches": searches})

    # 4) 术语速查（结果按白名单过滤）
    terms = []
    for t in DEMO_TERMS:
        res = get(base, "/api/term?t=" + urllib.parse.quote(t) + "&k=6")
        rows = [r for r in res.get("results", []) if allowed(r.get("course"))]
        terms.append({"t": t, "results": rows})
    dump(out / "terms.json", {"terms": terms})

    # 5) 代表笔记完整详情（仅白名单 note_id；chunks 带正文 + glossary + links）
    keep_ids = {n["note_id"] for n in notes}
    note_ids = sorted((hit_note_ids | set(EXTRA_NOTE_IDS)) & keep_ids)
    print(f"  capturing {len(note_ids)} note details: {note_ids}")
    index = {}
    for nid in note_ids:
        note = get(base, f"/api/note?id={nid}")
        chunks = get(base, f"/api/note/{nid}/chunks?content=1")["chunks"]
        gloss = get(base, f"/api/note/{nid}/glossary")["terms"]
        links = get(base, f"/api/note/{nid}/links")
        detail = {"note": note, "chunks": chunks, "glossary": gloss,
                  "links_out": links.get("out", []), "links_in": links.get("in", [])}
        dump(out / "notes" / f"{nid}.json", detail)
        index[str(nid)] = {"rel_path": note.get("rel_path"), "title": note.get("title")}

    # 6) rel_path -> note_id 映射（仅白名单），供离线跨笔记跳转解析
    path_map = {n["rel_path"]: n["note_id"] for n in notes if n.get("rel_path")}
    dump(out / "index.json",
         {"captured_note_ids": note_ids, "note_index": index, "path_map": path_map})

    print("done.")


if __name__ == "__main__":
    main()
