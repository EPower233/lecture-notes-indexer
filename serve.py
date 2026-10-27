# -*- coding: utf-8 -*-
"""serve.py — 讲义库检索 HTTP API（FastAPI + seekdb）。

启动:
  py -3.10 -m uvicorn serve:app --host 127.0.0.1 --port 8000

接口:
  GET /health
  GET /api/search?q=...&mode=hybrid|vector|fts&k=10&alpha=0.5&course=&kind=&lecture=&kw=
  GET /api/term?t=时间戳
  GET /api/note?id=42            (或 ?path=CMU/.../xxx.md)
  GET /api/note/{note_id}/chunks
  GET /api/chunk/{chunk_id}
  GET /api/courses
  GET /api/stats
"""
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import pymysql
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from search import Searcher

HOST = os.environ.get("SEEKDB_HOST", "127.0.0.1")
PORT = int(os.environ.get("SEEKDB_PORT", "20881"))
USER = os.environ.get("SEEKDB_USER", "root")
PASSWORD = os.environ.get("SEEKDB_PASSWORD", "")
DB = os.environ.get("SEEKDB_DB", "vault")
VAULT_DIR = Path(os.environ.get("VAULT_DIR", "E:/Github/Obsidian-Vault")).resolve()
WEB_DIR = Path(__file__).parent / "web"

state: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = Searcher(HOST, PORT, USER, PASSWORD, DB)
    s.model  # 预热模型，避免首个请求超时
    s.embed_query("warmup")
    state["searcher"] = s
    yield
    state.clear()


