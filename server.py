# Erome Analytics Server
# Roda no Railway — scrapa dados públicos do Erome e serve via API

from flask import Flask, jsonify
import requests
from bs4 import BeautifulSoup
import time
import threading
import re
from datetime import datetime, timezone

app = Flask(__name__)

# ── CONFIGURAÇÃO ─────────────────────────────────────────────────
ACCOUNTS   = []   # preenchido via POST /accounts
cache      = {}   # {username: {data, updatedAt}}
cta_config = {}   # {username: [cta1, cta2, ...]}
cta_status = {}   # {username: {albumId: {ok, checkedAt}}}

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

        return {
            'username':    username,
            'avatar':      avatar,
            'totalViews':  total_views,
            'followers':   followers,
            'albumCount':  album_count,
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

    for album in albums[:20]:  # verifica top 20
        href = album.get('href')
        aid  = album.get('id')
        if not href or not aid:
            continue
        try:
            r    = requests.get(href, headers=HEADERS, timeout=10)
            soup = BeautifulSoup(r.text, 'html.parser')
            # Pega texto de todos os comentários
            comm_text = ' '.join(
                el.get_text() for el in soup.select('.comment-text, .list-comment')
            ).lower()
            found = next((c for c in ctas if c.lower() in comm_text), None)
            results[aid] = {
                'ok':        bool(found),
                'foundCta':  found,
                'checkedAt': datetime.now(timezone.utc).isoformat(),
            }
            time.sleep(0.3)
        except:
            pass

    cta_status[username] = results
    return results


def refresh_all():
    """Atualiza cache de todas as contas"""
    for u in ACCOUNTS:
        data = scrape_profile(u)
        cache[u] = data
        print(f'[{datetime.now().strftime("%H:%M")}] Atualizado: {u} — {data.get("totalViews","?")} views')
        time.sleep(1)


def cta_refresh_all():
    """Verifica CTAs (a cada 10 min)"""
    for u in ACCOUNTS:
        check_ctas_for(u)
        print(f'[{datetime.now().strftime("%H:%M")}] CTAs verificados: {u}')
        time.sleep(1)


def background_loop():
    """Loop em background — atualiza dados a cada 15min e CTAs a cada 10min"""
    last_data = 0
    last_cta  = 0
    while True:
        now = time.time()
        if now - last_data >= 15 * 60:
            refresh_all()
            last_data = now
        if now - last_cta >= 10 * 60:
            cta_refresh_all()
            last_cta = now
        time.sleep(30)


# ── ROTAS API ────────────────────────────────────────────────────
@app.route('/')
def index():
    return jsonify({'status': 'ok', 'accounts': ACCOUNTS})


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
    if not d:
        # Busca na hora se não tem cache
        d = scrape_profile(username)
        cache[username] = d
    d['ctaStatus'] = cta_status.get(username, {})
    d['ctas']      = cta_config.get(username, [])
    return jsonify(d)


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


@app.route('/refresh')
def manual_refresh():
    """Força atualização manual"""
    threading.Thread(target=refresh_all, daemon=True).start()
    return jsonify({'ok': True, 'message': 'Atualização iniciada'})


# ── START ─────────────────────────────────────────────────────────
if __name__ == '__main__':
    # Inicia loop em background
    t = threading.Thread(target=background_loop, daemon=True)
    t.start()
    app.run(host='0.0.0.0', port=8080)
