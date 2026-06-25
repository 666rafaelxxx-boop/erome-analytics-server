# Erome Analytics Server — v2.0 (full dashboard)
# Roda no Railway 24/7 — scrapa dados públicos do Erome, calcula viral/streak/
# histórico/CTA igual ao userscript Tampermonkey, e serve um painel /admin
# completo e responsivo (sem precisar do navegador/PC do Rafael ligado).

from flask import Flask, jsonify, request, Response, session, redirect
import requests
from bs4 import BeautifulSoup
import time
import threading
import re
import json
import os
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor

# ================================================================
# CONFIG / PERSISTÊNCIA
# ================================================================
# IMPORTANTE: no Railway, "/tmp" NÃO é permanente — é zerado a cada redeploy
# ou reinício do container. Pra não perder histórico, isso precisa apontar
# pra um Railway Volume (disco persistente). Configure assim no Railway:
#   1. No seu serviço → aba "Volumes" → "+ New Volume"
#   2. Mount path: /data
#   3. Pronto — esse caminho passa a sobreviver a redeploys/restarts.
# Se quiser usar outro caminho, defina a env var DATA_DIR no Railway.
DATA_DIR  = os.environ.get('DATA_DIR', '/data')
DATA_FILE = os.path.join(DATA_DIR, 'erome_data.json')
TZ        = ZoneInfo('America/Sao_Paulo')  # horário de Rafael — usado em TUDO que é "hora do dia"

_state_lock = threading.RLock()  # protege leitura/escrita do estado global + arquivo

def _empty_state():
    return {
        'accounts':         [],
        'cta_config':       {},   # {username: [CTA1, CTA2, ...]}
        'cta_status':       {},   # {username: {albumId: {ok, foundCta, checkedAt}}}
        'commented':        {},   # {username: {albumId: {at, title, href, baselineViews, autoDetected, lastCta}}}
        'snapshots':        {},   # {username: [snap, ...]} snap0 = mais recente
        'hourly_snaps':     {},   # {username: [snap, ...]} versão leve, 1 por hora
        'viral_streak':     {},   # {username: {albumId: streakCount}}
        'deleted':          {},   # {username: [{id,title,views,at,hadCta}, ...]}
        'daily_history':    {},   # {username: [{date,views,savedAt}, ...]}
        'cta_hourly_agg':   {},   # {username: {"0":views,...,"23":views}} acumulado dia a dia
    }

def load_data():
    state = _empty_state()
    raw = None
    try:
        if os.path.exists(DATA_FILE):
            with open(DATA_FILE, 'r') as f:
                raw = json.load(f)
    except Exception as e:
        print(f'[LOAD] Erro lendo {DATA_FILE}: {e}')

    # Migração de versão anterior: o app.py antigo sempre salvava em /tmp/erome_data.json
    # (sem volume persistente). Se o arquivo novo (no volume) ainda não existe mas esse
    # antigo existe, importa ele automaticamente — assim você não perde as contas/CTAs
    # que já tinha configurado antes desta atualização.
    LEGACY_TMP_FILE = '/tmp/erome_data.json'
    if not raw and DATA_FILE != LEGACY_TMP_FILE and os.path.exists(LEGACY_TMP_FILE):
        try:
            with open(LEGACY_TMP_FILE, 'r') as f:
                raw = json.load(f)
            print(f'[MIGRATE] Importado {LEGACY_TMP_FILE} (versão antiga, sem volume) para {DATA_FILE}')
        except Exception as e:
            print(f'[MIGRATE] Erro lendo arquivo legado: {e}')

    if not raw:
        # Fallback pra variáveis de ambiente, útil só na primeiríssima configuração
        try:
            acc_env = os.environ.get('EROME_ACCOUNTS', '')
            if acc_env:
                state['accounts'] = [a.strip() for a in acc_env.split(',') if a.strip()]
            cta_env = os.environ.get('EROME_CTAS', '')
            if cta_env:
                state['cta_config'] = json.loads(cta_env)
        except Exception:
            pass
        return state

    for key in state.keys():
        if key in raw:
            state[key] = raw[key]

    # Migração: versões antigas guardavam "commented_albums" como LISTA por
    # conta (só id/title/href, sem CTA nem baseline). Se existir isso e ainda
    # não tiver sido migrado pro formato novo (dict), converte uma vez.
    old_commented = raw.get('commented_albums')
    if old_commented and not state.get('commented'):
        migrated = {}
        now_iso = datetime.now(timezone.utc).isoformat()
        for user, albums in old_commented.items():
            migrated[user] = {}
            for a in albums:
                aid = a.get('id')
                if not aid:
                    continue
                migrated[user][aid] = {
                    'at': now_iso, 'title': a.get('title', ''), 'href': a.get('href', ''),
                    'baselineViews': None, 'autoDetected': True, 'lastCta': None,
                }
        state['commented'] = migrated
        print(f'[MIGRATE] commented_albums (lista antiga) migrado para {len(migrated)} conta(s)')

    return state

def save_data():
    with _state_lock:
        payload = {
            'accounts':       ACCOUNTS,
            'cta_config':     cta_config,
            'cta_status':     cta_status,
            'commented':      commented,
            'snapshots':      snapshots,
            'hourly_snaps':   hourly_snaps,
            'viral_streak':   viral_streak,
            'deleted':        deleted,
            'daily_history':  daily_history,
            'cta_hourly_agg': cta_hourly_agg,
        }
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            tmp_path = DATA_FILE + '.tmp'
            with open(tmp_path, 'w') as f:
                json.dump(payload, f)
            os.replace(tmp_path, DATA_FILE)  # escrita atômica — evita arquivo corrompido se cair no meio
        except Exception as e:
            print(f'[SAVE] Erro: {e}')

app = Flask(__name__)

# ================================================================
# LOGIN DO /admin — protege o painel pra ninguém com o link acessar seus dados.
# Configure ADMIN_USER e ADMIN_PASS nas variáveis de ambiente do Railway.
# Sem essas duas variáveis configuradas, o /admin continua aberto (modo atual) —
# assim nada quebra até você configurar.
#
# IMPORTANTE: configure também uma 3ª variável, SECRET_KEY (qualquer texto longo
# e aleatório que só você sabe) — ela assina o cookie de sessão. Sem ela, uma
# nova é gerada aleatoriamente a cada reinício do servidor, e isso desloga todo
# mundo sempre que o Railway reiniciar o container.
# ================================================================
ADMIN_USER = os.environ.get('ADMIN_USER', '')
ADMIN_PASS = os.environ.get('ADMIN_PASS', '')
app.secret_key = os.environ.get('SECRET_KEY') or os.urandom(24).hex()
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=30)

_LOGIN_HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'login.html')
try:
    with open(_LOGIN_HTML_PATH, 'r', encoding='utf-8') as _f:
        LOGIN_HTML = _f.read()
except FileNotFoundError:
    LOGIN_HTML = '<h1 style="color:#f44;font-family:sans-serif">login.html não encontrado — coloque-o na mesma pasta do app.py</h1>'

@app.before_request
def _check_admin_auth():
    if not request.path.startswith('/admin'):
        return  # rotas legadas (/data, /push-cta-status etc.) continuam sem login, usadas pelo Tampermonkey
    if request.path in ('/admin/login', '/admin/logout'):
        return  # essas duas precisam ficar acessíveis mesmo deslogado
    if not ADMIN_USER or not ADMIN_PASS:
        return  # login não configurado ainda — não bloqueia, pra não travar quem ainda não fez essa parte
    if session.get('authed'):
        return  # sessão válida — segue normalmente
    if request.path.startswith('/admin/api/'):
        return jsonify({'ok': False, 'error': 'not_authenticated'}), 401
    return redirect('/admin/login')

@app.route('/admin/login', methods=['GET'])
def admin_login_page():
    return LOGIN_HTML

@app.route('/admin/login', methods=['POST'])
def admin_login_submit():
    body = request.get_json(silent=True) or request.form
    user = (body.get('username') or '').strip()
    pw   = body.get('password') or ''
    if ADMIN_USER and ADMIN_PASS and user == ADMIN_USER and pw == ADMIN_PASS:
        session.clear()
        session['authed'] = True
        session.permanent = True
        return jsonify({'ok': True})
    return jsonify({'ok': False, 'msg': 'Usuário ou senha incorretos.'}), 401

@app.route('/admin/logout', methods=['POST'])
def admin_logout():
    session.clear()
    return jsonify({'ok': True})

_saved          = load_data()
ACCOUNTS        = _saved['accounts']
cta_config      = _saved['cta_config']
cta_status      = _saved['cta_status']
commented       = _saved['commented']
snapshots       = _saved['snapshots']
hourly_snaps    = _saved['hourly_snaps']
viral_streak    = _saved['viral_streak']
deleted         = _saved['deleted']
daily_history   = _saved['daily_history']
cta_hourly_agg  = _saved['cta_hourly_agg']

