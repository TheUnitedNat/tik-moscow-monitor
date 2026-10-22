"""Telegram UI for Moscow TIK public decisions. Does not persist Telegram secrets."""
from __future__ import annotations
import asyncio, html, logging, os
from datetime import datetime,time as tm,timedelta
from zoneinfo import ZoneInfo
from telegram import Update,InlineKeyboardButton as B,InlineKeyboardMarkup as M
from telegram.constants import ParseMode
from telegram.ext import ApplicationBuilder,CommandHandler,CallbackQueryHandler,ContextTypes
import storage,scanner
from regions import ORDER

LOG=logging.getLogger('tik-monitor')
MSK=ZoneInfo('Europe/Moscow')
TOKEN=os.getenv('TELEGRAM_BOT_TOKEN','').strip()
ALLOWED={int(x.strip()) for x in os.getenv('ALLOWED_CHAT_IDS','').split(',') if x.strip().lstrip('-').isdigit()}
SCAN_LOCK=asyncio.Lock()
LOOKBACK=int(os.getenv('LOOKBACK_DAYS','120'))
SCAN_HOURS=int(os.getenv('SCAN_HOURS','6'))
DAILY_HOUR=int(os.getenv('DAILY_HOUR_MSK','9'))
DAILY_MINUTE=int(os.getenv('DAILY_MINUTE_MSK','15'))

def esc(s): return html.escape(str(s or ''),quote=True)

def authorized(update: Update):
    return not ALLOWED or (update.effective_chat is not None and update.effective_chat.id in ALLOWED)

async def deny(update):
    if update.callback_query: await update.callback_query.answer('Нет доступа к этому боту',show_alert=True)
    elif update.message: await update.message.reply_text('Этот бот работает в частном режиме.')

def menu():
    return M([[B('📰 Последние решения',callback_data='mode:latest')],
              [B('🔔 Ежедневные уведомления',callback_data='mode:subscribe')],
              [B('📋 Мои подписки',callback_data='subs')],
              [B('🔄 Обновить решения сейчас',callback_data='refresh')]])

def select_area(mode):
    rows=[[B('🌍 Все ТИК Москвы',callback_data=f'pick:{mode}:all:0')]]
    for i in range(0,len(ORDER),3):
        rows.append([B(x,callback_data=f'pick:{mode}:ao:{x}') for x in ORDER[i:i+3]])
    rows.append([B('🏛 Выбрать конкретную ТИК',callback_data=f'tiklist:{mode}:all:0')])
    rows.append([B('⬅️ Меню',callback_data='menu')])
    return M(rows)

def kind_buttons(mode,scope_type,value):
    return M([[B('📄 Все решения',callback_data=f'run:{mode}:{scope_type}:{value}:all')],
              [B('🗳 Только УИК / ПРГ / резерв',callback_data=f'run:{mode}:{scope_type}:{value}:uik')],
              [B('⬅️ Назад',callback_data=f'mode:{mode}')]])

