"""SQLite: decisions, subscriptions and per-user delivery markers."""
from __future__ import annotations
import os, sqlite3
from pathlib import Path
from contextlib import contextmanager
from datetime import datetime, timezone

DATA_DIR = Path(os.getenv('DATA_DIR', 'data'))
DB_PATH = DATA_DIR / 'tik_bot.sqlite3'

def now() -> str:
    return datetime.now(timezone.utc).isoformat()

@contextmanager
def db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    con=sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory=sqlite3.Row
    con.execute('PRAGMA busy_timeout=30000')
    con.execute('PRAGMA journal_mode=WAL')
    try:
        yield con
        con.commit()
    finally:
        con.close()

def init_db():
    with db() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS tecs(id TEXT PRIMARY KEY,name TEXT NOT NULL,okrug TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS decisions(
          id TEXT PRIMARY KEY,tec_id TEXT NOT NULL,tec_name TEXT NOT NULL,
          title TEXT NOT NULL,number TEXT,decision_date TEXT,link TEXT,
          seen_at TEXT NOT NULL,is_baseline INTEGER NOT NULL DEFAULT 0);
        CREATE INDEX IF NOT EXISTS decisions_tec_date ON decisions(tec_id,decision_date);
        CREATE INDEX IF NOT EXISTS decisions_seen ON decisions(seen_at);
        CREATE TABLE IF NOT EXISTS subscriptions(
          id INTEGER PRIMARY KEY AUTOINCREMENT,chat_id INTEGER NOT NULL,
          scope_type TEXT NOT NULL,scope_value TEXT NOT NULL,kind TEXT NOT NULL,
          created_at TEXT NOT NULL,UNIQUE(chat_id,scope_type,scope_value,kind));
        CREATE TABLE IF NOT EXISTS delivered(subscription_id INTEGER NOT NULL,
          decision_id TEXT NOT NULL,sent_at TEXT NOT NULL,
          PRIMARY KEY(subscription_id,decision_id));
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT);
        ''')

def meta(key, default=None):
    with db() as c:
        r=c.execute('SELECT value FROM meta WHERE key=?',(key,)).fetchone()
        return r['value'] if r else default

def set_meta(key,value):
    with db() as c:
        c.execute('INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,str(value)))

def sub_create(chat_id, scope_type, scope_value, kind):
    with db() as c:
        c.execute('INSERT OR IGNORE INTO subscriptions(chat_id,scope_type,scope_value,kind,created_at) VALUES(?,?,?,?,?)',(chat_id,scope_type,scope_value,kind,now()))

def sub_list(chat_id=None):
    with db() as c:
        sql='SELECT * FROM subscriptions'
        args=()
        if chat_id is not None:
            sql+=' WHERE chat_id=?'; args=(chat_id,)
        return [dict(x) for x in c.execute(sql+' ORDER BY id',args)]

def sub_delete(chat_id, sub_id):
    with db() as c:
        r=c.execute('DELETE FROM subscriptions WHERE chat_id=? AND id=?',(chat_id,sub_id))
        return r.rowcount

def tec_list(ao=None):
    with db() as c:
        if ao is None: q=c.execute('SELECT * FROM tecs ORDER BY name')
        else: q=c.execute('SELECT * FROM tecs WHERE okrug=? ORDER BY name',(ao,))
        return [dict(r) for r in q]

UIK_TERMS=('уик','участков','резерв','ваканс','назначен','полномоч','кандидатур','предложени')
def matching_kind(title,kind):
    if kind=='all': return True
    s=title.casefold().replace('ё','е')
    return any(w in s for w in UIK_TERMS)

def scope_sql(scope_type,scope_value):
    if scope_type=='all': return '',[]
    if scope_type=='ao': return ' AND t.okrug=?',[scope_value]
    if scope_type=='tik': return ' AND d.tec_id=?',[scope_value]
    raise ValueError('Неизвестная область')

def latest(scope_type,scope_value,kind,limit=10):
    where,params=scope_sql(scope_type,scope_value)
    # get several extra to allow keyword filtering; limit at end
    with db() as c:
        rows=[dict(r) for r in c.execute('''SELECT d.* FROM decisions d LEFT JOIN tecs t ON t.id=d.tec_id
          WHERE 1=1 '''+where+''' ORDER BY d.decision_date DESC,d.seen_at DESC LIMIT 1500''',params)]
    return [r for r in rows if matching_kind(r['title'],kind)][:limit]

def unsent(sub,limit=200):
    where,params=scope_sql(sub['scope_type'],sub['scope_value'])
    with db() as c:
        rows=[dict(r) for r in c.execute('''SELECT d.* FROM decisions d
            LEFT JOIN tecs t ON t.id=d.tec_id
            LEFT JOIN delivered p ON p.subscription_id=? AND p.decision_id=d.id
            WHERE d.is_baseline=0 AND p.decision_id IS NULL AND d.seen_at>=? '''+where+'''
            ORDER BY d.seen_at ASC LIMIT 1000''',[sub['id'],sub['created_at'],*params])]
    return [r for r in rows if matching_kind(r['title'],sub['kind'])][:limit]

def mark_delivered(sub_id,ids):
    with db() as c:
        c.executemany('INSERT OR IGNORE INTO delivered(subscription_id,decision_id,sent_at) VALUES(?,?,?)',[(sub_id,x,now()) for x in ids])
