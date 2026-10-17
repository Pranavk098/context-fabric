"""Context Fabric — durable SQLite store for ContextBlock records (v0 filing cabinet)."""

from kernel.store.sqlite_store import SqliteStore, StoreConflict

__all__ = ["SqliteStore", "StoreConflict"]
