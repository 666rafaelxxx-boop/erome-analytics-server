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
    rows = ''
    for u in ACCOUNTS:
        ctas = ', '.join(cta_config.get(u, []))
        st   = cta_status.get(u, {})
        gone = sum(1 for v in st.values() if not v.get('ok'))
        ok_n = sum(1 for v in st.values() if v.get('ok'))
        sbadge = f'<span style="color:#ff2244">🔴 {gone} sumiu(ram)</span>' if gone else (f'<span style="color:#22cc55">🟢 {ok_n} ok</span>' if ok_n else '<span style="color:#888">⏳ verificando</span>')
        rows += '<tr><td>@' + u + '</td><td>' + (ctas or '—') + '</td><td>' + sbadge + '</td><td><a href="/remove?account=' + u + '">✕ Remover</a></td></tr>'

    cta_rows = ''
    for u in ACCOUNTS:
        for aid, v in cta_status.get(u, {}).items():
            ico = '🟢' if v.get('ok') else '🔴'
            col = '#22cc55' if v.get('ok') else '#ff2244'
            cta_rows += f'<div style="font-size:12px;padding:6px 0;border-bottom:1px solid #111"><span style="color:{col}">{ico}</span> @{u} — álbum <code>{aid}</code> — CTA: <strong>{v.get("foundCta","—")}</strong> — {str(v.get("checkedAt",""))[:16]}</div>'

    html = f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Erome Analytics Admin</title>
<style>
body{{font-family:Inter,sans-serif;background:#0d0d0d;color:#f0f0f0;padding:20px;max-width:800px;margin:0 auto}}
h1{{color:#ff2244;font-size:20px;margin-bottom:4px}}
h2{{color:#ff224488;font-size:11px;text-transform:uppercase;letter-spacing:.1em;margin:24px 0 10px}}
table{{width:100%;border-collapse:collapse}}
th,td{{padding:10px 12px;text-align:left;border-bottom:1px solid #1a1a1a;font-size:13px}}
th{{color:#ff224466;font-size:10px;text-transform:uppercase;letter-spacing:.1em}}
td a{{color:#ff2244;text-decoration:none}}
.card{{background:#111;border:1px solid #ff224420;border-radius:12px;padding:16px;margin-bottom:14px}}
.row{{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:8px}}
input{{background:#0a0a0a;border:1px solid #ff224430;border-radius:8px;color:#fff;padding:9px 12px;font-size:13px;flex:1;min-width:140px;outline:none}}
input:focus{{border-color:#ff2244}}
button,.btn{{background:linear-gradient(135deg,#ff2244,#880020);border:none;border-radius:8px;color:#fff;padding:9px 18px;cursor:pointer;font-size:13px;font-weight:700;text-decoration:none;display:inline-block}}
.hint{{font-size:11px;color:#333;margin-top:6px;line-height:1.6}}
.link{{background:#111;border:1px solid #ff224430;color:#ff2244;border-radius:8px;padding:7px 14px;cursor:pointer;font-size:12px;text-decoration:none;display:inline-block;margin-bottom:12px}}
</style>
</head>
<body>
<h1>⬛ Erome Analytics</h1>
<a href="/admin" class="link">↺ Atualizar página</a>
<a href="/refresh" class="link">⚡ Forçar refresh dos dados</a>

<h2>Contas monitoradas</h2>
<div class="card">
<table>
<tr><th>Conta</th><th>CTAs ativos</th><th>Status</th><th></th></tr>
{rows if rows else '<tr><td colspan="4" style="color:#444;font-size:12px">Nenhuma conta adicionada ainda</td></tr>'}
</table>
</div>

<h2>Adicionar conta</h2>
<div class="card">
<form action="/admin/add" method="POST">
<div class="row">
<input name="account" placeholder="Username (ex: ModoNoturnoBR)" required>
<input name="ctas" placeholder="CTAs separados por vírgula (ex: NEZBRASIL, CTANOVO)">
<button type="submit">＋ Adicionar</button>
</div>
<div class="hint">💡 Adicione todos os seus CTAs ativos separados por vírgula.</div>
</form>
</div>

<h2>Atualizar CTAs de uma conta</h2>
<div class="card">
<form action="/admin/ctas" method="POST">
<div class="row">
<input name="account" placeholder="Username" required>
<input name="ctas" placeholder="Novos CTAs separados por vírgula" required>
<button type="submit">Atualizar</button>
</div>
<div class="hint">⚠️ Substitui todos os CTAs da conta. Use quando mudar seu CTA no Telegram.</div>
</form>
</div>

<h2>Status dos CTAs</h2>
<div class="card">
{cta_rows if cta_rows else '<div style="color:#444;font-size:12px">Ainda verificando... aguarde até 10 minutos.</div>'}
</div>

<div style="font-size:10px;color:#1e1e1e;margin-top:20px">
Dados: cada 15min · CTAs: cada 10min · <a href="/" style="color:#333">API JSON</a>
</div>
</body>
</html>"""
    return html

@app.route('/admin/add', methods=['POST'])
def admin_add():
    from flask import request
    u    = request.form.get('account','').strip()
    craw = request.form.get('ctas','').strip()
    if u and u not in ACCOUNTS:
        ACCOUNTS.append(u)
    if craw and u:
        cta_config[u] = [x.strip().upper() for x in craw.split(',') if x.strip()]
    threading.Thread(target=refresh_all, daemon=True).start()
    from flask import redirect
    return redirect('/admin')

@app.route('/admin/ctas', methods=['POST'])
def admin_ctas():
    from flask import request, redirect
    u    = request.form.get('account','').strip()
    craw = request.form.get('ctas','').strip()
    if u and craw:
        cta_config[u] = [x.strip().upper() for x in craw.split(',') if x.strip()]
    threading.Thread(target=cta_refresh_all, daemon=True).start()
    return redirect('/admin')

@app.route('/remove')
def remove_account():
    from flask import request, redirect
    u = request.args.get('account','').strip()
    if u in ACCOUNTS: ACCOUNTS.remove(u)
    cta_config.pop(u, None)
    cta_status.pop(u, None)
    cache.pop(u, None)
    return redirect('/admin')

# ── START ─────────────────────────────────────────────────────────
if __name__ == '__main__':
    # Inicia loop em background
    t = threading.Thread(target=background_loop, daemon=True)
    t.start()
    app.run(host='0.0.0.0', port=8080)
