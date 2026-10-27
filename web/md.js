/* 迷你 Markdown 渲染器：先整体转义再做受控替换，只产生白名单标签。
 * renderMd(src, {resolveFile(path)->url}) -> html string
 * 链接分类：#锚点 → <a class="term">（术语弹窗）；*.md → <a class="mdlink">（跳笔记页）；http → 外链。
 */
function esc(s) {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;")
          .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function renderMd(src, opts) {
  opts = opts || {};
  const blocks = [];
  // 1) 保护围栏代码块
  src = src.replace(/```(\w*)\r?\n([\s\S]*?)```/g, (m, lang, body) => {
    blocks.push("<pre><code>" + esc(body.replace(/\n$/, "")) + "</code></pre>");
    return "\u0000B" + (blocks.length - 1) + "\u0000";
  });
  // 2) 整体转义
  src = esc(src);
  src = src.replace(/&lt;br\s*\/?&gt;/g, "<br>");
  // 3) 行内代码
  src = src.replace(/`([^`\n]+)`/g, (m, c) => {
    blocks.push("<code>" + c + "</code>");
    return "\u0000B" + (blocks.length - 1) + "\u0000";
  });
  // 4) 图片
  src = src.replace(/!\[([^\]]*)\]\(([^)\s]+)\)/g, (m, alt, p) => {
    const url = opts.resolveFile ? opts.resolveFile(decodeEntities(p)) : decodeEntities(p);
    return '<img src="' + url.replace(/"/g, "%22") + '" alt="' + alt + '" loading="lazy" title="' + alt + '">';
  });
  // 5) 链接
  src = src.replace(/\[([^\]]*)\]\(([^)\s]+)\)/g, (m, text, href) => {
    href = decodeEntities(href);
    if (/^https?:/i.test(href)) {
      return '<a href="' + href.replace(/"/g, "%22") + '" target="_blank" rel="noopener">' + text + "</a>";
    }
    if (href.startsWith("#")) {
      return '<a class="term" data-term="' + href.slice(1) + '">' + text + "</a>";
    }
    const hash = href.indexOf("#");
    const path = hash >= 0 ? href.slice(0, hash) : href;
    const anchor = hash >= 0 ? href.slice(hash + 1) : "";
    if (/\.md$/i.test(path)) {
      return '<a class="mdlink" data-path="' + path + '" data-anchor="' + anchor + '">' + text + "</a>";
    }
    return text;
  });
  // 6) 粗体/斜体/脚注标记
  src = src.replace(/\[\^([^\]\s]+)\]/g, "<sup>[$1]</sup>");
  src = src.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  src = src.replace(/(^|[^*\w])\*([^*\n]+)\*(?!\w)/g, "$1<em>$2</em>");
  // 7) 块级
  const out = [];
  let para = [], list = null, quote = false;
  const closePara = () => { if (para.length) { out.push("<p>" + para.join("<br>") + "</p>"); para = []; } };
  const closeList = () => { if (list) { out.push("</" + list + ">"); list = null; } };
  const closeQuote = () => { if (quote) { out.push("</blockquote>"); quote = false; } };
  for (const line of src.split("\n")) {
    const t = line.trim();
    if (/^\u0000B\d+\u0000$/.test(t)) { closePara(); closeList(); closeQuote(); out.push(t); continue; }
    if (!t) { closePara(); closeList(); closeQuote(); continue; }
    let m;
    if ((m = t.match(/^(#{4,6})\s+(.*)$/))) {
      closePara(); closeList(); closeQuote();
      out.push("<h" + (m[1].length + 1) + ">" + m[2] + "</h" + (m[1].length + 1) + ">");
    } else if (/^(-{3,}|\*{3,})$/.test(t)) {
      closePara(); closeList(); closeQuote(); out.push("<hr>");
    } else if ((m = t.match(/^&gt;\s?(.*)$/))) {
      closePara(); closeList();
      if (!quote) { out.push("<blockquote>"); quote = true; }
      para.push(m[1]);
      if (para.length) { out.push("<p>" + para.join("<br>") + "</p>"); para = []; }
    } else if ((m = t.match(/^[-*+]\s+(.*)$/))) {
      closePara(); closeQuote();
      if (list !== "ul") { closeList(); out.push("<ul>"); list = "ul"; }
      out.push("<li>" + m[1] + "</li>");
    } else if ((m = t.match(/^\d+[.)]\s+(.*)$/))) {
      closePara(); closeQuote();
      if (list !== "ol") { closeList(); out.push("<ol>"); list = "ol"; }
      out.push("<li>" + m[1] + "</li>");
    } else if (/^\[\^[^\]]+\]:/.test(t)) {
      closePara(); closeList(); closeQuote();
      out.push('<p style="color:var(--muted);font-size:12.5px">' + t + "</p>");
    } else {
      closeList(); closeQuote();
      para.push(t);
    }
  }
  closePara(); closeList(); closeQuote();
  let html = out.join("\n");
  html = html.replace(/\u0000B(\d+)\u0000/g, (m, i) => blocks[+i]);
  return html;
}

function decodeEntities(s) {
  return s.replace(/&amp;/g, "&").replace(/&lt;/g, "<")
          .replace(/&gt;/g, ">").replace(/&quot;/g, '"');
}

function highlight(text, words) {
  let html = esc(text);
  for (const w of words) {
    if (!w) continue;
    const re = new RegExp(w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "gi");
    html = html.replace(re, (m) => "\u0001" + m + "\u0002");
  }
  return html.replace(/\u0001/g, "<mark>").replace(/\u0002/g, "</mark>");
}
