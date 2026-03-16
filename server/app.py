"""
ELO.LIVE - CS2 Premier ELO Overlay
=====================================
Backend: Flask + Steam OpenID + SQLite/PostgreSQL
"""

from flask import Flask, redirect, request, session, jsonify, send_from_directory
import requests, re, os, json, threading, time
from datetime import datetime
from db import db, User
from steam_auth import get_steam_profile

app = Flask(__name__, static_folder="../client", template_folder="../client")

# ── CONFIG ────────────────────────────────────────────────────────────────────
app.config["SECRET_KEY"]              = os.environ.get("SECRET_KEY", "dev-secret-change-in-prod")
_db_url = os.environ.get("DATABASE_URL", "sqlite:///elo.db")
if _db_url.startswith("postgres://"):
    _db_url = _db_url.replace("postgres://", "postgresql+pg8000://", 1)
elif _db_url.startswith("postgresql://") and "+pg8000" not in _db_url:
    _db_url = _db_url.replace("postgresql://", "postgresql+pg8000://", 1)
app.config["SQLALCHEMY_DATABASE_URI"] = _db_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"]   = os.environ.get("RAILWAY_ENVIRONMENT") is not None
db.init_app(app)

with app.app_context():
    db.create_all()

REFRESH_SEC = 90


# ── ELO FETCH ─────────────────────────────────────────────────────────────────

GCPD_PATTERNS = [
    r'Premier(?:[^<]{0,400}?)(\d{5,6})',
    r'(\d{5,6})(?:[^<]{0,100}?)Premier',
    r'"cs_rating"\s*:\s*(\d+)',
    r'"premier_rating"\s*:\s*(\d+)',
    r'"skillrating"\s*[>:,\s]+(\d{4,6})',
    r'<td[^>]*>\s*(\d{5,6})\s*</td>',
]

def fetch_elo(steam_id, cookies):
    """Fetchuje Premier ELO preko gcpd stranice koristeći Steam cookies."""
    try:
        sess = requests.Session()
        sess.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://steamcommunity.com/",
        })
        sess.cookies.update(cookies)
        url  = f"https://steamcommunity.com/profiles/{steam_id}/gcpd/730/?tab=matchmaking"
        resp = sess.get(url, timeout=15)
        html = resp.text
        for p in GCPD_PATTERNS:
            m = re.search(p, html, re.IGNORECASE | re.DOTALL)
            if m:
                val = int(m.group(1))
                if 5000 < val < 40000:
                    print(f"[ELO] gcpd {steam_id}: {val}")
                    return val
    except Exception as e:
        print(f"[ELO] gcpd greska: {e}")
    return None


def refresh_all_users():
    """Background thread - osvezava ELO za sve korisnike svakih REFRESH_SEC sekundi."""
    while True:
        with app.app_context():
            users = User.query.filter(User.steam_cookies.isnot(None)).all()
            for user in users:
                try:
                    cookies = json.loads(user.steam_cookies)
                    elo = fetch_elo(user.steam_id, cookies)
                    if elo:
                        prev = user.current_elo
                        user.current_elo = elo
                        user.prev_elo    = prev
                        user.elo_diff    = (elo - prev) if prev and prev != elo else None
                        user.last_update = datetime.utcnow()
                        db.session.commit()
                        print(f"[refresh] {user.username}: {elo:,}")
                except Exception as e:
                    print(f"[refresh] Greska {user.steam_id}: {e}")
        time.sleep(REFRESH_SEC)


threading.Thread(target=refresh_all_users, daemon=True).start()


# ── ROUTES ────────────────────────────────────────────────────────────────────

@app.route("/", methods=["GET", "POST"])
def index():
    return send_from_directory("../client", "index.html")


@app.route("/login/steam")
def login_steam():
    return_url = request.host_url.rstrip("/") + "/auth/steam/callback"
    steam_url  = (
        "https://steamcommunity.com/openid/login"
        "?openid.ns=http://specs.openid.net/auth/2.0"
        "&openid.mode=checkid_setup"
        f"&openid.return_to={return_url}"
        f"&openid.realm={request.host_url}"
        "&openid.identity=http://specs.openid.net/auth/2.0/identifier_select"
        "&openid.claimed_id=http://specs.openid.net/auth/2.0/identifier_select"
    )
    return redirect(steam_url)


