# 增加sqlite的需求

- cache 目录是空的。 这意味着每次运行都是全量构建。如果加了 SQLite 作为查询层，全量重建 SQLite 的成本会随项目规模线性增长。
- manifest.json 只记录了源文件的 timestamp，没有记录 extraction 的 schema version。 如果你将来改了 node/edge 的数据模型（比如加了一个 embedding 字段），所有缓存都需要失效。你需要一个 schema
  migration 策略。
- 没有 lineage（数据血缘）。 graph.json 里一条 INFERRED 边的来源是 LLM，但 具体是哪次 LLM 调用、用的什么 prompt、置信度分数是多少——这些都没有记录。如果 SQLite
  要支持溯源查询（"这条边为什么存在？"），当前的数据是缺失的。

  他的建议：

  ▎ "设计一个三层存储：raw extraction → validated graph → query index。每层有自己的 schema version 和增量更新策略。SQLite 放在 query index 层。这样 extraction 的变化不会直接 break
  ▎ 搜索，而且你可以重跑某一步而不需要全部重来。"

  ---

  1. Dr. Anna Petrov — 软件架构与 API 设计专家 (前 Google Staff Engineer, API 设计评审)

  她会怎么说：

  ▎ "你们在 '输出文件' 和 '可查询服务' 之间缺了一个抽象层。"

  发现的核心问题：

- 当前架构是 batch-oriented 的。 detect → extract → build → cluster → analyze → report → export 是一条直线管道，终点是静态文件。但搜索需要的是一个随时可查询的服务。这不是加一个 SQLite
  文件就能解决的——你需要一个 query layer。
- MCP server 和 CLI 之间有概念冲突。 CLI 是 "运行一次生成文件"，MCP server 是 "加载文件提供查询"。但如果数据源变了，MCP server 加载的还是旧数据。你需要一个 watch → rebuild → reload
  的自动刷新机制（watch.py 已经存在但似乎没有和 serve.py 集成）。
- graph.json 是 single-file 的，不支持多项目。 如果用户有 3 个项目，就有 3 个 graphify-out/ 目录。SQLite 的搜索应该能跨项目查询。

  她的建议：

  ▎ "定义一个 GraphStore 抽象接口，底下可以有 JSONFileStore、SQLiteStore、甚至 Neo4jStore。搜索接口统一为 search(query, filters, limit) →
  ▎ results。这样你可以在不改变上层代码的情况下切换存储后端。"

  ---

  1. Dr. Wei Zhang — 安全、性能与规模专家 (前 AWS RDS 性能团队)

  他会怎么说：

  ▎ "379 个节点时一切都很美好。37,000 个节点时你会后悔今天的每个决定。"

  发现的核心问题：

- graph.json 的加载是 O(n) 的。 338KB 现在没问题，但如果分析 Linux kernel 级别的代码库（~30M 行），graph.json 可能到 100MB+。json.load() 会占用大量内存，而 SQLite 可以做懒加载和分页查询。
- MCP serve.py 的 BFS 搜索没有结果限制。 query_graph 工具的 BFS 会遍历整个可达子图。如果从 main() 开始 BFS，在 379 节点的图上就访问 35 个节点——但如果图有 10,000 个节点，一次查询可能返回几 MB
  的文本。虽然代码里有 token budget，但实现是截断而不是剪枝。
- 没有查询缓存。 相同的搜索查询会反复执行 BFS。SQLite 天然有 prepared statement cache，但如果查询逻辑还在 Python 层做图遍历，SQLite 的缓存帮不上忙。
- norm_label 的去重策略过于简单。 当前只做了 lower() + diacritics 移除。两个同名函数（如不同文件里的 parse()）会生成不同的 ID 但相同的 norm_label，搜索时会返回多个结果但没有区分度信息。

  他的建议：

  ▎ "SQLite 的核心价值是索引和查询规划器。把 WHERE 过滤、全文搜索、聚合统计都下推到 SQLite 层执行，不要在 Python 里遍历图然后过滤。对节点表建 community、file_type、source_file
  ▎ 的复合索引。对边表建 (source, relation) 和 (target, relation) 的复合索引。"

  ---
  综合评审结论

  ┌──────────┬──────────────────────┬────────────────────────────────┬────────┐
  │   维度   │       当前状态       │            核心问题            │ 优先级 │
  ├──────────┼──────────────────────┼────────────────────────────────┼────────┤
  │ 存储模型 │ 纯 JSON 文件         │ 无索引、无增量更新、无查询优化 │ P0     │
  ├──────────┼──────────────────────┼────────────────────────────────┼────────┤
  │ 搜索能力 │ str.lower() in label │ 无分词、无排序、无语义理解     │ P0     │
  ├──────────┼──────────────────────┼────────────────────────────────┼────────┤
  │ 查询接口 │ MCP BFS/DFS          │ 无图模式匹配、无聚合统计       │ P1     │
  ├──────────┼──────────────────────┼────────────────────────────────┼────────┤
  │ AI 集成  │ 无                   │ 缺向量搜索、缺 GraphRAG 管线   │ P1     │
  ├──────────┼──────────────────────┼────────────────────────────────┼────────┤
  │ 数据管线 │ 全量 batch           │ 无节点级增量、无 schema 版本   │ P2     │
  ├──────────┼──────────────────────┼────────────────────────────────┼────────┤
  │ 架构抽象 │ 硬耦合               │ 缺存储抽象层、多项目不支持     │ P2     │
  ├──────────┼──────────────────────┼────────────────────────────────┼────────┤
  │ 可扩展性 │ O(n) 加载            │ 大规模时内存/性能瓶颈          │ P3     │
  └──────────┴──────────────────────┴────────────────────────────────┴────────┘

  共识建议：SQLite 作为辅助查询索引层（不是替代 JSON），FTS5 做全文搜索，sqlite-vec 做向量搜索，保留 NetworkX 做图计算。引入 GraphStore 抽象层隔离存储实现。

  需要我为某个具体方案做详细的技术设计吗？

✻ Cogitated for 5m 27s

❯ 共识建议：SQLite 作为辅助查询索引层（不是替代 JSON），FTS5 做全文搜索，sqlite-vec 做向量搜索，保留 NetworkX 做图计算。引入 GraphStore 抽象层隔离存储实现。 客户期望能有一个工业级的实现,
  而且要求是在端侧使用的,可以连接现有的llm, agent在进行任务工作时,比如查询代码, 查询文档内容,查询图片等工作可以通过对话得到想要的准确信息. 这个该怎么设计?

⏺ 这是一个重大的架构设计任务。我先深入研究一下当前的代码库，以了解具体需要修改什么。

  Read 5 files (ctrl+o to expand)

