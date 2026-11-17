import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from imagegen.db import Database


class ReviewMigrationTests(unittest.TestCase):
    def test_legacy_review_columns_and_values_survive_reopen(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'app.sqlite'
            with closing(sqlite3.connect(path)) as connection:
                connection.execute('CREATE TABLE reviews(generation_id TEXT PRIMARY KEY, accepted INTEGER NOT NULL, note TEXT NOT NULL, created_at TEXT NOT NULL)')
                connection.execute("INSERT INTO reviews VALUES('old',1,'preserved','2026-01-01')")
                connection.commit()
            Database(path)
            with closing(sqlite3.connect(path)) as connection:
                row=connection.execute('SELECT accepted,note,status,passport_json FROM reviews').fetchone()
                self.assertEqual(row,(1,'preserved','approved','{}'))
            Database(path)
            with closing(sqlite3.connect(path)) as connection:
                self.assertEqual(connection.execute('SELECT count(*) FROM reviews').fetchone()[0],1)

    def test_existing_pending_review_is_not_approved_on_reopen(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'app.sqlite';db=Database(path)
            with db.transaction() as connection:
                # Preserve the real foreign-key relationship in the fixture.
                connection.execute("INSERT INTO projects(id,title,idea,story,style,created_at) VALUES('p','p','p','p','p','now')")
                connection.execute("INSERT INTO generations(id,project_id,purpose,snapshot,seed,state,created_at) VALUES('g','p','scene','{}',0,'succeeded','now')")
                connection.execute("INSERT INTO reviews(generation_id,accepted,note,created_at,status) VALUES('g',1,'pending','now','pending_review')")
            Database(path)
            self.assertEqual(db.one("SELECT status FROM reviews WHERE generation_id='g'")['status'],'pending_review')
