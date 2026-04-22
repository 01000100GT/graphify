# SQLite-based graph store with FTS5 full-text search
from __future__ import annotations

import json
import re
import sqlite3
import threading
import unicodedata
from pathlib import Path
from typing import Generator

from .store import GraphStore


def _strip_diacritics(text: str) -> str:
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


_SCHEMA_V1 = """\
-- Schema version tracking
CREATE TABLE IF NOT EXISTS _meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
INSERT OR IGNORE INTO _meta (key, value) VALUES ('schema_version', '2');

-- Core node table
CREATE TABLE IF NOT EXISTS nodes (
    id             TEXT PRIMARY KEY,
    label          TEXT NOT NULL DEFAULT '',
    file_type      TEXT NOT NULL DEFAULT '',
    source_file    TEXT NOT NULL DEFAULT '',
    source_location TEXT NOT NULL DEFAULT '',
    community      INTEGER,
    norm_label     TEXT NOT NULL DEFAULT '',
    degree         INTEGER NOT NULL DEFAULT 0,
    raw_text       TEXT,
    created_at     REAL NOT NULL DEFAULT (strftime('%s','now')),
    updated_at     REAL NOT NULL DEFAULT (strftime('%s','now'))
);

-- Core edge table
CREATE TABLE IF NOT EXISTS edges (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source          TEXT NOT NULL,
    target          TEXT NOT NULL,
    relation        TEXT NOT NULL DEFAULT '',
    confidence      TEXT NOT NULL DEFAULT 'EXTRACTED',
    confidence_score REAL NOT NULL DEFAULT 1.0,
    weight          REAL NOT NULL DEFAULT 1.0,
    source_file     TEXT NOT NULL DEFAULT '',
    source_location TEXT NOT NULL DEFAULT '',
    _src            TEXT NOT NULL DEFAULT '',
    _tgt            TEXT NOT NULL DEFAULT '',
    UNIQUE(source, target, relation)
);

-- Indexes for graph traversal
CREATE INDEX IF NOT EXISTS idx_nodes_source_file ON nodes(source_file);
CREATE INDEX IF NOT EXISTS idx_nodes_file_type   ON nodes(file_type);
CREATE INDEX IF NOT EXISTS idx_nodes_community   ON nodes(community);
CREATE INDEX IF NOT EXISTS idx_nodes_degree      ON nodes(degree DESC);
CREATE INDEX IF NOT EXISTS idx_edges_source      ON edges(source);
CREATE INDEX IF NOT EXISTS idx_edges_target      ON edges(target);
CREATE INDEX IF NOT EXISTS idx_edges_relation    ON edges(relation);
"""

_FTS5_SCHEMA = """\
-- FTS5 full-text search virtual table (content table pattern)
-- tokenize: unicode61 with underscore as token character for code identifiers
CREATE VIRTUAL TABLE IF NOT EXISTS nodes_fts USING fts5(
    label,
    norm_label,
    source_file,
    content='nodes',
    content_rowid='rowid',
    tokenize="unicode61 tokenchars '_'"
);

-- Triggers to keep FTS5 in sync
CREATE TRIGGER IF NOT EXISTS nodes_fts_ai AFTER INSERT ON nodes BEGIN
    INSERT INTO nodes_fts(rowid, label, norm_label, source_file)
    VALUES (new.rowid, new.label, new.norm_label, new.source_file);
END;

CREATE TRIGGER IF NOT EXISTS nodes_fts_ad AFTER DELETE ON nodes BEGIN
    INSERT INTO nodes_fts(nodes_fts, rowid, label, norm_label, source_file)
    VALUES ('delete', old.rowid, old.label, old.norm_label, old.source_file);
END;

CREATE TRIGGER IF NOT EXISTS nodes_fts_au AFTER UPDATE ON nodes BEGIN
    INSERT INTO nodes_fts(nodes_fts, rowid, label, norm_label, source_file)
    VALUES ('delete', old.rowid, old.label, old.norm_label, old.source_file);
    INSERT INTO nodes_fts(rowid, label, norm_label, source_file)
    VALUES (new.rowid, new.label, new.norm_label, new.source_file);
END;
"""


def _fts5_available(conn: sqlite3.Connection) -> bool:
    """Check if FTS5 is compiled into this SQLite build."""
    try:
        conn.execute("SELECT fts5(?1)", ("test",))
        return True
    except sqlite3.OperationalError:
        return False


def _code_aware_tokens(text: str) -> str:
    """Expand text for FTS5 querying: split camelCase/snake_case into separate tokens."""
    text = re.sub(r'[_\-]', ' ', text)
    text = re.sub(r'([a-z])([A-Z])', r'\1 \2', text)
    return text


