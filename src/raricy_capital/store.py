"""Portable single-writer SQLite records, transactions and persistent events."""
from __future__ import annotations
from contextlib import contextmanager
import json,sqlite3,threading
from pathlib import Path
from typing import Any
from .contracts import now_ms

class FundStore:
    def __init__(self,path:Path|str):
        self.path=Path(path);self.path.parent.mkdir(parents=True,exist_ok=True)
        self.lock=threading.RLock();self._depth=0
        self.db=sqlite3.connect(self.path,timeout=30,isolation_level=None,check_same_thread=False)
        self.db.row_factory=sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL');self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('PRAGMA foreign_keys=ON');self.db.execute('PRAGMA busy_timeout=30000')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS records(namespace TEXT NOT NULL,key TEXT NOT NULL,value TEXT NOT NULL,updated_ms INTEGER NOT NULL,PRIMARY KEY(namespace,key));
        CREATE TABLE IF NOT EXISTS audit_events(id INTEGER PRIMARY KEY AUTOINCREMENT,event_key TEXT UNIQUE,created_ms INTEGER NOT NULL,fund_id TEXT,event TEXT NOT NULL,level TEXT NOT NULL,details TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS audit_events_created ON audit_events(created_ms);
        PRAGMA user_version=1;
        ''')

    @contextmanager
    def transaction(self):
        with self.lock:
            depth=self._depth
            self.db.execute('BEGIN IMMEDIATE' if not depth else f'SAVEPOINT fund_sp_{depth}')
            self._depth+=1
            try:
                yield self
                self.db.execute('COMMIT' if not depth else f'RELEASE SAVEPOINT fund_sp_{depth}')
            except BaseException:
                self.db.execute('ROLLBACK' if not depth else f'ROLLBACK TO SAVEPOINT fund_sp_{depth}')
                if depth:self.db.execute(f'RELEASE SAVEPOINT fund_sp_{depth}')
                raise
            finally:self._depth-=1

    def get(self,namespace:str,key:str,default:Any=None):
        with self.lock:
            r=self.db.execute('SELECT value FROM records WHERE namespace=? AND key=?',(namespace,key)).fetchone()
        return json.loads(r[0]) if r else default

    def put(self,namespace:str,key:str,value:Any):
        encoded=json.dumps(value,ensure_ascii=False,allow_nan=False,separators=(',',':'))
        with self.lock:self.db.execute('INSERT INTO records VALUES(?,?,?,?) ON CONFLICT(namespace,key) DO UPDATE SET value=excluded.value,updated_ms=excluded.updated_ms',(namespace,key,encoded,now_ms()))

    def claim(self,namespace:str,key:str,value:Any)->bool:
        encoded=json.dumps(value,ensure_ascii=False,allow_nan=False,separators=(',',':'))
        with self.lock:return self.db.execute('INSERT OR IGNORE INTO records VALUES(?,?,?,?)',(namespace,key,encoded,now_ms())).rowcount==1

    def list_items(self,namespace:str,prefix:str='',limit:int|None=10000):
        with self.lock:
            rows=self.db.execute('SELECT key,value FROM records WHERE namespace=? ORDER BY key',(namespace,)).fetchall()
        return [(r[0],json.loads(r[1])) for r in rows if r[0].startswith(prefix)][:limit]

    def list(self,namespace:str,prefix:str='',limit:int|None=10000):
        return [v for _,v in self.list_items(namespace,prefix,limit)]

    def append_event(self,event:str,*,fund_id:str|None=None,level:str='info',details:dict|None=None,event_key:str|None=None,created_ms:int|None=None):
        with self.lock:
            cur=self.db.execute('INSERT OR IGNORE INTO audit_events(event_key,created_ms,fund_id,event,level,details) VALUES(?,?,?,?,?,?)',
                (event_key,created_ms or now_ms(),fund_id,event,level,json.dumps(details or {},ensure_ascii=False,allow_nan=False)))
            return cur.lastrowid

    def events(self,limit:int=100,fund_id:str|None=None):
        sql='SELECT * FROM audit_events';args=[]
        if fund_id:sql+=' WHERE fund_id=?';args.append(fund_id)
        sql+=' ORDER BY id DESC LIMIT ?';args.append(min(max(int(limit),1),1000))
        with self.lock:rows=self.db.execute(sql,args).fetchall()
        return [dict(r,details=json.loads(r['details'])) for r in rows]

    def backup(self,destination:Path|str):
        dest=Path(destination);dest.parent.mkdir(parents=True,exist_ok=True)
        with self.lock:
            target=sqlite3.connect(dest)
            try:self.db.backup(target)
            finally:target.close()

    def close(self):
        with self.lock:self.db.close()