# Estado só de runtime (não precisa persistir — recalculado na hora)
account_runtime = {}   # {username: {'state': 'ok'|'loading'|'error', 'message': str}}
scan_status     = {'running': False, 'progress': '', 'pct': 0, 'account': None, 'finished': ''}

print(f'[INIT] {len(ACCOUNTS)} conta(s): {ACCOUNTS} | armazenamento: {DATA_FILE}')
if not os.path.exists(DATA_DIR):
    print(f'[INIT] ⚠️  {DATA_DIR} não existe ainda — sem Volume configurado, os dados vão se perder no próximo restart!')

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept': 'text/html,application/xhtml+xml',
    'Accept-Language': 'pt-BR,pt;q=0.9',
}

# ================================================================
# NOTIFICAÇÕES TELEGRAM — avisa CTA sumido / viral confirmado / deletado com CTA
# direto no celular, sem precisar abrir o /admin. Configure TELEGRAM_BOT_TOKEN e
# TELEGRAM_CHAT_ID nas variáveis do Railway. Sem essas duas, fica em silêncio
# (não quebra nada, só não avisa).
# ================================================================
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT_ID   = os.environ.get('TELEGRAM_CHAT_ID', '')

def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f'https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage',
            json={'chat_id': TELEGRAM_CHAT_ID, 'text': message, 'parse_mode': 'HTML'},
            timeout=10,
        )
    except Exception as e:
        print(f'[TELEGRAM] Erro ao enviar notificação: {e}')

def _fmt_views(n):
    if n is None:
        return '—'
    if n >= 1_000_000: return f'{n/1_000_000:.1f}M'.replace('.', ',')
    if n >= 1_000:     return f'{n/1_000:.1f}K'.replace('.', ',')
    return str(n)

# ================================================================
# HELPERS DE TEMPO / NÚMERO
# ================================================================
def now_iso():
    return datetime.now(timezone.utc).isoformat()

def iso_ts(iso_str):
    """ISO string -> timestamp (segundos). Robusto a string vazia/inválida."""
    if not iso_str:
        return 0
    try:
        return datetime.fromisoformat(iso_str).timestamp()
    except Exception:
        return 0

def hour_of(ts):
    """Hora do dia (0-23) no horário de Rafael (America/Sao_Paulo), a partir de um timestamp unix."""
    return datetime.fromtimestamp(ts, TZ).hour

def today_key():
    """Chave de 'hoje' no fuso de Rafael, no formato dd/mm (igual ao toLocaleDateString pt-BR usado no script)."""
    return datetime.now(TZ).strftime('%d/%m')

def parse_num(raw):
    if not raw:
        return 0
    raw = str(raw).strip().replace(' ', '')
    m = re.match(r'^([\d.,]+)([KMB]?)$', raw, re.I)
    if not m:
        return 0
    n = m.group(1).replace(',', '.')
    try:
        v = float(n)
    except ValueError:
        return 0
    s = m.group(2).upper()
    if s == 'K': return int(round(v * 1_000))
    if s == 'M': return int(round(v * 1_000_000))
    if s == 'B': return int(round(v * 1_000_000_000))
    return int(round(v))

# ================================================================
# SCRAPING (com retries — mesma resiliência que foi adicionada no userscript)
# ================================================================
def fetch_with_retries(url, label, attempts=3, status_cb=None):
    last_err = None
    for attempt in range(1, attempts + 1):
        try:
            if status_cb and attempt > 1:
                status_cb(f'Tentando {label} de novo ({attempt}/{attempts})...')
            r = requests.get(url, headers=HEADERS, timeout=15)
            if r.status_code == 429:
                # Backoff bem mais curto que antes (era 60/120/240s — passava de
                # 7 minutos de espera total e parecia "travado pra sempre" na tela).
                wait = 20 * attempt
                if status_cb:
                    status_cb(f'Erome pediu uma pausa — aguardando {wait}s (tentativa {attempt}/{attempts})...')
                print(f'[429] {label} — aguardando {wait}s (tentativa {attempt}/{attempts})...')
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r
        except Exception as e:
            last_err = e
            print(f'[FETCH] Falha em {label} (tentativa {attempt}/{attempts}): {e}')
            if attempt < attempts:
                time.sleep(3 * attempt)
    raise last_err or Exception('falha desconhecida')

def _parse_album_card(el):
    if el.select_one('.album-user'):  # repost — ignora, igual ao userscript
        return None
    link  = el.select_one('a.album-link')
    href  = link['href'] if link else None
    img   = el.select_one('img.album-thumbnail')
    alt   = img.get('alt', '') if img else ''
    title = re.sub(r'#\S+$', '', alt).strip() or 'Sem título'
    vspan = el.select_one('span.album-bottom-views')
    vraw  = re.sub(r'[^\d.,KMBkmb]', '', vspan.get_text()) if vspan else ''
    views = parse_num(vraw)
    # Mesma fonte de ID que o userscript usa (SÓ o atributo id do elemento, sem
    # tentar extrair nada da URL) — antes eu tinha um regex extra pegando o ID
    # da href quando disponível, o que gerava um ID DIFERENTE do que o
    # Tampermonkey sempre usou. Isso quebra silenciosamente a comparação entre
    # o histórico importado do navegador e os dados novos lidos pelo servidor
    # (álbuns "comentados" nunca encontram correspondência = aparecem como
    # "deletados" mesmo existindo).
    aid = el.get('id', '').replace('album-', '')
    if not aid:
        return None
    return {'id': aid, 'title': title, 'href': href, 'views': views}

def scrape_profile(username, progress_cb=None, status_cb=None):
    try:
        r = fetch_with_retries(f'https://www.erome.com/{username}?t=posts', f'página 1 de @{username}', status_cb=status_cb)
        soup = BeautifulSoup(r.text, 'html.parser')

        total_views, followers, album_count = 0, 0, 0
        ui = soup.select_one('.user-info')
        if not ui:
            # Sem ".user-info" não é "conta sem dados" — é quase certo uma página de
            # bloqueio/captcha do Erome (volume de requisições alto), não o perfil de
            # verdade. Levanta erro explícito em vez de devolver "0 álbuns, 0 views",
            # que seria tratado como sucesso e sobrescreveria o histórico bom.
            raise Exception('Página de perfil não reconhecida — possível bloqueio temporário do Erome (tente de novo em alguns minutos)')

        for span in ui.select(':scope > span'):
            txt   = span.get_text()
            nodes = [c for c in span.children if isinstance(c, str) and c.strip()]
            raw   = nodes[0].strip() if nodes else (txt.split()[0] if txt.split() else '')
            num   = parse_num(raw)
            up    = txt.upper()
            if 'ALBUM'     in up: album_count = num
            if 'VIEW'      in up: total_views = num
            if 'FOLLOWERS' in up: followers   = num  # "FOLLOW" sozinho também batia em "FOLLOWING" e sobrescrevia com o valor errado

        av_el  = soup.select_one('img.avatar, .avatar img')
        avatar = av_el.get('src') if av_el else None

        albums = []
        for el in soup.select('div.album[id^="album-"]'):
            a = _parse_album_card(el)
            if a:
                albums.append(a)
        if progress_cb:
            progress_cb(1, len(albums))

        has_next = bool(soup.select_one('a[rel="next"]') or soup.select('.pagination .page-item:not(.active) a[href*="page="]'))
        page = 2
        while has_next:
            r2 = fetch_with_retries(f'https://www.erome.com/{username}?t=posts&page={page}', f'página {page} de @{username}', status_cb=status_cb)
            s2 = BeautifulSoup(r2.text, 'html.parser')
            new_ones = []
            for el in s2.select('div.album[id^="album-"]'):
                a = _parse_album_card(el)
                if a:
                    albums.append(a)
                    new_ones.append(a)
            if progress_cb:
                progress_cb(page, len(albums))
            has_next = bool(s2.select_one('a[rel="next"]') or s2.select('.pagination .page-item:not(.active) a[href*="page="]'))
            if not has_next or not new_ones:
                break
            page += 1
            time.sleep(0.6)

        return {
            'username':   username,
            'avatar':     avatar,
            'totalViews': total_views,
            'followers':  followers,
            'albumCount': len(albums),
            'albums':     albums,
            'fetchedAt':  now_iso(),
            'error':      None,
        }
    except Exception as ex:
        return {'username': username, 'error': str(ex), 'fetchedAt': now_iso(), 'albums': []}


# ================================================================
# SNAPSHOTS (histórico de 15-20min, base de tudo)
# ================================================================
def push_snapshot(u, data):
    lst = snapshots.setdefault(u, [])
    lst.insert(0, data)
    if len(lst) > 96:
        del lst[96:]

