# Erome Analytics Server
# Roda no Railway — scrapa dados públicos do Erome e serve via API

from flask import Flask, jsonify
import requests
from bs4 import BeautifulSoup
import time
import threading
import re
import json
import os
from datetime import datetime, timezone

DATA_FILE = '/tmp/erome_data.json'

def load_data():
    # 1. Tenta arquivo /tmp
    try:
        if os.path.exists(DATA_FILE):
            with open(DATA_FILE, 'r') as f:
                data = json.load(f)
                if data and data.get('accounts'):
                    return data
    except: pass
    # 2. Tenta variavel de ambiente EROME_DATA (configurada no Railway)
    try:
        env_data = os.environ.get('EROME_DATA', '')
        if env_data:
            return json.loads(env_data)
    except: pass
    # 3. Tenta variaveis individuais (mais simples de manter no Railway)
    accounts = []
    cta_cfg  = {}
    try:
        acc_env = os.environ.get('EROME_ACCOUNTS', '')
        if acc_env:
            accounts = [a.strip() for a in acc_env.split(',') if a.strip()]
    except: pass
    try:
        cta_env = os.environ.get('EROME_CTAS', '')
        if cta_env:
            cta_cfg = json.loads(cta_env)
    except: pass
    if accounts:
        return {'accounts': accounts, 'cta_config': cta_cfg}
    return {}

def save_data():
    data = {
        'accounts':         ACCOUNTS,
        'cta_config':       cta_config,
        'cta_status':       cta_status,
        'commented_albums': commented_albums,
        'view_history':     view_history,
    }
    # Salva em /tmp
    try:
        with open(DATA_FILE, 'w') as f:
            json.dump(data, f)
    except Exception as e:
        print(f'[SAVE] Erro arquivo: {e}')
    print(f'[SAVE] {len(ACCOUNTS)} contas | CTAs: {list(cta_config.keys())}')

app = Flask(__name__)

# ── CONFIGURAÇÃO ─────────────────────────────────────────────────
_saved           = load_data()
ACCOUNTS         = _saved.get('accounts', [])
cache            = {}
view_history     = _saved.get('view_history', {})
cta_config       = _saved.get('cta_config', {})
cta_status       = _saved.get('cta_status', {})
commented_albums = _saved.get('commented_albums', {})
scan_status      = {'running': False, 'progress': '', 'started': '', 'finished': ''}
print(f'[INIT] Carregado: {ACCOUNTS} | CTAs: {list(cta_config.keys())}')

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept': 'text/html,application/xhtml+xml',
    'Accept-Language': 'pt-BR,pt;q=0.9',
}

# ── SCRAPING ─────────────────────────────────────────────────────
def parse_num(raw):
    if not raw:
        return 0
    raw = str(raw).strip().replace(' ', '')
    m = re.match(r'^([\d.,]+)([KMB]?)$', raw, re.I)
    if not m:
        return 0
    n = m.group(1).replace(',', '.')
    v = float(n)
    s = m.group(2).upper()
    if s == 'K': return int(v * 1_000)
    if s == 'M': return int(v * 1_000_000)
    if s == 'B': return int(v * 1_000_000_000)
    return int(v)

