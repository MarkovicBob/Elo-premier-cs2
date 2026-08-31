"""
ELO.LIVE - CS2 Premier ELO Overlay
=====================================
Backend: FastAPI + Steam OpenID + SQLite/PostgreSQL
"""

from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import threading
import time
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
import requests
from starlette.middleware.sessions import SessionMiddleware
import uvicorn

BASE_DIR = os.path.dirname(__file__)
CLIENT_DIR = Path(BASE_DIR).parent.joinpath("client").resolve()
LOCAL_DIR = Path(BASE_DIR).joinpath("local").resolve()
load_dotenv(os.path.join(BASE_DIR, ".env"))

from db import db, User
from steam_auth import get_steam_profile


_refresh_stop_event = threading.Event()


@asynccontextmanager
async def lifespan(_app):
    initialize_database()
    _refresh_stop_event.clear()
    refresh_thread = threading.Thread(target=refresh_all_users, daemon=True)
    refresh_thread.start()
    try:
        yield
    finally:
        _refresh_stop_event.set()
        refresh_thread.join(timeout=2)
        db.session.remove()


app = FastAPI(title="ELO.LIVE", lifespan=lifespan)

# ── CONFIG ────────────────────────────────────────────────────────────────────
_db_url = os.environ.get("DATABASE_URL", "sqlite:///elo.db")
if _db_url.startswith("postgres://"):
    _db_url = _db_url.replace("postgres://", "postgresql+pg8000://", 1)
elif _db_url.startswith("postgresql://") and "+pg8000" not in _db_url:
    _db_url = _db_url.replace("postgresql://", "postgresql+pg8000://", 1)

# Zadrzavamo dosadasnju lokaciju relativne SQLite baze u server/instance/
# kako migracija ne bi napravila praznu bazu na drugom mjestu.
if _db_url.startswith("sqlite:///"):
    sqlite_path = _db_url[len("sqlite:///"):]
    if not os.path.isabs(sqlite_path):
        sqlite_file = Path(BASE_DIR).joinpath("instance", sqlite_path).resolve()
        sqlite_file.parent.mkdir(parents=True, exist_ok=True)
        _db_url = f"sqlite:///{sqlite_file.as_posix()}"

db.configure(_db_url)

app.add_middleware(
    SessionMiddleware,
    secret_key=os.environ.get("SECRET_KEY", "dev-secret-change-in-prod"),
    same_site="lax",
    https_only=os.environ.get("RAILWAY_ENVIRONMENT") is not None,
)


def initialize_database():
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
ON_DEMAND_REFRESH_SEC = max(10, int(os.environ.get("ON_DEMAND_REFRESH_SEC", "15")))
APP_TIMEZONE_NAME = os.environ.get("APP_TIMEZONE", "Europe/Belgrade")
try:
    APP_TZ = ZoneInfo(APP_TIMEZONE_NAME)
except Exception:
    APP_TZ = datetime.now().astimezone().tzinfo or timezone.utc

_refresh_locks = {}
_refresh_locks_guard = threading.Lock()
_refresh_attempts = {}


# ── ELO FETCH ─────────────────────────────────────────────────────────────────


class SteamSessionExpiredError(Exception):
    pass

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
    resp = sess.get(url, timeout=15, allow_redirects=True)
    content_type = (resp.headers.get("content-type") or "").lower()

    if "/login/" in resp.url:
        raise SteamSessionExpiredError("Steam Community trazi novu prijavu.")

    if "application/json" in content_type:
        return resp.json()

    html = resp.text or ""
    redirected_away = resp.history and "/gcpd/" not in resp.url
    looks_signed_out = (
        "<title>Sign In</title>" in html
        or "Sign In" in html
        or "login_btn" in html
        or "account_pulldown" not in html
    )
    has_gcpd_content = "Premier" in html or "matchhistorypremier" in html or 'class="val_left"' in html

    if redirected_away or (looks_signed_out and not has_gcpd_content):
        raise SteamSessionExpiredError("Steam je odbio GCPD pristup sa sacuvanim cookies.")

    if has_gcpd_content:
        return {"success": True, "html": html}

    raise ValueError("GCPD ajax nije vratio ni JSON ni prepoznatljiv HTML s ELO podacima.")


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


