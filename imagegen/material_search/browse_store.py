"""Thread-safe, process-local search results. No search-response disk cache."""
import sqlite3
import threading
from contextlib import contextmanager

class BrowseStore:
    def __init__(self, database):
        self.lock=threading.RLock()
        self.conn=sqlite3.connect(':memory:',check_same_thread=False)
        self.conn.row_factory=sqlite3.Row
        for table in ('material_browse_sessions','material_browse_items'):
            self.conn.execute(database.one('SELECT sql FROM sqlite_master WHERE name=?',(table,))['sql'])

    @contextmanager
    def transaction(self):
        with self.lock:
            try:
                yield self.conn
                self.conn.commit()
            except BaseException:
                self.conn.rollback()
                raise

    def all(self,sql,params=()):
        with self.lock:return [dict(r) for r in self.conn.execute(sql,params)]
    def one(self,sql,params=()):
        rows=self.all(sql,params);return rows[0] if rows else None