⏺ 现在我对代码库有了全面的了解。让我来设计工业级架构。

  ---
  Graphify 端侧智能查询系统 — 技术设计文档

  一、系统定位

  ┌─────────────────────────────────────────────────────────────────┐
  │                     用户 (开发者 / Agent)                        │
  │                    "缓存系统是怎么工作的？"                        │
  └───────────────────────────┬─────────────────────────────────────┘
                              │ 自然语言对话
  ┌───────────────────────────▼─────────────────────────────────────┐
  │                    MCP / API Gateway                             │
  │           (协议适配: MCP stdio / HTTP / CLI)                     │
  └───────────────────────────┬─────────────────────────────────────┘
                              │
  ┌───────────────────────────▼─────────────────────────────────────┐
  │                  QueryEngine (查询引擎)                          │
  │  ┌──────────┐   ┌──────────┐   ┌───────────┐                   │
  │  │ Stage 1  │   │ Stage 2  │   │ Stage 3   │                   │
  │  │ FTS5     │ + │ Vector   │ + │ Graph     │ → 融合排序         │
  │  │ 关键词召回│   │ 语义召回  │   │ 扩展+剪枝 │                   │
  │  └──────────┘   └──────────┘   └───────────┘                   │
  └───────────────────────────┬─────────────────────────────────────┘
                              │ 结构化子图上下文
  ┌───────────────────────────▼─────────────────────────────────────┐
  │                  LLM (本地 / 远程)                               │
  │           Ollama · Claude API · OpenAI · ...                    │
  │           基于子图上下文生成准确回答                               │
  └─────────────────────────────────────────────────────────────────┘

  核心原则：端侧运行，零外部依赖用于搜索，LLM 仅用于生成环节。

  ---
  二、数据模型 — SQLite Schema

  -- ═══════════════════════════════════════════
  -- 核心表: nodes
  -- ═══════════════════════════════════════════
  CREATE TABLE nodes (
      id          TEXT PRIMARY KEY,          -- graphify node ID (e.g. "build_build")
      label       TEXT NOT NULL,             -- 人类可读标签 (e.g. "build()")
      norm_label  TEXT NOT NULL,             -- 去重音+小写, 用于搜索
      source_file TEXT NOT NULL DEFAULT '',  -- 源文件路径
      source_loc  TEXT NOT NULL DEFAULT '',  -- 源码位置 (e.g. "L42")
      file_type   TEXT NOT NULL DEFAULT '',  -- code|doc|pdf|image|video|audio
      community   INTEGER,                  -- 社区 ID
      degree      INTEGER NOT NULL DEFAULT 0, -- 连接数 (冗余存储, 避免计算)
      rationale   TEXT NOT NULL DEFAULT '',  -- 完整 docstring/描述 (不被截断)
      raw_text    TEXT NOT NULL DEFAULT '',  -- 源码片段 / 文档原文 (可搜索)
      created_at  REAL NOT NULL DEFAULT (strftime('%s','now')),
      updated_at  REAL NOT NULL DEFAULT (strftime('%s','now'))
  );

  -- 索引: 按文件、类型、社区查询
  CREATE INDEX idx_nodes_source_file ON nodes(source_file);
  CREATE INDEX idx_nodes_file_type   ON nodes(file_type);
  CREATE INDEX idx_nodes_community   ON nodes(community);
  CREATE INDEX idx_nodes_degree      ON nodes(degree DESC);

  -- ═══════════════════════════════════════════
  -- 核心表: edges
  -- ═══════════════════════════════════════════
  CREATE TABLE edges (
      id          INTEGER PRIMARY KEY AUTOINCREMENT,
      source_id   TEXT NOT NULL REFERENCES nodes(id),
      target_id   TEXT NOT NULL REFERENCES nodes(id),
      relation    TEXT NOT NULL DEFAULT '',    -- calls|imports|contains|semantically_similar_to
      confidence  TEXT NOT NULL DEFAULT 'EXTRACTED',  -- EXTRACTED|INFERRED|AMBIGUOUS
      conf_score  REAL,                        -- INFERRED 边的置信度分数
      weight      REAL NOT NULL DEFAULT 1.0,
      src_dir     TEXT,                        -- 原始方向 _src
      tgt_dir     TEXT,                        -- 原始方向 _tgt
      UNIQUE(source_id, target_id, relation)
  );

  -- 索引: 图遍历加速 (BFS/DFS 的核心查询)
  CREATE INDEX idx_edges_source ON edges(source_id);
  CREATE INDEX idx_edges_target ON edges(target_id);
  CREATE INDEX idx_edges_relation ON edges(relation);

  -- ═══════════════════════════════════════════
  -- 社区表: communities
  -- ═══════════════════════════════════════════
  CREATE TABLE communities (
      id          INTEGER PRIMARY KEY,
      label       TEXT NOT NULL,
      cohesion    REAL NOT NULL DEFAULT 0.0,
      node_count  INTEGER NOT NULL DEFAULT 0,
      description TEXT NOT NULL DEFAULT ''    -- 社区摘要描述
  );

  -- ═══════════════════════════════════════════
  -- 文档块表: chunks (用于 RAG)
  -- ═══════════════════════════════════════════
  CREATE TABLE chunks (
      id          INTEGER PRIMARY KEY AUTOINCREMENT,
      node_id     TEXT REFERENCES nodes(id),  -- 关联的节点
      source_file TEXT NOT NULL,
      chunk_text  TEXT NOT NULL,              -- 原始文本块
      chunk_index INTEGER NOT NULL DEFAULT 0, -- 块在文件中的顺序
      start_line  INTEGER,                    -- 起始行号
      end_line    INTEGER,                    -- 结束行号
      chunk_type  TEXT NOT NULL DEFAULT 'doc' -- doc|code|transcript|image_desc
  );

  CREATE INDEX idx_chunks_node_id     ON chunks(node_id);
  CREATE INDEX idx_chunks_source_file ON chunks(source_file);

  -- ═══════════════════════════════════════════
  -- FTS5 全文搜索虚拟表
  -- ═══════════════════════════════════════════
  -- 代码标识符分词器: _extract_generic → extract, generic
  CREATE VIRTUAL TABLE nodes_fts USING fts5(
      label,
      norm_label,
      rationale,
      raw_text,
      source_file,
      content='nodes',
      content_rowid='rowid',
      tokenize='unicode61 categories "L* N* Co" tokenchars "_"'
  );

  CREATE VIRTUAL TABLE chunks_fts USING fts5(
      chunk_text,
      source_file,
      content='chunks',
      content_rowid='rowid',
      tokenize='unicode61 categories "L*N* Co" tokenchars "_"'
  );

  -- ═══════════════════════════════════════════
  -- 向量表: sqlite-vec
  -- ═══════════════════════════════════════════
  -- 每个节点一个 embedding (label + rationale 的语义表示)
  CREATE VIRTUAL TABLE node_vec USING vec0(
      node_id  TEXT PRIMARY KEY,
      embedding float[384]    -- all-MiniLM-L6-v2 的维度
  );

  -- 每个文档块一个 embedding (chunk_text 的语义表示)
  CREATE VIRTUAL TABLE chunk_vec USING vec0(
      chunk_id  INTEGER PRIMARY KEY,
      embedding float[384]
  );

  -- ═══════════════════════════════════════════
  -- 元数据表: schema version + 构建信息
  -- ═══════════════════════════════════════════
  CREATE TABLE metadata (
      key   TEXT PRIMARY KEY,
      value TEXT NOT NULL
  );
  INSERT INTO metadata VALUES ('schema_version', '1');
  INSERT INTO metadata VALUES ('embedding_model', 'all-MiniLM-L6-v2');
  INSERT INTO metadata VALUES ('embedding_dims', '384');

  ---
  三、GraphStore 抽象层