def tik_page(mode,area,page):
    lst=storage.tec_list(None if area=='all' else area)
    # For unclassified TIKs, use All TIK selector; never drop them.
    page=max(0,page); per=8
    if not lst:
        return 'ТИК пока не загружены. Нажмите «Обновить решения».',M([[B('⬅️ Меню',callback_data='menu')]])
    pages=max(1,(len(lst)+per-1)//per)
    page=min(page,pages-1)
    subset=lst[page*per:(page+1)*per]
    rows=[[B(r['name'].removeprefix('Территориальная избирательная комиссия '),callback_data=f'pick:{mode}:tik:{r["id"]}')] for r in subset]
    nav=[]
    if page>0: nav.append(B('◀️',callback_data=f'tiklist:{mode}:{area}:{page-1}'))
    nav.append(B(f'{page+1}/{pages}',callback_data='noop'))
    if page<pages-1: nav.append(B('▶️',callback_data=f'tiklist:{mode}:{area}:{page+1}'))
    rows.append(nav)
    rows.append([B('↩️ К округам',callback_data=f'mode:{mode}')])
    return f'Выберите ТИК (страница {page+1} из {pages}):',M(rows)

def title_scope(kind,value):
    if kind=='all': return 'Все ТИК Москвы'
    if kind=='ao': return value
    if kind=='tik':
        for tec in storage.tec_list():
            if tec['id']==value: return tec['name']
        return 'ТИК #'+value
    return value

def pretty(d):
    head=f'📅 {esc(d["decision_date"])} · <b>{esc(d["tec_name"])}</b>'
    if d['number']: head+=f' · № {esc(d["number"])}'
    heading=esc(d['title'])
    if d['link'] and d['link'].startswith('https://'):
        heading+=f' · <a href="{esc(d["link"])}">документ</a>'
    return head+'\n'+heading

async def safe_edit(update,text,keyboard=None):
    q=update.callback_query
    try:
        await q.edit_message_text(text,reply_markup=keyboard,parse_mode=ParseMode.HTML,disable_web_page_preview=True)
    except Exception as e:
        if 'Message is not modified' not in str(e): raise

async def start(update:Update,ctx:ContextTypes.DEFAULT_TYPE):
    if not authorized(update): return await deny(update)
    await update.message.reply_text('🗳 <b>ТИК-Монитор Москвы</b>\n\nВыбирайте административный округ или ТИК, смотрите последние решения и подписывайтесь на ежедневную сводку.\n\nКоманда /id покажет ID Вашего чата для приватного режима.',parse_mode=ParseMode.HTML,reply_markup=menu())

async def get_id(update:Update,ctx:ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f'ID чата: <code>{update.effective_chat.id}</code>',parse_mode=ParseMode.HTML)

async def cb(update:Update,ctx:ContextTypes.DEFAULT_TYPE):
    if not authorized(update): return await deny(update)
    q=update.callback_query; await q.answer()
    data=q.data
    if data=='noop': return
    if data=='menu': return await safe_edit(update,'🗳 Главное меню',menu())
    if data.startswith('mode:'):
        return await safe_edit(update,'Выберите административный округ Москвы или конкретную ТИК:',select_area(data.split(':')[1]))
    if data=='subs':
        subs=storage.sub_list(update.effective_chat.id)
        if not subs:
            return await safe_edit(update,'Подписок пока нет.',M([[B('🔔 Добавить подписку',callback_data='mode:subscribe')],[B('⬅️ Меню',callback_data='menu')]]))
        lines=['<b>Ваши ежедневные подписки (09:15 МСК)</b>']
        buttons=[]
        for s in subs:
            lines.append(f'• №{s["id"]}: {esc(title_scope(s["scope_type"],s["scope_value"]))} — {"УИК / ПРГ" if s["kind"]=="uik" else "все решения"}')
            buttons.append([B(f'❌ Удалить №{s["id"]}',callback_data=f'delete:{s["id"]}')])
        buttons.append([B('➕ Добавить',callback_data='mode:subscribe')])
        buttons.append([B('⬅️ Меню',callback_data='menu')])
        return await safe_edit(update,'\n'.join(lines),M(buttons))
    if data.startswith('delete:'):
        storage.sub_delete(update.effective_chat.id,int(data.split(':')[1]))
        return await safe_edit(update,'✅ Подписка удалена.',M([[B('📋 Мои подписки',callback_data='subs')],[B('⬅️ Меню',callback_data='menu')]]))
    if data.startswith('tiklist:'):
        _,mode,area,page=data.split(':',3)
        text,keyboard=tik_page(mode,area,int(page))
        return await safe_edit(update,esc(text),keyboard)
    if data.startswith('pick:'):
        _,mode,kind,value=data.split(':',3)
        return await safe_edit(update,f'Выбрано: <b>{esc(title_scope(kind,value))}</b>\n\nКакие решения интересуют?',kind_buttons(mode,kind,value))
    if data.startswith('run:'):
        _,mode,kind,value,flt=data.split(':',4)
        title=title_scope(kind,value)
        if mode=='subscribe':
            storage.sub_create(update.effective_chat.id,kind,value,flt)
            return await safe_edit(update,f'✅ Ежедневная подписка оформлена.\n\n<b>{esc(title)}</b>\nФильтр: {"УИК / ПРГ / резерв" if flt=="uik" else "все решения"}\nСводки в 09:15 по Москве, даже если новых решений нет.',M([[B('📋 Мои подписки',callback_data='subs')],[B('⬅️ Меню',callback_data='menu')]]))
        docs=storage.latest(kind,value,flt,10)
        lines=[f'<b>📰 Последние решения: {esc(title)}</b>']
        if not docs: lines.append('Пока записей нет. Воспользуйтесь кнопкой «Обновить».')
        else:
            for i,d in enumerate(docs,1): lines.append(f'\n<b>{i}.</b> '+pretty(d))
        # Split at decision boundaries, never inside HTML tags/entities.
        chunks=[]
        current=lines[0]
        for item in lines[1:]:
            if len(current)+len(item)>3100:
                chunks.append(current); current=item.lstrip('\n')
            else: current+=item
        chunks.append(current)
        keyboard=M([[B('🔔 Подписаться',callback_data=f'run:subscribe:{kind}:{value}:{flt}')],[B('⬅️ Меню',callback_data='menu')]])
        await safe_edit(update,chunks[0],keyboard)
        for chunk in chunks[1:]:
            await ctx.bot.send_message(update.effective_chat.id,chunk,parse_mode=ParseMode.HTML,disable_web_page_preview=True)
        return
    if data=='refresh':
        if SCAN_LOCK.locked(): return await safe_edit(update,'⏳ Обновление уже идёт. Попробуйте чуть позже.',menu())
        await safe_edit(update,'🔄 Проверяю официальный сайт МГИК. После завершения отправлю результат в этот чат.')
        ctx.application.create_task(do_scan(ctx.application,update.effective_chat.id))
        return

async def do_scan(app,chat_id=None):
    if SCAN_LOCK.locked(): return
    async with SCAN_LOCK:
        try:
            result=await asyncio.to_thread(scanner.scan,LOOKBACK)
            msg=f'✅ Проверка закончена. ТИК: {result["tecs"]}, решений в окне: {result["docs"]}, новых: {result["new"]}. Ошибок ТИК: {len(result["failed"])}.'
            LOG.info(msg)
        except Exception as exc:
            LOG.exception('Не удалось проверить МГИК')
            msg=f'⚠️ Проверка сайта не удалась: {str(exc)[:400]}'
        if chat_id:
            await app.bot.send_message(chat_id,msg,reply_markup=menu())

async def periodic_scan(context:ContextTypes.DEFAULT_TYPE):
    await do_scan(context.application)

async def morning_scan(context:ContextTypes.DEFAULT_TYPE):
    await do_scan(context.application)

async def daily_digest(context:ContextTypes.DEFAULT_TYPE):
    app=context.application
    # Avoid an inconsistent digest while the scanner is still downloading.
    async with SCAN_LOCK:
        subs=storage.sub_list()
        by_chat={}
        for s in subs: by_chat.setdefault(s['chat_id'],[]).append(s)
        for chat_id,subscriptions in by_chat.items():
            try:
                await app.bot.send_message(chat_id,'🗳 <b>Ежедневная сводка МГИК</b> · '+datetime.now(MSK).strftime('%d.%m.%Y'),parse_mode=ParseMode.HTML)
            except Exception: LOG.exception('Не удалось отправить заголовок чату %s',chat_id); continue
            for sub in subscriptions:
                rows=storage.unsent(sub,limit=60)
                label=title_scope(sub['scope_type'],sub['scope_value'])
                if not rows:
                    try: await app.bot.send_message(chat_id,f'📍 {label}: новых решений нет.')
                    except Exception: LOG.exception('Ошибка отправки чату %s',chat_id)
                    continue
                for item in rows:
                    try:
                        await app.bot.send_message(chat_id,'📍 <b>'+esc(label)+'</b>\n'+pretty(item),parse_mode=ParseMode.HTML,disable_web_page_preview=True)
                        storage.mark_delivered(sub['id'],[item['id']])
                        await asyncio.sleep(.07)
                    except Exception:
                        LOG.exception('Ошибка доставки решения %s для %s',item['id'],chat_id)
                        break
                if len(rows)>=60:
                    await app.bot.send_message(chat_id,'⚠️ Есть ещё решения для этой подписки. Они попадут в следующую сводку.')

async def error_handler(update,context):
    LOG.exception('Telegram handler failed: %s',context.error)

async def init_jobs(app):
    # First scan immediately; existing decisions are baseline (not new alerts).
    app.create_task(do_scan(app))

async def help_cmd(update:Update,ctx:ContextTypes.DEFAULT_TYPE):
    return await start(update,ctx)

def main():
    if not TOKEN or TOKEN=='INSERT_NEW_TOKEN_HERE':
        raise SystemExit('Нужен TELEGRAM_BOT_TOKEN в .env, новый токен после /revoke в @BotFather')
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(name)s %(message)s')
    storage.init_db()
    app=ApplicationBuilder().token(TOKEN).post_init(init_jobs).build()
    app.add_handler(CommandHandler('start',start))
    app.add_handler(CommandHandler('help',help_cmd))
    app.add_handler(CommandHandler('id',get_id))
    app.add_handler(CallbackQueryHandler(cb))
    app.add_error_handler(error_handler)
    app.job_queue.run_repeating(periodic_scan,interval=timedelta(hours=SCAN_HOURS),first=timedelta(hours=SCAN_HOURS),name='periodic_scan')
    # 08:50 MSK, then digest 09:15. Scanning can take minutes.
    app.job_queue.run_daily(morning_scan,time=tm(8,50,tzinfo=MSK),name='morning_scan')
    app.job_queue.run_daily(daily_digest,time=tm(DAILY_HOUR,DAILY_MINUTE,tzinfo=MSK),name='daily_digest')
    LOG.info('Bot started. Subscriptions: %s',len(storage.sub_list()))
    app.run_polling(drop_pending_updates=False)

if __name__=='__main__': main()
