# -*- coding: utf-8 -*-
"""verify.py — 校验 vault 库数据并演示 seekdb 混合检索。

用法:
  py -3.10 verify.py [--host 127.0.0.1 --port 2881 --user root --password ""]
  py -3.10 verify.py --query "乐观并发控制的验证阶段做什么"
"""
import argparse
import io
import sys

import pymysql

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")


def vec_literal(v):
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=20881)
    ap.add_argument("--user", default="root")
    ap.add_argument("--password", default="")
    ap.add_argument("--db", default="vault")
    ap.add_argument("--model", default="BAAI/bge-small-zh-v1.5")
    ap.add_argument("--query", default="乐观并发控制 OCC 验证阶段要检查什么")
    ap.add_argument("--keyword", default="时间戳 验证")
    args = ap.parse_args()

    conn = pymysql.connect(host=args.host, port=args.port, user=args.user,
                           password=args.password, database=args.db, charset="utf8mb4")
    cur = conn.cursor()

    print("========== 1) 各表行数 ==========")
    for t in ("notes", "chunks", "glossary", "links"):
        cur.execute(f"SELECT COUNT(*) FROM {t}")
        print(f"{t:<10}: {cur.fetchone()[0]}")

    print("\n========== 2) notes 按课程/类型 ==========")
    cur.execute("SELECT course, kind, COUNT(*) FROM notes GROUP BY course, kind ORDER BY course, kind")
    for course, kind, cnt in cur.fetchall():
        print(f"  {course or '(root)':<45} {kind:<10} {cnt}")

    print("\n========== 3) 随机抽 3 条 chunk ==========")
    cur.execute("""
        SELECT c.chunk_id, n.rel_path, c.heading_path, c.page_start, c.page_end,
               LEFT(c.content, 150)
        FROM chunks c JOIN notes n ON n.note_id = c.note_id
        ORDER BY RAND() LIMIT 3""")
    for cid, path, hp, ps, pe, preview in cur.fetchall():
        print(f"\n[chunk {cid}] {path}\n  heading: {hp}\n  page: {ps}-{pe}\n  {preview}...")

    print("\n========== 4) glossary 抽样 ==========")
    cur.execute("""SELECT n.course, g.term, g.term_en, LEFT(g.definition, 80)
                   FROM glossary g JOIN notes n ON n.note_id = g.note_id
                   ORDER BY RAND() LIMIT 3""")
    for course, term, en, d in cur.fetchall():
        print(f"  [{course}] {term} ({en}): {d}...")

    print("\n========== 5) links 有效性 ==========")
    cur.execute("SELECT valid, COUNT(*) FROM links GROUP BY valid")
    for v, c in cur.fetchall():
        print(f"  valid={v}: {c}")
    cur.execute("SELECT link_text, target_rel, anchor FROM links WHERE valid=0 LIMIT 3")
    rows = cur.fetchall()
    if rows:
        print("  无效链接示例:")
        for t, r, a in rows:
            print(f"    [{t}] -> {r} {a or ''}")

    print(f"\n========== 6) 向量检索: {args.query!r} ==========")
    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer(args.model)
    qv = vec_literal(model.encode([args.query], normalize_embeddings=True)[0])
    cur.execute(f"""
        SELECT c.chunk_id, n.rel_path, c.heading_path, c.page_start,
               l2_distance(c.emb, '{qv}') AS dist
        FROM chunks c JOIN notes n ON n.note_id = c.note_id
        ORDER BY l2_distance(c.emb, '{qv}') LIMIT 5""")
    for cid, path, hp, pg, dist in cur.fetchall():
        print(f"  d={float(dist):.4f} [chunk {cid}] p{pg} {path}\n      {hp}")

    print(f"\n========== 7) 全文检索: {args.keyword!r} ==========")
    cur.execute("""
        SELECT c.chunk_id, n.rel_path, c.heading_path
        FROM chunks c JOIN notes n ON n.note_id = c.note_id
        WHERE MATCH(c.content) AGAINST (%s IN BOOLEAN MODE)
        LIMIT 5""", (args.keyword,))
    for cid, path, hp in cur.fetchall():
        print(f"  [chunk {cid}] {path}\n      {hp}")

    print(f"\n========== 8) 混合检索(单条SQL: 全文过滤+向量排序): kw={args.keyword!r} q={args.query!r} ==========")
    cur.execute(f"""
        SELECT c.chunk_id, n.rel_path, c.heading_path, c.page_start,
               l2_distance(c.emb, '{qv}') AS dist
        FROM chunks c JOIN notes n ON n.note_id = c.note_id
        WHERE MATCH(c.content) AGAINST (%s IN BOOLEAN MODE)
        ORDER BY l2_distance(c.emb, '{qv}') LIMIT 5""", (args.keyword,))
    for cid, path, hp, pg, dist in cur.fetchall():
        print(f"  d={float(dist):.4f} [chunk {cid}] p{pg} {path}\n      {hp}")

    conn.close()


if __name__ == "__main__":
    main()