def last_saved_snapshot(u):
    """O snapshot que estava em [0] ANTES do push desta rodada — usar pra diff/deleted DURANTE o refresh."""
    lst = snapshots.get(u, [])
    return lst[0] if lst else None

def prev_snapshot(u):
    """Usar DEPOIS que o snapshot novo já foi empurrado — [0] é o atual, [1] é 1 ciclo atrás."""
    lst = snapshots.get(u, [])
    return lst[1] if len(lst) > 1 else None

def current_snapshot(u):
    lst = snapshots.get(u, [])
    return lst[0] if lst else None

def snap_of_24h(u):
    lst = snapshots.get(u, [])
    if len(lst) < 2:
        return None
    now = time.time()
    best, best_diff = None, float('inf')
    for snap in lst:
        diff = abs(now - iso_ts(snap['fetchedAt']) - 86400)
        if diff < best_diff:
            best_diff, best = diff, snap
    return best if best_diff < 2 * 3600 else None

pending_deleted = {}  # {username: {albumId: {title, views}}} — "suspeitos" aguardando confirmação no próximo ciclo

def detect_deleted(u, curr, prev):
    if not prev or not prev.get('albums'):
        return
    curr_ids = {a['id'] for a in curr.get('albums', [])}
    prev_map = {a['id']: a for a in prev.get('albums', [])}
    gone_now = [a for aid, a in prev_map.items() if aid not in curr_ids]

    log      = deleted.setdefault(u, [])
    existing = {d['id'] for d in log}
    comm     = commented.get(u, {})
    pending  = pending_deleted.setdefault(u, {})

    # Confirma como deletado quem JÁ estava suspeito desde o ciclo anterior E continua
    # ausente agora. Uma única leitura com falha pontual (ex: 1 página de paginação que
    # não carregou) não é mais suficiente pra logar como deletado — precisa sumir 2 vezes
    # seguidas. Isso é o que estava causando os álbuns antigos reaparecendo na aba
    # Deletados pouco depois de limpar: uma leitura incompleta isolada já bastava.
    confirmed_ids = set()
    for aid in list(pending.keys()):
        if aid in curr_ids:
            del pending[aid]  # voltou a aparecer — era leitura incompleta, falso alarme
            continue
        if aid in existing:
            del pending[aid]
            continue
        info = pending.pop(aid)
        had_cta = aid in comm
        log.insert(0, {'id': aid, 'title': info['title'], 'views': info['views'], 'at': now_iso(), 'hadCta': had_cta})
        confirmed_ids.add(aid)
        if had_cta:
            print(f'[DELETED] Vídeo com CTA deletado (confirmado em 2 ciclos) em @{u}: {info["title"][:50]}')
            send_telegram(
                f'🗑️ <b>VÍDEO COM CTA DELETADO</b>\n@{u}\n"{info["title"][:60]}"\n'
                f'Esse post tinha seu link comentado e saiu do ar.'
            )

    # Marca os que desapareceram NESTE ciclo como suspeitos — só confirma se sumirem de
    # novo no próximo ciclo também.
    for a in gone_now:
        if a['id'] in existing or a['id'] in confirmed_ids:
            continue
        pending.setdefault(a['id'], {'title': a['title'], 'views': a['views']})

    if len(log) > 500:
        del log[500:]

# ================================================================
# VIEWS HOJE / TIMELINE POR HORA / HISTÓRICO DIÁRIO
# ================================================================
def get_views_today(u):
    curr   = current_snapshot(u)
    snap24 = snap_of_24h(u)
    if not curr or not snap24:
        return None
    return max(0, curr['totalViews'] - snap24['totalViews'])

def _timeline_from_snapshots(snap_list, id_filter=None):
    """Núcleo compartilhado por get_views_timeline e get_commented_views_timeline."""
    if len(snap_list) < 2:
        return []
    now    = time.time()
    cutoff = now - 86400
    recent = sorted([s for s in snap_list if iso_ts(s['fetchedAt']) > cutoff], key=lambda s: iso_ts(s['fetchedAt']))
    if len(recent) < 2:
        return []

    raw_points = []
    for i in range(1, len(recent)):
        prev_s, cur_s = recent[i - 1], recent[i]
        prev_map = {a['id']: a['views'] for a in prev_s.get('albums', [])}
        delta = 0
        for a in cur_s.get('albums', []):
            if id_filter is not None and a['id'] not in id_filter:
                continue
            pv = prev_map.get(a['id'], 0)  # post novo (sem baseline) = todas as views contam como "ganhas" no período
            if a['views'] > pv:
                delta += a['views'] - pv
        if delta <= 0:
            continue
        t_prev     = iso_ts(prev_s['fetchedAt'])
        t_cur      = iso_ts(cur_s['fetchedAt'])
        span_hours = max(1, round(max(t_cur - t_prev, 60) / 3600))
        per_hour   = delta / span_hours
        for h in range(span_hours):
            raw_points.append((hour_of(t_cur - h * 3600), per_hour))

    by_hour = {}
    for hod, v in raw_points:
        by_hour[hod] = by_hour.get(hod, 0) + v

    points = []
    current_hour = hour_of(now)
    for i in range(23, -1, -1):
        h = (current_hour - i + 24) % 24
        points.append({'label': f'{h:02d}h', 'hour': h, 'views': round(by_hour.get(h, 0))})
    return points

def get_views_timeline(u):
    return _timeline_from_snapshots(snapshots.get(u, []))

def get_commented_views_timeline(u):
    ids = set((commented.get(u) or {}).keys())
    if not ids:
        return []
    return _timeline_from_snapshots(snapshots.get(u, []), id_filter=ids)

def get_views_gained_24h_for_ids(u, id_set):
    lst = snapshots.get(u, [])
    if len(lst) < 2 or not id_set:
        return 0
    cutoff = time.time() - 86400
    recent = sorted([s for s in lst if iso_ts(s['fetchedAt']) > cutoff], key=lambda s: iso_ts(s['fetchedAt']))
    if len(recent) < 2:
        return 0
    total = 0
    for i in range(1, len(recent)):
        prev_map = {a['id']: a['views'] for a in recent[i - 1].get('albums', [])}
        for a in recent[i].get('albums', []):
            if a['id'] not in id_set:
                continue
            pv = prev_map.get(a['id'], 0)
            if a['views'] > pv:
                total += a['views'] - pv
    return total

def get_top_cta_hours(u, top_n=3):
    agg = cta_hourly_agg.get(u)
    if not agg:
        return []
    entries = [{'hour': int(h), 'views': v} for h, v in agg.items()]
    entries.sort(key=lambda e: -e['views'])
    return entries[:top_n]

def check_daily_reset(u):
    curr = current_snapshot(u)
    if not curr:
        return
    snap24 = snap_of_24h(u)
    if not snap24:
        return
    snap_age = time.time() - iso_ts(snap24['fetchedAt'])
    if snap_age < 23 * 3600:
        return

    key  = today_key()
    hist = daily_history.setdefault(u, [])
    if any(d['date'] == key for d in hist):
        return

    views_gained = max(0, curr['totalViews'] - snap24['totalViews'])
    hist.insert(0, {'date': key, 'views': views_gained, 'savedAt': now_iso()})
    if len(hist) > 30:
        del hist[30:]

    cta_tl = get_commented_views_timeline(u)
    if cta_tl:
        agg = cta_hourly_agg.setdefault(u, {})
        for t in cta_tl:
            if t['views'] > 0:
                key_h = str(t['hour'])
                agg[key_h] = agg.get(key_h, 0) + t['views']

# ================================================================
# SISTEMA VIRAL — janelas horárias (idêntico ao userscript)
# ================================================================
def save_hourly_snap(u):
    m = current_snapshot(u)
    if not m:
        return
    lst = hourly_snaps.setdefault(u, [])
    lst.insert(0, {
        'at': now_iso(),
        'albums': [{'id': a['id'], 'views': a['views'], 'title': a['title'], 'href': a['href']} for a in m.get('albums', [])],
        'totalViews': m.get('totalViews', 0),
    })
    if len(lst) > 48:
        del lst[48:]

def get_hourly_snap_of(u, hours_ago):
    hs = hourly_snaps.get(u, [])
    if not hs:
        return None
    target = time.time() - hours_ago * 3600
    best, best_diff = None, float('inf')
    for s in hs:
        diff = abs(iso_ts(s['at']) - target)
        if diff < best_diff:
            best_diff, best = diff, s
    return best if best_diff < 1800 else None

def get_snap_hours_ago(u, hours_ago):
    lst = snapshots.get(u, [])
    if len(lst) < 2:
        return None
    target = time.time() - hours_ago * 3600
    best, best_diff = None, float('inf')
    for s in lst:
        diff = abs(iso_ts(s['fetchedAt']) - target)
        if diff < best_diff:
            best_diff, best = diff, s
    return best if best_diff < 20 * 60 else None

