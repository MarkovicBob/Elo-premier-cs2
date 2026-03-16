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

@app.route("/")
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

    html = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8">
<link href="https://fonts.googleapis.com/css2?family=Bebas+Neue&family=Barlow:wght@400;700&display=swap" rel="stylesheet">
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{background:transparent;overflow:hidden;font-family:'Barlow',sans-serif}}
#w{{display:inline-flex;align-items:center;gap:10px;background:rgba(10,10,15,0.85);border:1px solid rgba(255,170,0,0.3);border-left:3px solid #ffaa00;border-radius:6px;padding:10px 18px 10px 14px}}
svg{{width:26px;height:26px;flex-shrink:0}}
.dv{{width:1px;height:28px;background:rgba(255,255,255,0.1)}}
.lb{{font-size:9px;font-weight:700;letter-spacing:2.5px;color:#ffaa00;text-transform:uppercase;margin-bottom:3px}}
#e{{font-family:'Bebas Neue',sans-serif;font-size:30px;color:#fff;line-height:1;letter-spacing:1px;min-width:70px}}
#s{{font-size:10px;color:rgba(255,255,255,0.35);margin-top:2px}}
#b{{font-size:11px;font-weight:700;padding:3px 7px;border-radius:3px;display:none}}
.u{{background:rgba(0,200,100,0.18);color:#00cc66;display:block}}
.dn{{background:rgba(220,50,50,0.18);color:#ff4444;display:block}}
@keyframes bl{{0%,100%{{opacity:1}}50%{{opacity:.3}}}}
@keyframes pop{{0%{{transform:scale(1)}}50%{{transform:scale(1.1)}}100%{{transform:scale(1)}}}}
.bl{{animation:bl 1.4s ease-in-out infinite}}
.pop{{animation:pop .3s ease}}
</style></head><body>
<div id="w">
<svg viewBox="0 0 100 100" xmlns="http://www.w3.org/2000/svg">
<rect width="100" height="100" rx="14" fill="#1a1a2e"/>
<path d="M50 12 L82 28 L82 58 C82 75 66 87 50 92 C34 87 18 75 18 58 L18 28 Z" fill="none" stroke="#ffaa00" stroke-width="5" stroke-linejoin="round"/>
<path d="M37 51 L45 60 L64 39" stroke="#ffaa00" stroke-width="6" stroke-linecap="round" stroke-linejoin="round" fill="none"/>
</svg>
<div class="dv"></div>
<div><div class="lb">Premier ELO</div><div id="e" class="bl">---</div><div id="s">loading...</div></div>
<div id="b"></div>
</div>
<script>
let last=null;
const $e=document.getElementById('e'),$s=document.getElementById('s'),$b=document.getElementById('b');
async function poll(){{
  try{{
    const d=await fetch('/api/elo/{steam_id}').then(r=>r.json());
    if(d.elo){{
      $e.classList.remove('bl');$e.classList.add('pop');
      setTimeout(()=>$e.classList.remove('pop'),350);
      $e.textContent=d.elo.toLocaleString('en-US');
      $s.textContent='S4 - '+(d.ts||'');
      if(d.diff&&d.diff!==0&&last!==null){{
        $b.textContent=(d.diff>0?'+':'')+d.diff;
        $b.className=d.diff>0?'u':'dn';
        setTimeout(()=>{{$b.className='';$b.style.display='none';}},8000);
      }}
      last=d.elo;
    }}else{{$s.textContent='ceka ELO...';}}
  }}catch(e){{$s.textContent='retry...';}}
}}
poll();setInterval(poll,15000);
</script></body></html>"""

    response = app.make_response(html)
    response.headers["Content-Type"] = "text/html; charset=utf-8"
    response.headers["ngrok-skip-browser-warning"] = "true"
    response.headers["Access-Control-Allow-Origin"] = "*"
    return response


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)