@app.route("/auth/steam/callback")
def steam_callback():
    params = dict(request.args)
    params["openid.mode"] = "check_authentication"
    resp = requests.post("https://steamcommunity.com/openid/login", data=params, timeout=10)
    if "is_valid:true" not in resp.text:
        return "Steam login nije uspeo.", 401

    claimed_id = request.args.get("openid.claimed_id", "")
    steam_id   = claimed_id.split("/")[-1]
    if not steam_id.isdigit():
        return "Neispravan Steam ID.", 400

    profile = get_steam_profile(steam_id)

    user = User.query.filter_by(steam_id=steam_id).first()
    if not user:
        user = User(steam_id=steam_id)
        db.session.add(user)

    user.username   = profile.get("personaname", f"Player_{steam_id[-4:]}")
    user.avatar_url = profile.get("avatarfull", "")
    db.session.commit()

    session["steam_id"] = steam_id
    session["username"] = user.username
    session["avatar"]   = user.avatar_url

    return redirect("/dashboard")


@app.route("/dashboard")
def dashboard():
    if "steam_id" not in session:
        return redirect("/")
    return send_from_directory("../client", "dashboard.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")


# ── API ───────────────────────────────────────────────────────────────────────

@app.route("/api/me")
def api_me():
    if "steam_id" not in session:
        return jsonify({"error": "not_logged_in"}), 401

    user = User.query.filter_by(steam_id=session["steam_id"]).first()
    if not user:
        return jsonify({"error": "user_not_found"}), 404

    return jsonify({
        "steam_id":    user.steam_id,
        "username":    user.username,
        "avatar":      user.avatar_url,
        "elo":         user.current_elo,
        "prev_elo":    user.prev_elo,
        "diff":        user.elo_diff,
        "last_update": user.last_update.strftime("%H:%M") if user.last_update else None,
        "has_setup":   bool(user.steam_cookies),
        "overlay_url": f"/overlay/{user.steam_id}",
    })


@app.route("/api/save-cookies", methods=["POST"])
def save_cookies():
    """Korisnik unosi Steam cookies kao alternativu API kljucu."""
    if "steam_id" not in session:
        return jsonify({"error": "not_logged_in"}), 401

    data       = request.get_json()
    session_id = data.get("sessionid", "").strip()
    login_sec  = data.get("steamLoginSecure", "").strip()

    if not session_id or not login_sec:
        return jsonify({"error": "Popuni oba polja"}), 400

    user = User.query.filter_by(steam_id=session["steam_id"]).first()
    user.steam_cookies = json.dumps({
        "sessionid": session_id,
        "steamLoginSecure": login_sec,
    })
    db.session.commit()

    # Odmah fetchuj ELO
    cookies = {"sessionid": session_id, "steamLoginSecure": login_sec}
    elo = fetch_elo(session["steam_id"], cookies)

    if elo:
        user.current_elo = elo
        user.last_update = datetime.utcnow()
        db.session.commit()

    return jsonify({"success": True, "elo": elo})


@app.route("/api/elo/<steam_id>")
def api_elo(steam_id):
    """Javni endpoint za overlay."""
    user = User.query.filter_by(steam_id=steam_id).first()
    if not user:
        return jsonify({"error": "user_not_found"}), 404

    return jsonify({
        "elo":      user.current_elo,
        "prev":     user.prev_elo,
        "diff":     user.elo_diff,
        "ts":       user.last_update.strftime("%H:%M") if user.last_update else None,
        "username": user.username,
    })


# ── OVERLAY ───────────────────────────────────────────────────────────────────