def get_album_hourly_peak(u, album_id, hours_window=24):
    hs = hourly_snaps.get(u, [])
    if len(hs) < 2:
        return None
    cutoff = time.time() - hours_window * 3600
    recent = sorted([s for s in hs if iso_ts(s['at']) > cutoff], key=lambda s: iso_ts(s['at']))
    if len(recent) < 2:
        return None
    peak = 0
    for i in range(1, len(recent)):
        prev_map = {a['id']: a['views'] for a in recent[i - 1].get('albums', [])}
        cur_alb  = next((a for a in recent[i].get('albums', []) if a['id'] == album_id), None)
        if not cur_alb:
            continue
        pv    = prev_map.get(album_id, cur_alb['views'])
        delta = max(0, cur_alb['views'] - pv)
        peak  = max(peak, delta)
    return peak

# ================================================================
# IMPORTAÇÃO DO BACKUP ANTIGO (botão "⇩ Exportar" do userscript Tampermonkey)
# — migração única de histórico já acumulado no navegador pro servidor.
# Os formatos são quase idênticos (foi feito de propósito), então a maior
# parte é só mesclar listas/dicts sem perder nada que já existe nos dois lados.
# ================================================================
def _merge_snapshot_list(existing, incoming, cap, time_key):
    combined = {}
    for s in (existing or []) + (incoming or []):
        key = s.get(time_key)
        if key:
            combined[key] = s
    merged = sorted(combined.values(), key=lambda s: s[time_key], reverse=True)
    return merged[:cap]

def _merge_deleted_list(existing, incoming, cap=500):
    seen = {d['id']: d for d in (incoming or []) if d.get('id')}
    for d in (existing or []):  # existing tem prioridade se o mesmo id aparecer nos dois
        if d.get('id'):
            seen[d['id']] = d
    return sorted(seen.values(), key=lambda d: d.get('at', ''), reverse=True)[:cap]

def _merge_daily_history(existing, incoming, cap=30):
    merged = list(existing or [])
    have = {d['date'] for d in merged}
    for d in (incoming or []):
        if d.get('date') and d['date'] not in have:
            merged.append(d)
            have.add(d['date'])
    return merged[:cap]

def _merge_commented(existing, incoming):
    merged = dict(incoming or {})
    merged.update(existing or {})  # existing (já no servidor) sobrescreve em caso de conflito
    return merged

def _merge_cta_status(existing, incoming):
    merged = dict(incoming or {})
    for aid, v in (existing or {}).items():
        old = merged.get(aid)
        if not old or (v.get('checkedAt', '') >= old.get('checkedAt', '')):
            merged[aid] = v
    return merged

def _merge_viral_streak(existing, incoming):
    merged = dict(incoming or {})
    for aid, v in (existing or {}).items():
        merged[aid] = max(merged.get(aid, 0), v)
    return merged

def _merge_cta_hourly_agg(existing, incoming):
    merged = dict(incoming or {})
    for h, v in (existing or {}).items():
        merged[h] = merged.get(h, 0) + v
    return merged

def import_legacy_export(data):
    if not data or not isinstance(data.get('accounts'), list):
        return {'ok': False, 'msg': 'Arquivo não reconhecido como backup do Erome Analytics (exporte de novo pelo botão ⇩ Exportar no Tampermonkey).'}

    incoming_accounts = []
    for a in data['accounts']:
        name = a if isinstance(a, str) else (a.get('username') if isinstance(a, dict) else None)
        if name:
            incoming_accounts.append(name)
    if not incoming_accounts:
        return {'ok': False, 'msg': 'Nenhuma conta encontrada dentro do arquivo.'}

    global_cta_list = [c.upper() for c in (data.get('ctaList') or [])]
    added = []
    for u in incoming_accounts:
        if u not in ACCOUNTS:
            ACCOUNTS.append(u)
            added.append(u)

        snapshots[u]      = _merge_snapshot_list(snapshots.get(u), (data.get('snapshots') or {}).get(u), 96, 'fetchedAt')
        hourly_snaps[u]   = _merge_snapshot_list(hourly_snaps.get(u), (data.get('hourlySnaps') or {}).get(u), 48, 'at')
        deleted[u]        = _merge_deleted_list(deleted.get(u), (data.get('deleted') or {}).get(u))
        daily_history[u]  = _merge_daily_history(daily_history.get(u), (data.get('dailyHistory') or {}).get(u))
        commented[u]      = _merge_commented(commented.get(u), (data.get('commented') or {}).get(u))
        cta_status[u]     = _merge_cta_status(cta_status.get(u), (data.get('ctaStatus') or {}).get(u))
        viral_streak[u]   = _merge_viral_streak(viral_streak.get(u), (data.get('viralStreak') or {}).get(u))
        cta_hourly_agg[u] = _merge_cta_hourly_agg(cta_hourly_agg.get(u), (data.get('ctaHourlyAgg') or {}).get(u))

        # O CTA list do userscript antigo é global (vale pra todas as contas) — aplica em cada uma sem duplicar
        lst = cta_config.setdefault(u, [])
        for c in global_cta_list:
            if c not in lst:
                lst.append(c)

    save_data()
    snap_counts = {u: len(snapshots.get(u, [])) for u in incoming_accounts}
    return {
        'ok': True, 'accounts': incoming_accounts, 'addedAccounts': added,
        'msg': f'Histórico mesclado em {len(incoming_accounts)} conta(s)! ' +
               ', '.join(f'@{u}: {n} snapshots' for u, n in snap_counts.items()),
    }

def get_viral(u):
    m = current_snapshot(u)
    if not m:
        return []
    prev1h = get_hourly_snap_of(u, 1)
    prev   = prev1h or prev_snapshot(u)
    if not prev:
        return []
    pm        = {a['id']: a['views'] for a in prev.get('albums', [])}
    is_hourly = prev1h is not None

    all_albums = []
    for a in m.get('albums', []):
        if a['id'] not in pm:
            continue  # post sem baseline real (recém publicado) — não entra na análise
        delta = max(0, a['views'] - pm[a['id']])
        if delta <= 0:
            continue
        all_albums.append({**a, 'delta': delta, 'isHourly': is_hourly})

    if not all_albums:
        return []
    avg      = sum(a['delta'] for a in all_albums) / len(all_albums)
    variance = sum((a['delta'] - avg) ** 2 for a in all_albums) / len(all_albums)
    std_dev  = variance ** 0.5
    threshold = avg + std_dev
    for a in all_albums:
        a['ratio']     = (a['delta'] / avg) if avg > 0 else 1
        a['standout']  = a['delta'] > threshold
        a['avg']       = avg
        a['threshold'] = threshold
    all_albums.sort(key=lambda a: -a['delta'])
    return all_albums

def get_standout(u):
    return [a for a in get_viral(u) if a['standout']]

def update_viral_streak(u, standouts):
    streak = viral_streak.setdefault(u, {})
    ids = {a['id'] for a in standouts}
    for a in standouts:
        streak[a['id']] = streak.get(a['id'], 0) + 1
    for aid in list(streak.keys()):
        if aid not in ids:
            streak[aid] = max(0, streak.get(aid, 0) - 1)

def get_confirmed_viral(u):
    hs       = hourly_snaps.get(u, [])
    streak   = viral_streak.get(u, {})
    standout = get_standout(u)
    if not standout:
        return []
    if len(hs) < 3:
        return [a for a in standout if streak.get(a['id'], 0) >= 3]
    snap2h    = get_hourly_snap_of(u, 2) or hs[min(2, len(hs) - 1)]
    existed2h = {a['id'] for a in (snap2h or {}).get('albums', [])}
    result = []
    for a in standout:
        if streak.get(a['id'], 0) < 2:
            continue
        if a['id'] not in existed2h:
            continue
        result.append(a)
    return result