# graphify/store/__init__.py

  from __future__ import annotations
  from abc import ABC, abstractmethod
  from dataclasses import dataclass
  from typing import Generator

  @dataclass
  class Node:
      id: str
      label: str
      source_file: str
      source_loc: str
      file_type: str
      community: int | None
      degree: int
      rationale: str
      raw_text: str

  @dataclass
  class Edge:
      source_id: str
      target_id: str
      relation: str
      confidence: str
      conf_score: float | None
      weight: float

  @dataclass
  class SearchResult:
      """一次搜索的统一返回结构"""
      node_id: str
      label: str
      score: float              # 综合得分 (0.0-1.0)
      match_type: str           # "fts" | "vector" | "graph" | "hybrid"
      source_file: str
      snippet: str              # 高亮片段

  class GraphStore(ABC):
      """存储抽象层 — 所有存储后端的统一接口"""

      # ── 写入 ──────────────────────────────────────────                                                                                                                                        
                                                                                                                                                                                                  
      @abstractmethod                                                                                                                                                                             
      def upsert_nodes(self, nodes: list[Node]) -> int:
          """插入或更新节点。返回实际变更数。"""                                                                                                                                                  
                                                                                                                                                                                                  
      @abstractmethod                                                                                                                                                                             
      def upsert_edges(self, edges: list[Edge]) -> int:                                                                                                                                           
          """插入或更新边。返回实际变更数。"""                                                                                                                                                    
                                                                                                                                                                                                  
      @abstractmethod                                                                                                                                                                             
      def delete_nodes(self, node_ids: list[str]) -> int:                                                                                                                                         
          """删除节点及其关联边。"""                                                                                                                                                              
                                                                                                                                                                                                  
      @abstractmethod                                                                                                                                                                             
      def upsert_communities(self, communities: list[dict]) -> int:                                                                                                                               
          """更新社区信息。"""                                                                                                                                                                    
                                                                                                                                                                                                  
      @abstractmethod                                                                                                                                                                             
      def upsert_chunks(self, chunks: list[dict]) -> int:                                                                                                                                         
          """插入或更新文档块。"""                                                                                                                                                                
                                                                                                                                                                                                  
      @abstractmethod                                                                                                                                                                             
      def upsert_embeddings(self, embeddings: list[tuple[str, list[float]]]) -> int:                                                                                                              
          """存储节点向量。参数: [(node_id, embedding), ...]"""                                                                                                                                   
                                                                                                                                                                                                  
      @abstractmethod                                                                                                                                                                             
      def upsert_chunk_embeddings(self, embeddings: list[tuple[int, list[float]]]) -> int:                                                                                                        
          """存储文档块向量。参数: [(chunk_id, embedding), ...]"""                                                                                                                                
                                                                                                                                                                                                  
      # ── 搜索 ──────────────────────────────────────────                                                                                                                                        
                                                                                                                                                                                                  
      @abstractmethod                                                                                                                                                                             
      def fts_search(self, query: str, limit: int = 20, filters: dict | None = None) -> list[SearchResult]:
          """FTS5 全文搜索。filters: {file_type, source_file, community}"""                                                                                                                       
                                                                                                                                                                                                  
      @abstractmethod                                                                                                                                                                             
      def vector_search(self, query_embedding: list[float], limit: int = 20,                                                                                                                      
                        threshold: float = 0.5) -> list[SearchResult]:                                                                                                                            
          """向量近似搜索。"""                                                                                                                                                                    
                                                                                                                                                                                                  
      @abstractmethod                                                                                                                                                                             
      def chunk_vector_search(self, query_embedding: list[float], limit: int = 20,                                                                                                                
                              threshold: float = 0.5) -> list[dict]:                                                                                                                              
          """文档块向量搜索 (用于 RAG)。"""                                                                                                                                                       
                                                                                                                                                                                                  
      # ── 图查询 ──────────────────────────────────────────                                                                                                                                      
                                                                                                                                                                                                  
      @abstractmethod                                                                                                                                                                             
      def neighbors(self, node_id: str, depth: int = 1,
                    relation_filter: str | None = None) -> tuple[list[Node], list[Edge]]:                                                                                                         
          """BFS 获取邻居节点和边。"""                                                                                                                                                            
                                                                                                                                                                                                  
      @abstractmethod                                                                                                                                                                             
      def shortest_path(self, source_id: str, target_id: str,                                                                                                                                     
                        max_hops: int = 8) -> list[tuple[Node, Edge | None]]:                                                                                                                     
          """最短路径。返回 [(node, incoming_edge), ...]"""                                                                                                                                       
                                                                                                                                                                                                  
      @abstractmethod                                                                                                                                                                             
      def community_nodes(self, community_id: int) -> list[Node]:                                                                                                                                 
          """获取社区内所有节点。"""                                                                                                                                                              
                                                                                                                                                                                                  
      @abstractmethod                                                                                                                                                                             
      def god_nodes(self, top_n: int = 10) -> list[Node]:                                                                                                                                         
          """获取连接数最多的节点。"""                                                                                                                                                            
                                                                                                                                                                                                  
      @abstractmethod                                                                                                                                                                             
      def subgraph(self, node_ids: list[str]) -> tuple[list[Node], list[Edge]]:                                                                                                                   
          """获取指定节点集的导出子图。"""                                                                                                                                                        
                                                                                                                                                                                                  
      # ── 统计 ──────────────────────────────────────────                                                                                                                                        
                                                                                                                                                                                                  
      @abstractmethod                                                                                                                                                                             
      def stats(self) -> dict:
          """返回 {nodes, edges, communities, ...}"""                                                                                                                                             
                                                                                                                                                                                                  
      # ── 生命周期 ──────────────────────────────────────                                                                                                                                        
                                                                                                                                                                                                  
      @abstractmethod                                                                                                                                                                             
      def sync_from_graph(self, graph_path: str) -> None:
          """从 graph.json 同步到存储 (增量)。"""                                                                                                                                                 
                                                                                                                                                                                                  
      @abstractmethod                                                                                                                                                                             
      def begin_transaction(self) -> None:                                                                                                                                                        
          ...                                                                                                                                                                                     
                                                                                                                                                                                                  
      @abstractmethod                                                                                                                                                                             
      def commit(self) -> None:                                                                                                                                                                   
          ...                                                                                                                                                                                     
                                                                                                                                                                                                  
      @abstractmethod                                                                                                                                                                             
      def rollback(self) -> None:                                                                                                                                                                 
          ...                                                                                                                                                                                     
                                                                                                                                                                                                  
                                                                                                                                                                                                  
  class SQLiteStore(GraphStore):
      """基于 SQLite + FTS5 + sqlite-vec 的端侧存储实现"""

      def __init__(self, db_path: str = "graphify-out/graphify.db"):                                                                                                                              
          import sqlite3                                                                                                                                                                          
          self.conn = sqlite3.connect(db_path)                                                                                                                                                    
          self.conn.execute("PRAGMA journal_mode=WAL")       # 并发读                                                                                                                             
          self.conn.execute("PRAGMA foreign_keys=ON")                                                                                                                                             
          self.conn.execute("PRAGMA cache_size=-65536")       # 64MB cache                                                                                                                        
          self._init_schema()                                                                                                                                                                     
                                                                                                                                                                                                  
      # ... 实现所有抽象方法 ...                                                                                                                                                                  
                  
  ---
  四、三阶段查询引擎