class SQLiteStore(GraphStore):
    """SQLite-based graph store with FTS5 full-text search.

    Uses WAL mode for concurrent reads during writes.
    Schema migration via _meta table version tracking.
    """

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self._conn: sqlite3.Connection | None = None
        self._fts_ok: bool | None = None
        self._lock = threading.Lock()

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA cache_size=-32768")  # 32MB cache
            self._init_schema()
        return self._conn

    def _init_schema(self) -> None:
        assert self._conn is not None
        cur = self._conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='_meta'")
        if cur.fetchone() is None:
            # Fresh database: create everything
            self._conn.executescript(_SCHEMA_V1)
            if _fts5_available(self._conn):
                try:
                    self._conn.executescript(_FTS5_SCHEMA)
                    self._fts_ok = True
                except sqlite3.OperationalError:
                    self._fts_ok = False
            else:
                self._fts_ok = False
        else:
            # Existing database: check version and migrate
            row = self._conn.execute("SELECT value FROM _meta WHERE key='schema_version'").fetchone()
            version = int(row["value"]) if row else 0
            if version < 2:
                self._migrate_v1_to_v2()
            # Check FTS5 availability
            self._fts_ok = _fts5_available(self._conn) and self._has_fts_table()

    def _has_fts_table(self) -> bool:
        assert self._conn is not None
        cur = self._conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='nodes_fts'")
        return cur.fetchone() is not None

    def _migrate_v1_to_v2(self) -> None:
        """Add FTS5 tables to a v1 database."""
        assert self._conn is not None
        if _fts5_available(self._conn):
            try:
                self._conn.executescript(_FTS5_SCHEMA)
            except sqlite3.OperationalError:
                pass
        self._conn.execute("UPDATE _meta SET value='2' WHERE key='schema_version'")
        self._conn.commit()

    # ── Write ────────────────────────────────────────────────────────

    def upsert_nodes(self, nodes: list[dict]) -> int:
        if not nodes:
            return 0
        cols = ["id", "label", "file_type", "source_file", "source_location",
                "community", "norm_label", "degree", "raw_text", "updated_at"]
        rows = []
        now = _now()
        for n in nodes:
            rows.append((
                n.get("id", ""),
                n.get("label", ""),
                n.get("file_type", ""),
                n.get("source_file", ""),
                n.get("source_location", ""),
                n.get("community"),
                n.get("norm_label") or _strip_diacritics(n.get("label", "")).lower(),
                n.get("degree", 0),
                n.get("raw_text"),
                now,
            ))
        self.conn.executemany(
            f"INSERT OR REPLACE INTO nodes ({', '.join(cols)}) "
            f"VALUES ({', '.join('?' * len(cols))})",
            rows,
        )
        self.conn.commit()
        return len(rows)

    def upsert_edges(self, edges: list[dict]) -> int:
        if not edges:
            return 0
        rows = []
        for e in edges:
            rows.append((
                e.get("source", ""),
                e.get("target", ""),
                e.get("relation", ""),
                e.get("confidence", "EXTRACTED"),
                e.get("confidence_score", 1.0),
                e.get("weight", 1.0),
                e.get("source_file", ""),
                e.get("source_location", ""),
                e.get("_src", e.get("source", "")),
                e.get("_tgt", e.get("target", "")),
            ))
        self.conn.executemany(
            "INSERT OR REPLACE INTO edges "
            "(source, target, relation, confidence, confidence_score, weight, "
            "source_file, source_location, _src, _tgt) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        self.conn.commit()
        return len(rows)

    def delete_nodes(self, node_ids: list[str]) -> int:
        if not node_ids:
            return 0
        # Delete edges referencing these nodes
        for batch in _chunked(node_ids, 500):
            placeholders = ",".join("?" * len(batch))
            self.conn.execute(f"DELETE FROM edges WHERE source IN ({placeholders})", batch)
            self.conn.execute(f"DELETE FROM edges WHERE target IN ({placeholders})", batch)
            self.conn.execute(f"DELETE FROM nodes WHERE id IN ({placeholders})", batch)
        self.conn.commit()
        return len(node_ids)

    # ── Search ───────────────────────────────────────────────────────

    def search_nodes(self, query: str, limit: int = 20, filters: dict | None = None) -> list[dict]:
        """Search nodes using FTS5 (if available) or LIKE fallback."""
        if self._fts_ok:
            return self._search_fts(query, limit, filters)
        return self._search_like(query, limit, filters)

    def _search_fts(self, query: str, limit: int, filters: dict | None) -> list[dict]:
        tokens = _code_aware_tokens(query).split()
        if not tokens:
            return []
        # Build FTS5 query: each token as a prefix match joined by OR
        fts_query = " OR ".join(f'"{t}"*' if len(t) > 2 else f'"{t}"' for t in tokens)
        sql = """
            SELECT n.id, n.label, n.file_type, n.source_file, n.source_location,
                   n.community, n.norm_label, n.degree,
                   -bm25(nodes_fts) as score
            FROM nodes_fts f
            JOIN nodes n ON n.rowid = f.rowid
            WHERE nodes_fts MATCH ?
        """
        params: list = [fts_query]
        if filters:
            if filters.get("file_type"):
                sql += " AND n.file_type = ?"
                params.append(filters["file_type"])
            if filters.get("source_file"):
                sql += " AND n.source_file LIKE ?"
                params.append(f"%{filters['source_file']}%")
            if filters.get("community") is not None:
                sql += " AND n.community = ?"
                params.append(filters["community"])
        sql += " ORDER BY score DESC LIMIT ?"
        params.append(limit)
        rows = self.conn.execute(sql, params).fetchall()
        return [_row_to_dict(r) for r in rows]

    def _search_like(self, query: str, limit: int, filters: dict | None) -> list[dict]:
        """Fallback LIKE-based search when FTS5 is unavailable."""
        term = f"%{_strip_diacritics(query).lower()}%"
        sql = """
            SELECT id, label, file_type, source_file, source_location,
                   community, norm_label, degree, 0.5 as score
            FROM nodes
            WHERE (norm_label LIKE ? OR source_file LIKE ?)
        """
        params: list = [term, term]
        if filters:
            if filters.get("file_type"):
                sql += " AND file_type = ?"
                params.append(filters["file_type"])
            if filters.get("source_file"):
                sql += " AND source_file LIKE ?"
                params.append(f"%{filters['source_file']}%")
            if filters.get("community") is not None:
                sql += " AND community = ?"
                params.append(filters["community"])
        sql += " ORDER BY degree DESC LIMIT ?"
        params.append(limit)
        rows = self.conn.execute(sql, params).fetchall()
        return [_row_to_dict(r) for r in rows]

    def get_node(self, node_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
        return _row_to_dict(row) if row else None

    def get_neighbors(self, node_id: str, relation_filter: str = "") -> list[dict]:
        """Return neighbors with edge details. Each dict has 'node' and 'edge' keys."""
        sql = """
            SELECT n.id, n.label, n.file_type, n.source_file, n.source_location,
                   n.community, n.degree,
                   e.relation, e.confidence, e.confidence_score, e.weight
            FROM edges e
            JOIN nodes n ON n.id = e.target
            WHERE e.source = ?
        """
        params: list = [node_id]
        if relation_filter:
            sql += " AND e.relation LIKE ?"
            params.append(f"%{relation_filter}%")
        sql += " UNION ALL "
        sql += """
            SELECT n.id, n.label, n.file_type, n.source_file, n.source_location,
                   n.community, n.degree,
                   e.relation, e.confidence, e.confidence_score, e.weight
            FROM edges e
            JOIN nodes n ON n.id = e.source
            WHERE e.target = ?
        """
        params.append(node_id)
        if relation_filter:
            sql += " AND e.relation LIKE ?"
            params.append(f"%{relation_filter}%")
        rows = self.conn.execute(sql, params).fetchall()
        results = []
        for r in rows:
            d = _row_to_dict(r)
            results.append({
                "node": {k: d[k] for k in ("id", "label", "file_type", "source_file",
                                            "source_location", "community", "degree")},
                "edge": {k: d[k] for k in ("relation", "confidence", "confidence_score", "weight")},
            })
        return results

    def get_community(self, community_id: int) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM nodes WHERE community = ? ORDER BY degree DESC",
            (community_id,),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def god_nodes(self, top_n: int = 10) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM nodes ORDER BY degree DESC LIMIT ?",
            (top_n,),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    # ── Stats ────────────────────────────────────────────────────────

    def stats(self) -> dict:
        n_nodes = self.conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
        n_edges = self.conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        n_communities = self.conn.execute(
            "SELECT COUNT(DISTINCT community) FROM nodes WHERE community IS NOT NULL"
        ).fetchone()[0]
        return {
            "nodes": n_nodes,
            "edges": n_edges,
            "communities": n_communities,
        }

    # ── Bulk / Transaction ───────────────────────────────────────────

    def begin_bulk(self) -> None:
        self.conn.execute("BEGIN")

    def commit_bulk(self) -> None:
        self.conn.commit()

    def rollback(self) -> None:
        self.conn.rollback()

    # ── Sync ─────────────────────────────────────────────────────────

    def sync_from_graph(self, graph_path: str) -> dict:
        """Load graph.json and upsert all nodes/edges into SQLite."""
        import networkx as nx
        from networkx.readwrite import json_graph

        raw = json.loads(Path(graph_path).read_text(encoding="utf-8"))
        try:
            G = json_graph.node_link_graph(raw, edges="links")
        except TypeError:
            G = json_graph.node_link_graph(raw)

        degree = dict(G.degree())

        # Build community map
        node_community: dict[str, int] = {}
        for nid, data in G.nodes(data=True):
            cid = data.get("community")
            if cid is not None:
                node_community[nid] = cid

        # Upsert nodes
        nodes = []
        for nid, data in G.nodes(data=True):
            nodes.append({
                "id": nid,
                "label": data.get("label", ""),
                "file_type": data.get("file_type", ""),
                "source_file": data.get("source_file", ""),
                "source_location": data.get("source_location", ""),
                "community": node_community.get(nid),
                "norm_label": data.get("norm_label") or _strip_diacritics(data.get("label", "")).lower(),
                "degree": degree.get(nid, 0),
                "raw_text": data.get("raw_text"),
            })

        # Upsert edges
        edges = []
        for u, v, data in G.edges(data=True):
            edges.append({
                "source": u,
                "target": v,
                "relation": data.get("relation", ""),
                "confidence": data.get("confidence", "EXTRACTED"),
                "confidence_score": data.get("confidence_score", 1.0),
                "weight": data.get("weight", 1.0),
                "source_file": data.get("source_file", ""),
                "source_location": data.get("source_location", ""),
                "_src": data.get("_src", u),
                "_tgt": data.get("_tgt", v),
            })

        self.begin_bulk()
        try:
            n = self.upsert_nodes(nodes)
            m = self.upsert_edges(edges)
            self.commit_bulk()
        except Exception:
            self.rollback()
            raise

        return {"nodes_added": n, "edges_added": m}

    def rebuild_fts(self) -> None:
        """Rebuild the FTS5 index from scratch."""
        if self._fts_ok:
            self.conn.execute("INSERT INTO nodes_fts(nodes_fts) VALUES ('rebuild')")
            self.conn.commit()

    # ── Convenience ──────────────────────────────────────────────────

    def node_exists(self, node_id: str) -> bool:
        row = self.conn.execute("SELECT 1 FROM nodes WHERE id = ?", (node_id,)).fetchone()
        return row is not None

    def neighbors_bfs(self, start_ids: list[str], depth: int = 2) -> tuple[list[dict], list[dict]]:
        """BFS from start nodes using recursive CTE. Returns (nodes, edges)."""
        if not start_ids:
            return [], []
        placeholders = ",".join("?" * len(start_ids))
        sql = f"""
            WITH RECURSIVE bfs(level, node_id) AS (
                VALUES {",".join(f"(0, ?)" for _ in start_ids)}
                UNION ALL
                SELECT b.level + 1, e.target
                FROM edges e, bfs b
                WHERE e.source = b.node_id AND b.level < ?
            )
            SELECT DISTINCT n.id, n.label, n.file_type, n.source_file,
                   n.source_location, n.community, n.degree
            FROM bfs b
            JOIN nodes n ON n.id = b.node_id
            ORDER BY b.level, n.degree DESC
        """
        params = list(start_ids) + [depth]
        node_rows = self.conn.execute(sql, params).fetchall()
        found_ids = [r["id"] for r in node_rows]

        # Get edges between found nodes
        if len(found_ids) > 1:
            id_placeholders = ",".join("?" * len(found_ids))
            edge_rows = self.conn.execute(f"""
                SELECT source, target, relation, confidence, confidence_score, weight
                FROM edges
                WHERE source IN ({id_placeholders}) AND target IN ({id_placeholders})
            """, found_ids + found_ids).fetchall()
        else:
            edge_rows = []

        nodes = [_row_to_dict(r) for r in node_rows]
        edges = [_row_to_dict(r) for r in edge_rows]
        return nodes, edges

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None


def _row_to_dict(row: sqlite3.Row) -> dict:
    if row is None:
        return {}
    return {k: row[k] for k in row.keys()}


def _now() -> float:
    import time
    return time.time()


def _chunked(lst: list, size: int) -> Generator[list, None, None]:
    for i in range(0, len(lst), size):
        yield lst[i:i + size]
