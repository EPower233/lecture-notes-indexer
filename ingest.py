# -*- coding: utf-8 -*-
"""讲义库索引器 ingest.py — 解析 Obsidian vault，切块、抽取术语/链接/页码，
用 BAAI/bge-small-zh-v1.5 生成 512 维向量，批量写入 seekdb (vault 库)。

用法:
  py -3.10 ingest.py --vault E:/Github/Obsidian-Vault \
      --host 127.0.0.1 --port 20881 --user root --password "" --db vault
"""
import argparse
import io
import re
import sys
import unicodedata
from pathlib import Path
from urllib.parse import unquote

import pymysql

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

MAX_CHUNK_CHARS = 800
OVERLAP_CHARS = 100
EMBED_BATCH = 64
INSERT_BATCH = 200

CJK_RE = re.compile(r"[\u4e00-\u9fff]")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
PAGE_RE = re.compile(r"\U0001F4CD\s*\*?PPT 第 ([0-9\u2013\u2014\-,，、\s]+) 页")
PAGE_NUM_RE = re.compile(r"\d+")
MD_LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)]+)\)")
INTERNAL_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\((?!https?:)([^)\s]+\.md)(#[^)]*)?\)")
FM_EN_RE = re.compile(r"^\*\*([^*]+)\*\*[：:]\s*(.*)$")
FIRST_SEEN_RE = re.compile(r"（本讲首次出现[：:]([^；)]*)[；;]引入于[：:]([^）)]*)）\s*$")
LECTURE_FILE_RE = re.compile(r"(\d{2}-\d{3})-lecture-(\d+)(?:-([a-z0-9\-]+))?")
LECTURE_DIR_RE = re.compile(r"^L(\d+)\s+([A-Za-z][\w.\-]*)\s+(.*)$")


def strip_md(text: str) -> str:
    text = MD_LINK_RE.sub(r"\1", text)
    text = re.sub(r"[*_`~]", "", text)
    return text.strip()


def slugify(text: str) -> str:
    text = strip_md(text).lower()
    text = unicodedata.normalize("NFC", text)
    text = re.sub(r"[^\w\u4e00-\u9fff\- ]", "", text)
    return re.sub(r"\s+", "-", text.strip())


def split_frontmatter(text: str):
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---", 4)
    if end < 0:
        return {}, text
    fm = {}
    for line in text[4:end].splitlines():
        if ":" in line and not line.startswith((" ", "\t", "#")):
            k, _, v = line.partition(":")
            fm[k.strip()] = v.strip().strip('"').strip("'")
    return fm, text[end + 4 :].lstrip("\n")


def parse_page_line(line: str):
    m = PAGE_RE.search(line)
    if not m:
        return None
    nums = [int(x) for x in PAGE_NUM_RE.findall(m.group(1))]
    return (min(nums), max(nums)) if nums else None


def humanize_slug(slug: str) -> str:
    words = slug.replace("-", " ").strip()
    return words.title() if words and words.isascii() else words


def split_oversized(text: str):
    """把超长章节按段落切成 <=MAX_CHUNK_CHARS 的片，带 OVERLAP_CHARS 重叠。"""
    if len(text) <= MAX_CHUNK_CHARS:
        return [text]
    paras = text.split("\n\n")
    parts, cur = [], ""
    for p in paras:
        if cur and len(cur) + len(p) + 2 > MAX_CHUNK_CHARS:
            parts.append(cur)
            tail = cur[-OVERLAP_CHARS:]
            cur = (tail + "\n\n" + p) if tail else p
        else:
            cur = (cur + "\n\n" + p) if cur else p
        while len(cur) > MAX_CHUNK_CHARS * 2:  # 单段超长时硬切
            parts.append(cur[:MAX_CHUNK_CHARS])
            cur = cur[MAX_CHUNK_CHARS - OVERLAP_CHARS :]
    if cur.strip():
        parts.append(cur)
    return [p.strip() for p in parts if p.strip()]


class Note:
    def __init__(self, path: Path, rel: str):
        self.path, self.rel = path, rel
        self.chunks, self.glossary, self.links = [], [], []
        self.anchors = set()


def parse_metadata(rel: str, fm: dict, text: str):
    parts = Path(rel).parts
    fname = parts[-1]
    dirs = [d for d in parts[:-1] if not d.startswith(".")]
    meta = {
        "course": None, "course_code": None, "lecture_no": None,
        "kind": "other", "agent": None, "zh_title": None, "en_title": None,
    }
    if dirs and dirs[0] == "CMU" and len(dirs) > 1:
        meta["course"] = dirs[1]
    elif dirs:
        meta["course"] = "/".join(d for d in dirs if d != "Lectures")

    m = LECTURE_FILE_RE.search(fname)
    if m:
        meta["course_code"] = m.group(1)
        meta["lecture_no"] = int(m.group(2))
        slug = m.group(3) or ""
        meta["kind"] = "transcript" if slug == "transcript" else "lecture"
        if slug and slug != "transcript":
            meta["en_title"] = humanize_slug(slug)
        folder = dirs[-1] if dirs else ""
        dm = LECTURE_DIR_RE.match(folder)
        if dm:
            meta["lecture_no"] = meta["lecture_no"] or int(dm.group(1))
            meta["agent"] = dm.group(2)
            zh_tokens = []
            for tok in dm.group(3).split():
                if CJK_RE.search(tok):
                    zh_tokens.append(tok)
                else:
                    break
            if zh_tokens:
                meta["zh_title"] = " ".join(zh_tokens)
            if not meta["en_title"]:
                rest = dm.group(3).split()
                en_start = next((i for i, t in enumerate(rest) if not CJK_RE.search(t)), None)
                if en_start is not None:
                    meta["en_title"] = " ".join(rest[en_start:])
    title = fm.get("title")
    if not title:
        for line in text.splitlines():
            hm = HEADING_RE.match(line)
            if hm and len(hm.group(1)) == 1:
                title = strip_md(hm.group(2))
                break
    meta["title"] = title or Path(fname).stem
    meta["has_frontmatter"] = 1 if fm else 0
    return meta


def parse_note(note: Note):
    text = note.path.read_text(encoding="utf-8", errors="replace")
    fm, body = split_frontmatter(text)
    meta = parse_metadata(note.rel, fm, body)
    note.meta = meta

    body = re.sub(r"<details>.*?</details>", "", body, flags=re.S)
    lines = body.splitlines()

    heading_stack = []      # [(level, text)]
    cur_h1 = None
    cur_lines = []
    sections = []           # (heading_path, level, lines)
    in_glossary = False
    gloss_sections = []
    gloss_cur = None

    def heading_path_str(stack):
        return " > ".join(t for _, t in stack)

    def flush():
        nonlocal cur_lines
        if cur_lines:
            sections.append((heading_path_str(heading_stack), list(cur_lines)))
            cur_lines = []

    for line in lines:
        hm = HEADING_RE.match(line)
        if hm:
            level, raw = len(hm.group(1)), hm.group(2).strip()
            clean = strip_md(raw)
            note.anchors.add(clean)
            note.anchors.add(slugify(raw))
            if level == 1:
                flush()
                cur_h1 = clean
                heading_stack = [(1, clean)]
                continue
            if level in (2, 3):
                if in_glossary and level == 3 and gloss_cur:
                    gloss_sections.append(gloss_cur)
                    gloss_cur = None
                flush()
                heading_stack = [h for h in heading_stack if h[0] < level]
                heading_stack.append((level, clean))
                if level == 2:
                    in_glossary = "术语速查表" in clean
                    if in_glossary:
                        continue
                if in_glossary and level == 3:
                    gloss_cur = {"term": clean, "lines": []}
                    continue
            else:
                if in_glossary and gloss_cur is not None:
                    gloss_cur["lines"].append(line)
                else:
                    cur_lines.append(line)
                continue
        else:
            if in_glossary and gloss_cur is not None:
                gloss_cur["lines"].append(line)
            else:
                cur_lines.append(line)
    if in_glossary and gloss_cur:
        gloss_sections.append(gloss_cur)
    flush()

    # --- 术语表 ---
    for g in gloss_sections:
        body_text = "\n".join(g["lines"]).strip()
        term_en, definition, first_seen, introduced = None, body_text, None, None
        first_line = body_text.split("\n", 1)[0].strip() if body_text else ""
        fm_en = FM_EN_RE.match(first_line)
        if fm_en:
            term_en = fm_en.group(1).strip()
            rest = fm_en.group(2).strip()
            fs = FIRST_SEEN_RE.search(rest)
            if fs:
                first_seen, introduced = fs.group(1).strip(), fs.group(2).strip()
                rest = FIRST_SEEN_RE.sub("", rest).strip()
            definition = rest + ("\n" + body_text.split("\n", 1)[1] if "\n" in body_text else "")
        note.glossary.append({
            "term": g["term"][:128], "term_en": (term_en or "")[:256] or None,
            "definition": definition or None, "first_seen": (first_seen or "")[:64] or None,
            "introduced_in": (introduced or "")[:64] or None,
        })
        note.anchors.add(g["term"])

    # --- 切块 + 页码 + 链接 ---
    last_page = (None, None)
    seq = 0
    for hpath, sec_lines in sections:
        content_lines, page = [], None
        for ln in sec_lines:
            pg = parse_page_line(ln)
            if pg:
                if page is None:
                    page = pg
                last_page = pg
                continue
            content_lines.append(ln)
        content = "\n".join(content_lines).strip()
        if len(content) < 10:
            continue
        eff_page = page or last_page
        for j, piece in enumerate(split_oversized(content)):
            seq += 1
            hp = hpath if j == 0 else f"{hpath} (part {j+1})"
            chunk = {
                "seq": seq, "heading_path": hp[:760], "content": piece,
                "page_start": eff_page[0], "page_end": eff_page[1],
                "links": [],
            }
            for lm in INTERNAL_MD_LINK_RE.finditer(piece):
                label = strip_md(lm.group(1))[:250]
                target_raw, anchor = lm.group(2), (lm.group(3) or "")[1:]
                target = unquote(target_raw)
                anchor = unquote(anchor)
                resolved = (note.path.parent / target).resolve()
                try:
                    target_rel = resolved.relative_to(VAULT_ROOT).as_posix()
                except ValueError:
                    target_rel = resolved.as_posix()
                chunk["links"].append({"link_text": label, "target_rel": target_rel[:760],
                                       "anchor": anchor[:250] or None})
            note.chunks.append(chunk)
    note.meta["char_count"] = len(text)


VAULT_ROOT = Path()


def vec_literal(v):
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


def main():
    global VAULT_ROOT
    ap = argparse.ArgumentParser()
    ap.add_argument("--vault", default="E:/Github/Obsidian-Vault")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=20881)
    ap.add_argument("--user", default="root")
    ap.add_argument("--password", default="")
    ap.add_argument("--db", default="vault")
    ap.add_argument("--model", default="BAAI/bge-small-zh-v1.5")
    ap.add_argument("--schema", default=str(Path(__file__).parent / "schema.sql"))
    args = ap.parse_args()
    VAULT_ROOT = Path(args.vault).resolve()

    conn = pymysql.connect(host=args.host, port=args.port, user=args.user,
                           password=args.password, database=args.db,
                           charset="utf8mb4", autocommit=False,
                           connect_timeout=10, read_timeout=300, write_timeout=300)
    cur = conn.cursor()
    print(f"[db] connected to {args.host}:{args.port}/{args.db}")

    # 1) 扫描
    files = sorted(p for p in VAULT_ROOT.rglob("*.md")
                   if not any(part.startswith(".") for part in p.relative_to(VAULT_ROOT).parts))
    print(f"[scan] {len(files)} markdown files")

    notes = []
    rel2id = {}
    for i, p in enumerate(files, start=1):
        n = Note(p, p.relative_to(VAULT_ROOT).as_posix())
        rel2id[n.rel] = i
        notes.append(n)
    for n in notes:
        parse_note(n)
    total_chunks = sum(len(n.chunks) for n in notes)
    total_gloss = sum(len(n.glossary) for n in notes)
    total_links = sum(len(c["links"]) for n in notes for c in n.chunks)
    print(f"[parse] chunks={total_chunks} glossary={total_gloss} links={total_links}")

    # 2) 向量（带本地缓存，内容不变则跳过编码）
    import hashlib
    import numpy as np
    from sentence_transformers import SentenceTransformer
    all_chunks = [(n, c) for n in notes for c in n.chunks]
    texts = [f"{c['heading_path']}\n{c['content']}" for _, c in all_chunks]
    digest = hashlib.md5(("\x00".join(texts)).encode("utf-8")).hexdigest()[:16]
    cache = Path(__file__).parent / f"emb_cache_{digest}.npy"
    if cache.exists():
        embs = np.load(cache)
        print(f"[embed] loaded {embs.shape} from cache {cache.name}")
    else:
        model = SentenceTransformer(args.model)
        print(f"[embed] encoding {len(texts)} chunks with {args.model} (CPU)...")
        embs = model.encode(texts, batch_size=EMBED_BATCH, normalize_embeddings=True,
                            show_progress_bar=True)
        np.save(cache, embs)
        print(f"[embed] saved cache {cache.name}")
    assert embs.shape[1] == 512, f"expect 512-dim, got {embs.shape[1]}"

    # 3) 写库（全量重建）
    for t in ("links", "glossary", "chunks", "notes"):
        cur.execute(f"DELETE FROM {t}")
    conn.commit()

    for n in notes:
        m = n.meta
        cur.execute(
            "INSERT INTO notes (note_id, rel_path, title, course, course_code, lecture_no,"
            " kind, agent, zh_title, en_title, has_frontmatter, char_count)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (rel2id[n.rel], n.rel, m["title"], m["course"], m["course_code"], m["lecture_no"],
             m["kind"], m["agent"], m["zh_title"], m["en_title"], m["has_frontmatter"],
             m["char_count"]))
    conn.commit()
    print(f"[db] notes inserted: {len(notes)}")

    chunk_id = 0
    rows = []
    for (n, c), emb in zip(all_chunks, embs):
        chunk_id += 1
        c["chunk_id"] = chunk_id
        rows.append((chunk_id, rel2id[n.rel], c["seq"], c["heading_path"], c["content"],
                     c["page_start"], c["page_end"], len(c["content"]), vec_literal(emb)))
        if len(rows) >= INSERT_BATCH:
            cur.executemany("INSERT INTO chunks (chunk_id, note_id, seq, heading_path,"
                            " content, page_start, page_end, char_count, emb)"
                            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)", rows)
            conn.commit()
            print(f"[db] chunks {chunk_id}/{total_chunks}", end="\r")
            rows = []
    if rows:
        cur.executemany("INSERT INTO chunks (chunk_id, note_id, seq, heading_path,"
                        " content, page_start, page_end, char_count, emb)"
                        " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)", rows)
        conn.commit()
    print(f"\n[db] chunks inserted: {chunk_id}")

    gid = 0
    for n in notes:
        for g in n.glossary:
            gid += 1
            cur.execute("INSERT INTO glossary (glossary_id, note_id, term, term_en,"
                        " definition, first_seen, introduced_in) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                        (gid, rel2id[n.rel], g["term"], g["term_en"], g["definition"],
                         g["first_seen"], g["introduced_in"]))
    conn.commit()
    print(f"[db] glossary inserted: {gid}")

    lid, valid_cnt = 0, 0
    for n in notes:
        for c in n.chunks:
            for lk in c["links"]:
                lid += 1
                dst = rel2id.get(lk["target_rel"])
                ok = dst is not None
                if ok and lk["anchor"]:
                    tgt = notes[dst - 1]
                    ok = (lk["anchor"] in tgt.anchors
                          or lk["anchor"].lower() in tgt.anchors
                          or slugify(lk["anchor"]) in tgt.anchors)
                valid_cnt += ok
                cur.execute("INSERT INTO links (link_id, src_note_id, src_chunk_id, link_text,"
                            " target_rel, anchor, dst_note_id, valid) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                            (lid, rel2id[n.rel], c["chunk_id"], lk["link_text"],
                             lk["target_rel"], lk["anchor"], dst, int(ok)))
    conn.commit()
    print(f"[db] links inserted: {lid} (valid={valid_cnt}, invalid={lid - valid_cnt})")

    # 4) 统计
    print("\n========== 统计 ==========")
    cur.execute("SELECT COUNT(*) FROM notes"); print("notes   :", cur.fetchone()[0])
    cur.execute("SELECT COUNT(*) FROM chunks"); print("chunks  :", cur.fetchone()[0])
    cur.execute("SELECT COUNT(*) FROM glossary"); print("glossary:", cur.fetchone()[0])
    cur.execute("SELECT COUNT(*) FROM links"); print("links   :", cur.fetchone()[0])
    cur.execute("SELECT course, kind, COUNT(*) FROM notes GROUP BY course, kind ORDER BY course, kind")
    print("\n按课程/类型:")
    for course, kind, cnt in cur.fetchall():
        print(f"  {course or '(root)':<40} {kind:<10} {cnt}")
    cur.execute("SELECT MIN(char_count), AVG(char_count), MAX(char_count) FROM chunks")
    mn, av, mx = cur.fetchone()
    print(f"\nchunk 字符数: min={mn} avg={av:.0f} max={mx}")
    cur.execute("SELECT COUNT(*) FROM chunks WHERE emb IS NULL")
    print("无向量 chunk:", cur.fetchone()[0])
    conn.close()


if __name__ == "__main__":
    main()
