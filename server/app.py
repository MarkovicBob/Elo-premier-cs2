"""
ELO.LIVE - CS2 Premier ELO Overlay
=====================================
Backend: Flask + Steam OpenID + SQLite/PostgreSQL
"""

from flask import Flask, redirect, request, session, jsonify, send_from_directory
import requests, re, os, json, threading, time
from datetime import datetime, timezone
from dotenv import load_dotenv
from zoneinfo import ZoneInfo

BASE_DIR = os.path.dirname(__file__)
load_dotenv(os.path.join(BASE_DIR, ".env"))

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
        ("today_wins",     "INTEGER DEFAULT 0"),
        ("today_losses",   "INTEGER DEFAULT 0"),
        ("today_ties",     "INTEGER DEFAULT 0"),
        ("today_matches",  "INTEGER DEFAULT 0"),
        ("today_date",     "VARCHAR(10)"),
    ]
    with db.engine.connect() as conn:
        dialect = db.engine.dialect.name  # 'sqlite' ili 'postgresql'
        if dialect == "sqlite":
            existing = [row[1] for row in conn.execute(text("PRAGMA table_info(users)")).fetchall()]
        else:
            rows = conn.execute(text(
                "SELECT column_name FROM information_schema.columns WHERE table_name='users'"
            )).fetchall()
            existing = [row[0] for row in rows]
        for col, typ in new_cols:
            if col not in existing:
                pg_typ = typ.replace("INTEGER", "INTEGER").replace("VARCHAR(10)", "VARCHAR(10)")
                conn.execute(text(f"ALTER TABLE users ADD COLUMN {col} {pg_typ}"))
        conn.commit()

REFRESH_SEC = 30
APP_TIMEZONE_NAME = os.environ.get("APP_TIMEZONE", "Europe/Belgrade")
try:
    APP_TZ = ZoneInfo(APP_TIMEZONE_NAME)
except Exception:
    APP_TZ = datetime.now().astimezone().tzinfo or timezone.utc


# ── ELO FETCH ─────────────────────────────────────────────────────────────────

def utc_now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _as_utc(dt):
    if not dt:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def local_day_key(dt=None):
    base = _as_utc(dt) if dt else datetime.now(timezone.utc)
    return base.astimezone(APP_TZ).strftime("%Y-%m-%d")


def format_local_hm(dt):
    if not dt:
        return None
    return _as_utc(dt).astimezone(APP_TZ).strftime("%H:%M")


def parse_steam_gmt(value):
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S GMT").replace(tzinfo=timezone.utc).astimezone(APP_TZ)

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


def _cookie_value(sess, name):
    for cookie in sess.cookies:
        if cookie.name == name and "steamcommunity.com" in cookie.domain:
            return cookie.value
    return None


def _refresh_steam_cookies(cookies):
    """
    Ako sessionid istekne, steamLoginSecure cesto moze da izvuce novi sessionid
    bez novog rucnog copy/paste-a iz browsera.
    """
    if not cookies or not cookies.get("steamLoginSecure"):
        return cookies

    sess = requests.Session()
    sess.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://steamcommunity.com/",
    })
    if cookies.get("sessionid"):
        sess.cookies.set("sessionid", cookies["sessionid"], domain=".steamcommunity.com", path="/")
    sess.cookies.set("steamLoginSecure", cookies["steamLoginSecure"], domain=".steamcommunity.com", path="/")

    try:
        sess.get("https://steamcommunity.com/", timeout=12)
    except Exception:
        return cookies

    refreshed = dict(cookies)
    new_sessionid = _cookie_value(sess, "sessionid")
    new_login_secure = _cookie_value(sess, "steamLoginSecure")
    if new_sessionid:
        refreshed["sessionid"] = new_sessionid
    if new_login_secure:
        refreshed["steamLoginSecure"] = new_login_secure
    return refreshed


def _gcpd_json(sess, steam_id, tab, continue_token=None):
    url = f"https://steamcommunity.com/profiles/{steam_id}/gcpd/730/?tab={tab}&ajax=1"
    if continue_token:
        url += f"&continue_token={continue_token}"
    return sess.get(url, timeout=15).json()


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


