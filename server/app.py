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
    # Auto-migrate: dodaj nove kolone ako ne postoje (SQLite ne podrzava ALTER TABLE ADD IF NOT EXISTS)
    from sqlalchemy import text
    new_cols = [
        ("total_wins",     "INTEGER DEFAULT 0"),
        ("total_losses",   "INTEGER DEFAULT 0"),
        ("total_ties",     "INTEGER DEFAULT 0"),
        ("session_wins",   "INTEGER DEFAULT 0"),
        ("session_losses", "INTEGER DEFAULT 0"),
        ("session_ties",   "INTEGER DEFAULT 0"),
        ("session_date",   "VARCHAR(10)"),
        ("session_elo",    "INTEGER"),
    ]
    with db.engine.connect() as conn:
        existing = [row[1] for row in conn.execute(text("PRAGMA table_info(users)")).fetchall()]
        for col, typ in new_cols:
            if col not in existing:
                conn.execute(text(f"ALTER TABLE users ADD COLUMN {col} {typ}"))
        conn.commit()

REFRESH_SEC = 30


# ── ELO FETCH ─────────────────────────────────────────────────────────────────

def _steam_session(cookies):
    """Pravi requests.Session sa Steam cookies pravilno postavljenim (domain + path)."""
    sess = requests.Session()
    sess.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Referer": "https://steamcommunity.com/",
    })
    for name, val in cookies.items():
        sess.cookies.set(name, val, domain=".steamcommunity.com", path="/")
    return sess


ELO_PATTERNS = [
    r'"cs_rating"\s*:\s*(\d+)',
    r'"premier_rating"\s*:\s*(\d+)',
    r'"skillrating"\s*:\s*(\d+)',
    r'cs_rating["\s]*[=:]\s*["\s]*(\d+)',
    r'<name>cs_rating</name>[^<]*<value>(\d+)</value>',
    r'rating_calibration_votes[^,}]*,\s*"cs_rating"\s*:\s*(\d+)',
]


def _search_elo(text):
    for pat in ELO_PATTERNS:
        m = re.search(pat, text, re.IGNORECASE | re.DOTALL)
        if m:
            val = int(m.group(1))
            if 1000 < val < 50000:
                return val
    return None


def fetch_elo(steam_id, cookies):
    """
    Prioritet:
      1. Steam Web API  (ISteamUserStats)  — treba STEAM_API_KEY env var
      2. GCPD render endpoint              — treba svjeze Steam cookies
      3. GCPD stranica + XML stats         — fallback
    """

    # ── 1. Steam Web API ───────────────────────────────────────────────────────
    api_key = os.environ.get("STEAM_API_KEY", "")
    if api_key:
        try:
            url = (f"https://api.steampowered.com/ISteamUserStats/"
                   f"GetUserStatsForGame/v2/?appid=730&steamid={steam_id}&key={api_key}")
            resp = requests.get(url, timeout=10)
            if resp.ok:
                stats = {s["name"]: s["value"]
                         for s in resp.json().get("playerstats", {}).get("stats", [])}
                for stat in ("cs_rating", "premier_rating", "skillrating"):
                    val = stats.get(stat, 0)
                    if 1000 < val < 50000:
                        print(f"[ELO] API {steam_id}: {val} ({stat})")
                        return val
                # Ispisi koji stat-ovi postoje radi debug-a
                rating_stats = {k: v for k, v in stats.items() if "rating" in k.lower()}
                print(f"[ELO] API {steam_id}: cs_rating nije tu. Rating stats: {rating_stats}")
        except Exception as e:
            print(f"[ELO] API greska: {e}")

    # ── 2. GCPD ajax endpoint (vraca JSON sa HTML tablicom) ───────────────────
    if cookies:
        sess = _steam_session(cookies)
        ajax_url = f"https://steamcommunity.com/profiles/{steam_id}/gcpd/730/?tab=matchmaking&ajax=1"
        try:
            resp = sess.get(ajax_url, timeout=15)
            data = resp.json()
            if data.get("success"):
                html = data["html"]
                # Format: <td>Premier</td><td>wins</td><td>ties</td><td>losses</td><td>ELO</td>
                m = re.search(
                    r'<td>Premier</td><td>(\d+)</td><td>(\d+)</td><td>(\d+)</td><td>(\d{4,6})</td>',
                    html, re.IGNORECASE
                )
                if m:
                    wins, ties, losses, elo = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
                    if 1000 < elo < 50000:
                        print(f"[ELO] GCPD ajax {steam_id}: {elo} W:{wins} T:{ties} L:{losses}")
                        return {"elo": elo, "wins": wins, "ties": ties, "losses": losses}
        except Exception as e:
            print(f"[ELO] GCPD ajax greska: {e}")

    print(f"[ELO] {steam_id}: ELO nije pronadjen. Dodaj STEAM_API_KEY ili osvjezi cookies.")
    return None


