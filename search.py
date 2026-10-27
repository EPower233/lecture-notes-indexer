# -*- coding: utf-8 -*-
"""search.py — 讲义库检索核心 + CLI。

混合检索在 seekdb 单条 SQL 内完成：全文相关度 MATCH() 与向量距离 l2_distance()
加权融合排序，结构化过滤（课程/类型/讲次）同条 SQL 下推。

CLI:
  py -3.10 search.py "MVCC 如何避免回滚" --k 5
  py -3.10 search.py "两阶段锁的死锁怎么检测和处理" --mode vector --course CMU15445_Final
  py -3.10 search.py --term 时间戳
  py -3.10 search.py --note 42
"""
import argparse
import io
import re
import sys
import threading

import pymysql

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

BGE_QUERY_INSTRUCTION = "为这个句子生成表示以用于检索相关文章："
_FTS_STRIP_RE = re.compile(r"[^\w\u4e00-\u9fff\s]", re.UNICODE)

SELECT_COLS = """
    c.chunk_id, c.note_id, n.rel_path, n.course, n.course_code, n.lecture_no, n.kind,
    n.title, c.heading_path, c.page_start, c.page_end, LEFT(c.content, 220)"""


def fts_query(raw: str) -> str:
    """清洗成 BOOLEAN MODE 安全查询：去掉 +-><~*:"() 等运算符，词间空格=OR。"""
    return " ".join(_FTS_STRIP_RE.sub(" ", raw).split())