app = FastAPI(title="讲义库索引器 API", version="0.1.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                   allow_headers=["*"])


def searcher() -> Searcher:
    s = state.get("searcher")
    if s is None:
        raise HTTPException(503, "searcher not ready")
    return s


@app.get("/health")
def health():
    s = searcher()
    cur = s.conn.cursor()
    cur.execute("SELECT COUNT(*) FROM chunks")
    return {"status": "ok", "chunks": cur.fetchone()[0]}


@app.get("/api/search")
def api_search(
    q: str = Query(..., min_length=1),
    mode: str = Query("hybrid", pattern="^(hybrid|vector|fts)$"),
    k: int = Query(10, ge=1, le=100),
    alpha: float = Query(0.5, ge=0.0, le=1.0, description="hybrid 中向量分权重"),
    course: Optional[str] = None,
    kind: Optional[str] = Query(None, pattern="^(lecture|transcript|other)$"),
    lecture: Optional[int] = None,
    kw: Optional[str] = Query(None, description="覆盖全文检索词"),
):
    rows = searcher().search(q, mode=mode, k=k, alpha=alpha, course=course,
                             kind=kind, lecture_no=lecture, keywords=kw)
    return {"query": q, "mode": mode, "alpha": alpha, "count": len(rows),
            "results": rows}


@app.get("/api/term")
def api_term(t: str = Query(..., min_length=1), k: int = Query(10, ge=1, le=50)):
    return {"term": t, "results": searcher().search_term(t, k)}


@app.get("/api/note")
def api_note(id: Optional[int] = None, path: Optional[str] = None):
    if id is None and not path:
        raise HTTPException(400, "need id or path")
    note = searcher().get_note(note_id=id, rel_path=path)
    if not note:
        raise HTTPException(404, "note not found")
    return note


@app.get("/api/note/{note_id}/chunks")
def api_note_chunks(note_id: int, content: bool = False):
    cur = searcher().conn.cursor()
    cols = ("chunk_id, seq, heading_path, page_start, page_end, char_count"
            + (", content" if content else ""))
    cur.execute(f"SELECT {cols} FROM chunks WHERE note_id = %s ORDER BY seq", (note_id,))
    keys = ["chunk_id", "seq", "heading_path", "page_start", "page_end",
            "char_count"] + (["content"] if content else [])
    return {"note_id": note_id,
            "chunks": [dict(zip(keys, r)) for r in cur.fetchall()]}


@app.get("/api/note/{note_id}/glossary")
def api_note_glossary(note_id: int):
    cur = searcher().conn.cursor()
    cur.execute("SELECT glossary_id, term, term_en, definition, first_seen,"
                " introduced_in FROM glossary WHERE note_id = %s ORDER BY glossary_id",
                (note_id,))
    keys = ["glossary_id", "term", "term_en", "definition", "first_seen",
            "introduced_in"]
    return {"note_id": note_id,
            "terms": [dict(zip(keys, r)) for r in cur.fetchall()]}


@app.get("/api/note/{note_id}/links")
def api_note_links(note_id: int):
    cur = searcher().conn.cursor()
    cur.execute("SELECT l.link_id, l.link_text, l.target_rel, l.anchor, l.valid,"
                " l.src_chunk_id, n.title"
                " FROM links l LEFT JOIN notes n ON n.note_id = l.dst_note_id"
                " WHERE l.src_note_id = %s ORDER BY l.link_id", (note_id,))
    keys = ["link_id", "link_text", "target_rel", "anchor", "valid",
            "src_chunk_id", "dst_title"]
    out = [dict(zip(keys, r)) for r in cur.fetchall()]
    cur.execute("SELECT l.link_id, l.link_text, l.anchor, l.src_chunk_id, n.title,"
                " n.rel_path"
                " FROM links l JOIN notes n ON n.note_id = l.src_note_id"
                " WHERE l.dst_note_id = %s ORDER BY l.link_id", (note_id,))
    keys = ["link_id", "link_text", "anchor", "src_chunk_id", "src_title",
            "src_path"]
    incoming = [dict(zip(keys, r)) for r in cur.fetchall()]
    return {"note_id": note_id, "out": out, "in": incoming}


@app.get("/api/notes")
def api_notes(course: Optional[str] = None,
              kind: Optional[str] = Query(None, pattern="^(lecture|transcript|other)$"),
              limit: int = Query(500, ge=1, le=1000)):
    cur = searcher().conn.cursor()
    where, params = [], []
    if course:
        where.append("course = %s"); params.append(course)
    if kind:
        where.append("kind = %s"); params.append(kind)
    w = ("WHERE " + " AND ".join(where)) if where else ""
    cur.execute(f"SELECT note_id, rel_path, title, course, course_code, lecture_no,"
                f" kind, zh_title, en_title, char_count FROM notes {w}"
                f" ORDER BY course, lecture_no, kind, note_id LIMIT %s",
                params + [limit])
    keys = ["note_id", "rel_path", "title", "course", "course_code", "lecture_no",
            "kind", "zh_title", "en_title", "char_count"]
    return {"notes": [dict(zip(keys, r)) for r in cur.fetchall()]}


@app.get("/api/vault-file")
def api_vault_file(note_id: int, path: str):
    """按笔记相对路径读取 vault 内文件（slides 截图等）。严格防目录穿越，只读。"""
    cur = searcher().conn.cursor()
    cur.execute("SELECT rel_path FROM notes WHERE note_id = %s", (note_id,))
    row = cur.fetchone()
    if not row:
        raise HTTPException(404, "note not found")
    target = (VAULT_DIR / Path(row[0]).parent / path).resolve()
    if not target.is_relative_to(VAULT_DIR) or not target.is_file():
        raise HTTPException(404, "file not found")
    return FileResponse(target)


@app.get("/api/chunk/{chunk_id}")
def api_chunk(chunk_id: int):
    cur = searcher().conn.cursor()
    cur.execute("SELECT c.chunk_id, c.note_id, c.seq, c.heading_path, c.content,"
                " c.page_start, c.page_end, n.rel_path, n.course, n.lecture_no"
                " FROM chunks c JOIN notes n ON n.note_id = c.note_id"
                " WHERE c.chunk_id = %s", (chunk_id,))
    row = cur.fetchone()
    if not row:
        raise HTTPException(404, "chunk not found")
    keys = ["chunk_id", "note_id", "seq", "heading_path", "content",
            "page_start", "page_end", "rel_path", "course", "lecture_no"]
    return dict(zip(keys, row))


@app.get("/api/courses")
def api_courses():
    return {"courses": searcher().courses()}


@app.get("/api/stats")
def api_stats():
    return searcher().stats()


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/ui/")


app.mount("/ui", StaticFiles(directory=WEB_DIR, html=True), name="ui")