def refresh_all_users():
    """Background thread - osvezava ELO za sve korisnike svakih REFRESH_SEC sekundi."""
    while True:
        with app.app_context():
            has_api_key = bool(os.environ.get("STEAM_API_KEY", ""))
            # Sa API kljucem fetchujemo sve usere, bez njega samo one sa cookies
            if has_api_key:
                users = User.query.all()
            else:
                users = User.query.filter(User.steam_cookies.isnot(None)).all()
            today = datetime.utcnow().strftime("%Y-%m-%d")
            for user in users:
                try:
                    cookies = json.loads(user.steam_cookies) if user.steam_cookies else {}
                    result = fetch_elo(user.steam_id, cookies)
                    if result:
                        elo = result["elo"]
                        prev = user.current_elo
                        user.current_elo = elo
                        user.prev_elo    = prev
                        user.elo_diff    = (elo - prev) if prev and prev != elo else None
                        user.last_update = datetime.utcnow()
                        # W/L/T tracking
                        new_w = result.get("wins", 0)
                        new_l = result.get("losses", 0)
                        new_t = result.get("ties", 0)
                        # Reset session baseline at start of new day
                        if user.session_date != today:
                            user.session_elo    = user.current_elo or elo
                            user.session_wins   = new_w
                            user.session_losses = new_l
                            user.session_ties   = new_t
                            user.session_date   = today
                        user.total_wins   = new_w
                        user.total_losses = new_l
                        user.total_ties   = new_t
                        db.session.commit()
                        sw = new_w - (user.session_wins or new_w)
                        sl = new_l - (user.session_losses or new_l)
                        print(f"[refresh] {user.username}: {elo:,} | W:{sw} L:{sl} danas")
                except Exception as e:
                    print(f"[refresh] Greska {user.steam_id}: {e}")
        time.sleep(REFRESH_SEC)


threading.Thread(target=refresh_all_users, daemon=True).start()


# ── ROUTES ────────────────────────────────────────────────────────────────────

@app.route("/", methods=["GET", "POST"])
def index():
    if "steam_id" in session:
        return redirect("/dashboard")
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


@app.route("/api/status")
def api_status():
    return jsonify({"has_api_key": bool(os.environ.get("STEAM_API_KEY", ""))})


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
        "session_w":   max(0, (user.total_wins   or 0) - (user.session_wins   or 0)),
        "session_l":   max(0, (user.total_losses or 0) - (user.session_losses or 0)),
        "session_t":   max(0, (user.total_ties   or 0) - (user.session_ties   or 0)),
        "elo_today":   (user.current_elo or 0) - (user.session_elo or user.current_elo or 0),
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
        "elo":       user.current_elo,
        "prev":      user.prev_elo,
        "diff":      user.elo_diff,
        "ts":        user.last_update.strftime("%H:%M") if user.last_update else None,
        "username":  user.username,
        "session_w": max(0, (user.total_wins   or 0) - (user.session_wins   or 0)),
        "session_l": max(0, (user.total_losses or 0) - (user.session_losses or 0)),
        "session_t": max(0, (user.total_ties   or 0) - (user.session_ties   or 0)),
        "elo_today": (user.current_elo or 0) - (user.session_elo or user.current_elo or 0),
    })