# ================================================================
# POSTS NOVOS / DIAS SEM POSTAR
# ================================================================
def get_days_since_post(u):
    lst = snapshots.get(u, [])
    if len(lst) < 2 or not lst[0].get('albums'):
        return None
    current_ids = {a['id'] for a in lst[0]['albums']}
    for i in range(1, len(lst)):
        ids_at_i = {a['id'] for a in lst[i].get('albums', [])}
        if any(aid not in ids_at_i for aid in current_ids):
            post_ts = iso_ts(lst[i - 1]['fetchedAt'])
            diff_h  = (time.time() - post_ts) / 3600
            if diff_h < 24:
                return 0
            return int(diff_h // 24)
    oldest = lst[-1]
    diff_h = (time.time() - iso_ts(oldest['fetchedAt'])) / 3600
    if diff_h < 24:
        return 0
    return int(diff_h // 24)

def get_new_posts_count(curr, prev):
    if not prev or not prev.get('albums'):
        return 0
    prev_ids = {a['id'] for a in prev['albums']}
    return sum(1 for a in curr.get('albums', []) if a['id'] not in prev_ids)

def get_post_message(new_posts, days_sem):
    if new_posts > 0:
        if new_posts >= 10: return {'emoji': '🚀', 'msg': f'MONSTRO! {new_posts} posts hoje! A Porsche já tá no showroom esperando!'}
        if new_posts >= 6:  return {'emoji': '🔥', 'msg': f'Focado demais! {new_posts} álbuns novos hoje! Assim vai longe!'}
        if new_posts >= 3:  return {'emoji': '💪', 'msg': f'{new_posts} posts hoje! Tá no ritmo certo, continua assim!'}
        if new_posts == 2: return {'emoji': '👍', 'msg': '2 posts hoje! Bom começo, mas dá pra mais né?'}
        if new_posts == 1: return {'emoji': '😴', 'msg': 'Preguiça né? A Porsche vai demorar a vir com 1 postinho só kk'}
    if days_sem is None or days_sem == 0:
        return None
    if days_sem == 1: return {'emoji': '⚠️', 'msg': 'Psiu... já faz 1 dia sem postar. O algoritmo esquece rápido!'}
    if days_sem == 2: return {'emoji': '😬', 'msg': '2 dias sem postar! Seus seguidores estão com saudade...'}
    if days_sem == 3: return {'emoji': '🚨', 'msg': '3 dias! Cara, o algoritmo já te esqueceu. Volta pro trabalho!'}
    if days_sem >= 7: return {'emoji': '💀', 'msg': f'{days_sem} dias sem postar?! A Porsche foi embora... corre!'}
    return {'emoji': '⏰', 'msg': f'{days_sem} dias sem postar. Hora de voltar à ação!'}

# ================================================================
# CTAs NOS COMENTÁRIOS
# ================================================================
def fetch_album_comments(href):
    r = requests.get(href, headers=HEADERS, timeout=15)
    r.raise_for_status()
    # Mesma proteção do scrape_profile: uma resposta curta/sem cara de página real do
    # Erome é mais provável bloqueio/captcha do que "o álbum realmente não tem nada".
    # Sem isso, um bloqueio temporário virava falso alarme de "CTA sumiu" em massa.
    low = r.text.lower()
    if len(r.text) < 1500 or 'checking your browser' in low or 'cf-browser-verification' in low or 'captcha' in low or 'attention required' in low:
        raise Exception('Resposta suspeita ao ler comentários (possível bloqueio temporário)')
    return BeautifulSoup(r.text, 'html.parser')

def _comment_text(soup):
    parts = []
    for sel in ['span.comment-text', '.comment-text', '.comment-content', '.list-comment', 'div.comment']:
        parts += [el.get_text() for el in soup.select(sel)]
    comm_div = soup.select_one('.comments, #comments, [class*="comment"]')
    if comm_div:
        parts.append(comm_div.get_text())
    return ' '.join(parts).lower()

def check_ctas_for(u):
    """Verifica CTA só dos álbuns marcados como 'comentados' — igual ao checkCTAs() do userscript.
    (Antes verificava TODOS os álbuns da conta a cada 10min — bem mais pesado e sem sentido,
    já que só os marcados como comentados têm CTA seu pra checar.)"""
    ctas = cta_config.get(u, [])
    comm = commented.get(u, {})
    if not ctas or not comm:
        return
    st = cta_status.setdefault(u, {})
    for album_id, c in list(comm.items()):
        href = c.get('href')
        if not href:
            continue
        try:
            soup      = fetch_album_comments(href)
            comm_text = _comment_text(soup)
            found     = next((ct for ct in ctas if ct.lower() in comm_text), None)
            prev      = st.get(album_id)
            st[album_id] = {'ok': bool(found), 'foundCta': found, 'checkedAt': now_iso()}
            if found:
                c['lastCta'] = found
            if prev and prev.get('ok') and not found:
                print(f'[CTA] SUMIU em @{u} — {c.get("title","")[:50]}')
                send_telegram(
                    f'🚨 <b>CTA SUMIU DOS COMENTÁRIOS</b>\n@{u}\n"{c.get("title","")[:60]}"\n'
                    f'Nenhum dos seus CTAs foi encontrado — pode ter sido removido ou apagado.'
                )
            time.sleep(0.6)
        except Exception as ex:
            print(f'[CTA] Erro @{u}/{album_id}: {ex}')

# ================================================================
# COMENTADOS — marcar/desmarcar
# ================================================================
def mark_commented(u, album_id, title, href, views):
    comm = commented.setdefault(u, {})
    existing = comm.get(album_id, {})
    comm[album_id] = {
        'at': existing.get('at', now_iso()),
        'title': title or existing.get('title', ''),
        'href': href or existing.get('href', ''),
        'baselineViews': existing.get('baselineViews', views),
        'autoDetected': existing.get('autoDetected', False),
        'lastCta': existing.get('lastCta'),
    }

def unmark_commented(u, album_id):
    if u in commented and album_id in commented[u]:
        del commented[u][album_id]
    if u in cta_status and album_id in cta_status[u]:
        del cta_status[u][album_id]

# ================================================================
# SCANNER DE CTAs — varre álbuns SEM CTA ainda confirmado (igual à v3.24 do userscript)
# ================================================================
def scan_all_albums(u):
    global scan_status
    if scan_status.get('running'):
        return
    ctas = cta_config.get(u, [])
    if not ctas:
        scan_status = {'running': False, 'progress': 'Cadastre CTAs antes de escanear.', 'pct': 0, 'account': u, 'finished': now_iso()}
        return

    m = current_snapshot(u)
    if not m or not m.get('albums'):
        scan_status = {'running': False, 'progress': 'Sem álbuns carregados ainda — atualize a conta primeiro.', 'pct': 0, 'account': u, 'finished': now_iso()}
        return

    comm  = commented.setdefault(u, {})
    queue = [a for a in m['albums'] if a.get('href') and a['id'] not in comm]
    skipped = len(m['albums']) - len(queue)
    total   = len(queue)

    if total == 0:
        scan_status = {'running': False, 'progress': f'Nada novo pra escanear — {skipped} álbuns já marcados continuam monitorados.', 'pct': 100, 'account': u, 'finished': now_iso()}
        return

    scan_status = {'running': True, 'progress': f'Escaneando 0/{total} álbuns novos...', 'pct': 0, 'account': u, 'finished': ''}
    checked, found, new_found = 0, 0, 0

    for album in queue:
        try:
            soup      = fetch_album_comments(album['href'])
            comm_text = _comment_text(soup)
            found_cta = next((ct for ct in ctas if ct.lower() in comm_text), None)
            if found_cta:
                found += 1
                is_new = album['id'] not in comm
                comm[album['id']] = {
                    'at': now_iso(), 'title': album['title'], 'href': album['href'],
                    'baselineViews': album.get('views'), 'autoDetected': True, 'lastCta': found_cta,
                }
                if is_new:
                    new_found += 1
        except Exception as ex:
            print(f'[SCAN] Erro {album.get("href")}: {ex}')
        checked += 1
        scan_status['pct']      = int((checked / total) * 100)
        scan_status['progress'] = f'Escaneando {checked}/{total} álbuns — {found} CTAs encontrados'
        time.sleep(0.6)

    save_data()
    scan_status = {
        'running': False, 'pct': 100, 'account': u, 'finished': now_iso(),
        'progress': f'Concluído! {found} CTAs encontrados ({new_found} novos) em {total} álbuns verificados.'
                    + (f' ({skipped} já marcados foram pulados)' if skipped else ''),
    }

# ================================================================
# REFRESH — dados da conta
# ================================================================
_refreshing = set()

def refresh_account(u):
    if u in _refreshing:
        return
    _refreshing.add(u)
    account_runtime[u] = {'state': 'loading', 'message': '', 'progress': 'Iniciando…'}
    try:
        def _on_progress(page, count):
            # Mesmo texto que o userscript já mostrava (#ea-prog) — só que agora
            # vem do servidor via polling, pra você ver em tempo real o que está
            # sendo lido, mesmo se o navegador estiver fechado e você abrir depois.
            account_runtime[u]['progress'] = f'Página {page} · {count} álbuns…'
        def _on_status(msg):
            account_runtime[u]['progress'] = msg

        data = scrape_profile(u, progress_cb=_on_progress, status_cb=_on_status)
        if data.get('error'):
            account_runtime[u] = {'state': 'error', 'message': data['error'], 'progress': ''}
            print(f'[REFRESH] Erro @{u}: {data["error"]}')
            return

        # ── BLINDAGEM CRÍTICA ──────────────────────────────────────────────
        # Nunca aceita uma leitura com 0 álbuns se já existia histórico bom com
        # álbuns > 0. Isso é o que causou o bug de "tudo aparecendo deletado":
        # uma resposta vazia (bloqueio temporário) era tratada como sucesso
        # legítimo e sobrescrevia o histórico todo. Agora isso é rejeitado e o
        # histórico anterior fica intacto — só tenta de novo no próximo ciclo.
        prev_check = current_snapshot(u)
        if prev_check and prev_check.get('albumCount', 0) > 0 and data.get('albumCount', 0) == 0:
            msg = (f'Resposta vazia/suspeita (0 álbuns, antes havia {prev_check["albumCount"]}) '
                   f'— mantendo dados anteriores por segurança, tentando de novo no próximo ciclo.')
            account_runtime[u] = {'state': 'error', 'message': msg, 'progress': ''}
            print(f'[REFRESH] ⚠️ @{u}: {msg}')
            return

        with _state_lock:
            prev = last_saved_snapshot(u)
            detect_deleted(u, data, prev)
            push_snapshot(u, data)
            check_daily_reset(u)

        new_posts = get_new_posts_count(data, prev)
        days_sem  = get_days_since_post(u)
        post_msg  = get_post_message(new_posts, days_sem)
        if post_msg:
            print(f'[{u}] {post_msg["emoji"]} {post_msg["msg"]}')
        if prev and prev.get('albumCount', 0) > data['albumCount']:
            print(f'[{u}] ⚠️ {prev["albumCount"] - data["albumCount"]} álbum(ns) removido(s)')

        account_runtime[u] = {'state': 'ok', 'message': '', 'progress': ''}
        check_ctas_for(u)
        save_data()
    finally:
        _refreshing.discard(u)

def refresh_all():
    # Era sequencial (1 conta esperando a outra) — em contas com bastante histórico
    # "Atualizar tudo" parecia travado por minutos. A correção anterior paralelizou
    # 4 ao mesmo tempo, mas isso bombardeou o Erome com requisições demais de uma vez
    # e ele aparentemente começou a devolver respostas de bloqueio temporário — foi
    # isso que causou o bug de tudo aparecer "deletado". Agora: só 2 simultâneas,
    # com um pequeno intervalo entre o início de cada uma — ainda bem mais rápido
    # que sequencial puro, mas sem rajada de requisições.
    accs = list(ACCOUNTS)
    if not accs:
        return
    def _staggered(item):
        i, u = item
        if i == 1:  # só escalona o início das 2 primeiras (as únicas que podem ficar concorrentes de verdade, já que max_workers=2) — da 3ª em diante, o próprio limite de workers já garante o espaçamento
            time.sleep(2.0)
        refresh_account(u)
    with ThreadPoolExecutor(max_workers=min(2, len(accs))) as ex:
        list(ex.map(_staggered, enumerate(accs)))

def check_viral_alerts(u):
    """Avisa exatamente no momento em que um vídeo é CONFIRMADO viral (streak chega a 3)
    — não repete depois disso, igual ao checkViralAlerts() do userscript."""
    confirmed = get_confirmed_viral(u)
    streak    = viral_streak.get(u, {})
    comm      = commented.get(u, {})
    for a in confirmed:
        if streak.get(a['id'], 0) != 3:
            continue  # só dispara no ciclo exato em que confirma — evita repetir toda hora
        was_commented = a['id'] in comm
        if was_commented:
            send_telegram(
                f'🔄 <b>RENOVAR COMENTÁRIO</b>\n@{u}\n"{a["title"][:60]}"\n'
                f'Já comentou antes — vale ir de novo!'
            )
        else:
            send_telegram(
                f'🎯 <b>HORA DE COMENTAR!</b>\n@{u}\n"{a["title"][:60]}"\n'
                f'+{_fmt_views(a["delta"])} views na última hora — viralizando de verdade!'
            )

def viral_refresh_all():
    for u in list(ACCOUNTS):
        if not current_snapshot(u):
            continue
        with _state_lock:
            save_hourly_snap(u)
            standout = get_standout(u)
            update_viral_streak(u, standout)
            check_viral_alerts(u)
        time.sleep(0.5)
    save_data()
    print(f'[VIRAL] refresh horário ✓ {datetime.now(TZ).strftime("%H:%M")}')

def cta_refresh_all():
    for u in list(ACCOUNTS):
        check_ctas_for(u)
        time.sleep(1)
    save_data()

# ================================================================
# LOOP EM BACKGROUND — 3 timers, igual ao userscript (15-20min / 1h / 10min)
# ================================================================
def background_loop():
    last_data, last_cta, last_viral = 0, 0, 0
    DATA_INTERVAL  = 20 * 60   # 20min (margem de segurança contra bloqueio do Erome)
    CTA_INTERVAL   = 10 * 60
    VIRAL_INTERVAL = 60 * 60
    while True:
        now = time.time()
        try:
            if now - last_data >= DATA_INTERVAL:
                refresh_all()
                last_data = now
            if now - last_cta >= CTA_INTERVAL:
                cta_refresh_all()
                last_cta = now
            if now - last_viral >= VIRAL_INTERVAL:
                viral_refresh_all()
                last_viral = now
        except Exception as ex:
            print(f'[LOOP] Erro inesperado: {ex}')
        time.sleep(30)

# ================================================================
# PAYLOAD DO DASHBOARD — tudo que o /admin precisa, numa única chamada
# ================================================================
def build_dashboard_payload(u):
    m = current_snapshot(u)
    if not m:
        rt = account_runtime.get(u, {})
        return {'username': u, 'hasData': False, 'state': rt.get('state', 'none'), 'message': rt.get('message', '')}

    prev    = prev_snapshot(u)
    comm    = commented.get(u, {})
    cta_st  = cta_status.get(u, {})
    cta_lst = cta_config.get(u, [])

    def diff(key):
        if not prev:
            return None
        return m.get(key, 0) - prev.get(key, m.get(key, 0))

    timeline   = get_views_timeline(u)
    gain1h_ov  = timeline[-1]['views'] if timeline else None
    views_today = get_views_today(u)
    days_sem   = get_days_since_post(u)
    new_posts  = get_new_posts_count(m, prev)
    post_msg   = get_post_message(new_posts, days_sem)

    standout_all  = get_standout(u)
    confirmed_all = get_confirmed_viral(u)
    standout_pending  = [a for a in standout_all  if a['id'] not in comm]
    confirmed_pending = [a for a in confirmed_all if a['id'] not in comm]

    alerts = []
    # Antes esse alerta comparava só os 2 últimos snapshots (prev vs atual) — isso é
    # DESCONECTADO da aba Deletados, então clicar em "Limpar" lá não fazia esse aviso
    # sumir, e ele também podia ficar reaparecendo por várias rodadas. Agora ele usa
    # o MESMO log persistido da aba Deletados: limpar lá também limpa aqui, e o aviso
    # expira sozinho depois de 1h mesmo que você não entre na aba.
    dlog = deleted.get(u, [])
    recent_deletions = [d for d in dlog if (time.time() - iso_ts(d.get('at', ''))) < 3600]
    if recent_deletions:
        alerts.append({'type': 'warn', 'icon': '⚠️',
                        'text': f"{len(recent_deletions)} álbum(ns) removido(s) recentemente. Veja a aba Deletados."})
    if post_msg:
        cls = 'ok' if new_posts > 0 else ('fire' if (days_sem or 0) >= 3 else 'warn')
        alerts.append({'type': cls, 'icon': post_msg['emoji'], 'text': post_msg['msg']})

    overview = {
        'totalViews': m['totalViews'], 'followers': m['followers'], 'albumCount': m['albumCount'],
        'followersDiff': diff('followers'), 'albumCountDiff': diff('albumCount'),
        'gain1h': gain1h_ov, 'viewsToday': views_today, 'daysSem': days_sem, 'newPosts': new_posts,
        'postMsg': post_msg, 'alerts': alerts, 'timeline': timeline,
        'dailyHistory': daily_history.get(u, []),
        'top8': sorted(m.get('albums', []), key=lambda a: -a['views'])[:8],
        'toCommentCount': len(confirmed_pending) or len(standout_pending),
        'toCommentLabel': 'confirmados' if confirmed_pending else ('em destaque' if standout_pending else 'normal'),
        'fetchedAt': m['fetchedAt'], 'albumsRead': len(m.get('albums', [])), 'avatar': m.get('avatar'),
    }

    ranking = sorted(m.get('albums', []), key=lambda a: -a['views'])

    streak        = viral_streak.get(u, {})
    standout_ids  = {a['id'] for a in standout_all}
    confirmed_ids = {a['id'] for a in confirmed_all}
    viral_pending = [a for a in get_viral(u) if a['id'] not in comm]
    viral_rows = [{
        'rank': i + 1, 'id': a['id'], 'title': a['title'], 'href': a['href'],
        'delta': a['delta'], 'views': a['views'], 'ratio': a['ratio'],
        'standout': a['id'] in standout_ids, 'confirmed': a['id'] in confirmed_ids,
        'streak': streak.get(a['id'], 0),
    } for i, a in enumerate(viral_pending[:30])]
    viral_tab = {'hasPrev': prev is not None, 'rows': viral_rows, 'toCommentCount': len(confirmed_pending)}

    # ---- Comentados ----
    entries      = sorted(comm.items(), key=lambda kv: -iso_ts(kv[1].get('at', '')))
    albums_map   = {a['id']: a for a in m.get('albums', [])}
    prev1h       = get_hourly_snap_of(u, 1) or get_snap_hours_ago(u, 1)
    prev_map_1h  = {a['id']: a['views'] for a in (prev1h or {}).get('albums', [])}
    prev_day     = get_hourly_snap_of(u, 24) or get_snap_hours_ago(u, 24)
    prev_map_day = {a['id']: a['views'] for a in (prev_day or {}).get('albums', [])}
    has_hourly   = prev1h is not None
    has_daily    = prev_day is not None

    deleted_count = sum(1 for aid, _ in entries if aid not in albums_map)
    gone_count    = sum(1 for aid, _ in entries if cta_st.get(aid) and not cta_st[aid].get('ok'))

    total_gain_1h, count_gain_1h = 0, 0
    for aid, c in entries:
        alb = albums_map.get(aid)
        if not alb or not has_hourly:
            continue
        pv = prev_map_1h.get(aid, alb['views'])
        total_gain_1h += max(0, alb['views'] - pv)
        count_gain_1h += 1
    avg_gain_1h = (total_gain_1h / count_gain_1h) if count_gain_1h else 0

    cta_timeline   = get_commented_views_timeline(u)
    gain24h_total  = sum(t['views'] for t in cta_timeline)
    top_hours      = get_top_cta_hours(u, 3)

    groups = {}
    for aid, c in entries:
        st_e  = cta_st.get(aid)
        found = (st_e.get('foundCta') if (st_e and st_e.get('ok')) else None) or c.get('lastCta')
        key   = found or '— sem CTA identificado'
        g = groups.setdefault(key, {'ids': set(), 'count': 0})
        g['ids'].add(aid); g['count'] += 1

    cta_summary = []
    for keyword, g in groups.items():
        gain24_g = get_views_gained_24h_for_ids(u, g['ids'])
        gain1_g  = 0
        if has_hourly:
            for aid in g['ids']:
                alb = albums_map.get(aid)
                if not alb:
                    continue
                pv = prev_map_1h.get(aid, alb['views'])
                gain1_g += max(0, alb['views'] - pv)
        cta_summary.append({'keyword': keyword, 'count': g['count'], 'gain1h': gain1_g, 'gain24h': gain24_g})
    cta_summary.sort(key=lambda g: (g['keyword'].startswith('—'), -g['gain24h']))

    rows = []
    for aid, c in entries:
        alb         = albums_map.get(aid)
        is_deleted  = alb is None
        st_e        = cta_st.get(aid)
        cta_found   = (st_e.get('foundCta') if (st_e and st_e.get('ok')) else None) or c.get('lastCta')
        cur_views   = alb['views'] if alb else None
        gain1h_e = gain24h_e = temp_icon = None
        if alb and has_hourly:
            pv = prev_map_1h.get(aid, alb['views'])
            gain1h_e = max(0, alb['views'] - pv)
            peak = get_album_hourly_peak(u, aid)
            above_avg = avg_gain_1h > 0 and gain1h_e >= avg_gain_1h
            if peak is not None and peak >= 5:
                ratio = (gain1h_e / peak) if peak else 0
                if ratio >= 0.6 or above_avg:
                    temp_icon = 'fire'
                elif ratio < 0.3 and not above_avg:
                    temp_icon = 'ice'
        if alb and has_daily:
            pvd = prev_map_day.get(aid, alb['views'])
            gain24h_e = max(0, alb['views'] - pvd)
        days = int((time.time() - iso_ts(c.get('at', ''))) // 86400) if c.get('at') else None
        rows.append({
            'id': aid, 'title': c.get('title', ''), 'href': c.get('href', ''),
            'ctaFound': cta_found, 'isDeleted': is_deleted, 'curViews': cur_views,
            'gain1h': gain1h_e, 'gain24h': gain24h_e, 'tempIcon': temp_icon,
            'ctaOk': st_e.get('ok') if st_e else None, 'ctaChecked': bool(st_e), 'days': days,
        })

    checked_times = [v.get('checkedAt') for v in cta_st.values() if v.get('checkedAt')]
    commented_tab = {
        'rows': rows, 'goneCount': gone_count, 'deletedCount': deleted_count, 'hasHourly': has_hourly,
        'totalGain1h': total_gain_1h, 'gain24hTotal': gain24h_total, 'ctaTimeline': cta_timeline,
        'topHours': top_hours, 'ctaSummary': cta_summary,
        'lastCheckedAt': max(checked_times) if checked_times else None,
    }

    # ---- Deletados ----
    log = deleted.get(u, [])
    deleted_tab = {
        'log': log, 'lost': sum(d.get('views', 0) for d in log),
        'ctaCount': sum(1 for d in log if d.get('hadCta')),
        'lostCta': sum(d.get('views', 0) for d in log if d.get('hadCta')),
    }

    # ---- CTAs ----
    gone_entries = []
    for aid, st_e in cta_st.items():
        if not st_e.get('ok') and aid in comm:
            c = comm[aid]
            gone_entries.append({'id': aid, 'title': c.get('title', ''), 'href': c.get('href', ''),
                                  'checkedAt': st_e.get('checkedAt')})
    ctas_tab = {'ctaList': cta_lst, 'goneEntries': gone_entries, 'hasChecks': bool(cta_st)}

    return {
        'username': u, 'hasData': True, 'state': account_runtime.get(u, {}).get('state', 'ok'),
        'ctaList': cta_lst, 'overview': overview, 'ranking': ranking, 'viral': viral_tab,
        'commented': commented_tab, 'deleted': deleted_tab, 'ctas': ctas_tab,
    }

# ================================================================
# FRONT-END DO /admin — carregado de um arquivo separado (admin.html),
# que precisa estar na MESMA PASTA que este app.py no seu repositório.
# ================================================================
_ADMIN_HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'admin.html')
try:
    with open(_ADMIN_HTML_PATH, 'r', encoding='utf-8') as _f:
        ADMIN_HTML = _f.read()
except FileNotFoundError:
    ADMIN_HTML = '<h1 style="color:#f44;font-family:sans-serif">admin.html não encontrado — coloque-o na mesma pasta do app.py</h1>'

# ================================================================
# ROTAS — API legada (compatível com o userscript Tampermonkey)
# ================================================================
@app.route('/')
def index():
    return jsonify({'status': 'ok', 'accounts': ACCOUNTS, 'ctas': cta_config})

@app.route('/data')
def all_data():
    result = []
    for u in ACCOUNTS:
        d = dict(current_snapshot(u) or {'username': u, 'error': account_runtime.get(u, {}).get('message')})
        d['ctaStatus'] = cta_status.get(u, {})
        d['ctas']      = cta_config.get(u, [])
        result.append(d)
    return jsonify(result)

@app.route('/data/<username>')
def account_data(username):
    if username not in ACCOUNTS:
        return jsonify({'error': 'conta não cadastrada'}), 404
    d = dict(current_snapshot(username) or {})
    d['username']    = username
    d['viewsToday']  = get_views_today(username)
    d['ctaStatus']   = cta_status.get(username, {})
    d['ctas']        = cta_config.get(username, [])
    gone = [v for v in cta_status.get(username, {}).values() if not v.get('ok') and v.get('foundCta')]
    d['ctaAlert']    = len(gone)
    d['checkedAt']   = now_iso()
    return jsonify(d)

@app.route('/setup')
def setup():
    users = [u.strip() for u in request.args.get('accounts', '').split(',') if u.strip()]
    ctas_raw = request.args.get('ctas', '')
    for u in users:
        if u not in ACCOUNTS:
            ACCOUNTS.append(u)
    if ctas_raw:
        for part in ctas_raw.split(','):
            if ':' in part:
                u, tags = part.split(':', 1)
                cta_config[u.strip()] = [t.strip().upper() for t in tags.split('+')]
    save_data()
    threading.Thread(target=refresh_all, daemon=True).start()
    return jsonify({'ok': True, 'accounts': ACCOUNTS, 'ctas': cta_config,
                     'msg': 'Configurado! Aguarde e acesse /data/SuaConta ou /admin'})

@app.route('/accounts', methods=['POST'])
def set_accounts():
    body = request.json or {}
    for u in body.get('accounts', []):
        if u not in ACCOUNTS:
            ACCOUNTS.append(u)
    if 'ctas' in body:
        for u, ctas in body['ctas'].items():
            cta_config[u] = ctas
    save_data()
    threading.Thread(target=refresh_all, daemon=True).start()
    return jsonify({'ok': True, 'accounts': ACCOUNTS})

@app.route('/ctas', methods=['POST'])
def set_ctas():
    body = request.json or {}
    for u, ctas in body.items():
        cta_config[u] = [c.upper() for c in ctas]
    save_data()
    return jsonify({'ok': True, 'ctas': cta_config})

@app.route('/push-cta-status', methods=['POST'])
def push_cta_status():
    """Recebe status de CTA do Tampermonkey (que tem acesso logado) — continua funcionando
    em paralelo ao servidor, caso Rafael queira usar os dois ao mesmo tempo."""
    body     = request.json or {}
    username = (body.get('username') or '').strip()
    status   = body.get('status', {})
    if username and status:
        st   = cta_status.setdefault(username, {})
        comm = commented.setdefault(username, {})
        for aid, v in status.items():
            existing = st.get(aid, {})
            st[aid] = {
                'ok':        v.get('ok', existing.get('ok', True)),
                'foundCta':  v.get('foundCta', existing.get('foundCta')),
                'checkedAt': v.get('checkedAt', existing.get('checkedAt', now_iso())),
            }
            existing_c = comm.get(aid, {})
            comm[aid] = {
                'at':            existing_c.get('at', now_iso()),
                'title':         v.get('title') or existing_c.get('title', ''),
                'href':          v.get('href')  or existing_c.get('href', ''),
                'baselineViews': existing_c.get('baselineViews'),
                'autoDetected':  existing_c.get('autoDetected', False),
                'lastCta':       st[aid]['foundCta'] or existing_c.get('lastCta'),
            }
        save_data()
        print(f'[PUSH] {len(status)} CTAs recebidos de @{username}')
    return jsonify({'ok': True})

@app.route('/sync-commented', methods=['POST'])
def sync_commented():
    body     = request.json or {}
    username = (body.get('username') or '').strip()
    albums   = body.get('albums', [])
    if username and albums:
        comm = commented.setdefault(username, {})
        for a in albums:
            aid = a.get('id')
            if not aid:
                continue
            existing = comm.get(aid, {})
            comm[aid] = {
                'at':            existing.get('at', now_iso()),
                'title':         a.get('title', '') or existing.get('title', ''),
                'href':          a.get('href', '')  or existing.get('href', ''),
                'baselineViews': existing.get('baselineViews'),
                'autoDetected':  existing.get('autoDetected', False),
                'lastCta':       existing.get('lastCta'),
            }
        cta_status[username] = {}
        save_data()
        threading.Thread(target=lambda: check_ctas_for(username), daemon=True).start()
        print(f'[SYNC] {len(albums)} álbuns recebidos de @{username} — status resetado')
    return jsonify({'ok': True, 'synced': len(albums)})

@app.route('/debug-comments')
def debug_comments():
    url = request.args.get('url', '')
    if not url:
        return 'Passe ?url=https://www.erome.com/a/ALBUMID'
    try:
        soup = fetch_album_comments(url)
        results = {}
        for sel in ['span.comment-text', '.comment-text', '.comment-content', '.list-comment', 'div.comment', '.comments', '#comments']:
            els = soup.select(sel)
            if els:
                results[sel] = [e.get_text()[:100] for e in els[:3]]
        return jsonify({'selectors_found': results, 'has_comment_section': bool(soup.select_one('[class*="comment"]'))})
    except Exception as e:
        return str(e)

# ================================================================
# ROTAS — Painel /admin (novo, responsivo, AJAX/JSON)
# ================================================================
@app.route('/admin')
def admin_page():
    return ADMIN_HTML

@app.route('/admin/api/accounts')
def api_accounts():
    out = []
    for u in ACCOUNTS:
        rt = account_runtime.get(u, {})
        state = rt.get('state') or ('ok' if current_snapshot(u) else 'none')
        out.append({'username': u, 'state': state, 'message': rt.get('message', ''), 'progress': rt.get('progress', '')})
    return jsonify(out)

@app.route('/admin/api/dashboard/<username>')
def api_dashboard(username):
    if username not in ACCOUNTS:
        return jsonify({'error': 'conta não cadastrada'}), 404
    return jsonify(build_dashboard_payload(username))

@app.route('/admin/api/add-account', methods=['POST'])
def api_add_account():
    body = request.json or {}
    u = (body.get('username') or '').strip().lstrip('@').replace('/', '')
    if not u:
        return jsonify({'ok': False, 'msg': 'Digite um username'}), 400
    if u.lower() in [a.lower() for a in ACCOUNTS]:
        return jsonify({'ok': False, 'msg': 'Já adicionada'}), 400
    ACCOUNTS.append(u)
    save_data()
    threading.Thread(target=refresh_account, args=(u,), daemon=True).start()
    return jsonify({'ok': True, 'username': u})

@app.route('/admin/api/remove-account', methods=['POST'])
def api_remove_account():
    body = request.json or {}
    u = body.get('username')
    if u in ACCOUNTS:
        ACCOUNTS.remove(u)
        for d in (snapshots, hourly_snaps, deleted, viral_streak, commented, daily_history, cta_hourly_agg, cta_status, cta_config, pending_deleted):
            d.pop(u, None)
        account_runtime.pop(u, None)
        save_data()
    return jsonify({'ok': True})

@app.route('/admin/api/refresh', methods=['POST'])
def api_refresh():
    body = request.json or {}
    u = body.get('username')
    if u:
        threading.Thread(target=refresh_account, args=(u,), daemon=True).start()
    else:
        threading.Thread(target=refresh_all, daemon=True).start()
    return jsonify({'ok': True})

@app.route('/admin/api/mark-commented', methods=['POST'])
def api_mark_commented():
    body = request.json or {}
    u, aid = body.get('username'), body.get('id')
    if not u or not aid:
        return jsonify({'ok': False}), 400
    mark_commented(u, aid, body.get('title', ''), body.get('href', ''), body.get('views'))
    save_data()
    return jsonify({'ok': True})

@app.route('/admin/api/unmark-commented', methods=['POST'])
def api_unmark_commented():
    body = request.json or {}
    u, aid = body.get('username'), body.get('id')
    if u and aid:
        unmark_commented(u, aid)
        save_data()
    return jsonify({'ok': True})

@app.route('/admin/api/add-cta', methods=['POST'])
def api_add_cta():
    body = request.json or {}
    u, cta = body.get('username'), (body.get('cta') or '').strip().upper()
    if not u or not cta:
        return jsonify({'ok': False}), 400
    lst = cta_config.setdefault(u, [])
    if cta not in lst:
        lst.append(cta)
    save_data()
    return jsonify({'ok': True, 'ctas': lst})

@app.route('/admin/api/remove-cta', methods=['POST'])
def api_remove_cta():
    body = request.json or {}
    u, cta = body.get('username'), body.get('cta')
    if u in cta_config and cta in cta_config[u]:
        cta_config[u].remove(cta)
        save_data()
    return jsonify({'ok': True})

@app.route('/admin/api/scan', methods=['POST'])
def api_scan():
    body = request.json or {}
    u = body.get('username')
    if not u:
        return jsonify({'ok': False}), 400
    threading.Thread(target=scan_all_albums, args=(u,), daemon=True).start()
    return jsonify({'ok': True})

@app.route('/admin/api/import', methods=['POST'])
def api_import():
    try:
        if 'file' in request.files:
            raw = request.files['file'].read()
            data = json.loads(raw)
        else:
            data = request.get_json(force=True)
    except Exception as e:
        return jsonify({'ok': False, 'msg': f'Arquivo inválido: {e}'}), 400
    result = import_legacy_export(data)
    return jsonify(result), (200 if result.get('ok') else 400)

@app.route('/admin/api/test-telegram', methods=['POST'])
def api_test_telegram():
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return jsonify({'ok': False, 'msg': 'Configure TELEGRAM_BOT_TOKEN e TELEGRAM_CHAT_ID no Railway primeiro.'}), 400
    send_telegram('✅ <b>Erome Analytics</b>\nNotificações configuradas com sucesso! Você vai receber avisos aqui de CTA sumido, vídeo viral confirmado e deleções com CTA ativo.')
    return jsonify({'ok': True})

@app.route('/admin/api/scan-status')
def api_scan_status():
    return jsonify(scan_status)

@app.route('/admin/api/clear-deleted', methods=['POST'])
def api_clear_deleted():
    body = request.json or {}
    u = body.get('username')
    if u:
        deleted[u] = []
        save_data()
    return jsonify({'ok': True})

# ================================================================
# INICIA O LOOP DE FUNDO — precisa estar fora do "if __name__", porque
# no Railway é o gunicorn que importa este módulo (nunca roda "python app.py"
# diretamente, então esse bloco nunca executaria se estivesse só lá dentro).
# ================================================================
threading.Thread(target=background_loop, daemon=True).start()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)), debug=False)