@app.route("/overlay/<steam_id>")
def overlay(steam_id):
    user = User.query.filter_by(steam_id=steam_id).first()
    if not user:
        return "User not found", 404

    try:
        sid = max(1, min(10, int(request.args.get("style", 1))))
    except (ValueError, TypeError):
        sid = 1

    # Common JS for all styles - tier color system
    js = f"""<script>
const TIERS=[
  {{min:30000,c:'#ffd700',bg:'rgba(40,28,0,.93)',  tint:'rgba(50,35,0,.82)',  glow:'rgba(255,215,0,.4)' }},
  {{min:25000,c:'#f44336',bg:'rgba(42,6,6,.93)',   tint:'rgba(50,8,8,.82)',   glow:'rgba(244,67,54,.4)' }},
  {{min:20000,c:'#e91e8c',bg:'rgba(80,0,50,.93)',  tint:'rgba(100,0,60,.82)', glow:'rgba(233,30,140,.4)'}},
  {{min:15000,c:'#9c27b0',bg:'rgba(44,0,66,.93)',  tint:'rgba(55,0,80,.82)',  glow:'rgba(156,39,176,.4)'}},
  {{min:10000,c:'#3f51b5',bg:'rgba(16,22,84,.93)', tint:'rgba(20,28,100,.82)',glow:'rgba(63,81,181,.4)' }},
  {{min:5000, c:'#64b5f6',bg:'rgba(8,32,66,.93)',  tint:'rgba(12,40,80,.82)', glow:'rgba(100,181,246,.4)'}},
  {{min:0,    c:'#9e9e9e',bg:'rgba(22,22,22,.93)', tint:'rgba(25,25,25,.82)', glow:'rgba(158,158,158,.4)'}},
];
function T(e){{return TIERS.find(t=>e>=t.min)||TIERS[6];}}
let last=null;
const $e=document.getElementById('e');
const $e1=document.getElementById('e1');
const $e2=document.getElementById('e2');
const $s=document.getElementById('s');
const $b=document.getElementById('b');
async function poll(){{
  try{{
    const d=await fetch('/api/elo/{steam_id}').then(r=>r.json());
    if(d.elo){{
      const t=T(d.elo);
      if($e1){{
        const parts=d.elo.toLocaleString('en-US').split(',');
        $e1.textContent=parts[0];
        if($e2) $e2.textContent=parts[1]||'';
        $e1.classList.remove('bl');$e1.classList.add('pop');
        setTimeout(()=>$e1.classList.remove('pop'),350);
      }} else if($e){{
        $e.classList.remove('bl');$e.classList.add('pop');
        setTimeout(()=>$e.classList.remove('pop'),350);
        $e.textContent=d.elo.toLocaleString('en-US');
      }}
      document.querySelectorAll('[data-c]').forEach(el=>el.style.color=t.c);
      document.querySelectorAll('[data-bl]').forEach(el=>el.style.borderLeftColor=t.c);
      document.querySelectorAll('[data-bc]').forEach(el=>{{el.style.borderColor=t.c+'55';}});
      document.querySelectorAll('[data-fill]').forEach(el=>el.style.background=t.c);
      document.querySelectorAll('[data-tint]').forEach(el=>el.style.background=t.tint);
      document.querySelectorAll('[data-bg]').forEach(el=>el.style.background=t.bg);
      document.querySelectorAll('[data-boxglow]').forEach(el=>el.style.boxShadow=`0 0 0 1px ${{t.c}}55,0 0 18px ${{t.glow}},inset 0 1px 0 rgba(255,255,255,.08),inset 0 -1px 0 rgba(0,0,0,.4)`);
      document.querySelectorAll('[data-cssvar]').forEach(el=>{{el.style.setProperty('--c',t.c);el.style.setProperty('--bg',t.bg);el.style.setProperty('--glow',t.glow);}});
      document.querySelectorAll('[data-glow]').forEach(el=>{{el.style.textShadow=`0 0 20px ${{t.c}}aa,0 0 40px ${{t.c}}44`;}});
      if(d.diff&&d.diff!==0&&last!==null&&$b){{
        $b.textContent=(d.diff>0?'+':'')+d.diff;
        $b.className=d.diff>0?'u':'dn';
        setTimeout(()=>{{$b.className='';$b.style.display='none';}},8000);
      }}
      last=d.elo;
    }}
  }}catch(err){{}}
}}
poll();setInterval(poll,15000);
</script>"""

    base = """*{margin:0;padding:0;box-sizing:border-box}body{background:transparent;overflow:hidden}
@keyframes bl{0%,100%{opacity:1}50%{opacity:.3}}@keyframes pop{0%{transform:scale(1)}50%{transform:scale(1.1)}100%{transform:scale(1)}}
.bl{animation:bl 1.4s ease-in-out infinite}.pop{animation:pop .3s ease}
#b{font-size:11px;font-weight:700;padding:2px 7px;border-radius:3px;display:none}
.u{background:rgba(0,200,100,.18);color:#00cc66;display:block}.dn{background:rgba(220,50,50,.18);color:#ff4444;display:block}"""

    FONTS = "https://fonts.googleapis.com/css2?family=Bebas+Neue&family=Barlow:wght@400;700;900&family=Share+Tech+Mono&family=Orbitron:wght@700&display=swap"

    # 10 genuinely different overlay layouts
    if sid == 1:
        # Classic Shield - horizontal, dark bg, shield icon
        css = "body{font-family:'Barlow',sans-serif}#w{display:inline-flex;align-items:center;gap:10px;background:rgba(10,10,15,.88);border:1px solid rgba(255,170,0,.25);border-left:3px solid #ffaa00;border-radius:6px;padding:10px 18px 10px 12px}.lb{font-size:8px;font-weight:700;letter-spacing:2.5px;text-transform:uppercase;margin-bottom:3px}#e{font-family:'Bebas Neue';font-size:30px;color:#fff;line-height:1;letter-spacing:1px;min-width:70px}#s{font-size:9px;color:rgba(255,255,255,.28);margin-top:2px}"
        body = """<div id="w" data-bl><svg width="24" height="24" viewBox="0 0 100 100" style="flex-shrink:0"><rect width="100" height="100" rx="14" fill="#1a1a2e"/><path d="M50 12L82 28L82 58C82 75 66 87 50 92C34 87 18 75 18 58L18 28Z" fill="none" stroke="#ffaa00" stroke-width="5"/><path d="M37 51L45 60L64 39" stroke="#ffaa00" stroke-width="6" fill="none"/></svg>
<div style="width:1px;height:26px;background:rgba(255,255,255,.1)"></div>
<div><div class="lb" data-c style="color:#ffaa00">PREMIER ELO</div><div id="e" class="bl">---</div><div id="s"></div></div>
<div id="b"></div></div>"""

    elif sid == 2:
        # CS2 Premier badge — CSS variables, pseudo-element depth, skewed // slash, split number
        css = "html,body{width:100%;height:100%;margin:0;padding:0}body{display:flex;align-items:center;padding:5px 6px}#badge{--c:#9e9e9e;--bg:rgba(22,22,22,.93);--glow:rgba(158,158,158,.25);display:inline-flex;align-items:stretch;border-radius:3px;overflow:visible;position:relative;background:var(--bg);box-shadow:0 0 0 1px rgba(255,255,255,.12),0 0 22px var(--glow),inset 0 1px 0 rgba(255,255,255,.1),inset 0 -1px 0 rgba(0,0,0,.4)}#badge::after{content:'';position:absolute;inset:0;border-radius:3px;background:linear-gradient(170deg,rgba(255,255,255,.07) 0%,transparent 55%);pointer-events:none;z-index:2}.slash{display:flex;align-items:center;padding:8px 4px 8px 10px;font-family:'Bebas Neue',sans-serif;font-size:34px;line-height:1;letter-spacing:-5px;color:var(--c);transform:skewX(-10deg);text-shadow:0 0 8px var(--glow);position:relative;z-index:1}.num{display:flex;align-items:baseline;padding:7px 14px 7px 8px;font-family:'Bebas Neue',sans-serif;line-height:1;position:relative;z-index:1}.num .big{font-size:46px;letter-spacing:0;color:var(--c);-webkit-text-stroke:1px var(--c);text-shadow:0 0 10px var(--glow)}.num .sep{font-size:46px;color:var(--c);-webkit-text-stroke:1px var(--c)}.num .small{font-size:28px;letter-spacing:1px;color:var(--c);-webkit-text-stroke:.7px var(--c);opacity:.8}#s{display:none}"
        body = """<div id="badge" data-cssvar>
<div class="slash">//</div>
<div class="num"><span id="e1" class="big bl">--</span><span class="sep">,</span><span id="e2" class="small">---</span></div>
<div id="b" style="display:none"></div></div><div id="s" style="display:none"></div>"""

    elif sid == 3:
        # Minimal - just the number, colored bar underneath, no background
        css = "body{font-family:'Barlow',sans-serif}#e{font-family:'Bebas Neue';font-size:38px;color:#fff;line-height:1;letter-spacing:3px}#bar{height:3px;background:#ffaa00;margin-top:4px;border-radius:2px}.lb{font-size:7px;font-weight:700;letter-spacing:3px;color:rgba(255,255,255,.35);text-transform:uppercase;margin-bottom:5px}#s{font-size:8px;color:rgba(255,255,255,.25);margin-top:3px}"
        body = """<div><div class="lb" data-c style="color:rgba(255,255,255,.35)">PREMIER ELO</div><div id="e" class="bl" data-c style="color:#fff">---</div><div id="bar" data-fill></div><div id="s"></div><div id="b" style="margin-top:3px"></div></div>"""

    elif sid == 4:
        # Pill / Capsule - rounded, single line, ELO label + number
        css = "body{font-family:'Barlow',sans-serif}#w{display:inline-flex;align-items:stretch;background:rgba(8,8,12,.9);border:1px solid rgba(255,170,0,.25);border-radius:50px;overflow:hidden}#left{padding:7px 12px 7px 14px;border-right:1px solid rgba(255,255,255,.07)}#right{padding:7px 14px 7px 12px;display:flex;align-items:center;gap:6px}.tag{font-size:7px;font-weight:700;letter-spacing:2px;text-transform:uppercase;margin-bottom:2px}#e{font-family:'Bebas Neue';font-size:24px;line-height:1;letter-spacing:1px}#s{display:none}"
        body = """<div id="w" data-bl><div id="left" data-bg style="background:rgba(255,170,0,.1)"><div class="tag" data-c style="color:rgba(255,255,255,.4)">PREMIER</div><div id="e" class="bl" data-c style="color:#ffaa00">---</div></div>
<div id="right"><div id="b"></div></div></div><div id="s"></div>"""

    elif sid == 5:
        # Neon Glow - dark bg, number glows with tier color
        css = "body{font-family:'Barlow',sans-serif;text-align:center}#wrap{display:inline-block;padding:8px 18px}.lb{font-size:7px;font-weight:700;letter-spacing:4px;color:rgba(255,255,255,.3);text-transform:uppercase;margin-bottom:5px}#e{font-family:'Bebas Neue';font-size:44px;color:#ffaa00;line-height:1;letter-spacing:3px;text-shadow:0 0 20px #ffaa00aa,0 0 40px #ffaa0044}#s{font-size:8px;color:rgba(255,255,255,.22);margin-top:4px}"
        body = """<div id="wrap"><div class="lb" data-c style="color:rgba(255,255,255,.3)">PREMIER ELO</div><div id="e" class="bl" data-c data-glow>---</div><div id="s"></div><div id="b" style="margin-top:3px;display:inline-block"></div></div>"""

    elif sid == 6:
        # Vertical Card - stacked layout, rank color on left side
        css = "body{font-family:'Barlow',sans-serif}#w{display:inline-flex;flex-direction:column;background:rgba(8,8,14,.92);border:1px solid rgba(255,255,255,.07);border-left:4px solid #ffaa00;border-radius:6px;padding:12px 16px;min-width:120px}.lb{font-size:7px;font-weight:700;letter-spacing:3px;text-transform:uppercase;margin-bottom:7px}#e{font-family:'Bebas Neue';font-size:38px;color:#fff;line-height:1;letter-spacing:2px}#s{font-size:8px;color:rgba(255,255,255,.22);margin-top:5px}"
        body = """<div id="w" data-bl><div class="lb" data-c style="color:#ffaa00">PREMIER ELO</div><div id="e" class="bl">---</div><div id="b" style="margin-top:5px"></div><div id="s"></div></div>"""

    elif sid == 7:
        # HUD Brackets - corner bracket design
        css = "body{font-family:'Share Tech Mono',monospace}#wrap{display:inline-block;position:relative;padding:12px 16px}.br{position:absolute;width:10px;height:10px}.tl{top:0;left:0;border-top:2px solid #ffaa00;border-left:2px solid #ffaa00}.tr{top:0;right:0;border-top:2px solid #ffaa00;border-right:2px solid #ffaa00}.bl2{bottom:0;left:0;border-bottom:2px solid #ffaa00;border-left:2px solid #ffaa00}.br2{bottom:0;right:0;border-bottom:2px solid #ffaa00;border-right:2px solid #ffaa00}.lb{font-size:7px;letter-spacing:3px;color:rgba(255,255,255,.35);margin-bottom:5px;text-align:center}#e{font-family:'Bebas Neue';font-size:30px;color:#ffaa00;line-height:1;letter-spacing:2px;text-align:center}#s{font-size:7px;color:rgba(255,255,255,.22);margin-top:4px;text-align:center}"
        body = """<div id="wrap"><div class="br tl" data-fill style="background:none;border-color:#ffaa00" id="c1"></div><div class="br tr" data-fill style="background:none;border-color:#ffaa00" id="c2"></div><div class="br bl2" data-fill style="background:none;border-color:#ffaa00" id="c3"></div><div class="br br2" data-fill style="background:none;border-color:#ffaa00" id="c4"></div>
<div class="lb" data-c style="color:rgba(255,255,255,.35)">PREMIER ELO</div><div id="e" class="bl" data-c>---</div><div id="s"></div><div id="b" style="text-align:center;margin-top:3px"></div></div>"""

    elif sid == 8:
        # Terminal / Retro DOS style
        css = "body{font-family:'Share Tech Mono',monospace}#w{display:inline-block;background:rgba(0,10,0,.94);border:1px solid #00ff4122;border-radius:3px;padding:10px 16px}.prompt{font-size:8px;color:#00ff4166;letter-spacing:1px;margin-bottom:6px}#e{font-size:28px;color:#00ff41;line-height:1;letter-spacing:3px}#s{font-size:8px;color:#00ff4144;margin-top:5px}#b{color:#00ff41}"
        body = """<div id="w"><div class="prompt">&gt; PREMIER_ELO.exe</div><div id="e" class="bl">---</div><div id="s"></div><div id="b" style="margin-top:3px"></div></div>"""

    elif sid == 9:
        # Frosted Glass - modern, blur effect
        css = "body{font-family:'Barlow',sans-serif}#w{display:inline-flex;align-items:center;gap:12px;background:rgba(255,255,255,.06);backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);border:1px solid rgba(255,255,255,.14);border-left:3px solid rgba(255,255,255,.5);border-radius:14px;padding:12px 20px}.lb{font-size:7px;font-weight:700;letter-spacing:3px;text-transform:uppercase;color:rgba(255,255,255,.4);margin-bottom:5px}#e{font-family:'Bebas Neue';font-size:32px;color:#fff;line-height:1;letter-spacing:1px;min-width:78px}#s{font-size:8px;color:rgba(255,255,255,.25);margin-top:3px}"
        body = """<div id="w" data-bl><div><div class="lb" data-c style="color:rgba(255,255,255,.4)">PREMIER ELO</div><div id="e" class="bl">---</div><div id="s"></div></div><div id="b"></div></div>"""

    else:  # sid == 10
        # XL Bold - huge number, accent line, minimal text
        css = "body{font-family:'Barlow',sans-serif}#e{font-family:'Bebas Neue';font-size:58px;color:#fff;line-height:1;letter-spacing:4px}#line{height:3px;background:#ffaa00;margin:5px 0 4px;border-radius:2px}#foot{display:flex;align-items:center;justify-content:space-between}.lb{font-size:7px;font-weight:700;letter-spacing:3px;text-transform:uppercase;color:rgba(255,255,255,.35)}"
        body = """<div><div id="e" class="bl" data-c style="color:#fff">---</div><div id="line" data-fill></div><div id="foot"><div class="lb" data-c style="color:rgba(255,255,255,.35)">PREMIER ELO</div><div id="b"></div></div></div><div id="s" style="display:none"></div>"""

    html = f"""<!DOCTYPE html><html><head><meta charset="UTF-8">
<link href="{FONTS}" rel="stylesheet">
<style>{base}{css}</style></head><body>
{body}
{js}
</body></html>"""

    response = app.make_response(html)
    response.headers["Content-Type"] = "text/html; charset=utf-8"
    response.headers["ngrok-skip-browser-warning"] = "true"
    response.headers["Access-Control-Allow-Origin"] = "*"
    return response


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)