def _elo_payload(elo, wins=None, ties=None, losses=None, source=None):
    payload = {"elo": int(elo)}
    if wins is not None:
        payload["wins"] = int(wins)
    if ties is not None:
        payload["ties"] = int(ties)
    if losses is not None:
        payload["losses"] = int(losses)
    if source:
        payload["source"] = source
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


def _get_refresh_lock(steam_id):
    with _refresh_locks_guard:
        lock = _refresh_locks.get(steam_id)
        if lock is None:
            lock = threading.Lock()
            _refresh_locks[steam_id] = lock
        return lock


def _seconds_since_update(last_update, now=None):
    if not last_update:
        return None
    now = now or utc_now()
    last = _as_utc(last_update).replace(tzinfo=None)
    return (now - last).total_seconds()


def _refresh_is_due(user, max_age, now=None):
    if max_age is None:
        return True
    if user.current_elo is None or user.last_update is None:
        return True
    age = _seconds_since_update(user.last_update, now=now)
    return age is None or age >= max_age


def _refresh_user_from_source(steam_id, max_age=None, min_interval=None, force=False):
    lock = _get_refresh_lock(steam_id)
    if not lock.acquire(blocking=False):
        return User.query.filter_by(steam_id=steam_id).first(), False

    try:
        user = User.query.filter_by(steam_id=steam_id).first()
        if not user:
            return None, False

        now = utc_now()
        if not force and not _refresh_is_due(user, max_age, now=now):
            return user, False

        throttle_window = min_interval if min_interval is not None else max_age
        if not force and throttle_window:
            last_attempt = _refresh_attempts.get(steam_id)
            now_monotonic = time.monotonic()
            if last_attempt is not None and (now_monotonic - last_attempt) < throttle_window:
                return user, False
            _refresh_attempts[steam_id] = now_monotonic
        else:
            _refresh_attempts[steam_id] = time.monotonic()

        cookies = json.loads(user.steam_cookies) if user.steam_cookies else {}
        result = fetch_elo(user.steam_id, cookies)
        if not result:
            return user, False

        _apply_elo_result(user, result, now=now)
        if result.get("refreshed_cookies"):
            user.steam_cookies = json.dumps(result["refreshed_cookies"])
        db.session.commit()
        return user, True
    except Exception as e:
        db.session.rollback()
        print(f"[refresh] Greska {steam_id}: {e}")
        return User.query.filter_by(steam_id=steam_id).first(), False
    finally:
        lock.release()


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
                        payload = _elo_payload(elo, wins=wins, ties=ties, losses=losses, source="gcpd")
                        payload.update(_fetch_today_premier_stats(sess, steam_id))
                        payload["refreshed_cookies"] = refreshed_cookies
                        print(f"[ELO] GCPD ajax {steam_id}: {elo} W:{wins} T:{ties} L:{losses}")
                        return payload
        except SteamSessionExpiredError as e:
            print(f"[ELO] Cookies istekli za {steam_id}: {e}")
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
                        payload = _elo_payload(val, source=f"steam_web_api:{stat}")
                        if cookies:
                            payload["refreshed_cookies"] = _refresh_steam_cookies(cookies)
                        return payload
                # Ispisi koji stat-ovi postoje radi debug-a
                rating_stats = {k: v for k, v in stats.items() if "rating" in k.lower()}
                print(f"[ELO] API {steam_id}: cs_rating nije tu. Rating stats: {rating_stats}")
            else:
                print(f"[ELO] API {steam_id}: HTTP {resp.status_code} {resp.text[:120]}")
        except Exception as e:
            # Requests izuzeci mogu sadrzati puni URL sa STEAM_API_KEY vrijednoscu.
            print(f"[ELO] API greska ({type(e).__name__}): Steam API nije dostupan.")

    print(f"[ELO] {steam_id}: ELO nije pronadjen. Dodaj STEAM_API_KEY ili osvjezi cookies.")
    return None


