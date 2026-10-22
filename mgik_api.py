#!/usr/bin/env python3
"""Монитор решений ТИК Москвы через публичный API МГИК.

Режим первого запуска: сохраняет опубликованные решения за выбранный период,
не шлёт старые документы. Новые записи отправляет в Telegram.
"""
from __future__ import annotations
import argparse
import datetime as dt
import hashlib
import html
import json
import logging
import os
from pathlib import Path
import re
import sys
import time
from typing import Any
from urllib.parse import urljoin

import requests

BASE = 'https://www.mosgorizbirkom.ru'
LIST = f'{BASE}/api/v1/tec/list/'
SOLUTIONS = f'{BASE}/api/v1/tec/solutions/'
STATE_PATH = Path(os.getenv('STATE_FILE', 'state.json'))
TIMEOUT = 25
SESSION = requests.Session()
SESSION.headers.update({'User-Agent': 'MGIK-TIK-Decisions-Monitor/1.0 (+personal civic monitor)', 'Accept': 'application/json'})
LOG = logging.getLogger('mgik')


def get_json(url: str, params: dict[str, Any]) -> Any:
    error: Exception | None = None
    for n in range(4):
        try:
            r = SESSION.get(url, params=params, timeout=TIMEOUT)
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as e:
            error = e
            LOG.warning('Попытка %d: %s: %s', n + 1, url, e)
            if n < 3:
                time.sleep(min(2 ** n, 8))
    raise RuntimeError(f'Не удалось получить {url}: {error}')


def rows_and_total(obj: Any) -> tuple[list[dict], int | None]:
    if isinstance(obj, list):
        return [r for r in obj if isinstance(r, dict)], len(obj)
    if not isinstance(obj, dict):
        raise RuntimeError('Незнакомый формат API: ожидался JSON-объект или список')
    root = obj
    for nested in ('data', 'result', 'response'):
        if isinstance(root.get(nested), dict) and any(k in root[nested] for k in ('results', 'items', 'objects', 'list', 'rows')):
            root = root[nested]
            break
    for key in ('results', 'items', 'objects', 'list', 'rows', 'data'):
        if isinstance(root.get(key), list):
            count = next((root.get(k) for k in ('total_count', 'count', 'total', 'totalCount') if root.get(k) is not None), None)
            return [r for r in root[key] if isinstance(r, dict)], int(count) if count is not None else None
    raise RuntimeError('Неизвестная структура API: ' + json.dumps(obj, ensure_ascii=False)[:300])


def fetch_all(url: str, params: dict[str, Any], page_size: int = 100) -> list[dict]:
    offset, all_rows = 0, []
    while True:
        payload = get_json(url, {**params, 'limit': page_size, 'offset': offset})
        rows, total = rows_and_total(payload)
        all_rows.extend(rows)
        if total is not None and len(all_rows) >= total:
            break
        if not rows or len(rows) < page_size:
            break
        offset += len(rows)
        if offset > 100000:
            raise RuntimeError('API возвращает слишком много страниц: возможная ошибка пагинации')
        time.sleep(.12)
    return all_rows


def field(d: dict, *keys: str) -> Any:
    for k in keys:
        if d.get(k) is not None and d[k] != '':
            return d[k]
    return None


def string(value: Any) -> str:
    if isinstance(value, dict):
        return str(field(value, 'name', 'title', 'value', 'url', 'file', 'path', 'id') or '')
    return str(value or '')


def tec_id(tec: dict) -> str:
    value = field(tec, 'pk', 'id', 'tec_id', 'tec_pk')
    if value is None:
        raise RuntimeError('В записи ТИК отсутствует идентификатор: ' + repr(tec)[:250])
    return str(value)


def tec_name(tec: dict) -> str:
    return string(field(tec, 'name', 'title', 'full_name', 'short_name', 'label') or f'ТИК #{tec_id(tec)}')


def normalize_link(value: Any) -> str:
    if isinstance(value, dict):
        value = field(value, 'url', 'file', 'path', 'link', 'download_url', 'href')
    link = string(value).strip()
    if not link:
        return ''
    return urljoin(BASE, link) if link.startswith(('/', 'http')) else ''