# graphify/query.py

  from __future__ import annotations
  from dataclasses import dataclass
  from graphify.store import GraphStore, SearchResult

  @dataclass
  class QueryResult:
      answer: str                  # LLM 生成的回答
      source_nodes: list[str]      # 引用的节点 ID
      source_chunks: list[str]     # 引用的文档块
      confidence: float            # 置信度
      latency_ms: float            # 查询耗时

  class QueryEngine:
      """三阶段混合检索 + GraphRAG 生成"""

      def __init__(self, store: GraphStore, embedder, llm_client=None):                                                                                                                           
          self.store = store                                                                                                                                                                      
          self.embedder = embedder          # 本地 embedding 模型                                                                                                                                 
          self.llm = llm_client             # 可选: 本地 Ollama / 远程 API                                                                                                                        
                                                                                                                                                                                                  
      def search(self, query: str, *, limit: int = 10,                                                                                                                                            
                 mode: str = "hybrid", filters: dict | None = None) -> list[SearchResult]:                                                                                                        
          """统一搜索入口 — 不调用 LLM, 纯检索"""                                                                                                                                                 
          results: dict[str, SearchResult] = {}                                                                                                                                                   
                                                                                                                                                                                                  
          # ── Stage 1: FTS5 关键词召回 ──                                                                                                                                                        
          if mode in ("hybrid", "keyword"):                                                                                                                                                       
              fts_results = self.store.fts_search(query, limit=limit * 2, filters=filters)                                                                                                        
              for r in fts_results:                                                                                                                                                               
                  r.score *= 0.4                              # FTS 权重                                                                                                                          
                  r.match_type = "fts"                                                                                                                                                            
                  results[r.node_id] = r                                                                                                                                                          
                                                                                                                                                                                                  
          # ── Stage 2: 向量语义召回 ──                                                                                                                                                           
          if mode in ("hybrid", "semantic") and self.embedder:                                                                                                                                    
              query_emb = self.embedder.embed(query)                                                                                                                                              
              vec_results = self.store.vector_search(query_emb, limit=limit * 2)                                                                                                                  
              for r in vec_results:                                                                                                                                                               
                  if r.node_id in results:                                                                                                                                                        
                      results[r.node_id].score += r.score * 0.6  # 融合                                                                                                                           
                      results[r.node_id].match_type = "hybrid"                                                                                                                                    
                  else:                                                                                                                                                                           
                      r.score *= 0.6                           # Vector 权重                                                                                                                      
                      r.match_type = "vector"                                                                                                                                                     
                      results[r.node_id] = r                                                                                                                                                      
                                                                                                                                                                                                  
          # ── 排序 + 截断 ──                                                                                                                                                                     
          ranked = sorted(results.values(), key=lambda r: r.score, reverse=True)                                                                                                                  
          return ranked[:limit]                                                                                                                                                                   
                                                                                                                                                                                                  
      def ask(self, question: str, *, token_budget: int = 4000) -> QueryResult:                                                                                                                   
          """GraphRAG: 检索 + 图扩展 + LLM 生成"""                                                                                                                                                
                                                                                                                                                                                                  
          # 1. 混合检索 top-K 候选节点                                                                                                                                                            
          candidates = self.search(question, limit=5)                                                                                                                                             
                                                                                                                                                                                                  
          if not candidates:                                                                                                                                                                      
              return QueryResult(                                                                                                                                                                 
                  answer="未找到相关信息。",                                                                                                                                                      
                  source_nodes=[], source_chunks=[],                                                                                                                                              
                  confidence=0.0, latency_ms=0.0,                                                                                                                                                 
              )                                                                                                                                                                                   
                                                                                                                                                                                                  
          # 2. 从候选节点 BFS 扩展子图 (深度=2, 抓取上下文)                                                                                                                                       
          all_node_ids = set()                                                                                                                                                                    
          all_edges = []                                                                                                                                                                          
          for c in candidates:                                                                                                                                                                    
              nodes, edges = self.store.neighbors(c.node_id, depth=2)                                                                                                                             
              all_node_ids.update(n.id for n in nodes)                                                                                                                                            
              all_edges.extend(edges)                                                                                                                                                             
                                                                                                                                                                                                  
          # 3. 向量检索相关文档块 (RAG)                                                                                                                                                           
          query_emb = self.embedder.embed(question) if self.embedder else None                                                                                                                    
          chunks = []                                                                                                                                                                             
          if query_emb:                                                                                                                                                                           
              chunks = self.store.chunk_vector_search(query_emb, limit=5)                                                                                                                         
                                                                                                                                                                                                  
          # 4. 组装上下文 prompt                                                                                                                                                                  
          context = self._build_context(                                                                                                                                                          
              candidate_ids=[c.node_id for c in candidates],                                                                                                                                      
              subgraph_nodes=all_node_ids,                                                                                                                                                        
              subgraph_edges=all_edges,                                                                                                                                                           
              chunks=chunks,                                                                                                                                                                      
              budget=token_budget,                                                                                                                                                                
          )                                                                                                                                                                                       
                                                                                                                                                                                                  
          # 5. LLM 生成回答                                                                                                                                                                       
          answer = self._generate(question, context)                                                                                                                                              
                                                                                                                                                                                                  
          return QueryResult(                                                                                                                                                                     
              answer=answer,                                                                                                                                                                      
              source_nodes=[c.node_id for c in candidates],                                                                                                                                       
              source_chunks=[c["chunk_text"][:100] for c in chunks],                                                                                                                              
              confidence=candidates[0].score if candidates else 0.0,                                                                                                                              
              latency_ms=0.0,                                                                                                                                                                     
          )                                                                                                                                                                                       
                                                                                                                                                                                                  
      def _build_context(self, candidate_ids, subgraph_nodes,                                                                                                                                     
                          subgraph_edges, chunks, budget) -> str:                                                                                                                                 
          """把子图 + 文档块渲染为 LLM 可消费的文本"""                                                                                                                                            
          ...                                                                                                                                                                                     
                                                                                                                                                                                                  
      def _generate(self, question: str, context: str) -> str:                                                                                                                                    
          """调用 LLM 生成回答 (支持多种后端)"""                                                                                                                                                  
          if self.llm:                                                                                                                                                                            
              return self.llm.generate(                                                                                                                                                           
                  system="你是代码库知识图谱的助手。根据提供的图谱上下文回答问题。",                                                                                                              
                  user=f"## 上下文\n{context}\n\n## 问题\n{question}",                                                                                                                            
              )                                                                                                                                                                                   
          # 无 LLM 时返回结构化上下文                                                                                                                                                             
          return context                                                                                                                                                                          
                                                                                                                                                                                                  
  ---
  五、端侧 Embedding 方案

  ┌──────────────────────────────────────────────────────────┐
  │                 Embedding 服务层                          │
  │  ┌────────────────────────────────────────────────────┐  │
  │  │  首选: sentence-transformers (all-MiniLM-L6-v2)    │  │
  │  │  - 22MB 模型, 384 维                                │  │
  │  │  - 多语言支持 (中英混合)                             │  │
  │  │  - CPU 推理 < 5ms/条                                │  │
  │  └────────────────────────────────────────────────────┘  │
  │  ┌────────────────────────────────────────────────────┐  │
  │  │  备选: ONNX Runtime (无 PyTorch 依赖)               │  │
  │  │  - 模型转换为 ONNX 格式                              │  │
  │  │  - 更小的运行时占用                                   │  │
  │  └────────────────────────────────────────────────────┘  │
  │  ┌────────────────────────────────────────────────────┐  │
  │  │  降级: 无向量搜索                                    │  │
  │  │  - 仅 FTS5 + Graph 遍历                             │  │
  │  │  - 仍有工业级可用性                                   │  │
  │  └────────────────────────────────────────────────────┘  │
  │                                                          │
  │  对什么做 Embedding:                                      │
  │  - Node: label + " " + rationale                         │
  │  - Chunk: chunk_text (512 token 滑动窗口)                │
  │  - 特殊: image description / audio transcript            │
  └──────────────────────────────────────────────────────────┘