def refresh_all_users():
    """Background thread - osvezava ELO za sve korisnike svakih REFRESH_SEC sekundi."""
    while not _refresh_stop_event.is_set():
        try:
            has_api_key = bool(os.environ.get("STEAM_API_KEY", ""))
            # Sa API kljucem fetchujemo sve usere, bez njega samo one sa cookies
            if has_api_key:
                users = User.query.all()
            else:
                users = User.query.filter(User.steam_cookies.isnot(None)).all()
            for user in users:
                refreshed_user, refreshed = _refresh_user_from_source(user.steam_id, force=True)
                if not refreshed_user or not refreshed:
                    continue

                if refreshed_user.today_date == local_day_key():
                    print(
                        f"[refresh] {refreshed_user.username}: {refreshed_user.current_elo:,} | "
                        f"W:{refreshed_user.today_wins or 0} T:{refreshed_user.today_ties or 0} "
                        f"L:{refreshed_user.today_losses or 0} danas"
                    )
                else:
                    print(f"[refresh] {refreshed_user.username}: {refreshed_user.current_elo:,}")
        except Exception as exc:
            db.session.rollback()
            print(f"[refresh] Background greska: {exc}")
        finally:
            db.session.remove()

        _refresh_stop_event.wait(REFRESH_SEC)


@app.middleware("http")
async def database_and_cache_middleware(request, call_next):
    session_token = db.begin_request()
    try:
        response = await call_next(request)
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store, max-age=0"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response
    finally:
        db.end_request(session_token)


# ── ROUTES ────────────────────────────────────────────────────────────────────

@app.api_route("/", methods=["GET", "POST"])
def index(request: Request):
    if "steam_id" in request.session:
        return RedirectResponse("/dashboard", status_code=302)
    return FileResponse(CLIENT_DIR / "index.html")


@app.get("/login/steam")
def login_steam(request: Request):
    host_url = str(request.base_url)
    return_url = host_url.rstrip("/") + "/auth/steam/callback"
    steam_params = {
        "openid.ns": "http://specs.openid.net/auth/2.0",
        "openid.mode": "checkid_setup",
        "openid.return_to": return_url,
        "openid.realm": host_url,
        "openid.identity": "http://specs.openid.net/auth/2.0/identifier_select",
        "openid.claimed_id": "http://specs.openid.net/auth/2.0/identifier_select",
    }
    steam_url = f"https://steamcommunity.com/openid/login?{urlencode(steam_params)}"
    return RedirectResponse(steam_url, status_code=302)


@app.get("/auth/steam/callback")
def steam_callback(request: Request):
    params = dict(request.query_params)
    params["openid.mode"] = "check_authentication"
    resp = requests.post("https://steamcommunity.com/openid/login", data=params, timeout=10)
    if "is_valid:true" not in resp.text:
        return HTMLResponse("Steam login failed.", status_code=401)

    claimed_id = request.query_params.get("openid.claimed_id", "")
    steam_id   = claimed_id.split("/")[-1]
    if not steam_id.isdigit():
        return HTMLResponse("Invalid Steam ID.", status_code=400)

    profile = get_steam_profile(steam_id)

    user = User.query.filter_by(steam_id=steam_id).first()
    if not user:
        user = User(steam_id=steam_id)
        db.session.add(user)

    user.username   = profile.get("personaname", f"Player_{steam_id[-4:]}")
    user.avatar_url = profile.get("avatarfull", "")
    db.session.commit()

    request.session["steam_id"] = steam_id
    request.session["username"] = user.username
    request.session["avatar"]   = user.avatar_url

    return RedirectResponse("/dashboard", status_code=302)


@app.get("/dashboard")
def dashboard(request: Request):
    if "steam_id" not in request.session:
        return RedirectResponse("/", status_code=302)
    return FileResponse(CLIENT_DIR / "dashboard.html")


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/", status_code=302)


# ── API ───────────────────────────────────────────────────────────────────────


@app.get("/api/status")
def api_status():
    return {"has_api_key": bool(os.environ.get("STEAM_API_KEY", ""))}


@app.get("/local/{filename:path}")
def serve_local(filename):
    """Servira fajlove iz server/local/ foldera (gitignored, za custom assets)."""
    target = LOCAL_DIR.joinpath(filename).resolve()
    try:
        target.relative_to(LOCAL_DIR)
    except ValueError:
        return HTMLResponse("Not found", status_code=404)
    if not target.is_file():
        return HTMLResponse("Not found", status_code=404)
    return FileResponse(target)