@app.route("/api/set-elo", methods=["POST"])
def set_elo_manual():
    """Ručno postavljanje ELO-a kad automatski ne radi."""
    if "steam_id" not in session:
        return jsonify({"error": "not_logged_in"}), 401
    data = request.get_json()
    elo = data.get("elo")
    try:
        elo = int(elo)
        if not (1000 < elo < 50000):
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({"error": "Nevažeći ELO (mora biti između 1000 i 50000)"}), 400

    user = User.query.filter_by(steam_id=session["steam_id"]).first()
    if not user:
        return jsonify({"error": "user_not_found"}), 404

    prev = user.current_elo
    user.current_elo = elo
    user.prev_elo    = prev
    user.elo_diff    = (elo - prev) if prev else None
    user.last_update = datetime.utcnow()
    db.session.commit()
    return jsonify({"success": True, "elo": elo})


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
      // W/T/L danas (style 2)
      const $sw=document.getElementById('sw');
      const $sl2=document.getElementById('sl2');
      const $st=document.getElementById('st');
      if($sw) $sw.textContent='W: '+(d.session_w??0);
      if($sl2) $sl2.textContent='L: '+(d.session_l??0);
      if($st) $st.textContent='T: '+(d.session_t??0);
      // ELO diff danas (style 3)
      const $etd=document.getElementById('elo_today');
      if($etd){{
        const td=d.elo_today??0;
        $etd.textContent=(td>=0?'+':'')+td;
        $etd.style.color=td>=0?'#00cc66':'#ff4444';
      }}
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

    # CS2 badge base CSS — shared across all styles
    CS2 = ("html,body{width:100%;height:100%;margin:0;padding:0}"
           "body{display:flex;align-items:flex-start;padding:5px 6px}"
           "#badge{--c:#9e9e9e;--bg:rgba(22,22,22,.93);--glow:rgba(158,158,158,.25);"
           "display:inline-flex;align-items:stretch;border-radius:3px;overflow:visible;"
           "position:relative;background:var(--bg);"
           "box-shadow:0 0 0 1px rgba(255,255,255,.12),0 0 22px var(--glow),"
           "inset 0 1px 0 rgba(255,255,255,.1),inset 0 -1px 0 rgba(0,0,0,.4)}"
           "#badge::after{content:'';position:absolute;inset:0;border-radius:3px;"
           "background:linear-gradient(170deg,rgba(255,255,255,.07) 0%,transparent 55%);"
           "pointer-events:none;z-index:2}"
           ".slash{display:flex;align-items:center;padding:8px 4px 8px 10px;"
           "font-family:'Bebas Neue',sans-serif;font-size:34px;line-height:1;"
           "letter-spacing:-5px;color:var(--c);transform:skewX(-10deg);"
           "text-shadow:0 0 8px var(--glow);position:relative;z-index:1}"
           ".num{display:flex;align-items:baseline;padding:7px 14px 7px 8px;"
           "font-family:'Bebas Neue',sans-serif;line-height:1;position:relative;z-index:1}"
           ".big{font-size:46px;letter-spacing:0;color:var(--c);-webkit-text-stroke:1px var(--c);text-shadow:0 0 10px var(--glow)}"
           ".sep{font-size:46px;color:var(--c);-webkit-text-stroke:1px var(--c)}"
           ".small{font-size:28px;letter-spacing:1px;color:var(--c);-webkit-text-stroke:.7px var(--c);opacity:.8}"
           "#s{display:none}")

    BADGE_HTML = """<div id="badge" data-cssvar>
<div class="slash">//</div>
<div class="num"><span id="e1" class="big bl">--</span><span class="sep">,</span><span id="e2" class="small">---</span></div>
</div>"""

    if sid == 1:
        # Style 1 — samo CS2 badge
        css = CS2
        body = BADGE_HTML + """<div id="b" style="display:none"></div><div id="s" style="display:none"></div>"""

    elif sid == 2:
        # Style 2 — CS2 badge + W/T/L danas
        css = (CS2 + "body{flex-direction:column;gap:4px}"
               "#row{display:flex;gap:8px;padding:2px 2px;align-items:center;font-family:'Barlow',sans-serif}"
               ".wlt{font-size:12px;font-weight:700;letter-spacing:.5px}"
               "#sw{color:#00cc66}#sl2{color:#ff4444}#st{color:rgba(255,255,255,.45)}"
               ".div{color:rgba(255,255,255,.2);font-size:10px;margin:0 2px}")
        body = (BADGE_HTML +
                """<div id="row">
<span id="sw" class="wlt">W: -</span><span class="div">|</span>
<span id="st" class="wlt">T: -</span><span class="div">|</span>
<span id="sl2" class="wlt">L: -</span>
</div>
<div id="b" style="display:none"></div><div id="s" style="display:none"></div>""")

    elif sid == 3:
        # Style 3 — CS2 badge + ELO diff danas
        css = (CS2 + "body{flex-direction:column;gap:4px}"
               "#row{padding:2px 2px;font-family:'Barlow',sans-serif;display:flex;align-items:center;gap:6px}"
               ".lbl{font-size:8px;font-weight:700;letter-spacing:2px;color:rgba(255,255,255,.3)}"
               "#elo_today{font-size:14px;font-weight:700;letter-spacing:.5px}")
        body = (BADGE_HTML +
                """<div id="row">
<span class="lbl">DANAS</span><span id="elo_today" style="color:#9e9e9e">+0</span>
</div>
<div id="b" style="display:none"></div><div id="s" style="display:none"></div>""")

    elif sid == 4:
        css = CS2
        body = BADGE_HTML + """<div id="b" style="display:none"></div><div id="s" style="display:none"></div>"""

    elif sid == 5:
        css = CS2
        body = BADGE_HTML + """<div id="b" style="display:none"></div><div id="s" style="display:none"></div>"""

    elif sid == 6:
        css = CS2
        body = BADGE_HTML + """<div id="b" style="display:none"></div><div id="s" style="display:none"></div>"""

    elif sid == 7:
        css = CS2
        body = BADGE_HTML + """<div id="b" style="display:none"></div><div id="s" style="display:none"></div>"""

    elif sid == 8:
        css = CS2
        body = BADGE_HTML + """<div id="b" style="display:none"></div><div id="s" style="display:none"></div>"""

    elif sid == 9:
        css = CS2
        body = BADGE_HTML + """<div id="b" style="display:none"></div><div id="s" style="display:none"></div>"""

    else:  # sid == 10
        css = CS2
        body = BADGE_HTML + """<div id="b" style="display:none"></div><div id="s" style="display:none"></div>"""

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