# graphify/embed.py

  from __future__ import annotations

  class Embedder:
      """端侧 embedding, 渐进降级"""

      def __init__(self, model_name: str = "all-MiniLM-L6-v2"):                                                                                                                                   
          self.model = None                                                                                                                                                                       
          self.dims = 384                                                                                                                                                                         
          try:                                                                                                                                                                                    
              from sentence_transformers import SentenceTransformer                                                                                                                               
              self.model = SentenceTransformer(model_name)                                                                                                                                        
              self.dims = self.model.get_sentence_embedding_dimension()                                                                                                                           
          except ImportError:                                                                                                                                                                     
              pass  # 降级: 无向量搜索                                                                                                                                                            
                                                                                                                                                                                                  
      def embed(self, text: str) -> list[float]:                                                                                                                                                  
          if self.model is None:                                                                                                                                                                  
              return []                                                                                                                                                                           
          return self.model.encode(text, normalize_embeddings=True).tolist()                                                                                                                      
                                                                                                                                                                                                  
      def embed_batch(self, texts: list[str]) -> list[list[float]]:                                                                                                                               
          if self.model is None:                                                                                                                                                                  
              return [[] for _ in texts]                                                                                                                                                          
          return self.model.encode(texts, normalize_embeddings=True).tolist()                                                                                                                     
                                                                                                                                                                                                  
      @property                                                                                                                                                                                   
      def available(self) -> bool:                                                                                                                                                                
          return self.model is not None                                                                                                                                                           
                                                                                                                                                                                                  
  ---
  六、LLM 连接层 — 支持所有主流后端

# graphify/llm.py

  from __future__ import annotations
  from abc import ABC, abstractmethod

  class LLMClient(ABC):
      @abstractmethod
      def generate(self, system: str, user: str, max_tokens: int = 2000) -> str: ...

  class OllamaClient(LLMClient):
      """本地 Ollama — 零延迟, 隐私优先"""
      def __init__(self, model: str = "qwen2.5-coder:7b"):
          self.model = model

      def generate(self, system, user, max_tokens=2000) -> str:                                                                                                                                   
          import requests
          resp = requests.post("http://localhost:11434/api/generate", json={                                                                                                                      
              "model": self.model, "prompt": f"{system}\n\n{user}",                                                                                                                               
              "stream": False, "options": {"num_predict": max_tokens},                                                                                                                            
          })                                                                                                                                                                                      
          return resp.json()["response"]                                                                                                                                                          
                                                                                                                                                                                                  
                                                                                                                                                                                                  
  class AnthropicClient(LLMClient):
      """Claude API — 最高质量"""
      def generate(self, system, user, max_tokens=2000) -> str:
          import anthropic
          client = anthropic.Anthropic()
          msg = client.messages.create(
              model="claude-sonnet-4-6-20250514",
              max_tokens=max_tokens,
              system=system,
              messages=[{"role": "user", "content": user}],
          )
          return msg.content[0].text

  class OpenAIClient(LLMClient):
      """OpenAI API"""
      def generate(self, system, user, max_tokens=2000) -> str:
          from openai import OpenAI
          client = OpenAI()
          resp = client.chat.completions.create(
              model="gpt-4o", max_tokens=max_tokens,
              messages=[
                  {"role": "system", "content": system},
                  {"role": "user", "content": user},
              ],
          )
          return resp.choices[0].message.content

  ---
  七、MCP 工具升级 — Agent 对话接口