def scrape_profile(username):
    try:
        url = f'https://www.erome.com/{username}?t=posts'
        r   = requests.get(url, headers=HEADERS, timeout=15)
        # Se 429, backoff exponencial: 1min, 2min, 4min (max 3 tentativas)
        retries = 0
        while r.status_code == 429 and retries < 3:
            wait = 60 * (2 ** retries)
            print(f'[429] Tentativa {retries+1}/3 — aguardando {wait}s...')
            time.sleep(wait)
            r = requests.get(url, headers=HEADERS, timeout=15)
            retries += 1
        if r.status_code == 429:
            print(f'[429] Erome bloqueando — usando cache existente')
            return cache.get(username) or {'error': '429', 'username': username, 'fetchedAt': datetime.now(timezone.utc).isoformat()}
        r.raise_for_status()
        soup = BeautifulSoup(r.text, 'html.parser')

        # Stats do perfil
        total_views = 0
        followers   = 0
        album_count = 0

        ui = soup.select_one('.user-info')
        if ui:
            for span in ui.select(':scope > span'):
                txt = span.get_text()
                # Pega o texto do primeiro text node
                nodes = [c for c in span.children if isinstance(c, str) and c.strip()]
                raw   = nodes[0].strip() if nodes else txt.split()[0]
                num   = parse_num(raw)
                if 'ALBUM' in txt.upper():  album_count  = num
                if 'VIEW'  in txt.upper():  total_views  = num
                if 'FOLLOW' in txt.upper(): followers    = num

        # Avatar
        avatar = None
        av_el  = soup.select_one('img.avatar, .avatar img')
        if av_el:
            avatar = av_el.get('src')

        # Álbuns da página 1
        albums = []
        for el in soup.select('div.album[id^="album-"]'):
            # Ignora reposts
            if el.select_one('.album-user'):
                continue
            link  = el.select_one('a.album-link')
            href  = link['href'] if link else None
            alt   = ''
            img   = el.select_one('img.album-thumbnail')
            if img:
                alt = img.get('alt', '')
            title = re.sub(r'#\S+$', '', alt).strip() or 'Sem título'
            vraw  = ''
            vspan = el.select_one('span.album-bottom-views')
            if vspan:
                vraw = re.sub(r'[^\d.,KMBkmb]', '', vspan.get_text())
            views = parse_num(vraw)
            aid   = el.get('id', '').replace('album-', '')
            if href:
                m2 = re.search(r'/a/([a-zA-Z0-9]+)', href)
                if m2:
                    aid = m2.group(1)
            if aid:
                albums.append({'id': aid, 'title': title, 'href': href, 'views': views})

        # Page 1 done — now paginate to get ALL albums
        page = 2
        while True:
            try:
                r2   = requests.get(f'https://www.erome.com/{username}?t=posts&page={page}', headers=HEADERS, timeout=10)
                s2   = BeautifulSoup(r2.text, 'html.parser')
                more = []
                for el in s2.select('div.album[id^="album-"]'):
                    if el.select_one('.album-user'):
                        continue
                    link  = el.select_one('a.album-link')
                    href2 = link['href'] if link else None
                    alt2  = ''
                    img2  = el.select_one('img.album-thumbnail')
                    if img2: alt2 = img2.get('alt', '')
                    title2 = re.sub(r'#\S+$', '', alt2).strip() or 'Sem título'
                    vraw2  = ''
                    vspan2 = el.select_one('span.album-bottom-views')
                    if vspan2: vraw2 = re.sub(r'[^\d.,KMBkmb]', '', vspan2.get_text())
                    views2 = parse_num(vraw2)
                    aid2   = el.get('id', '').replace('album-', '')
                    if href2:
                        m3 = re.search(r'/a/([a-zA-Z0-9]+)', href2)
                        if m3: aid2 = m3.group(1)
                    if aid2:
                        albums.append({'id': aid2, 'title': title2, 'href': href2, 'views': views2})
                        more.append(aid2)
                # Check if there's a next page
                has_next = bool(s2.select_one('a[rel="next"]') or s2.select('.pagination .page-item:not(.active) a[href*="page="]'))
                if not has_next or not more:
                    break
                page += 1
                time.sleep(0.5)
            except:
                break

        return {
            'username':    username,
            'avatar':      avatar,
            'totalViews':  total_views,
            'followers':   followers,
            'albumCount':  len(albums),
            'albums':      albums,
            'fetchedAt':   datetime.now(timezone.utc).isoformat(),
            'error':       None,
        }

    except Exception as ex:
        return {'username': username, 'error': str(ex), 'fetchedAt': datetime.now(timezone.utc).isoformat()}


def check_ctas_for(username):
    """Verifica CTAs nos comentários dos álbuns comentados (como visitante deslogado)"""
    ctas    = cta_config.get(username, [])
    albums  = (cache.get(username) or {}).get('albums', [])
    results = {}

    if not ctas or not albums:
        return results

    for album in albums:  # verifica TODOS
        href = album.get('href')
        aid  = album.get('id')
        if not href or not aid:
            continue
        try:
            r    = requests.get(href, headers=HEADERS, timeout=10)
            soup = BeautifulSoup(r.text, 'html.parser')
            # Múltiplos seletores — igual ao Tampermonkey
            # Baseado no HTML do Erome: div.comment > div.comment-content > span.comment-text
            selectors = [
                'span.comment-text',
                '.comment-text',
                '.comment-content',
                '.list-comment',
                'div.comment',
                '#comments',
            ]
            parts = []
            for sel in selectors:
                parts += [el.get_text() for el in soup.select(sel)]
            # Fallback: texto completo da seção de comentários
            comm_div = soup.select_one('.comments, #comments, [class*="comment"]')
            if comm_div:
                parts.append(comm_div.get_text())
            comm_text = ' '.join(parts).lower()
            found = next((ct for ct in ctas if ct.lower() in comm_text), None)
            prev  = results.get(aid, {})
            results[aid] = {
                'ok':        bool(found),
                'foundCta':  found,
                'checkedAt': datetime.now(timezone.utc).isoformat(),
            }
            # Só registra "sumiu" se antes estava OK
            if prev.get('ok') and not bool(found):
                print(f'[CTA] SUMIU em {aid} (@{username})')
            time.sleep(0.6)
        except Exception as ex:
            print(f'[CTA] Erro album {aid}: {ex}')

    cta_status[username] = results
    return results