def _elo_payload(elo, wins=None, ties=None, losses=None):
    payload = {"elo": int(elo)}
    if wins is not None:
        payload["wins"] = int(wins)
    if ties is not None:
        payload["ties"] = int(ties)
    if losses is not None:
        payload["losses"] = int(losses)
    return payload


def _split_premier_history_blocks(html):
    parts = re.split(r'<tr>\s*<td class="val_left">', html)
    return [f'<td class="val_left">{part}' for part in parts[1:]]


def _parse_premier_match(block, steam_id):
    ts_match = re.search(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} GMT)', block)
    score_match = re.search(r'csgo_scoreboard_score">(\d+)\s*:\s*(\d+)<', block)
    user_pos = block.find(f"/profiles/{steam_id}")
    score_pos = block.find("csgo_scoreboard_score")

    if not ts_match or not score_match or user_pos == -1 or score_pos == -1:
        return None

    left_score = int(score_match.group(1))
    right_score = int(score_match.group(2))
    player_on_left = user_pos < score_pos
    player_score = left_score if player_on_left else right_score
    enemy_score = right_score if player_on_left else left_score

    if player_score > enemy_score:
        result = "win"
    elif player_score < enemy_score:
        result = "loss"
    else:
        result = "tie"

    local_dt = parse_steam_gmt(ts_match.group(1))
    return {
        "local_day": local_dt.strftime("%Y-%m-%d"),
        "result": result,
    }


def _fetch_today_premier_stats(sess, steam_id, target_day=None, max_pages=6):
    target_day = target_day or local_day_key()
    wins = ties = losses = matches = 0
    continue_token = None

    for _ in range(max_pages):
        data = _gcpd_json(sess, steam_id, "matchhistorypremier", continue_token=continue_token)
        html = data.get("html", "")
        if not data.get("success") or not html:
            break

        stop = False
        for block in _split_premier_history_blocks(html):
            match = _parse_premier_match(block, steam_id)
            if not match:
                continue
            if match["local_day"] < target_day:
                stop = True
                break
            if match["local_day"] > target_day:
                continue

            matches += 1
            if match["result"] == "win":
                wins += 1
            elif match["result"] == "loss":
                losses += 1
            else:
                ties += 1

        if stop:
            break

        next_token = data.get("continue_token")
        if not next_token or str(next_token) == str(continue_token):
            break
        continue_token = next_token

    return {
        "today_wins": wins,
        "today_losses": losses,
        "today_ties": ties,
        "today_matches": matches,
        "today_date": target_day,
    }


def _daily_overlay_stats(user):
    today = local_day_key()

    if user.today_date == today:
        wins = user.today_wins or 0
        losses = user.today_losses or 0
        ties = user.today_ties or 0
    else:
        wins = max(0, (user.total_wins or 0) - (user.session_wins or 0))
        losses = max(0, (user.total_losses or 0) - (user.session_losses or 0))
        ties = max(0, (user.total_ties or 0) - (user.session_ties or 0))

    if user.session_date == today and user.current_elo is not None:
        start_elo = user.session_elo if user.session_elo is not None else user.current_elo
        elo_today = (user.current_elo or 0) - start_elo
    else:
        elo_today = 0

    return {
        "session_w": wins,
        "session_l": losses,
        "session_t": ties,
        "elo_today": elo_today,
    }


def _apply_elo_result(user, result, now=None):
    """Upisuje sveze ELO podatke u bazu bez obzira na izvor."""
    now = now or utc_now()
    today = local_day_key(now)
    new_day = user.session_date != today
    has_match_stats = all(key in result for key in ("wins", "ties", "losses"))

    elo = result["elo"]
    prev = user.current_elo
    prev_total_wins = user.total_wins or 0
    prev_total_losses = user.total_losses or 0
    prev_total_ties = user.total_ties or 0

    if new_day:
        user.session_date = today
        user.session_elo = prev if prev is not None else elo
        user.session_wins = prev_total_wins
        user.session_losses = prev_total_losses
        user.session_ties = prev_total_ties
    elif user.session_elo is None:
        user.session_elo = prev if prev is not None else elo
        user.session_date = today

    user.current_elo = elo
    user.prev_elo = prev
    user.elo_diff = (elo - prev) if prev is not None and prev != elo else None
    user.last_update = now

    if has_match_stats:
        user.total_wins = result["wins"]
        user.total_losses = result["losses"]
        user.total_ties = result["ties"]

    if result.get("today_date") == today:
        user.today_date = today
        user.today_wins = int(result.get("today_wins", 0))
        user.today_losses = int(result.get("today_losses", 0))
        user.today_ties = int(result.get("today_ties", 0))
        user.today_matches = int(result.get("today_matches", 0))
    elif new_day:
        user.today_date = today
        user.today_wins = 0
        user.today_losses = 0
        user.today_ties = 0
        user.today_matches = 0

    return elo