def solution_data(item: dict, fallback_tec: str, fallback_name: str) -> dict:
    rid = field(item, 'pk', 'id', 'solution_id')
    name = string(field(item, 'name', 'title', 'subject', 'text', 'description'))
    number = string(field(item, 'number', 'num', 'solution_number', 'registration_number'))
    date = string(field(item, 'date', 'solution_date', 'publication_date', 'created_at', 'published_at'))[:10]
    doc = field(item, 'document', 'file', 'pdf', 'attachment', 'document_url', 'url', 'link', 'files')
    if isinstance(doc, list):
        doc = doc[0] if doc else ''
    if not normalize_link(doc):
        # Поддержка полей старых выгрузок API и вложенных объектов.
        for key, val in item.items():
            if isinstance(val, str) and ('CustomDocument_' in val or val.lower().endswith('.pdf')):
                doc = val
                break
    link = normalize_link(doc)
    tec = field(item, 'tec', 'tec_id', 'tec__pk')
    if isinstance(tec, dict):
        tec = field(tec, 'pk', 'id')
    tec = str(tec or fallback_tec)
    text = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
    # Включаем устойчивые поля; изменение metadata не должно породить новую запись.
    uid = f'{tec}:{rid}' if rid is not None else 'hash:' + hashlib.sha256(f'{tec}|{date}|{number}|{name}|{link}'.encode()).hexdigest()[:24]
    return {'id': uid, 'tec_id': tec, 'tec_name': fallback_name, 'name': name or '(без названия)', 'number': number, 'date': date, 'url': link, 'hash': hashlib.sha256(text.encode()).hexdigest()}


def matches_keywords(item: dict, regex: str | None) -> bool:
    if not regex:
        return True
    return bool(re.search(regex, ' '.join([item['name'], item['number']]), flags=re.I))


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {'initialized': False, 'seen': {}, 'updated_at': None}
    state = json.loads(STATE_PATH.read_text(encoding='utf8'))
    if not isinstance(state.get('seen'), dict):
        raise RuntimeError('Некорректный файл состояния')
    return state


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(STATE_PATH.suffix + '.tmp')
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + '\n', encoding='utf8')
    tmp.replace(STATE_PATH)


def send_telegram(message: str, token: str, chat_id: str) -> None:
    endpoint = f'https://api.telegram.org/bot{token}/sendMessage'
    r = SESSION.post(endpoint, json={'chat_id': chat_id, 'text': message, 'parse_mode': 'HTML', 'disable_web_page_preview': True}, timeout=TIMEOUT)
    r.raise_for_status()
    result = r.json()
    if not result.get('ok'):
        raise RuntimeError(f'Telegram API: {result.get("description", "неизвестная ошибка")}')


def render(item: dict) -> str:
    esc = lambda x: html.escape(str(x), quote=True)
    lines = [f'🗳 <b>Новое решение ТИК Москвы</b>', f'🏛 <b>{esc(item["tec_name"])}</b>']
    if item['date']:
        lines.append('📅 ' + esc(item['date']))
    if item['number']:
        lines.append('🔢 № ' + esc(item['number']))
    lines.append('\n' + esc(item['name']))
    if item['url']:
        lines.append('\n📄 <a href="' + esc(item['url']) + '">Открыть документ</a>')
    else:
        lines.append('\n🔎 <a href="' + BASE + '/territorialnaya-izbiratelnaya-komissiya/">Каталог ТИК</a>')
    return '\n'.join(lines)