def refresh_all():
    """Atualiza cache de todas as contas"""
    for u in ACCOUNTS:
        data = scrape_profile(u)
        if data and not data.get('error'):
            cache[u] = data
        elif not cache.get(u):
            cache[u] = data  # guarda mesmo com erro se nao tem cache
        # Salva histórico de views para calcular views de hoje
        if data and not data.get('error') and data.get('totalViews'):
            if u not in view_history:
                view_history[u] = []
            view_history[u].append({'ts': time.time(), 'views': data['totalViews']})
            # Mantém só 48h de histórico
            cutoff = time.time() - 172800
            view_history[u] = [x for x in view_history[u] if x['ts'] > cutoff]
            # Persiste histórico a cada update
            save_data()
        print(f'[{datetime.now().strftime("%H:%M")}] Atualizado: {u} — {data.get("totalViews","?")} views')
        time.sleep(1)


def cta_refresh_all():
    """Verifica CTAs de todas as contas (a cada 10 min)"""
    for u in ACCOUNTS:
        try:
            check_ctas_for(u)
            print(f'[{datetime.now().strftime("%H:%M")}] CTAs ok: @{u} — {len(cta_status.get(u,{}))} albuns')
        except Exception as ex:
            print(f'[CTA] Erro em @{u}: {ex}')
        time.sleep(1)


def background_loop():
    """Loop em background — atualiza dados a cada 15min e CTAs a cada 10min"""
    last_data = 0
    last_cta  = 0
    while True:
        now = time.time()
        if now - last_data >= 20 * 60:  # 20min para evitar bloqueio
            refresh_all()
            last_data = now
        if now - last_cta >= 10 * 60:
            cta_refresh_all()
            last_cta = now
        time.sleep(30)


# ── ROTAS API ────────────────────────────────────────────────────
@app.route('/')
def index():
    return jsonify({'status': 'ok', 'accounts': ACCOUNTS, 'ctas': cta_config})

@app.route('/scan-page')
def scan_page():
    """Redireciona para admin - scan feito pelo Tampermonkey"""
    from flask import redirect
    if not scan_status.get('running'):
        # Inicia o scan
        scan_status['running']  = True
        scan_status['started']  = datetime.now(timezone.utc).isoformat()
        scan_status['progress'] = 'Iniciando...'
        scan_status['pct']      = 0
        scan_status['finished'] = ''

        def do_scan():
            for u in ACCOUNTS:
                try:
                    # Busca TODOS os albuns com paginacao completa
                    scan_status['progress'] = f'Buscando todos os albuns de @{u}...'
                    scan_status['pct']      = 2
                    data = scrape_profile(u)  # ja tem paginacao completa
                    if data and not data.get('error'):
                        cache[u] = data
                        print(f'[SCAN] {len(data.get("albums",[]))} albuns encontrados para @{u}')
                    albs  = (cache.get(u) or {}).get('albums', [])
                    total = len(albs)
                    scan_status['progress'] = f'Verificando CTAs em {total} albuns de @{u}...'
                    scan_status['progress'] = f'Verificando {total} albuns de @{u}...'

                    ctas = cta_config.get(u, [])
                    results = dict(cta_status.get(u, {}))
                    found_count = 0

                    for idx, album in enumerate(albs):
                        href = album.get('href')
                        aid  = album.get('id')
                        if not href or not aid:
                            continue
                        pct = 5 + int((idx / total) * 90)
                        scan_status['pct']      = pct
                        scan_status['progress'] = f'Album {idx+1}/{total} — {found_count} CTAs encontrados'
                        try:
                            r    = requests.get(href, headers=HEADERS, timeout=10)
                            soup = BeautifulSoup(r.text, 'html.parser')
                            parts = []
                            for sel in ['span.comment-text','.comment-text','.comment-content','.list-comment','div.comment']:
                                parts += [el.get_text() for el in soup.select(sel)]
                            comm_div = soup.select_one('.comments, #comments, [class*="comment"]')
                            if comm_div:
                                parts.append(comm_div.get_text())
                            txt   = ' '.join(parts).lower()
                            found = next((ct for ct in ctas if ct.lower() in txt), None)
                            prev  = results.get(aid, {})
                            if found:
                                found_count += 1
                                results[aid] = {'ok': True, 'foundCta': found, 'checkedAt': datetime.now(timezone.utc).isoformat()}
                            elif prev.get('ok'):
                                results[aid] = {'ok': False, 'foundCta': None, 'checkedAt': datetime.now(timezone.utc).isoformat()}
                                print(f'[SCAN] CTA SUMIU: {album.get("title",aid)}')
                            # Nao registra albuns sem historico e sem CTA
                            time.sleep(0.6)
                        except Exception as ex:
                            print(f'[SCAN] Erro {aid}: {ex}')

                    cta_status[u] = results
                    save_data()  # persiste resultado
                    scan_status['progress'] = f'Concluido! {found_count} CTAs encontrados em {total} albuns de @{u}'
                    scan_status['pct']      = 100
                except Exception as ex:
                    scan_status['progress'] = f'Erro: {ex}'
                    print(f'[SCAN] Erro geral: {ex}')

            scan_status['running']  = False
            scan_status['finished'] = datetime.now(timezone.utc).isoformat()

        threading.Thread(target=do_scan, daemon=True).start()

    return redirect('/admin')