@app.get("/api/me")
def api_me(request: Request):
    if "steam_id" not in request.session:
        return JSONResponse({"error": "not_logged_in"}, status_code=401)

    user, _ = _refresh_user_from_source(
        request.session["steam_id"],
        max_age=ON_DEMAND_REFRESH_SEC,
        min_interval=ON_DEMAND_REFRESH_SEC,
    )
    if not user:
        return JSONResponse({"error": "user_not_found"}, status_code=404)

    daily = _daily_overlay_stats(user)

    return {
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
    }


@app.post("/api/save-cookies")
def save_cookies(request: Request, data: dict):
    """Korisnik unosi Steam cookies kao alternativu API kljucu."""
    if "steam_id" not in request.session:
        return JSONResponse({"error": "not_logged_in"}, status_code=401)

    session_id = data.get("sessionid", "").strip()
    login_sec  = data.get("steamLoginSecure", "").strip()

    if not session_id or not login_sec:
        return JSONResponse(
            {"error_code": "missing_fields", "error": "Fill in both fields."},
            status_code=400,
        )

    user = User.query.filter_by(steam_id=request.session["steam_id"]).first()
    cookies = {"sessionid": session_id, "steamLoginSecure": login_sec}
    result = fetch_elo(request.session["steam_id"], cookies)

    if not result or result.get("source") != "gcpd":
        return JSONResponse(
            {
                "error_code": "invalid_cookies",
                "error": "The Steam cookies are invalid or cannot access the GCPD page. Copy fresh sessionid and steamLoginSecure values from your currently signed-in browser.",
            },
            status_code=400,
        )

    user.steam_cookies = json.dumps(result.get("refreshed_cookies") or cookies)
    _apply_elo_result(user, result)
    db.session.commit()

    return {"success": True, "elo": result["elo"]}


@app.get("/api/elo/{steam_id}")
def api_elo(steam_id):
    """Javni endpoint za overlay."""
    user, _ = _refresh_user_from_source(
        steam_id,
        max_age=ON_DEMAND_REFRESH_SEC,
        min_interval=ON_DEMAND_REFRESH_SEC,
    )
    if not user:
        return JSONResponse({"error": "user_not_found"}, status_code=404)

    daily = _daily_overlay_stats(user)

    return {
        "elo":       user.current_elo,
        "prev":      user.prev_elo,
        "diff":      user.elo_diff,
        "ts":        format_local_hm(user.last_update),
        "username":  user.username,
        **daily,
    }


@app.post("/api/set-elo")
def set_elo_manual(request: Request, data: dict):
    """Ručno postavljanje ELO-a kad automatski ne radi."""
    if "steam_id" not in request.session:
        return JSONResponse({"error": "not_logged_in"}, status_code=401)
    elo = data.get("elo")
    try:
        elo = int(elo)
        if not (1000 < elo < 50000):
            raise ValueError
    except (TypeError, ValueError):
        return JSONResponse(
            {"error": "Invalid ELO (must be between 1000 and 50000)"},
            status_code=400,
        )

    user = User.query.filter_by(steam_id=request.session["steam_id"]).first()
    if not user:
        return JSONResponse({"error": "user_not_found"}, status_code=404)

    prev = user.current_elo
    user.current_elo = elo
    user.prev_elo    = prev
    user.elo_diff    = (elo - prev) if prev else None
    user.last_update = utc_now()
    db.session.commit()
    return {"success": True, "elo": elo}


# ── OVERLAY ───────────────────────────────────────────────────────────────────

@app.get("/overlay/{steam_id}")
def overlay(steam_id, request: Request):
    user = User.query.filter_by(steam_id=steam_id).first()
    if not user:
        return HTMLResponse("User not found", status_code=404)

    try:
        sid = max(1, min(10, int(request.query_params.get("style", 1))))
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
    const d=await fetch('/api/elo/{steam_id}?_='+Date.now(), {{cache:'no-store'}}).then(r=>r.json());
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
<span class="lbl">TODAY</span><span id="elo_today" style="color:#9e9e9e">+0</span>
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

    return HTMLResponse(
        html,
        headers={
            "ngrok-skip-browser-warning": "true",
            "Access-Control-Allow-Origin": "*",
            "Cache-Control": "no-store, max-age=0",
            "Pragma": "no-cache",
        },
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    uvicorn.run(app, host="0.0.0.0", port=port)
