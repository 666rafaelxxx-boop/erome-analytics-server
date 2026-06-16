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
cache        = {}   # {username: {data, updatedAt}}
view_history = {}   # {username: [{ts, views}, ...]} para calcular views de hoje
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
        # Salva histórico de views para calcular views de hoje
        if data and not data.get('error') and data.get('totalViews'):
            if u not in view_history:
                view_history[u] = []
            view_history[u].append({'ts': time.time(), 'views': data['totalViews']})
            # Mantém só 48h de histórico
            cutoff = time.time() - 172800
            view_history[u] = [x for x in view_history[u] if x['ts'] > cutoff]
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
    return jsonify({'status': 'ok', 'accounts': ACCOUNTS, 'ctas': cta_config})

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
    if not d:
        d = scrape_profile(username)
        cache[username] = d

    # Calcula views de hoje (diferença entre cache atual e cache de 24h atrás)
    hist = view_history.get(username, [])
    views_today = None
    if hist and d and not d.get('error'):
        # Pega entrada mais próxima de 24h atrás
        now_ts = time.time()
        best   = None
        best_d = float('inf')
        for entry in hist:
            diff = abs(now_ts - entry['ts'] - 86400)
            if diff < best_d:
                best_d = diff
                best   = entry
        if best and best_d < 7200:  # dentro de 2h do alvo
            views_today = max(0, d['totalViews'] - best['views'])

    result = dict(d)
    result['viewsToday'] = views_today
    result['ctaStatus']  = cta_status.get(username, {})
    result['ctas']       = cta_config.get(username, [])
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


@app.route('/refresh')
def manual_refresh():
    """Força atualização manual"""
    threading.Thread(target=refresh_all, daemon=True).start()
    return jsonify({'ok': True, 'message': 'Atualização iniciada'})



@app.route('/admin')
def admin():
    # Coleta status de CTAs por album
    gone_albums = []
    ok_albums   = []
    for u in ACCOUNTS:
        for aid, v in cta_status.get(u, {}).items():
            alb_title = next((a.get('title','') for a in (cache.get(u) or {}).get('albums',[]) if a.get('id')==aid), aid)
            alb_href  = next((a.get('href','')  for a in (cache.get(u) or {}).get('albums',[]) if a.get('id')==aid), '')
            entry = {'user':u,'aid':aid,'title':alb_title,'href':alb_href,
                     'ok':v.get('ok'),'foundCta':v.get('foundCta','--'),'checkedAt':str(v.get('checkedAt',''))[:16]}
            if not v.get('ok'): gone_albums.append(entry)
            else: ok_albums.append(entry)

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
input[readonly]{color:#555}
.btn{background:linear-gradient(135deg,#ff2244,#880020);border:none;border-radius:8px;color:#fff;padding:9px 18px;cursor:pointer;font-size:13px;font-weight:700;white-space:nowrap}
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
</div>
""" + alert + """
<div class="sec">
<div class="sec-title">Minha conta</div>
<div class="card">
""" + (accs_html if accs_html else '<div style="color:#444;font-size:13px">Nenhuma conta ainda</div>') + """
<form action="/admin/add" method="POST" style="margin-top:12px">
<div class="row">
<input name="account" placeholder="Username (ex: ModoNoturnoBR)" required>
<button type="submit" class="btn">+ Adicionar</button>
</div>
</form>
</div>
</div>

<div class="sec">
<div class="sec-title">Meus CTAs</div>
<div class="card">
""" + (tags_html if tags_html else '<div style="color:#444;font-size:13px;margin-bottom:12px">Adicione uma conta primeiro</div>') + """
<form action="/admin/add-cta" method="POST" style="margin-top:4px">
<div class="row">
""" + ('<input name="account" value="' + ACCOUNTS[0] + '" readonly style="max-width:180px;color:#555">' if len(ACCOUNTS)==1 else '<input name="account" placeholder="Username" required style="max-width:180px">') + """
<input name="cta" placeholder="Palavra-chave (ex: NEZBRASIL)" required>
<button type="submit" class="btn">+ Adicionar CTA</button>
</div>
<div class="hint">Adicione uma palavra por vez. Clique no x para remover.</div>
</form>
</div>
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

@app.route('/admin/add-cta', methods=['POST'])
def admin_add_cta():
    from flask import request, redirect
    u   = request.form.get('account','').strip()
    cta = request.form.get('cta','').strip().upper()
    if u and cta:
        if u not in cta_config: cta_config[u] = []
        if cta not in cta_config[u]: cta_config[u].append(cta)
    threading.Thread(target=cta_refresh_all, daemon=True).start()
    return redirect('/admin')

@app.route('/admin/remove-cta')
def admin_remove_cta():
    from flask import request, redirect
    u   = request.args.get('account','').strip()
    cta = request.args.get('cta','').strip()
    if u in cta_config and cta in cta_config[u]:
        cta_config[u].remove(cta)
    return redirect('/admin')