@app.route('/scan')
def manual_scan():
    """Força scan imediato de CTAs em todos os álbuns"""
    from flask import redirect, request
    scan_status['running'] = True
    scan_status['started'] = datetime.now(timezone.utc).isoformat()
    scan_status['progress'] = 'Iniciando...'

    def do_scan():
        for u in ACCOUNTS:
            try:
                # Atualiza albuns primeiro
                scan_status['progress'] = f'Buscando albuns de @{u}...'
                data = scrape_profile(u)
                if data and not data.get('error'):
                    cache[u] = data
                    scan_status['progress'] = f'Verificando CTAs em {len(data.get("albums",[]))} albuns...'
                # Verifica CTAs
                check_ctas_for(u)
                scan_status['progress'] = f'Concluido @{u}: {len(cta_status.get(u,{}))} albuns verificados'
            except Exception as ex:
                scan_status['progress'] = f'Erro: {ex}'
                print(f'[SCAN] Erro: {ex}')
        scan_status['running'] = False
        scan_status['finished'] = datetime.now(timezone.utc).isoformat()

    threading.Thread(target=do_scan, daemon=True).start()
    return redirect('/admin?scan=1')

@app.route('/debug-comments')
def debug_comments():
    """Debug: mostra texto de comentarios de um album"""
    from flask import request
    url = request.args.get('url', '')
    if not url:
        return 'Passe ?url=https://www.erome.com/a/ALBUMID'
    try:
        r    = requests.get(url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(r.text, 'html.parser')
        # Tenta varios seletores
        results = {}
        for sel in ['span.comment-text', '.comment-text', '.comment-content',
                    '.list-comment', 'div.comment', '.comments', '#comments']:
            els = soup.select(sel)
            if els:
                results[sel] = [e.get_text()[:100] for e in els[:3]]
        # Verifica se tem secao de comentarios
        has_comments = bool(soup.select_one('[class*="comment"]'))
        # Texto completo da pagina (primeiros 500 chars das comments)
        page_text = soup.get_text()
        comment_idx = page_text.lower().find('comment')
        snippet = page_text[max(0,comment_idx-50):comment_idx+200] if comment_idx > 0 else 'nao encontrado'
        return __import__('flask').jsonify({
            'selectors_found': results,
            'has_comment_section': has_comments,
            'page_snippet': snippet,
            'status': r.status_code,
        })
    except Exception as e:
        return str(e)

@app.route('/setup')
def setup():
    """Configura contas e CTAs via GET — acesse no navegador"""
    from flask import request
    users = request.args.get('accounts', '').split(',')
    users = [u.strip() for u in users if u.strip()]
    ctas_raw = request.args.get('ctas', '')

    for u in users:
        if u not in ACCOUNTS:
            ACCOUNTS.append(u)

    # CTAs no formato: conta1:CTA1+CTA2,conta2:CTA3
    if ctas_raw:
        for part in ctas_raw.split(','):
            if ':' in part:
                u, tags = part.split(':', 1)
                cta_config[u.strip()] = [t.strip().upper() for t in tags.split('+')]

    # Força atualização
    threading.Thread(target=refresh_all, daemon=True).start()
    threading.Thread(target=cta_refresh_all, daemon=True).start()

    return jsonify({
        'ok': True,
        'accounts': ACCOUNTS,
        'ctas': cta_config,
        'msg': 'Configurado! Aguarde 30 segundos e acesse /data/SuaConta'
    })


@app.route('/data')
def all_data():
    """Retorna dados de todas as contas"""
    result = []
    for u in ACCOUNTS:
        d = cache.get(u, {})
        # Adiciona status de CTAs
        d['ctaStatus'] = cta_status.get(u, {})
        d['ctas']      = cta_config.get(u, [])
        result.append(d)
    return jsonify(result)


@app.route('/data/<username>')
def account_data(username):
    """Retorna dados de uma conta específica"""
    d = cache.get(username)
    if not d or d.get('error'):
        fresh = scrape_profile(username)
        if fresh and not fresh.get('error'):
            cache[username] = fresh
            d = fresh
        elif not d:
            d = fresh  # usa mesmo com erro se nao tem cache

    # Calcula views de hoje (diferença entre cache atual e cache de 24h atrás)
    hist = view_history.get(username, [])
    views_today = None
    if hist and d and not d.get('error'):
        now_ts = time.time()

        # Tenta pegar snapshot de 24h atrás
        best   = None
        best_d = float('inf')
        for entry in hist:
            diff = abs(now_ts - entry['ts'] - 86400)
            if diff < best_d:
                best_d = diff
                best   = entry

        if best and best_d < 7200:
            # Tem dados de 24h atrás — calcula normalmente
            views_today = max(0, d['totalViews'] - best['views'])
        elif len(hist) >= 2:
            # Ainda não tem 24h — usa o snapshot mais antigo disponível (precisa ter pelo menos 2 pontos)
            oldest = min(hist, key=lambda x: x['ts'])
            age_h  = (now_ts - oldest['ts']) / 3600
            if age_h >= 0.25 and oldest['views'] != d['totalViews']:
                views_today = max(0, d['totalViews'] - oldest['views'])
        # Se só tem 1 ponto de histórico (acabou de reiniciar), deixa None pra mostrar "coletando dados"

    # Calcula alerta de CTA
    st         = cta_status.get(username, {})
    gone       = [v for v in st.values() if not v.get('ok') and v.get('foundCta') not in (None,'None','--')]
    cta_alert  = len(gone)  # quantos CTAs sumiram

    result = dict(d)
    result['viewsToday'] = views_today
    result['ctaStatus']  = st
    result['ctas']       = cta_config.get(username, [])
    result['ctaAlert']   = cta_alert
    # Sempre mostra horário atual como "checkedAt" para o widget saber que está vivo
    result['checkedAt']  = datetime.now(timezone.utc).isoformat()
    return jsonify(result)


@app.route('/accounts', methods=['POST'])
def set_accounts():
    """Define quais contas monitorar"""
    from flask import request
    body = request.json or {}
    users = body.get('accounts', [])
    for u in users:
        if u not in ACCOUNTS:
            ACCOUNTS.append(u)
    # Atualiza CTAs se fornecidos
    if 'ctas' in body:
        for u, ctas in body['ctas'].items():
            cta_config[u] = ctas
    # Força atualização imediata
    threading.Thread(target=refresh_all, daemon=True).start()
    return jsonify({'ok': True, 'accounts': ACCOUNTS})


@app.route('/ctas', methods=['POST'])
def set_ctas():
    """Define CTAs por conta"""
    from flask import request
    body = request.json or {}
    for u, ctas in body.items():
        cta_config[u] = [c.upper() for c in ctas]
    return jsonify({'ok': True, 'ctas': cta_config})


@app.route('/push-cta-status', methods=['POST'])
def push_cta_status():
    """Recebe status dos CTAs do Tampermonkey (que ja tem acesso logado)"""
    from flask import request
    body     = request.json or {}
    username = body.get('username','').strip()
    status   = body.get('status', {})  # {albumId: {ok, foundCta, title, href}}
    if username and status:
        if username not in cta_status:
            cta_status[username] = {}
        # Mantém titulo e href no status
        for aid, v in status.items():
            existing = cta_status[username].get(aid, {})
            cta_status[username][aid] = {
                'ok':        v.get('ok', existing.get('ok', True)),
                'foundCta':  v.get('foundCta', existing.get('foundCta')),
                'checkedAt': v.get('checkedAt', existing.get('checkedAt', '')),
                'title':     v.get('title') or existing.get('title',''),
                'href':      v.get('href')  or existing.get('href',''),
            }
        # Atualiza lista de comentados tambem
        if username not in commented_albums:
            commented_albums[username] = []
        existing_ids = {a['id'] for a in commented_albums[username]}
        for aid, v in status.items():
            if aid not in existing_ids:
                commented_albums[username].append({
                    'id': aid,
                    'title': v.get('title',''),
                    'href':  v.get('href',''),
                })
        save_data()
        print(f'[PUSH] Status de {len(status)} CTAs recebido de @{username}')
    return __import__('flask').jsonify({'ok': True})

@app.route('/sync-commented', methods=['POST'])
def sync_commented():
    """Recebe lista de álbuns comentados do script Tampermonkey"""
    from flask import request
    body     = request.json or {}
    username = body.get('username', '').strip()
    albums   = body.get('albums', [])
    if username and albums:
        commented_albums[username] = albums
        # Limpa status antigo para evitar falsos positivos
        cta_status[username] = {}
        save_data()
        print(f'[SYNC] {len(albums)} albuns recebidos de @{username} — status resetado')
        threading.Thread(target=cta_refresh_all, daemon=True).start()
    return __import__('flask').jsonify({'ok': True, 'synced': len(albums)})

@app.route('/refresh')
def manual_refresh():
    """Força atualização manual"""
    from flask import request, redirect
    threading.Thread(target=refresh_all, daemon=True).start()
    # Se veio do admin, volta pro admin
    ref = request.referrer or ''
    if 'admin' in ref:
        return redirect('/admin')
    return jsonify({'ok': True, 'message': 'Atualização iniciada'})



@app.route('/admin')
def admin():
    # Coleta status de CTAs por album
    gone_albums = []
    ok_albums   = []
    for u in ACCOUNTS:
        for aid, v in cta_status.get(u, {}).items():
            # Tenta pegar titulo do cache, depois do status, depois usa o ID
            alb_title = next((a.get('title','') for a in (cache.get(u) or {}).get('albums',[]) if a.get('id')==aid), '')
            alb_href  = next((a.get('href','')  for a in (cache.get(u) or {}).get('albums',[]) if a.get('id')==aid), '')
            # Fallback: usa dados que vieram do Tampermonkey
            if not alb_title: alb_title = v.get('title','')
            if not alb_href:  alb_href  = v.get('href','')
            # Último fallback: monta href pelo ID
            if not alb_href and aid: alb_href = f'https://www.erome.com/a/{aid}'
            if not alb_title: alb_title = f'Álbum {aid}'
            entry = {'user':u,'aid':aid,'title':alb_title,'href':alb_href,
                     'ok':v.get('ok'),'foundCta':v.get('foundCta','--'),'checkedAt':str(v.get('checkedAt',''))[:16]}
            # Só mostra SUMIU se foundCta era válido antes (não None/None)
            if not v.get('ok') and v.get('foundCta') not in (None, 'None', '--', 'null'):
                gone_albums.append(entry)
            elif v.get('ok'):
                ok_albums.append(entry)

    def alb_row(e, gone=False):
        col  = '#ff4466' if gone else '#59E38A'
        icon = 'SUMIU' if gone else 'OK'
        t    = e['title'][:55]+'...' if len(e['title'])>55 else e['title']
        link = ('<a href="' + e['href'] + '" target="_blank" style="color:' + col + ';text-decoration:none">' + t + '</a>') if e['href'] else t
        return ('<div style="padding:12px 0;border-bottom:1px solid #111">'
            + '<div style="display:flex;align-items:center;gap:8px;margin-bottom:4px">'
            + '<span style="background:' + ('#2e0008' if gone else '#0d2e1a') + ';color:' + col + ';font-size:10px;font-weight:700;padding:2px 8px;border-radius:10px">' + icon + '</span>'
            + link
            + '</div>'
            + '<div style="font-size:11px;color:#555">@' + e['user'] + ' - CTA: ' + str(e['foundCta']) + ' - ' + e['checkedAt'] + '</div>'
            + '</div>')

    gone_html = ''.join(alb_row(e,True) for e in gone_albums) or '<div style="color:#444;font-size:13px;padding:12px 0">Nenhum CTA removido detectado.</div>'
    ok_html   = ''.join(alb_row(e,False) for e in ok_albums[:10]) or '<div style="color:#444;font-size:13px;padding:12px 0">Aguardando verificacao...</div>'

    # Tags de CTA
    tags_html = ''
    for u in ACCOUNTS:
        ctas = cta_config.get(u, [])
        tags = ''.join(
            '<span style="display:inline-flex;align-items:center;gap:6px;background:#0a0a0a;border:1px solid #ff224430;border-radius:20px;padding:5px 14px;margin:3px">'
            + '<span style="font-size:13px;color:#fff;font-family:monospace">' + cta + '</span>'
            + '<a href="/admin/remove-cta?account=' + u + '&cta=' + cta + '" style="color:#ff224466;text-decoration:none;font-size:15px;line-height:1;margin-left:4px">x</a>'
            + '</span>'
            for cta in ctas
        )
        tags_html += '<div style="margin-bottom:14px"><div style="font-size:11px;color:#ff224488;text-transform:uppercase;letter-spacing:.08em;font-weight:600;margin-bottom:8px">@' + u + '</div><div>' + (tags if tags else '<span style="color:#333;font-size:12px">Nenhum CTA cadastrado</span>') + '</div></div>'

    alert = ''
    if gone_albums:
        alert = '<div style="background:#1a0008;border:1px solid #ff2244;border-radius:10px;padding:12px 16px;margin-bottom:20px;font-size:13px;color:#ff8899">Atencao: ' + str(len(gone_albums)) + ' album(ns) com CTA removido!</div>'

    accs_html = ''
    for u in ACCOUNTS:
        accs_html += ('<div style="display:flex;align-items:center;justify-content:space-between;padding:10px 0;border-bottom:1px solid #111">'
            + '<span style="color:#ff2244;font-weight:600;font-size:14px">@' + u + '</span>'
            + '<a href="/remove?account=' + u + '" style="color:#ff224455;text-decoration:none;font-size:12px">Remover conta</a>'
            + '</div>')

    # Build admin page
    no_account = not ACCOUNTS
    main_account = ACCOUNTS[0] if ACCOUNTS else ''

    # Formulário de adicionar conta — sempre visível, mesmo com contas existentes,
    # pra dar pra cadastrar várias contas sem precisar remover a atual primeiro.
    acc_form = """
        <form action="/admin/add" method="POST">
            <div class="row">
                <input name="account" placeholder="Username (ex: ModoNoturnoBR)" required>
                <button type="submit" class="btn">+ Adicionar conta</button>
            </div>
            <div class="hint">Digite apenas o username sem @</div>
        </form>"""

    if no_account:
        acc_section = '<div style="color:#444;font-size:13px;margin-bottom:14px">Nenhuma conta cadastrada ainda.</div>' + acc_form
    else:
        acc_section = accs_html + '<div style="margin-top:14px">' + acc_form + '</div>'

    if no_account:
        cta_section = '<div style="color:#444;font-size:13px">Adicione uma conta primeiro.</div>'
    else:
        # Seletor de conta no formulário de CTA — antes ficava fixo na primeira
        # conta da lista, então contas adicionadas depois nunca conseguiam CTA.
        acc_options = ''.join('<option value="' + u + '">@' + u + '</option>' for u in ACCOUNTS)
        cta_form = (
            '<form action="/admin/add-cta" method="POST" style="margin-top:12px">' +
            '<div class="row">' +
            '<select name="account" style="background:#0a0a0a;border:1px solid #ff224430;border-radius:8px;color:#fff;padding:9px 12px;font-size:13px;outline:none">' + acc_options + '</select>' +
            '<input name="cta" placeholder="Palavra-chave (ex: NEZBRASIL)" required style="text-transform:uppercase">' +
            '<button type="submit" class="btn">+ Adicionar CTA</button>' +
            '</div>' +
            '<div class="hint">Escolha a conta no menu, adicione um CTA por vez. Clique no x para remover. Use quando mudar seu CTA no Telegram.</div>' +
            '</form>'
        )
        cta_section = tags_html + cta_form

    # Progress bar
    if scan_status.get('running'):
        prog_pct = scan_status.get('pct', 0)
        prog_txt = scan_status.get('progress', 'Escaneando...')
        progress_bar = f'''<div style="background:#0d2e1a;border:1px solid #22cc5540;border-radius:10px;padding:12px 16px;margin-bottom:16px">
            <div style="display:flex;justify-content:space-between;margin-bottom:8px">
                <span style="font-size:12px;color:#22cc55;font-weight:600">⏳ {prog_txt}</span>
                <span style="font-size:12px;color:#22cc5588">{prog_pct}%</span>
            </div>
            <div style="background:#0a1a10;border-radius:6px;height:6px;overflow:hidden">
                <div style="background:#22cc55;height:100%;width:{prog_pct}%;transition:width .3s;border-radius:6px"></div>
            </div>
            <div style="font-size:10px;color:#22cc5566;margin-top:6px">Atualiza automaticamente...</div>
            <meta http-equiv="refresh" content="3">
        </div>'''
    elif scan_status.get('finished') and scan_status.get('progress'):
        progress_bar = f'''<div style="background:#0d2e1a;border:1px solid #22cc5540;border-radius:10px;padding:12px 16px;margin-bottom:16px">
            <span style="font-size:12px;color:#22cc55">✅ {scan_status["progress"]}</span>
        </div>'''
    else:
        progress_bar = ''

    html = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Erome Analytics</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,sans-serif;background:#0a0a0a;color:#f0f0f0;padding:20px;max-width:660px;margin:0 auto}
h1{color:#fff;font-size:18px;font-weight:700;margin-bottom:16px}
h1 span{color:#ff2244}
.sec{margin-bottom:22px}
.sec-title{font-size:10px;font-weight:600;color:#ff224466;text-transform:uppercase;letter-spacing:.1em;margin-bottom:10px}
.card{background:#111;border:1px solid #ff224418;border-radius:12px;padding:16px}
.row{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:6px}
input{background:#0a0a0a;border:1px solid #ff224430;border-radius:8px;color:#fff;padding:9px 12px;font-size:13px;flex:1;min-width:120px;outline:none}
input:focus{border-color:#ff2244}
input::placeholder{color:#2a2a2a}
.btn{background:linear-gradient(135deg,#ff2244,#880020);border:none;border-radius:8px;color:#fff;padding:9px 18px;cursor:pointer;font-size:13px;font-weight:700;white-space:nowrap}
.btn:hover{filter:brightness(1.1)}
.btn-sm{background:#111;border:1px solid #ff224430;border-radius:8px;color:#ff2244;padding:6px 14px;cursor:pointer;font-size:12px;text-decoration:none;display:inline-block}
.hint{font-size:11px;color:#333;margin-top:8px;line-height:1.5}
.topbar{display:flex;gap:8px;margin-bottom:20px;flex-wrap:wrap}
</style>
</head>
<body>
<h1>Erome <span>Analytics</span></h1>
<div class="topbar">
<a href="/admin" class="btn-sm">Atualizar pagina</a>
<a href="/refresh" class="btn-sm">Forcar refresh dados</a>
<a href="/scan-page" class="btn-sm" style="background:#0d2e1a;border-color:#22cc5540;color:#22cc55">🔍 Escanear CTAs agora</a>
</div>
""" + progress_bar + """

""" + alert + """
<div class="sec">
<div class="sec-title">Minha conta</div>
<div class="card">""" + acc_section + """</div>
</div>

<div class="sec">
<div class="sec-title">Meus CTAs</div>
<div class="card">""" + cta_section + """</div>
</div>

<div class="sec">
<div class="sec-title">Albums com CTA removido</div>
<div class="card">""" + gone_html + """</div>
</div>

<div class="sec">
<div class="sec-title">Albums com CTA presente (ultimos 10)</div>
<div class="card">""" + ok_html + """</div>
</div>

<div style="font-size:10px;color:#1e1e1e;margin-top:16px;text-align:center">
Dados: 15min - CTAs: 10min - <a href="/" style="color:#222">API JSON</a>
</div>
</body>
</html>"""
    return html


@app.route('/admin/add', methods=['POST'])
def admin_add():
    from flask import request, redirect
    u    = request.form.get('account','').strip()
    craw = request.form.get('ctas','').strip()
    if u and u not in ACCOUNTS:
        ACCOUNTS.append(u)
    if craw and u:
        cta_config[u] = [x.strip().upper() for x in craw.split(',') if x.strip()]
    threading.Thread(target=refresh_all, daemon=True).start()
    return redirect('/admin')

@app.route('/admin/add-cta', methods=['POST'])
def admin_add_cta():
    from flask import request, redirect
    u   = request.form.get('account','').strip()
    cta = request.form.get('cta','').strip().upper()
    if u and cta:
        if u not in cta_config: cta_config[u] = []
        if cta not in cta_config[u]: cta_config[u].append(cta)
    save_data()
    threading.Thread(target=cta_refresh_all, daemon=True).start()
    return redirect('/admin')

@app.route('/admin/remove-cta')
def admin_remove_cta():
    from flask import request, redirect
    u   = request.args.get('account','').strip()
    cta = request.args.get('cta','').strip()
    if u in cta_config and cta in cta_config[u]:
        cta_config[u].remove(cta)
    save_data()
    return redirect('/admin')


# ── INICIA O LOOP DE FUNDO ───────────────────────────────────────
# Precisa estar aqui fora (e não dentro de um "if __name__ == '__main__'"),
# porque no Railway é o gunicorn que importa este módulo — ele nunca
# executa "python app.py" diretamente, então esse bloco nunca rodaria.
# É só essa thread que faltava: sem ela, o cache só era preenchido uma
# vez (no primeiro request via scrape on-demand) e nunca mais atualizado.
threading.Thread(target=background_loop, daemon=True).start()
