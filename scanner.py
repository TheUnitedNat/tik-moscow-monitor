"""Проверка официального реестра решений ТИК МГИК."""
from __future__ import annotations
import logging, time
from datetime import datetime,timedelta
from zoneinfo import ZoneInfo
import mgik_api as api
import storage
from regions import lookup

LOG=logging.getLogger(__name__)

def scan(days=120):
    """Sync function: call through asyncio.to_thread from Telegram event loop."""
    now=datetime.now(ZoneInfo('Europe/Moscow')).date()
    since=(now-timedelta(days=days)).isoformat()
    tecs=api.fetch_all(api.LIST,{},page_size=200)
    if not tecs: raise RuntimeError('Каталог ТИК пуст, обновление отменено')
    first = storage.meta('initialized')!='1'
    if first: LOG.info('Первоначальная загрузка: уведомления по архиву не отправляются')
    new=0; failures=[]; rows=[]
    with storage.db() as c:
        for rec in tecs:
            ident=api.tec_id(rec); name=api.tec_name(rec)
            c.execute('INSERT INTO tecs(id,name,okrug) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,okrug=excluded.okrug',(ident,name,lookup(name)))
    for i,tec in enumerate(tecs,1):
        tid=api.tec_id(tec); tname=api.tec_name(tec)
        try:
            docs=api.fetch_all(api.SOLUTIONS,{'tec__pk':tid,'date__gte':since,'date__lte':now.isoformat()},page_size=100)
            for raw in docs:
                rec=api.solution_data(raw,tid,tname)
                rows.append(rec)
            LOG.info('%d/%d %s: %d',i,len(tecs),tname,len(docs))
        except Exception as exc:
            failures.append(tname)
            LOG.warning('ТИК недоступна (%s): %s',tname,exc)
        time.sleep(.10)
    if first and failures:
        raise RuntimeError(f'Первичное заполнение неполное: ошибок ТИК {len(failures)}. Повторим позже')
    # Use one transaction; baseline only for initial scan.
    stamp=storage.now()
    with storage.db() as c:
        for r in rows:
            cursor=c.execute('''INSERT OR IGNORE INTO decisions
              (id,tec_id,tec_name,title,number,decision_date,link,seen_at,is_baseline)
              VALUES(?,?,?,?,?,?,?,?,?)''',(
              r['id'],r['tec_id'],r['tec_name'],r['name'],r['number'],r['date'],r['url'],stamp,int(first)))
            new+=cursor.rowcount
            # Update headline and PDF in case the document was edited in-place (no second notification).
            c.execute('UPDATE decisions SET title=?,number=?,decision_date=?,link=? WHERE id=?',(
                r['name'],r['number'],r['date'],r['url'],r['id']))
    storage.set_meta('initialized','1')
    storage.set_meta('scan_at',stamp)
    storage.set_meta('last_failed_count',str(len(failures)))
    storage.set_meta('last_new_count',str(0 if first else new))
    storage.set_meta('last_tik_count',str(len(tecs)))
    return {'tecs':len(tecs),'docs':len(rows),'new':0 if first else new,'failed':failures,'baseline':first}