# 在 serve.py 中新增的 MCP 工具定义

  @server.list_tools()
  async def list_tools() -> list[types.Tool]:
      return [
          # ── 现有工具保留 ──
          types.Tool(name="query_graph", ...),    # 向后兼容
          types.Tool(name="get_node", ...),
          types.Tool(name="get_neighbors", ...),
          types.Tool(name="shortest_path", ...),
          types.Tool(name="god_nodes", ...),
          types.Tool(name="graph_stats", ...),

          # ── 新增: 统一搜索 ──                                                                                                                                                                  
          types.Tool(                                                                                                                                                                             
              name="search",                                                                                                                                                                      
              description=(                                                                                                                                                                       
                  "搜索知识图谱 — 支持关键词、语义、混合模式。"                                                                                                                                   
                  "返回按相关性排序的节点列表, 包含上下文片段。"                                                                                                                                  
              ),                                                                                                                                                                                  
              inputSchema={                                                                                                                                                                       
                  "type": "object",                                                                                                                                                               
                  "properties": {                                                                                                                                                                 
                      "query": {"type": "string", "description": "搜索查询 (中英文均可)"},                                                                                                        
                      "mode": {                                                                                                                                                                   
                          "type": "string",                                                                                                                                                       
                          "enum": ["hybrid", "keyword", "semantic"],                                                                                                                              
                          "default": "hybrid",                                                                                                                                                    
                          "description": "hybrid=关键词+语义融合, keyword=纯FTS5, semantic=纯向量",                                                                                               
                      },                                                                                                                                                                          
                      "limit": {"type": "integer", "default": 10},                                                                                                                                
                      "filters": {                                                                                                                                                                
                          "type": "object",                                                                                                                                                       
                          "properties": {                                                                                                                                                         
                              "file_type": {"type": "string"},                                                                                                                                    
                              "source_file": {"type": "string"},                                                                                                                                  
                              "community": {"type": "integer"},                                                                                                                                   
                          },                                                                                                                                                                      
                      },                                                                                                                                                                          
                  },                                                                                                                                                                              
                  "required": ["query"],                                                                                                                                                          
              },                                                                                                                                                                                  
          ),                                                                                                                                                                                      
                                                                                                                                                                                                  
          # ── 新增: 对话问答 (GraphRAG) ──                                                                                                                                                       
          types.Tool(                                                                                                                                                                             
              name="ask",                                                                                                                                                                         
              description=(                                                                                                                                                                       
                  "基于知识图谱的对话问答。自动检索相关节点 → 扩展子图 → 生成回答。"                                                                                                              
                  "适用于: '缓存怎么工作的?', 'X和Y有什么关系?', '这个模块的架构是什么?'"                                                                                                         
              ),                                                                                                                                                                                  
              inputSchema={                                                                                                                                                                       
                  "type": "object",                                                                                                                                                               
                  "properties": {                                                                                                                                                                 
                      "question": {"type": "string", "description": "自然语言问题"},
                      "detail_level": {                                                                                                                                                           
                          "type": "string",                                                                                                                                                       
                          "enum": ["brief", "detailed", "comprehensive"],                                                                                                                         
                          "default": "detailed",                                                                                                                                                  
                      },                                                                                                                                                                          
                  },                                                                                                                                                                              
                  "required": ["question"],                                                                                                                                                       
              },                                                                                                                                                                                  
          ),                                                                                                                                                                                      
                                                                                                                                                                                                  
          # ── 新增: 代码追踪 ──                                                                                                                                                                  
          types.Tool(                                                                                                                                                                             
              name="trace",                                                                                                                                                                       
              description=(                                                                                                                                                                       
                  "追踪一个函数/模块的完整调用链和数据流。"                                                                                                                                       
                  "从指定节点出发, 沿调用边和导入边展开有向图。"                                                                                                                                  
              ),                                                                                                                                                                                  
              inputSchema={                                                                                                                                                                       
                  "type": "object",                                                                                                                                                               
                  "properties": {                                                                                                                                                                 
                      "label": {"type": "string", "description": "起始节点 (函数/类/文件名)"},                                                                                                    
                      "direction": {                                                                                                                                                              
                          "type": "string",                                                                                                                                                       
                          "enum": ["callers", "callees", "both"],                                                                                                                                 
                          "default": "both",                                                                                                                                                      
                      },                                                                                                                                                                          
                      "depth": {"type": "integer", "default": 3},                                                                                                                                 
                  },                                                                                                                                                                              
                  "required": ["label"],                                                                                                                                                          
              },                                                                                                                                                                                  
          ),                                                                                                                                                                                      
                                                                                                                                                                                                  
          # ── 新增: 差异查询 ──                                                                                                                                                                  
          types.Tool(
              name="what_changed",
              description=(                                                                                                                                                                       
                  "比较两次构建之间的图谱差异: 新增/删除/修改的节点和边。"                                                                                                                        
                  "需要配合 watch 模式的增量更新使用。"                                                                                                                                           
              ),                                                                                                                                                                                  
              inputSchema={                                                                                                                                                                       
                  "type": "object",                                                                                                                                                               
                  "properties": {                                                                                                                                                                 
                      "since": {"type": "string", "description": "时间戳或 'last'"},                                                                                                              
                  },                                                                                                                                                                              
              },                                                                                                                                                                                  
          ),                                                                                                                                                                                      
      ]                                                                                                                                                                                           
                                                                                                                                                                                                  
  ---
  八、数据管线 — 从提取到可查询

  当前管线:
    detect → extract → build → cluster → analyze → report → export
                                                        ↓
                                                     graph.json (静态)

  升级后管线:
    detect → extract → build → cluster → analyze → report → export
                    ↓                                      ↓
              ┌─────────────┐                         graph.json
              │  Indexer    │                         (人类可读)
              │  (新增)      │
              └──────┬──────┘
                     │
      ┌──────────────▼──────────────────┐
      │  graphify.db  (机器可查询)       │
      │                                 │
      │  1. nodes/edges 写入            │
      │  2. FTS5 索引自动更新           │
      │  3. communities 写入            │
      │  4. chunks 切分 + 写入          │
      │  5. embedding 计算 + 写入       │
      └─────────────────────────────────┘

