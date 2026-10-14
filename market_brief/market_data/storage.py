"""SQLite development persistence; move to Postgres in cloud production."""
import json
import sqlite3
from .models import Quote

class QuoteStore:
    def __init__(self,path='quotes.sqlite3'):
        self.path=path
        with sqlite3.connect(self.path) as db:
            db.execute('''CREATE TABLE IF NOT EXISTS market_quotes (
              symbol TEXT NOT NULL, source TEXT NOT NULL, as_of TEXT NOT NULL,
              payload TEXT NOT NULL, ingested_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              PRIMARY KEY(symbol, source, as_of))''')

    def upsert(self,quotes: list[Quote])->int:
        with sqlite3.connect(self.path) as db:
            for q in quotes:
                record=q.to_record()
                db.execute('''INSERT INTO market_quotes(symbol,source,as_of,payload) VALUES(?,?,?,?)
                  ON CONFLICT(symbol,source,as_of) DO UPDATE SET payload=excluded.payload''',
                           (q.symbol,q.source,record['as_of'],json.dumps(record,ensure_ascii=False)))
        return len(quotes)

    def count(self):
        with sqlite3.connect(self.path) as db:
            return db.execute('SELECT count(*) FROM market_quotes').fetchone()[0]