def fetch_elo(steam_id, cookies):
    """
    Prioritet:
      1. GCPD render endpoint              — kad korisnik poveze Steam cookies
      2. Steam Web API  (ISteamUserStats)  — fallback kad postoji STEAM_API_KEY
    """

    # ── 1. GCPD ajax endpoint (vraca JSON sa HTML tablicom) ───────────────────
    if cookies:
        refreshed_cookies = _refresh_steam_cookies(cookies)
        sess = _steam_session(refreshed_cookies)
        try:
            data = _gcpd_json(sess, steam_id, "matchmaking")
            if data.get("success"):
                html = data["html"]
                # Format: <td>Premier</td><td>wins</td><td>ties</td><td>losses</td><td>ELO</td>
                m = re.search(
                    r'<td>Premier</td><td>(\d+)</td><td>(\d+)</td><td>(\d+)</td><td>(\d{4,6})</td>',
                    html, re.IGNORECASE
                )
                if m:
                    wins = int(m.group(1))
                    ties = int(m.group(2))
                    losses = int(m.group(3))
                    elo = int(m.group(4))
                    if 1000 < elo < 50000:
                        payload = _elo_payload(elo, wins=wins, ties=ties, losses=losses)
                        payload.update(_fetch_today_premier_stats(sess, steam_id))
                        payload["refreshed_cookies"] = refreshed_cookies
                        print(f"[ELO] GCPD ajax {steam_id}: {elo} W:{wins} T:{ties} L:{losses}")
                        return payload
        except Exception as e:
            print(f"[ELO] GCPD ajax greska: {e}")

    # ── 2. Steam Web API ───────────────────────────────────────────────────────
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
                        payload = _elo_payload(val)
                        if cookies:
                            payload["refreshed_cookies"] = _refresh_steam_cookies(cookies)
                        return payload
                # Ispisi koji stat-ovi postoje radi debug-a
                rating_stats = {k: v for k, v in stats.items() if "rating" in k.lower()}
                print(f"[ELO] API {steam_id}: cs_rating nije tu. Rating stats: {rating_stats}")
        except Exception as e:
            print(f"[ELO] API greska: {e}")

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
            for user in users:
                try:
                    cookies = json.loads(user.steam_cookies) if user.steam_cookies else {}
                    result = fetch_elo(user.steam_id, cookies)
                    if not result:
                        continue

                    elo = _apply_elo_result(user, result)
                    if result.get("refreshed_cookies"):
                        user.steam_cookies = json.dumps(result["refreshed_cookies"])
                    db.session.commit()

                    if result.get("today_date") == local_day_key():
                        print(
                            f"[refresh] {user.username}: {elo:,} | "
                            f"W:{user.today_wins or 0} T:{user.today_ties or 0} L:{user.today_losses or 0} danas"
                        )
                    else:
                        print(f"[refresh] {user.username}: {elo:,}")
                except Exception as e:
                    db.session.rollback()
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


@app.route("/local/<path:filename>")
def serve_local(filename):
    """Servira fajlove iz server/local/ foldera (gitignored, za custom assets)."""
    local_dir = os.path.join(os.path.dirname(__file__), "local")
    return send_from_directory(local_dir, filename)


