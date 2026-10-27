/* snapshot.js — 展示站「快照模式」后端替身。
 *
 * Qoder Sites 的后端是 Supabase/Deno，跑不了 seekdb 的单 SQL 向量+ik 全文融合，
 * 也装不了 Python bge 模型。所以公开站不发 live 检索，而是内置一组从本机
 * live seekdb **实测抓取**的真实结果快照（见 tools/capture_snapshot.py）。
 *
 * 本文件实现一个与 serve.py 同形的 api(path) 替身：命中预置查询/已抓取笔记时
 * 返回真实快照数据；未命中时返回空并带 demoMiss 标记，由前端提示"在线混合检索
 * 需连本机 seekdb 栈"。所有数据均来自 data/*.json，不编造。
 */
(function () {
  const cache = {};
  async function j(f) {
    if (!(f in cache)) {
      const r = await fetch("data/" + f, { cache: "no-cache" });
      if (!r.ok) throw new Error("快照缺失 " + f + " (" + r.status + ")");
      cache[f] = await r.json();
    }
    return cache[f];
  }

  // 预置查询：q|mode -> results（真实 seekdb 抓取）
  let searchMap = null, presets = null;
  async function ensureSearch() {
    if (searchMap) return;
    const s = (await j("searches.json")).searches;
    searchMap = {};
    presets = [];
    const seen = new Set();
    for (const g of s) {
      searchMap[g.q + "|" + g.mode] = g;
      if (g.mode === "hybrid" && !seen.has(g.q)) { seen.add(g.q); presets.push({ q: g.q, mode: "hybrid" }); }
    }
  }

  function notFound(msg) { const e = new Error(msg || "not found"); e.status = 404; throw e; }

  async function apiSnapshot(path) {
    const qi = path.indexOf("?");
    const route = qi >= 0 ? path.slice(0, qi) : path;
    const qs = new URLSearchParams(qi >= 0 ? path.slice(qi + 1) : "");

    if (route === "/api/stats") return (await j("meta.json")).stats;
    if (route === "/api/courses") return (await j("meta.json")).courses;

    if (route === "/api/search") {
      await ensureSearch();
      const q = qs.get("q") || "", mode = qs.get("mode") || "hybrid";
      const hit = searchMap[q + "|" + mode];
      if (hit) return { query: q, mode, alpha: 0.5, count: hit.count, results: hit.results };
      return { query: q, mode, alpha: 0.5, count: 0, results: [], demoMiss: true,
               presets: presets.map(p => p.q) };
    }

    if (route === "/api/term") {
      const t = qs.get("t") || "";
      const all = (await j("terms.json")).terms;
      const hit = all.find(x => x.t === t);
      if (hit) return { term: t, results: hit.results };
      // 退化：在所有已抓取术语里按 term / term_en 模糊匹配
      const out = [];
      for (const grp of all) for (const r of grp.results)
        if (r.term === t || (r.term_en || "").toLowerCase().includes(t.toLowerCase())) out.push(r);
      return { term: t, results: out.slice(0, 10) };
    }

    if (route === "/api/notes") {
      let notes = (await j("notes.json")).notes;
      const course = qs.get("course"), kind = qs.get("kind");
      if (course) notes = notes.filter(n => n.course === course);
      if (kind) notes = notes.filter(n => n.kind === kind);
      const idx = await j("index.json");
      const captured = new Set(idx.captured_note_ids);
      return { notes: notes.map(n => Object.assign({}, n, { captured: captured.has(n.note_id) })) };
    }

    if (route === "/api/note") {
      const idx = await j("index.json");
      let id = qs.get("id");
      if (id == null) {
        const p = qs.get("path");
        id = p != null ? idx.path_map[p] : null;
      }
      if (id == null) notFound("链接目标不在示例快照中");
      id = Number(id);
      if (!idx.captured_note_ids.includes(id))
        notFound("该笔记未包含在公开示例快照中（示例内置 " + idx.captured_note_ids.length +
                 " 篇代表笔记）；完整浏览请在本机运行检索栈。");
      return (await j("notes/" + id + ".json")).note;
    }

    let m = route.match(/^\/api\/note\/(\d+)\/(chunks|glossary|links)$/);
    if (m) {
      const id = Number(m[1]), d = await j("notes/" + id + ".json");
      if (m[2] === "chunks") return { note_id: id, chunks: d.chunks };
      if (m[2] === "glossary") return { note_id: id, terms: d.glossary };
      return { note_id: id, out: d.links_out, in: d.links_in };
    }

    notFound("示例站不支持该接口：" + route);
  }

  // 预置的演示查询/术语（供前端渲染成可点的 chips）
  async function snapshotMeta() {
    await ensureSearch();
    const terms = (await j("terms.json")).terms.map(t => t.t);
    return { presets, terms };
  }

  // PPT 截图占位：示例站不内置 vault 图片（版权/体积），诚实标注
  const PLACEHOLDER = "data:image/svg+xml;utf8," + encodeURIComponent(
    '<svg xmlns="http://www.w3.org/2000/svg" width="640" height="120">' +
    '<rect width="100%" height="100%" fill="#f1f3f6" stroke="#d7dce3"/>' +
    '<text x="50%" y="50%" font-family="system-ui,sans-serif" font-size="15" ' +
    'fill="#8a94a3" text-anchor="middle" dominant-baseline="middle">' +
    'PPT 截图未内置于公开示例站 · 完整图见本机 Obsidian vault</text></svg>');

  window.apiSnapshot = apiSnapshot;
  window.snapshotMeta = snapshotMeta;
  window.SNAPSHOT_PLACEHOLDER = PLACEHOLDER;
  window.SNAPSHOT_MODE = true;
})();
