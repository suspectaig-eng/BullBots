"""
Thin wrapper around API-Football (v3.football.api-sports.io).

Works with either:
  - a direct API-Sports key (header: x-apisports-key), or
  - a RapidAPI key (header: x-rapidapi-key + x-rapidapi-host)

Set API_FOOTBALL_KEY (and optionally API_FOOTBALL_PROVIDER=rapidapi) as
environment variables. See README.md for how to get a key.
"""

import os
import time
import requests

API_KEY = os.environ.get("API_FOOTBALL_KEY", "")
PROVIDER = os.environ.get("API_FOOTBALL_PROVIDER", "direct")  # "direct" or "rapidapi"

if PROVIDER == "rapidapi":
    BASE_URL = "https://api-football-v1.p.rapidapi.com/v3"
    HEADERS = {
        "x-rapidapi-host": "api-football-v1.p.rapidapi.com",
        "x-rapidapi-key": API_KEY,
    }
else:
    BASE_URL = "https://v3.football.api-sports.io"
    HEADERS = {"x-apisports-key": API_KEY}

_cache = {}
CACHE_TTL = 60  # seconds — avoid hammering the API on repeated commands


def _get(endpoint, params=None, ttl=CACHE_TTL):
    """GET with a small in-memory cache. Returns the 'response' list from the API."""
    params = params or {}
    cache_key = (endpoint, tuple(sorted(params.items())))
    now = time.time()

    if cache_key in _cache:
        cached_at, data = _cache[cache_key]
        if now - cached_at < ttl:
            return data

    if not API_KEY:
        raise RuntimeError(
            "No API_FOOTBALL_KEY set. Get a free key at https://www.api-football.com "
            "or via RapidAPI, then set it as an environment variable."
        )

    resp = requests.get(f"{BASE_URL}/{endpoint}", headers=HEADERS, params=params, timeout=15)
    resp.raise_for_status()
    payload = resp.json()

    if payload.get("errors"):
        # API-Football returns errors as a dict or list depending on the failure type
        raise RuntimeError(f"API error on {endpoint}: {payload['errors']}")

    data = payload.get("response", [])
    _cache[cache_key] = (now, data)
    return data


# ---------- Leagues ----------

def search_league(name):
    """Find league(s) matching a name. Returns list of {id, name, country, type, current_season}."""
    data = _get("leagues", {"name": name}, ttl=3600)
    out = []
    for item in data:
        league = item["league"]
        country = item.get("country", {})
        seasons = item.get("seasons", [])
        current = next((s["year"] for s in seasons if s.get("current")), None)
        out.append({
            "id": league["id"],
            "name": league["name"],
            "type": league.get("type"),
            "country": country.get("name"),
            "season": current,
        })
    return out


# ---------- Teams ----------

def search_team(name):
    """Find team(s) matching a name. Returns list of {id, name, country}."""
    data = _get("teams", {"search": name}, ttl=3600)
    out = []
    for item in data:
        t = item["team"]
        out.append({"id": t["id"], "name": t["name"], "country": t.get("country")})
    return out


def team_statistics(team_id, league_id, season):
    """Season-long stats for a team in a given league: form, goals for/against, etc."""
    data = _get("teams/statistics", {"team": team_id, "league": league_id, "season": season}, ttl=1800)
    return data if isinstance(data, dict) else (data[0] if data else {})


def recent_fixtures(team_id, count=10):
    """Last N finished fixtures for a team, across any league."""
    return _get("fixtures", {"team": team_id, "last": count}, ttl=1800)


def head_to_head(team1_id, team2_id, count=10):
    """Last N meetings between two teams."""
    return _get("fixtures/headtohead", {"h2h": f"{team1_id}-{team2_id}", "last": count}, ttl=1800)


# ---------- Fixtures / live ----------

def live_fixtures(league_id=None):
    """Currently live fixtures, optionally filtered to one league."""
    params = {"live": "all"}
    if league_id:
        params["league"] = league_id
    return _get("fixtures", params, ttl=15)  # short TTL — this is live data


def fixtures_today(league_id=None):
    from datetime import date
    params = {"date": date.today().isoformat()}
    if league_id:
        params["league"] = league_id
    return _get("fixtures", params, ttl=60)


def upcoming_fixture_between(team1_id, team2_id, days_ahead=14):
    """Find the next scheduled fixture between two teams, if any, within N days."""
    from datetime import date, timedelta
    today = date.today()
    data = _get("fixtures", {
        "team": team1_id,
        "from": today.isoformat(),
        "to": (today + timedelta(days=days_ahead)).isoformat(),
    }, ttl=300)
    for fx in data:
        teams = fx["teams"]
        ids = {teams["home"]["id"], teams["away"]["id"]}
        if team2_id in ids:
            return fx
    return None


def fixture_statistics(fixture_id):
    """Live/final match stats: shots, possession, cards, corners, etc."""
    return _get("fixtures/statistics", {"fixture": fixture_id}, ttl=30)


def api_prediction(fixture_id):
    """API-Football's own algorithmic prediction for a fixture (used as one input signal, not the final word)."""
    data = _get("predictions", {"fixture": fixture_id}, ttl=1800)
    return data[0] if data else None
