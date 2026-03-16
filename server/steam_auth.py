import requests
import os

STEAM_API_KEY = os.environ.get("STEAM_API_KEY", "")


def validate_steam_login(params):
    """Verifikuje Steam OpenID response."""
    check_params = dict(params)
    check_params["openid.mode"] = "check_authentication"
    resp = requests.post("https://steamcommunity.com/openid/login", data=check_params, timeout=10)
    return "is_valid:true" in resp.text


def get_steam_profile(steam_id):
    """Dohvata Steam profil podatke."""
    # Pokusaj sa API kljucem ako postoji
    if STEAM_API_KEY:
        try:
            url  = f"https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v2/?key={STEAM_API_KEY}&steamids={steam_id}"
            resp = requests.get(url, timeout=10)
            data = resp.json()
            players = data.get("response", {}).get("players", [])
            if players:
                return players[0]
        except Exception as e:
            print(f"[Steam] Profile API greska: {e}")

    # Fallback: javni XML profil (ne zahtijeva API kljuc)
    try:
        import xml.etree.ElementTree as ET
        url  = f"https://steamcommunity.com/profiles/{steam_id}/?xml=1"
        resp = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        root = ET.fromstring(resp.content)
        name   = root.findtext("steamID") or f"Player_{steam_id[-4:]}"
        avatar = root.findtext("avatarFull") or root.findtext("avatarMedium") or ""
        print(f"[Steam] XML profil: {name}, avatar: {bool(avatar)}")
        return {"personaname": name, "avatarfull": avatar}
    except Exception as e:
        print(f"[Steam] XML profil greska: {e}")

    return {"personaname": f"Player_{steam_id[-4:]}", "avatarfull": ""}