# graphify/indexer.py

  from __future__ import annotations
  from pathlib import Path
  from graphify.store import SQLiteStore, Node, Edge
  from graphify.embed import Embedder

  class Indexer:
      """将提取结果索引到 SQLite, 紧跟在 build() 之后执行"""

      def __init__(self, store: SQLiteStore, embedder: Embedder | None = None):                                                                                                                   
          self.store = store                                                                                                                                                                      
          self.embedder = embedder                                                                                                                                                                
                                                                                                                                                                                                  
      def index_graph(self, graph_data: dict, full: bool = False) -> dict:                                                                                                                        
          """                                                                                                                                                                                     
          全量或增量索引。                                                                                                                                                                        
                                                                                                                                                                                                  
          graph_data: extract/build 的输出 dict                                                                                                                                                   
          full=True:  清空重建 (首次或 schema 变更时)                                                                                                                                             
          full=False: 增量 upsert                                                                                                                                                                 
                  
          返回 {"nodes_indexed": N, "edges_indexed": M, "embeddings_computed": E}                                                                                                                 
          """     
          self.store.begin_transaction()                                                                                                                                                          
          try:                                                                                                                                                                                    
              if full:                                                                                                                                                                            
                  self._full_reindex(graph_data)                                                                                                                                                  
              else:                                                                                                                                                                               
                  self._incremental_reindex(graph_data)                                                                                                                                           
              self.store.commit()                                                                                                                                                                 
          except Exception:                                                                                                                                                                       
              self.store.rollback()                                                                                                                                                               
              raise                                                                                                                                                                               
                                                                                                                                                                                                  
      def _full_reindex(self, data: dict) -> dict:                                                                                                                                                
          nodes = self._convert_nodes(data.get("nodes", []))                                                                                                                                      
          edges = self._convert_edges(data.get("links", data.get("edges", [])))                                                                                                                   
          communities = data.get("communities", [])                                                                                                                                               
                                                                                                                                                                                                  
          self.store.upsert_nodes(nodes)                                                                                                                                                          
          self.store.upsert_edges(edges)                                                                                                                                                          
          self.store.upsert_communities(communities)                                                                                                                                              
                                                                                                                                                                                                  
          # 切分文档块                                                                                                                                                                            
          chunks = self._chunk_documents(nodes)                                                                                                                                                   
          self.store.upsert_chunks(chunks)                                                                                                                                                        
                                                                                                                                                                                                  
          # 计算 embedding (批量, 可异步)                                                                                                                                                         
          if self.embedder and self.embedder.available:                                                                                                                                           
              self._compute_and_store_embeddings(nodes, chunks)                                                                                                                                   
                                                                                                                                                                                                  
      def _incremental_reindex(self, data: dict) -> dict:                                                                                                                                         
          """只处理新增/变更的节点"""                                                                                                                                                             
          new_nodes = self._convert_nodes(data.get("nodes", []))                                                                                                                                  
          existing = {n.id for n in new_nodes if self.store.node_exists(n.id)}                                                                                                                    
                                                                                                                                                                                                  
          # 删除旧节点关联数据                                                                                                                                                                    
          removed = [n.id for n in new_nodes if n.id in existing]                                                                                                                                 
          if removed:                                                                                                                                                                             
              self.store.delete_nodes(removed)                                                                                                                                                    
                                                                                                                                                                                                  
          self.store.upsert_nodes(new_nodes)                                                                                                                                                      
          self.store.upsert_edges(self._convert_edges(data.get("links", [])))                                                                                                                     
                                                                                                                                                                                                  
          if self.embedder and self.embedder.available:                                                                                                                                           
              new_only = [n for n in new_nodes if n.id not in existing]                                                                                                                           
              if new_only:                                                                                                                                                                        
                  chunks = self._chunk_documents(new_only)                                                                                                                                        
                  self.store.upsert_chunks(chunks)                                                                                                                                                
                  self._compute_and_store_embeddings(new_only, chunks)                                                                                                                            
                                                                                                                                                                                                  
      def _chunk_documents(self, nodes: list[Node]) -> list[dict]:                                                                                                                                
          """将 raw_text 切分为 512 token 的块, 滑动窗口 128 token 重叠"""                                                                                                                        
          chunks = []                                                                                                                                                                             
          for node in nodes:                                                                                                                                                                      
              if not node.raw_text:                                                                                                                                                               
                  continue                                                                                                                                                                        
              text = node.raw_text                                                                                                                                                                
              # 简单切分: ~512 词, 128 词重叠                                                                                                                                                     
              words = text.split()                                                                                                                                                                
              step = 384  # 512 - 128                                                                                                                                                             
              for i in range(0, len(words), step):                                                                                                                                                
                  chunk_words = words[i:i + 512]                                                                                                                                                  
                  chunks.append({                                                                                                                                                                 
                      "node_id": node.id,                                                                                                                                                         
                      "source_file": node.source_file,                                                                                                                                            
                      "chunk_text": " ".join(chunk_words),                                                                                                                                        
                      "chunk_index": i // step,                                                                                                                                                   
                      "chunk_type": node.file_type,                                                                                                                                               
                  })                                                                                                                                                                              
          return chunks                                                                                                                                                                           
                                                                                                                                                                                                  
      def _compute_and_store_embeddings(self, nodes, chunks):                                                                                                                                     
          texts = [f"{n.label} {n.rationale}" for n in nodes]                                                                                                                                     
          embeddings = self.embedder.embed_batch(texts)                                                                                                                                           
          self.store.upsert_embeddings([(n.id, e) for n, e in zip(nodes, embeddings) if e])                                                                                                       
                                                                                                                                                                                                  
          if chunks:                                                                                                                                                                              
              chunk_texts = [c["chunk_text"] for c in chunks]                                                                                                                                     
              chunk_embs = self.embedder.embed_batch(chunk_texts)                                                                                                                                 
              # chunk_id 在 upsert_chunks 时生成, 需要 store 返回 IDs                                                                                                                             
              ...                                                                                                                                                                                 
                                                                                                                                                                                                  
  ---
  九、端侧约束与优化策略

  ┌─────────────────────────────────────────────────────────────┐
  │                    端侧资源预算                               │
  ├──────────┬──────────┬──────────────────────────────────────┤
  │ 资源      │ 预算     │ 策略                                 │
  ├──────────┼──────────┼──────────────────────────────────────┤
  │ 磁盘     │ < 50MB   │ SQLite 单文件 + WAL 模式              │
  │          │          │ graphify.db ≈ graph.json × 1.5        │
  ├──────────┼──────────┼──────────────────────────────────────┤
  │ 内存     │ < 100MB  │ SQLite 不加载全图到内存                │
  │          │          │ NetworkX 只在 build/cluster 时用       │
  │          │          │ Embedding 模型 22MB                    │
  ├──────────┼──────────┼──────────────────────────────────────┤
  │ CPU      │ 搜索 <   │ FTS5 索引: O(1) 查找                  │
  │          │ 50ms     │ 向量搜索: sqlite-vec HNSW 索引        │
  │          │          │ 图遍历: SQLite recursive CTE          │
  ├──────────┼──────────┼──────────────────────────────────────┤
  │ Embedding│ < 5ms/条 │ 批量编码, 结果缓存到 SQLite           │
  │ 推理     │          │ 首次全量, 后续增量                     │
  ├──────────┼──────────┼──────────────────────────────────────┤
  │ 首次构建 │ < 30s    │ 379 节点: embed 2s + 索引 1s          │
  │          │          │ 37,000 节点: embed 3min + 索引 30s    │
  └──────────┴──────────┴──────────────────────────────────────┘

  SQLite 图遍历替代 NetworkX (关键优化)：

  -- 用递归 CTE 实现 BFS, 完全在 SQLite 层执行, 不加载到 Python
  WITH RECURSIVE bfs(level, node_id) AS (
      VALUES (0, 'build_build')          -- 起始节点
      UNION ALL
      SELECT b.level + 1, e.target_id
      FROM edges e, bfs b
      WHERE e.source_id = b.node_id
        AND b.level < 3                   -- 深度限制
  )
  SELECT DISTINCT n.id, n.label, n.source_file, n.community
  FROM bfs b JOIN nodes n ON n.id = b.node_id
  ORDER BY b.level, n.degree DESC;

  ---
  十、目录结构

  graphify/
  ├── __init__.py
  ├── __main__.py           # CLI (扩展: graphify serve, graphify reindex)
  ├── extract.py            # 现有: AST 提取
  ├── build.py              # 现有: 图构建
  ├── cluster.py            # 现有: 社区检测
  ├── analyze.py            # 现有: 图分析
  ├── report.py             # 现有: 报告生成
  ├── export.py             # 现有: 导出
  ├── cache.py              # 现有: 文件缓存
  ├── watch.py              # 现有: 文件监听 (升级: 触发 reindex)
  ├── serve.py              # 现有: MCP server (升级: 新工具)
  │
  ├── store/                # 新增: 存储抽象层
  │   ├── __init__.py       # GraphStore ABC
  │   ├── sqlite.py         # SQLiteStore 实现
  │   └── schema.sql        # DDL
  │
  ├── query.py              # 新增: QueryEngine (三阶段检索)
  ├── embed.py              # 新增: Embedder (端侧向量)
  ├── llm.py                # 新增: LLM 客户端适配
  ├── indexer.py            # 新增: 图谱→SQLite 索引管道
  │
  └── graphify-out/
      ├── graph.json         # 现有: 人类可读快照
      ├── graph.html         # 现有: 可视化
      ├── GRAPH_REPORT.md    # 现有: 审计报告
      ├── graphify.db        # 新增: 机器可查询索引
      └── cache/             # 现有: 提取缓存

  ---
  十一、使用场景端到端演示

  场景: 开发者问 "缓存系统怎么工作的？"

  Agent 调用 MCP tool: ask(question="缓存系统怎么工作的？")

  QueryEngine 内部执行:
    Stage 1 (FTS5):
      → "缓存" / "cache" → 命中: check_semantic_cache(), cache_dir(), cached_files()...
      → score: [0.82, 0.76, 0.71, ...]

    Stage 2 (Vector):                                                                                                                                                                             
      → embed("缓存系统怎么工作的") → 向量搜索                                                                                                                                                    
      → 命中: _body_content(), file_hash(), clear_cache()...                                                                                                                                      
      → score: [0.91, 0.84, 0.78, ...]                                                                                                                                                            
                                                                                                                                                                                                  
    融合排序: check_semantic_cache(0.91), cache_dir(0.86), _body_content(0.84)...                                                                                                                 
                                                                                                                                                                                                  
    Stage 3 (Graph Expand):                                                                                                                                                                       
      从 top-3 节点 BFS depth=2
      → 22 nodes, 31 edges 子图                                                                                                                                                                   
      → 包含: cache.py 文件节点, 7 个函数, 调用关系, community=7                                                                                                                                  
                                                                                                                                                                                                  
    RAG Chunks:                                                                                                                                                                                   
      → cache.py 源码块 2 个, 相关文档块 1 个                                                                                                                                                     
                                                                                                                                                                                                  
    LLM Generation:                                                                                                                                                                               
      System: "你是代码库知识图谱的助手..."                                                                                                                                                       
      Context: [22 nodes, 31 edges, 3 chunks]                                                                                                                                                     
      Question: "缓存系统怎么工作的？"                                                                                                                                                            
                                                                                                                                                                                                  
    回答:                                                                                                                                                                                         
      "Graphify 的缓存系统由 cache.py 模块实现, 包含以下核心组件:                                                                                                                                 
       1. file_hash() 计算文件的 SHA256 作为缓存键                                                                                                                                                
       2. load_cached() / save_cached() 管理缓存的读写                                                                                                                                            
       3. check_semantic_cache() 支持批量语义缓存检查                                                                                                                                             
       缓存存储在 graphify-out/cache/{hash}.json, 支持 Markdown frontmatter 忽略..."                                                                                                              
                                                                                                                                                                                                  
    耗时: FTS5 2ms + Vector 8ms + Graph 3ms + LLM 1.2s = ~1.2s                                                                                                                                    
                                                                                                                                                                                                  
  ---
  十二、渐进式实施路线

  ┌──────────────┬─────────────────────────────┬───────────────────────────────────┬───────────────────────────────────────┐
  │     阶段     │            内容             │               依赖                │                交付物                 │
  ├──────────────┼─────────────────────────────┼───────────────────────────────────┼───────────────────────────────────────┤
  │ P0: 基础     │ SQLiteStore + FTS5 + schema │ sqlite3 (标准库)                  │ graphify reindex CLI, fts_search 可用 │
  ├──────────────┼─────────────────────────────┼───────────────────────────────────┼───────────────────────────────────────┤
  │ P1: 语义     │ Embedder + sqlite-vec       │ sentence-transformers, sqlite-vec │ vector_search 可用, hybrid 模式       │
  ├──────────────┼─────────────────────────────┼───────────────────────────────────┼───────────────────────────────────────┤
  │ P2: GraphRAG │ QueryEngine + LLM client    │ Ollama/Anthropic/OpenAI           │ ask() 工具, 对话式问答                │
  ├──────────────┼─────────────────────────────┼───────────────────────────────────┼───────────────────────────────────────┤
  │ P3: 增量     │ watch.py 集成 indexer       │ watchdog                          │ 文件变更 → 自动 reindex               │
  ├──────────────┼─────────────────────────────┼───────────────────────────────────┼───────────────────────────────────────┤
  │ P4: 多模态   │ 图片描述/音频转录块索引     │ 现有 transcribe.py                │ 多模态内容可搜索                      │
  ├──────────────┼─────────────────────────────┼───────────────────────────────────┼───────────────────────────────────────┤
  │ P5: 跨项目   │ 多 graphify.db 联邦查询     │ 无新依赖                          │ ATTACH DATABASE 跨库搜索              │
  └──────────────┴─────────────────────────────┴───────────────────────────────────┴───────────────────────────────────────┘

  ---
  十三、与现有代码的兼容策略

  关键决策: 双写, 不替代

  graph.json          → 保留 (人类可读, 可视化, Git diff)
  graph.html          → 保留 (交互式浏览器可视化)
  GRAPH_REPORT.md     → 保留 (审计报告)
  graphify.db         → 新增 (机器可查询索引)

  build() 之后:
    1. to_json(G, ...)      → graph.json  (现有逻辑不变)
    2. to_html(G, ...)      → graph.html  (现有逻辑不变)
    3. generate(...)        → GRAPH_REPORT.md (现有逻辑不变)
    4. Indexer.index_graph(data)  → graphify.db (新增, 可选)

  serve.py:
    1. 检测 graphify.db 是否存在
    2. 存在 → SQLiteStore (FTS5 + Vector + Graph)
    3. 不存在 → 降级到现有 NetworkX 内存加载 (向后兼容)

  这个设计的核心思想是：SQLite 作为查询加速层, 不是存储替代。graph.json 仍然是 source of truth, graphify.db 是它的可查询投影。任何时刻删掉 graphify.db 都不影响系统运行, graphify reindex 可以从  
  graph.json 完整重建。