@app.route("/api/me")
def api_me():
    if "steam_id" not in session:
        return jsonify({"error": "not_logged_in"}), 401

    user = User.query.filter_by(steam_id=session["steam_id"]).first()
    if not user:
        return jsonify({"error": "user_not_found"}), 404

    daily = _daily_overlay_stats(user)

    return jsonify({
        "steam_id":    user.steam_id,
        "username":    user.username,
        "avatar":      user.avatar_url,
        "elo":         user.current_elo,
        "prev_elo":    user.prev_elo,
        "diff":        user.elo_diff,
        "last_update": format_local_hm(user.last_update),
        "has_setup":   bool(user.steam_cookies),
        "overlay_url": f"/overlay/{user.steam_id}",
        **daily,
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
    result = fetch_elo(session["steam_id"], cookies)

    if result:
        _apply_elo_result(user, result)
        if result.get("refreshed_cookies"):
            user.steam_cookies = json.dumps(result["refreshed_cookies"])
        db.session.commit()

    return jsonify({"success": True, "elo": result["elo"] if result else None})


@app.route("/api/elo/<steam_id>")
def api_elo(steam_id):
    """Javni endpoint za overlay."""
    user = User.query.filter_by(steam_id=steam_id).first()
    if not user:
        return jsonify({"error": "user_not_found"}), 404

    daily = _daily_overlay_stats(user)

    return jsonify({
        "elo":       user.current_elo,
        "prev":      user.prev_elo,
        "diff":      user.elo_diff,
        "ts":        format_local_hm(user.last_update),
        "username":  user.username,
        **daily,
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
    user.last_update = utc_now()
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
  {{min:30000,c:'#f5c518',bg:'linear-gradient(to right,#f5c518 0%,#120d00 100%)',glow:'rgba(245,197,24,.7)' }},
  {{min:25000,c:'#ef4444',bg:'linear-gradient(to right,#ef4444 0%,#140000 100%)',glow:'rgba(239,68,68,.7)' }},
  {{min:20000,c:'#ff50ff',bg:'linear-gradient(to right,#ff50ff 0%,#1c0020 100%)',glow:'rgba(255,80,255,.7)'}},
  {{min:15000,c:'#a855f7',bg:'linear-gradient(to right,#a855f7 0%,#0e0018 100%)',glow:'rgba(168,85,247,.7)'}},
  {{min:10000,c:'#4a6cf7',bg:'linear-gradient(to right,#4a6cf7 0%,#020514 100%)',glow:'rgba(74,108,247,.7)' }},
  {{min:5000, c:'#38bdf8',bg:'linear-gradient(to right,#38bdf8 0%,#000e1a 100%)',glow:'rgba(56,189,248,.7)'}},
  {{min:0,    c:'#9ca3af',bg:'linear-gradient(to right,#9ca3af 0%,#080808 100%)',glow:'rgba(156,163,175,.5)'}},
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
      if($sw) $sw.textContent='W:'+(d.session_w??0);
      if($sl2) $sl2.textContent='L:'+(d.session_l??0);
      if($st) $st.textContent='T:'+(d.session_t??0);
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
@keyframes bl{0%,100%{opacity:1}50%{opacity:.3}}@keyframes pop{0%{transform:scale(1)}50%{transform:scale(1.18)}100%{transform:scale(1)}}
.bl{animation:bl 1.4s ease-in-out infinite}.pop{animation:pop .3s ease}
#b{font-size:13px;font-weight:700;padding:3px 9px;border-radius:3px;display:none;position:absolute;top:32px;right:32px}
.u{background:rgba(0,200,100,.18);color:#00cc66;display:block}.dn{background:rgba(220,50,50,.18);color:#ff4444;display:block}"""

    FONTS = "https://fonts.googleapis.com/css2?family=Bebas+Neue&family=Barlow:wght@400;700;900&family=Share+Tech+Mono&family=Orbitron:wght@700&display=swap"

    # CS2 parallelogram badge CSS — shared across all styles
    CS2 = ("html,body{width:1080px;height:1080px;margin:0;padding:0;overflow:hidden}"
           "body{display:flex;align-items:flex-end;justify-content:flex-end;"
           "padding:0 50px 50px 0;position:relative}"
           # Outer parallelogram shape via skewX
           "#badge{--c:#9ca3af;--bg:linear-gradient(to right,#9ca3af 0%,#080808 100%);"
           "--glow:rgba(156,163,175,.5);"
           "display:inline-block;transform:skewX(-10deg);border-radius:4px;"
           "background:var(--bg);overflow:hidden;position:relative;"
           "box-shadow:0 8px 40px rgba(0,0,0,.8),0 0 70px var(--glow)}"
           # // accent strips via ::before (two bright vertical lines)
           "#badge::before{content:'';position:absolute;left:20px;top:0;bottom:0;"
           "width:5px;background:rgba(255,255,255,.9);"
           "box-shadow:13px 0 0 5px rgba(255,255,255,.9);z-index:3;pointer-events:none}"
           # Counter-skew inner so text is straight
           ".bi{transform:skewX(10deg);display:flex;flex-direction:column;"
           "padding:12px 40px 14px 58px;position:relative;z-index:1}"
           ".micro{font-family:'Barlow',sans-serif;font-size:12px;font-weight:700;"
           "letter-spacing:4px;color:rgba(255,255,255,.4);text-transform:uppercase;margin-bottom:4px}"
           ".nr{display:flex;align-items:baseline}"
           ".big{font-family:'Bebas Neue',sans-serif;font-size:96px;line-height:.9;"
           "color:#fff;letter-spacing:-1px}"
           ".sep{font-family:'Bebas Neue',sans-serif;font-size:96px;color:#fff;margin:0 1px}"
           ".small{font-family:'Bebas Neue',sans-serif;font-size:82px;line-height:.9;"
           "color:rgba(255,255,255,.88)}"
           "#s{display:none}")

    BADGE_HTML = """<div id="badge" data-cssvar><div class="bi">
<div class="micro">PREMIER ELO</div>
<div class="nr"><span id="e1" class="big bl">--</span><span class="sep">,</span><span id="e2" class="small">---</span></div>
</div></div>"""

    if sid == 1:
        # Style 1 — samo CS2 badge
        css = CS2
        body = BADGE_HTML + """<div id="b" style="display:none"></div><div id="s" style="display:none"></div>"""

    elif sid == 2:
        # Style 2 — CS2 badge + W/T/L danas
        css = (CS2 +
               "#badge .bi{padding:40px 44px 38px 58px}"
               "#row{display:flex;gap:14px;margin-top:16px;padding-top:16px;align-items:center;"
               "font-family:'Barlow',sans-serif;border-top:1px solid rgba(255,255,255,.07)}"
               ".wlt{font-size:34px;font-weight:900;letter-spacing:.5px;line-height:1}"
               "#sw{color:#00cc66}#sl2{color:#ff4444}#st{color:rgba(255,255,255,.4)}"
               ".div{color:rgba(255,255,255,.15);font-size:28px;font-weight:300;margin:0 2px}")
        body = ("""<div id="badge" data-cssvar><div class="bi">
<div class="micro">PREMIER ELO</div>
<div class="nr"><span id="e1" class="big bl">--</span><span class="sep">,</span><span id="e2" class="small">---</span></div>
<div id="row">
<span id="sw" class="wlt">W:-</span><span class="div">|</span>
<span id="st" class="wlt">T:-</span><span class="div">|</span>
<span id="sl2" class="wlt">L:-</span>
</div>
</div></div>""" + """<div id="b" style="display:none"></div><div id="s" style="display:none"></div>""")

    elif sid == 3:
        # Style 3 — CS2 badge + ELO diff danas
        css = (CS2 +
               "#badge .bi{padding:40px 44px 38px 58px}"
               "#row{display:flex;align-items:center;gap:14px;margin-top:16px;padding-top:16px;"
               "font-family:'Barlow',sans-serif;border-top:1px solid rgba(255,255,255,.07)}"
               ".lbl{font-size:13px;font-weight:700;letter-spacing:4px;"
               "color:rgba(255,255,255,.3);text-transform:uppercase}"
               "#elo_today{font-size:36px;font-weight:900;letter-spacing:.5px;line-height:1}")
        body = ("""<div id="badge" data-cssvar><div class="bi">
<div class="micro">PREMIER ELO</div>
<div class="nr"><span id="e1" class="big bl">--</span><span class="sep">,</span><span id="e2" class="small">---</span></div>
<div id="row">
<span class="lbl">DANAS</span><span id="elo_today" style="color:#9e9e9e">+0</span>
</div>
</div></div>""" + """<div id="b" style="display:none"></div><div id="s" style="display:none"></div>""")

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
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