class Searcher:
    def __init__(self, host="127.0.0.1", port=20881, user="root", password="",
                 db="vault", model="BAAI/bge-small-zh-v1.5"):
        self.conn_args = dict(host=host, port=port, user=user, password=password,
                              database=db, charset="utf8mb4")
        self.model_name = model
        self._local = threading.local()
        self._model = None

    @property
    def conn(self):
        # pymysql 连接非线程安全，FastAPI 线程池并发请求需每线程一条连接
        c = getattr(self._local, "conn", None)
        if c is None or not c.open:
            c = pymysql.connect(**self.conn_args)
            self._local.conn = c
        return c

    @property
    def model(self):
        if self._model is None:
            from sentence_transformers import SentenceTransformer
            self._model = SentenceTransformer(self.model_name)
        return self._model

    def embed_query(self, query: str) -> str:
        v = self.model.encode([BGE_QUERY_INSTRUCTION + query],
                              normalize_embeddings=True)[0]
        return "[" + ",".join(f"{x:.6f}" for x in v) + "]"

    def _filters(self, course, kind, lecture_no):
        where, params = [], []
        if course:
            where.append("n.course = %s"); params.append(course)
        if kind:
            where.append("n.kind = %s"); params.append(kind)
        if lecture_no is not None:
            where.append("n.lecture_no = %s"); params.append(int(lecture_no))
        return ("WHERE " + " AND ".join(where)) if where else "", params

    def search(self, query, mode="hybrid", k=10, alpha=0.5,
               course=None, kind=None, lecture_no=None, keywords=None):
        """mode: hybrid | vector | fts。返回 list[dict]。"""
        assert mode in ("hybrid", "vector", "fts")
        assert 0.0 <= alpha <= 1.0
        k = max(1, min(int(k), 100))
        where, fparams = self._filters(course, kind, lecture_no)
        cur = self.conn.cursor()
        rows = []

        if mode == "vector":
            vec = self.embed_query(query)
            sql = (f"SELECT{SELECT_COLS}, l2_distance(c.emb, %s) AS dist, NULL AS rel"
                   f" FROM chunks c JOIN notes n ON n.note_id = c.note_id {where}"
                   f" ORDER BY l2_distance(c.emb, %s) LIMIT %s")
            # pymysql 按 %s 在 SQL 文本中的出现顺序取值：WHERE 在 SELECT 之后
            cur.execute(sql, [vec] + fparams + [vec, k])
            for r in cur.fetchall():
                rows.append(self._row(r, score=None))

        elif mode == "fts":
            kw = fts_query(keywords or query)
            cond = (where + " AND ") if where else "WHERE "
            # 自然语言模式：ik 对查询侧自动分词，中文召回显著好于 BOOLEAN MODE
            sql = (f"SELECT{SELECT_COLS}, NULL AS dist,"
                   f" MATCH(c.content) AGAINST (%s) AS rel"
                   f" FROM chunks c JOIN notes n ON n.note_id = c.note_id"
                   f" {cond} MATCH(c.content) AGAINST (%s)"
                   f" ORDER BY rel DESC LIMIT %s")
            cur.execute(sql, [kw] + fparams + [kw, k])
            for r in cur.fetchall():
                rows.append(self._row(r, score=None))

        else:  # hybrid：单条 SQL，全文分 + 向量分加权融合
            vec = self.embed_query(query)
            kw = fts_query(keywords or query)
            rel = "MATCH(c.content) AGAINST (%s)"
            dist = "l2_distance(c.emb, %s)"
            score = f"(1-%s)*{rel}/(1+{rel}) + %s*(1/(1+{dist}))"
            sql = (f"SELECT{SELECT_COLS}, {dist} AS dist, {rel} AS rel,"
                   f" {score} AS score"
                   f" FROM chunks c JOIN notes n ON n.note_id = c.note_id {where}"
                   f" ORDER BY score DESC LIMIT %s")
            # SELECT 内占位符顺序: dist(vec), rel(kw), score:(1-a)→a, rel(kw), rel(kw), a, dist(vec)
            cur.execute(sql, [vec, kw, alpha, kw, kw, alpha, vec] + fparams + [k])
            for r in cur.fetchall():
                rows.append(self._row(r, score=float(r[-1])))
        return rows

    @staticmethod
    def _row(r, score):
        keys = ["chunk_id", "note_id", "rel_path", "course", "course_code",
                "lecture_no", "kind", "title", "heading_path", "page_start",
                "page_end", "snippet", "dist", "rel"]
        d = dict(zip(keys, r[:14]))
        d["snippet"] = (d["snippet"] or "").replace("\n", " ")[:200]
        d["dist"] = float(d["dist"]) if d["dist"] is not None else None
        d["rel"] = float(d["rel"]) if d["rel"] is not None else None
        d["score"] = score
        return d

    def search_term(self, term, k=10):
        cur = self.conn.cursor()
        cur.execute(
            "SELECT g.glossary_id, g.term, g.term_en, g.definition, g.introduced_in,"
            " n.course, n.lecture_no, n.rel_path"
            " FROM glossary g JOIN notes n ON n.note_id = g.note_id"
            " WHERE g.term = %s OR g.term_en LIKE %s ORDER BY g.term = %s DESC"
            " LIMIT %s", (term, f"%{term}%", term, k))
        keys = ["glossary_id", "term", "term_en", "definition", "introduced_in",
                "course", "lecture_no", "rel_path"]
        return [dict(zip(keys, r)) for r in cur.fetchall()]

    def get_note(self, note_id=None, rel_path=None):
        cur = self.conn.cursor()
        if note_id is not None:
            cur.execute("SELECT * FROM notes WHERE note_id = %s", (note_id,))
        else:
            cur.execute("SELECT * FROM notes WHERE rel_path = %s", (rel_path,))
        row = cur.fetchone()
        if not row:
            return None
        cols = [d[0] for d in cur.description]
        note = dict(zip(cols, row))
        cur.execute("SELECT COUNT(*), MIN(seq), MAX(seq) FROM chunks WHERE note_id = %s",
                    (note["note_id"],))
        note["chunk_count"], note["seq_min"], note["seq_max"] = cur.fetchone()
        cur.execute("SELECT COUNT(*), SUM(valid) FROM links WHERE src_note_id = %s",
                    (note["note_id"],))
        note["links_out"], note["links_out_valid"] = cur.fetchone()
        cur.execute("SELECT COUNT(*) FROM links WHERE dst_note_id = %s", (note["note_id"],))
        note["links_in"] = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM glossary WHERE note_id = %s", (note["note_id"],))
        note["glossary_count"] = cur.fetchone()[0]
        return note

    def courses(self):
        cur = self.conn.cursor()
        cur.execute("SELECT course, COUNT(*) FROM notes GROUP BY course ORDER BY course")
        return [{"course": c, "notes": n} for c, n in cur.fetchall()]

    def stats(self):
        cur = self.conn.cursor()
        out = {}
        for t in ("notes", "chunks", "glossary", "links"):
            cur.execute(f"SELECT COUNT(*) FROM {t}")
            out[t] = cur.fetchone()[0]
        cur.execute("SELECT valid, COUNT(*) FROM links GROUP BY valid")
        out["links_valid"] = {str(v): c for v, c in cur.fetchall()}
        return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("query", nargs="?", default=None)
    ap.add_argument("--mode", default="hybrid", choices=["hybrid", "vector", "fts"])
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--alpha", type=float, default=0.5, help="hybrid 中向量分权重")
    ap.add_argument("--course", default=None)
    ap.add_argument("--kind", default=None, choices=[None, "lecture", "transcript", "other"])
    ap.add_argument("--lecture", type=int, default=None)
    ap.add_argument("--keywords", default=None, help="覆盖全文检索词（默认用 query）")
    ap.add_argument("--term", default=None, help="查术语表")
    ap.add_argument("--note", default=None, help="按 note_id 或 rel_path 看笔记详情")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=20881)
    ap.add_argument("--user", default="root")
    ap.add_argument("--password", default="")
    ap.add_argument("--db", default="vault")
    args = ap.parse_args()

    s = Searcher(args.host, args.port, args.user, args.password, args.db)

    if args.term:
        for g in s.search_term(args.term):
            print(f"[{g['course']} L{g['lecture_no']}] {g['term']} ({g['term_en']})"
                  f" 引入于:{g['introduced_in']}\n  {g['definition']}\n  {g['rel_path']}")
        return

    if args.note:
        nid = int(args.note) if args.note.isdigit() else None
        note = s.get_note(note_id=nid, rel_path=None if nid else args.note)
        if not note:
            print("note not found")
            return
        for k, v in note.items():
            print(f"  {k}: {v}")
        return

    if not args.query:
        ap.error("需要 query 或 --term/--note")

    rows = s.search(args.query, mode=args.mode, k=args.k, alpha=args.alpha,
                    course=args.course, kind=args.kind, lecture_no=args.lecture,
                    keywords=args.keywords)
    print(f"== {args.mode} 检索: {args.query!r} (k={args.k}, alpha={args.alpha},"
          f" course={args.course}, kind={args.kind}, lecture={args.lecture}) ==")
    for i, r in enumerate(rows, 1):
        score = f"score={r['score']:.4f} " if r["score"] is not None else ""
        dist = f"d={r['dist']:.4f} " if r["dist"] is not None else ""
        rel = f"rel={r['rel']:.2f} " if r["rel"] is not None else ""
        page = f"p{r['page_start']}-{r['page_end']}" if r["page_start"] else "p?"
        print(f"\n{i}. {score}{dist}{rel}[chunk {r['chunk_id']}] {page}"
              f" {r['course']} L{r['lecture_no']} ({r['kind']})")
        print(f"   {r['heading_path']}")
        print(f"   {r['snippet']}...")


if __name__ == "__main__":
    main()