def main() -> int:
    p = argparse.ArgumentParser(description='Telegram-монитор новых решений ТИК Москвы')
    p.add_argument('--days', type=int, default=int(os.getenv('LOOKBACK_DAYS', '45')), help='за сколько последних дней проверять решения')
    p.add_argument('--keywords', default=os.getenv('KEYWORDS', ''), help='регулярное выражение для фильтра по заголовкам; пусто = все решения')
    p.add_argument('--tik', default=os.getenv('TIK_FILTER', ''), help='фильтр по названию ТИК, через запятую')
    p.add_argument('--dry-run', action='store_true', help='ничего не отправлять и не сохранять')
    p.add_argument('--test-message', action='store_true', help='отправить проверочное сообщение в Telegram')
    p.add_argument('--reset', action='store_true', help='заново создать базовую точку без отправки старых решений')
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    token, chat_id = os.getenv('TELEGRAM_BOT_TOKEN', ''), os.getenv('TELEGRAM_CHAT_ID', '')
    if args.test_message:
        if not token or not chat_id:
            raise RuntimeError('Укажите TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID')
        send_telegram('✅ Бот МГИК подключён. Мониторинг решений ТИК готов.', token, chat_id)
        return 0
    if not args.dry_run and (not token or not chat_id):
        raise RuntimeError('Укажите TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID')
    today = dt.datetime.now(dt.timezone(dt.timedelta(hours=3))).date()
    since = today - dt.timedelta(days=args.days)
    tik_filter = [v.casefold().strip() for v in args.tik.split(',') if v.strip()]
    tecs = fetch_all(LIST, {}, page_size=200)
    LOG.info('Каталог: %s ТИК', len(tecs))
    selected = [x for x in tecs if not tik_filter or any(word in tec_name(x).casefold() for word in tik_filter)]
    state = load_state()
    if args.reset:
        state = {'initialized': False, 'seen': {}, 'updated_at': None}
    collected: dict[str, dict] = {}
    errors: list[str] = []
    for i, tik in enumerate(selected, 1):
        try:
            records = fetch_all(SOLUTIONS, {'tec__pk': tec_id(tik), 'date__gte': since.isoformat(), 'date__lte': today.isoformat()})
            for rec in records:
                item = solution_data(rec, tec_id(tik), tec_name(tik))
                collected[item['id']] = item
            LOG.info('%d/%d %s: %d решений', i, len(selected), tec_name(tik), len(records))
        except Exception as e:
            LOG.error('Не смог проверить ТИК %s: %s', tec_name(tik), e)
            errors.append(f'{tec_name(tik)}: {e}')
        time.sleep(.15)
    if not selected:
        raise RuntimeError('После фильтра не осталось ТИК. Проверьте название района в TIK_FILTER')
    if len(errors) == len(selected):
        raise RuntimeError('Не удалось проверить ни одной ТИК; состояние не менялось')
    if errors and not state.get('initialized'):
        raise RuntimeError('Первое заполнение неполное: ошибки по части ТИК. Повторите запуск: базовая точка ещё не создана')
    LOG.info('Собрано %d решений; ошибок ТИК: %d', len(collected), len(errors))
    fresh = [x for k, x in collected.items() if k not in state['seen']]
    fresh.sort(key=lambda x: (x['date'], x['tec_name'], x['number'], x['id']))
    if not state.get('initialized'):
        LOG.info('Первый запуск: %d существующих решений записаны как базовая точка, без уведомлений', len(fresh))
        fresh = []
    else:
        LOG.info('Новых решений: %d', len(fresh))
    # ВАЖНО: даже отфильтрованные решения записываются как увиденные, иначе будут повторяться.
    pending = [x for x in fresh if matches_keywords(x, args.keywords)]
    LOG.info('К отправке с учётом фильтра: %d', len(pending))
    if args.dry_run:
        for item in pending[:20]:
            print(render(item) + '\n')
        return 0
    # Если Telegram недоступен, не помечаем неотправленные уведомления как увиденные.
    pending_ids = {x['id'] for x in pending}
    sent_ids: set[str] = set()
    for item in pending:
        try:
            send_telegram(render(item), token, chat_id)
            sent_ids.add(item['id'])
            time.sleep(.12)
        except Exception as e:
            LOG.error('Не доставлено %s: %s', item['id'], e)
    for key, item in collected.items():
        if key not in pending_ids or key in sent_ids:
            state['seen'][key] = {'date': item['date'], 'tec_id': item['tec_id']}
    state['initialized'] = True
    state['updated_at'] = dt.datetime.now(dt.timezone.utc).isoformat()
    state['last_errors'] = errors[:30]
    # Ограничиваем историю годом, чтобы state.json не рос бесконечно.
    cutoff = str(today - dt.timedelta(days=365))
    state['seen'] = {k: v for k, v in state['seen'].items() if not v.get('date') or v['date'] >= cutoff}
    save_state(state)
    if errors:
        LOG.warning('Не все ТИК проверены (%d); повторная проверка при следующем запуске', len(errors))
    if len(sent_ids) != len(pending):
        raise RuntimeError('Часть уведомлений не доставлена — повторим на следующем запуске')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as err:
        LOG.exception('Ошибка мониторинга: %s', err)
        sys.exit(1)
