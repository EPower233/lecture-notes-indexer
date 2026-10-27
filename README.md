# 讲义库索引器（Lecture Notes Indexer）

基于 **seekdb**（OceanBase AI 数据库）**向量 + 全文单 SQL 混合检索**的本地讲义库检索与浏览工具。
参赛作品 · 赛题 **Qoder × OceanBase「Build with Qoder」** · 全程用 **Qoder** 开发。

仓库：<https://github.com/EPower233/lecture-notes-indexer>

在线展示站（Qoder Sites）：<https://lecture-notes-indexer-ejzc6ua0tvs.qoder.zone>
—— 静态站 + 本机 seekdb 实测抓取的真实结果快照；完整在线混合检索请在本机运行下方检索栈。

把本地 Obsidian 讲义库里的 **CMU 15-445（数据库系统）** 与 **Mathematics（概率论与数理统计）** 双语讲义解析、切块、向量化并入库，支持混合检索、术语速查、按课程/讲次/类型过滤、笔记浏览与跨笔记跳转。

> 隐私说明：公开展示站与本仓库的快照数据**仅包含上述两类课程**，其余笔记（含个人笔记）不纳入抓取与发布。

---

## 快速开始

```bash
# 1) 启动 seekdb（Docker，宿主端口 20881；已设 restart=unless-stopped）
docker start seekdb

# 2) 从零建库建表 + 入库（已入库可跳过）
py -3.10 -c "import pymysql; sql=open('schema.sql',encoding='utf-8').read(); c=pymysql.connect(host='127.0.0.1',port=20881,user='root',password=''); cur=c.cursor(); [cur.execute(s) for s in [x for x in '\n'.join(l for l in sql.splitlines() if not l.strip().startswith('--')).split(';') if x.strip()]]; c.commit()"
py -3.10 ingest.py

# 3) 启动检索服务 + 前端
py -3.10 -m uvicorn serve:app --host 127.0.0.1 --port 8000
#    浏览器打开 http://127.0.0.1:8000/ui/
```

依赖：Python 3.10 · torch · sentence-transformers（BAAI/bge-small-zh-v1.5，512 维 CPU）· fastapi · uvicorn · pymysql · Docker Desktop。

---

## 目录结构

| 文件 | 说明 |
|---|---|
| `schema.sql` | seekdb 建库建表 DDL（`VECTOR(512)` + ik 全文索引 + HNSW 向量索引） |
| `ingest.py` | 入库管道：解析→切块→页码→术语→链接→bge 向量→批量入库→统计 |
| `search.py` | 检索核心 + CLI：**单条 SQL** 内完成全文相关度 × 向量距离加权融合 |
| `serve.py` | FastAPI HTTP 服务 + 前端静态托管（`/ui`） |
| `verify.py` | 一键验证脚本：行数/分布/随机切片/术语/链接有效性/三种检索 demo |
| `web/` | 前端：`index.html` 检索页 · `note.html` 笔记页 · `md.js` 渲染 · `style.css` 主题 |
| `docs/验证样例.md` | **交付物**：可复现验证步骤 + 真实运行输出 |
| `docs/开发记录.md` | **交付物**：Qoder 开发全过程、关键决策与问题解决 |

---

## 核心特性

- **库内混合检索**：`score = (1-α)·rel/(1+rel) + α·1/(1+dist)`，全文 `MATCH() AGAINST()` 与向量 `l2_distance()` 在 **seekdb 单条 SQL** 内融合排序，结构化过滤同条 SQL 下推——**不做客户端拼接**。
- **中文友好**：全文用 ik 分词（自然语言模式，复合词召回正常）；向量用 bge-small-zh-v1.5（query 侧带检索指令前缀）。
- **讲义结构还原**：保留 `heading_path`、就近「📍 PPT 第 N 页」页码、术语速查表、跨笔记内部链接（带 `valid` 校验）。
- **只读数据源**：全程不修改本地讲义库目录（由环境变量 `VAULT_DIR` 指定）；图片经 `/api/vault-file` 只读代理并防目录穿越。

---

## 交付物与赛题映射

见 [`docs/开发记录.md`](docs/开发记录.md) §六。验证证据见 [`docs/验证样例.md`](docs/验证样例.md)。

---

## 数据规模（公开快照子集，实测）

`notes 96 · chunks 7201 · glossary 1831 · links 1811（全部有效）` — 仅 CMU 15-445 与 Mathematics 两类课程；由 `tools/capture_snapshot.py` 按白名单从本机 live 栈抓取。
