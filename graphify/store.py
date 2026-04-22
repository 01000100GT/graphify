# GraphStore abstraction — storage backend interface for graphify query layer
from __future__ import annotations

from abc import ABC, abstractmethod


class GraphStore(ABC):
    """Abstract storage interface for the graphify query layer.

    Implementations: SQLiteStore (graphify.db), NullStore (fallback).
    """

    # ── Write ────────────────────────────────────────────────────────

    @abstractmethod
    def upsert_nodes(self, nodes: list[dict]) -> int:
        """Insert or update nodes. Returns the number of rows affected."""

    @abstractmethod
    def upsert_edges(self, edges: list[dict]) -> int:
        """Insert or update edges. Returns the number of rows affected."""

    @abstractmethod
    def delete_nodes(self, node_ids: list[str]) -> int:
        """Delete nodes and their associated edges. Returns count deleted."""

    # ── Search ───────────────────────────────────────────────────────

    @abstractmethod
    def search_nodes(self, query: str, limit: int = 20, filters: dict | None = None) -> list[dict]:
        """Search nodes. Returns list of node dicts with an added 'score' field.

        filters: optional dict with keys file_type, source_file, community.
        """

    @abstractmethod
    def get_node(self, node_id: str) -> dict | None:
        """Return full node data by ID, or None if not found."""

    @abstractmethod
    def get_neighbors(self, node_id: str, relation_filter: str = "") -> list[dict]:
        """Return neighbor nodes with edge details. Each dict has 'node' and 'edge' keys."""

    @abstractmethod
    def get_community(self, community_id: int) -> list[dict]:
        """Return all nodes in a community."""

    @abstractmethod
    def god_nodes(self, top_n: int = 10) -> list[dict]:
        """Return the most connected nodes, sorted by degree descending."""

    # ── Stats ────────────────────────────────────────────────────────

    @abstractmethod
    def stats(self) -> dict:
        """Return {nodes, edges, communities, ...}"""

    # ── Bulk / Transaction ───────────────────────────────────────────

    @abstractmethod
    def begin_bulk(self) -> None:
        """Start a bulk write transaction."""

    @abstractmethod
    def commit_bulk(self) -> None:
        """Commit the current bulk transaction."""

    @abstractmethod
    def rollback(self) -> None:
        """Rollback the current bulk transaction."""

    # ── Sync ─────────────────────────────────────────────────────────

    @abstractmethod
    def sync_from_graph(self, graph_path: str) -> dict:
        """Load graph.json and upsert all nodes/edges. Returns change summary."""

    @abstractmethod
    def rebuild_fts(self) -> None:
        """Rebuild the full-text search index from scratch."""


class NullStore(GraphStore):
    """No-op store used when SQLite is unavailable. Every method is a safe no-op."""

    def upsert_nodes(self, nodes: list[dict]) -> int:
        return 0

    def upsert_edges(self, edges: list[dict]) -> int:
        return 0

    def delete_nodes(self, node_ids: list[str]) -> int:
        return 0

    def search_nodes(self, query: str, limit: int = 20, filters: dict | None = None) -> list[dict]:
        return []

    def get_node(self, node_id: str) -> dict | None:
        return None

    def get_neighbors(self, node_id: str, relation_filter: str = "") -> list[dict]:
        return []

    def get_community(self, community_id: int) -> list[dict]:
        return []

    def god_nodes(self, top_n: int = 10) -> list[dict]:
        return []

    def stats(self) -> dict:
        return {"nodes": 0, "edges": 0, "communities": 0}

    def begin_bulk(self) -> None:
        pass

    def commit_bulk(self) -> None:
        pass

    def rollback(self) -> None:
        pass

    def sync_from_graph(self, graph_path: str) -> dict:
        return {"nodes_added": 0, "edges_added": 0}

    def rebuild_fts(self) -> None:
        pass
