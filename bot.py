"""
BullBot — Telegram basketball prediction bot
================================================
Gives a realistic, data-backed prediction (win probability, projected
score, and a full stats breakdown) instead of fake "99% accurate" picks.

No model can hit 99% on basketball outcomes — even the best sportsbooks
and analytics shops land around 65-75% on straight win/loss picks. This
bot is built to be genuinely useful within that reality: it shows you
the numbers behind each prediction so you can judge confidence yourself.

Data sources
------------
- NBA: balldontlie.io (free, no API key required)
- Other leagues (EuroLeague, NCAA, NBL, etc.): API-Basketball via
  RapidAPI (free tier available, requires a key — see README.md)

Commands
--------
/predict <Team A> vs <Team B> [league]   e.g. /predict Lakers vs Celtics
/today [league]                          today's games for a league
/leagues                                 list supported league codes
/help
"""

import os
import logging
import math
from datetime import date
from dataclasses import dataclass, field

import requests
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("hoopsbot")

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
RAPIDAPI_KEY = os.environ.get("RAPIDAPI_KEY")  # optional, for non-NBA leagues
BALLDONTLIE_API_KEY = os.environ.get("BALLDONTLIE_API_KEY")  # required, free — see README.md

BALLDONTLIE_BASE = "https://api.balldontlie.io/v1"


def _bdl_headers():
    return {"Authorization": BALLDONTLIE_API_KEY} if BALLDONTLIE_API_KEY else {}
APIBASKETBALL_BASE = "https://api-basketball.p.rapidapi.com"

LEAGUE_HELP = (
    "Supported league codes:\n"
    "  nba        - NBA (works out of the box)\n"
    "  euroleague - EuroLeague (needs RAPIDAPI_KEY)\n"
    "  ncaa       - NCAA Men's D1 (needs RAPIDAPI_KEY)\n"
    "  nbl        - Australian NBL (needs RAPIDAPI_KEY)\n"
    "Default league is nba if you don't specify one."
)


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------

@dataclass
class TeamStats:
    name: str
    games_played: int = 0
    wins: int = 0
    losses: int = 0
    pts_for_avg: float = 0.0
    pts_against_avg: float = 0.0
    last10_wins: int = 0
    last10_losses: int = 0
    home_win_pct: float | None = None
    away_win_pct: float | None = None

    @property
    def win_pct(self) -> float:
        return self.wins / self.games_played if self.games_played else 0.5

    @property
    def net_rating(self) -> float:
        return self.pts_for_avg - self.pts_against_avg

    @property
    def last10_pct(self) -> float:
        total = self.last10_wins + self.last10_losses
        return self.last10_wins / total if total else self.win_pct


@dataclass
class Prediction:
    team_a: TeamStats
    team_b: TeamStats
    prob_a: float
    prob_b: float
    proj_score_a: float
    proj_score_b: float
    factors: dict = field(default_factory=dict)
    h2h: "H2HStats | None" = None


@dataclass
class H2HStats:
    games_checked: int = 0
    a_wins: int = 0
    b_wins: int = 0
    a_avg_pts: float = 0.0
    b_avg_pts: float = 0.0
    last_meeting: str | None = None
    last_meeting_result: str | None = None

    @property
    def a_win_pct(self) -> float | None:
        return (self.a_wins / self.games_checked) if self.games_checked else None


# --------------------------------------------------------------------------
# NBA data via balldontlie.io (free, no key)
# --------------------------------------------------------------------------

def _bdl_find_team(name: str) -> dict | None:
    resp = requests.get(f"{BALLDONTLIE_BASE}/teams", headers=_bdl_headers(), timeout=10)
    resp.raise_for_status()
    teams = resp.json().get("data", [])
    name_lower = name.lower()
    for t in teams:
        if (
            name_lower in t["full_name"].lower()
            or name_lower in t["name"].lower()
            or name_lower == t["abbreviation"].lower()
        ):
            return t
    return None


def _bdl_team_stats(team_id: int, season: int) -> TeamStats:
    games, page = [], 1
    while True:
        resp = requests.get(
            f"{BALLDONTLIE_BASE}/games",
            params={"seasons[]": season, "team_ids[]": team_id, "per_page": 100, "page": page},
            headers=_bdl_headers(),
            timeout=10,
        )
        resp.raise_for_status()
        payload = resp.json()
        games.extend(payload.get("data", []))
        meta = payload.get("meta", {})
        if not meta.get("next_page"):
            break
        page = meta["next_page"]

    completed = [g for g in games if g.get("status") == "Final"]
    completed.sort(key=lambda g: g["date"])

    stats = TeamStats(name="")
    pts_for, pts_against = [], []
    home_results, away_results = [], []

    for g in completed:
        is_home = g["home_team"]["id"] == team_id
        team_score = g["home_team_score"] if is_home else g["visitor_team_score"]
        opp_score = g["visitor_team_score"] if is_home else g["home_team_score"]
        pts_for.append(team_score)
        pts_against.append(opp_score)
        won = team_score > opp_score
        (home_results if is_home else away_results).append(won)

    stats.games_played = len(completed)
    stats.wins = sum(1 for pf, pa in zip(pts_for, pts_against) if pf > pa)
    stats.losses = stats.games_played - stats.wins
    stats.pts_for_avg = sum(pts_for) / len(pts_for) if pts_for else 0
    stats.pts_against_avg = sum(pts_against) / len(pts_against) if pts_against else 0

    last10 = list(zip(pts_for, pts_against))[-10:]
    stats.last10_wins = sum(1 for pf, pa in last10 if pf > pa)
    stats.last10_losses = len(last10) - stats.last10_wins

    stats.home_win_pct = (sum(home_results) / len(home_results)) if home_results else None
    stats.away_win_pct = (sum(away_results) / len(away_results)) if away_results else None

    return stats


def get_nba_matchup(team_a_name: str, team_b_name: str, season: int | None = None) -> tuple[TeamStats, TeamStats]:
    if not BALLDONTLIE_API_KEY:
        raise ValueError(
            "NBA data requires a free BALLDONTLIE_API_KEY. Get one at balldontlie.io "
            "and add it as an environment variable. See README.md."
        )
    season = season or (date.today().year if date.today().month >= 10 else date.today().year - 1)
    ta = _bdl_find_team(team_a_name)
    tb = _bdl_find_team(team_b_name)
    if not ta or not tb:
        missing = team_a_name if not ta else team_b_name
        raise ValueError(f"Could not find NBA team matching '{missing}'.")

    stats_a = _bdl_team_stats(ta["id"], season)
    stats_a.name = ta["full_name"]
    stats_b = _bdl_team_stats(tb["id"], season)
    stats_b.name = tb["full_name"]
    return stats_a, stats_b


def get_nba_h2h(team_a_id: int, team_b_id: int, team_a_name: str, team_b_name: str, seasons_back: int = 3) -> H2HStats:
    """Pull head-to-head results between two NBA teams across recent seasons."""
    current_season = date.today().year if date.today().month >= 10 else date.today().year - 1
    seasons = [current_season - i for i in range(seasons_back)]

    games, page = [], 1
    while True:
        resp = requests.get(
            f"{BALLDONTLIE_BASE}/games",
            params={
                "seasons[]": seasons,
                "team_ids[]": [team_a_id, team_b_id],
                "per_page": 100,
                "page": page,
            },
            headers=_bdl_headers(),
            timeout=10,
        )
        resp.raise_for_status()
        payload = resp.json()
        games.extend(payload.get("data", []))
        meta = payload.get("meta", {})
        if not meta.get("next_page"):
            break
        page = meta["next_page"]

    # Keep only games where BOTH teams played each other, and only completed games
    matchups = [
        g for g in games
        if g.get("status") == "Final"
        and {g["home_team"]["id"], g["visitor_team"]["id"]} == {team_a_id, team_b_id}
    ]
    matchups.sort(key=lambda g: g["date"])

    h2h = H2HStats()
    h2h.games_checked = len(matchups)
    a_pts, b_pts = [], []

    for g in matchups:
        a_is_home = g["home_team"]["id"] == team_a_id
        a_score = g["home_team_score"] if a_is_home else g["visitor_team_score"]
        b_score = g["visitor_team_score"] if a_is_home else g["home_team_score"]
        a_pts.append(a_score)
        b_pts.append(b_score)
        if a_score > b_score:
            h2h.a_wins += 1
        else:
            h2h.b_wins += 1

    if matchups:
        h2h.a_avg_pts = sum(a_pts) / len(a_pts)
        h2h.b_avg_pts = sum(b_pts) / len(b_pts)
        last = matchups[-1]
        h2h.last_meeting = last["date"][:10]
        last_a_is_home = last["home_team"]["id"] == team_a_id
        last_a_score = last["home_team_score"] if last_a_is_home else last["visitor_team_score"]
        last_b_score = last["visitor_team_score"] if last_a_is_home else last["home_team_score"]
        winner = team_a_name if last_a_score > last_b_score else team_b_name
        h2h.last_meeting_result = f"{winner} won {max(last_a_score, last_b_score)}-{min(last_a_score, last_b_score)}"

    return h2h


def get_nba_live_games() -> list[dict]:
    """Today's NBA games with current status/score (live, scheduled, or final)."""
    resp = requests.get(
        f"{BALLDONTLIE_BASE}/games",
        params={"dates[]": date.today().isoformat()},
        headers=_bdl_headers(),
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json().get("data", [])


# --------------------------------------------------------------------------
# Other leagues via API-Basketball (RapidAPI) — requires RAPIDAPI_KEY
# --------------------------------------------------------------------------

LEAGUE_IDS = {
    "euroleague": 120,
    "ncaa": 116,
    "nbl": 44,
}


def _apib_headers():
    return {
        "x-rapidapi-key": RAPIDAPI_KEY,
        "x-rapidapi-host": "api-basketball.p.rapidapi.com",
    }


def _apib_find_team(name: str) -> dict | None:
    resp = requests.get(
        f"{APIBASKETBALL_BASE}/teams", params={"search": name}, headers=_apib_headers(), timeout=10
    )
    resp.raise_for_status()
    results = resp.json().get("response", [])
    return results[0] if results else None


def _apib_team_stats(team_id: int, league_id: int, season: str) -> TeamStats:
    resp = requests.get(
        f"{APIBASKETBALL_BASE}/statistics",
        params={"team": team_id, "league": league_id, "season": season},
        headers=_apib_headers(),
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json().get("response", {})
    games = data.get("games", {})
    points = data.get("points", {})

    stats = TeamStats(name="")
    stats.games_played = games.get("played", {}).get("all", 0) or 0
    stats.wins = games.get("wins", {}).get("all", {}).get("total", 0) or 0
    stats.losses = games.get("loses", {}).get("all", {}).get("total", 0) or 0
    stats.pts_for_avg = float(points.get("for", {}).get("average", {}).get("all", 0) or 0)
    stats.pts_against_avg = float(points.get("against", {}).get("average", {}).get("all", 0) or 0)
    home_played = games.get("played", {}).get("home", 0) or 0
    home_wins = games.get("wins", {}).get("home", {}).get("total", 0) or 0
    away_played = games.get("played", {}).get("away", 0) or 0
    away_wins = games.get("wins", {}).get("away", {}).get("total", 0) or 0
    stats.home_win_pct = (home_wins / home_played) if home_played else None
    stats.away_win_pct = (away_wins / away_played) if away_played else None
    stats.last10_wins = min(stats.wins, 10)
    stats.last10_losses = min(stats.losses, 10 - stats.last10_wins)
    return stats


def get_other_league_matchup(league: str, team_a_name: str, team_b_name: str, season: str | None = None) -> tuple[TeamStats, TeamStats]:
    if not RAPIDAPI_KEY:
        raise ValueError(
            f"'{league}' requires a RAPIDAPI_KEY (API-Basketball). See README.md for how to get a free key."
        )
    if league not in LEAGUE_IDS:
        raise ValueError(f"Unknown league '{league}'. " + LEAGUE_HELP)

    league_id = LEAGUE_IDS[league]
    season = season or str(date.today().year)

    ta = _apib_find_team(team_a_name)
    tb = _apib_find_team(team_b_name)
    if not ta or not tb:
        missing = team_a_name if not ta else team_b_name
        raise ValueError(f"Could not find team matching '{missing}' in {league}.")

    stats_a = _apib_team_stats(ta["id"], league_id, season)
    stats_a.name = ta["name"]
    stats_b = _apib_team_stats(tb["id"], league_id, season)
    stats_b.name = tb["name"]
    return stats_a, stats_b


# --------------------------------------------------------------------------
# Prediction model
# --------------------------------------------------------------------------
# This is a transparent, weighted heuristic — NOT a claim of near-certainty.
# Weights: season win% (0.35), recent form/last10 (0.20), net scoring
# margin (0.30), home/away split (0.15). Combined into a logistic curve
# so probabilities stay realistic instead of pinning to 0/100.

def _logistic(x: float) -> float:
    return 1 / (1 + math.exp(-x))


def predict_matchup(team_a: TeamStats, team_b: TeamStats, a_is_home: bool = True, h2h: H2HStats | None = None) -> Prediction:
    win_pct_edge = team_a.win_pct - team_b.win_pct
    form_edge = team_a.last10_pct - team_b.last10_pct
    net_rating_edge = team_a.net_rating - team_b.net_rating  # points

    a_split = (team_a.home_win_pct if a_is_home else team_a.away_win_pct)
    b_split = (team_b.away_win_pct if a_is_home else team_b.home_win_pct)
    split_edge = (a_split - b_split) if (a_split is not None and b_split is not None) else 0.0

    # H2H edge only counted if there's enough sample size (3+ meetings) to mean anything
    h2h_edge = 0.0
    if h2h and h2h.games_checked >= 3 and h2h.a_win_pct is not None:
        h2h_edge = h2h.a_win_pct - (1 - h2h.a_win_pct)  # ranges -1..1

    # Weights: win% 30%, recent form 18%, net rating 26%, home/away split 13%, H2H 13%
    score = (
        0.30 * win_pct_edge
        + 0.18 * form_edge
        + 0.26 * (net_rating_edge / 20)
        + 0.13 * split_edge
        + 0.13 * h2h_edge
    )

    # Home court adds a small, well-documented edge (~2.5-3 pts historically)
    home_bonus = 0.06 if a_is_home else -0.06
    prob_a = _logistic((score + home_bonus) * 4)  # *4 sharpens the curve modestly
    prob_a = min(max(prob_a, 0.05), 0.95)  # never claim near-certainty
    prob_b = 1 - prob_a

    avg_pace_a = team_a.pts_for_avg + team_a.pts_against_avg
    avg_pace_b = team_b.pts_for_avg + team_b.pts_against_avg
    game_pace = (avg_pace_a + avg_pace_b) / 4 if (avg_pace_a and avg_pace_b) else 220 / 2

    proj_a = (team_a.pts_for_avg + team_b.pts_against_avg) / 2
    proj_b = (team_b.pts_for_avg + team_a.pts_against_avg) / 2
    if a_is_home:
        proj_a += 1.5
    else:
        proj_b += 1.5

    return Prediction(
        team_a=team_a,
        team_b=team_b,
        prob_a=prob_a,
        prob_b=prob_b,
        proj_score_a=proj_a,
        proj_score_b=proj_b,
        factors={
            "win_pct_edge": win_pct_edge,
            "form_edge": form_edge,
            "net_rating_edge": net_rating_edge,
            "split_edge": split_edge,
            "h2h_edge": h2h_edge,
        },
        h2h=h2h,
    )


def format_prediction(pred: Prediction) -> str:
    a, b = pred.team_a, pred.team_b
    fav = a if pred.prob_a >= pred.prob_b else b
    fav_prob = max(pred.prob_a, pred.prob_b)

    lines = [
        f"🏀 *{a.name}* vs *{b.name}*",
        "",
        f"*Prediction:* {fav.name} favored ({fav_prob*100:.1f}% win probability)",
        f"*Projected score:* {a.name} {pred.proj_score_a:.0f} — {pred.proj_score_b:.0f} {b.name}",
        "",
        "*Win probability*",
        f"  {a.name}: {pred.prob_a*100:.1f}%",
        f"  {b.name}: {pred.prob_b*100:.1f}%",
        "",
        "*Season stats*",
        f"  {a.name}: {a.wins}-{a.losses} ({a.win_pct*100:.1f}%), {a.pts_for_avg:.1f} PPG / {a.pts_against_avg:.1f} opp PPG",
        f"  {b.name}: {b.wins}-{b.losses} ({b.win_pct*100:.1f}%), {b.pts_for_avg:.1f} PPG / {b.pts_against_avg:.1f} opp PPG",
        "",
        "*Recent form (last 10)*",
        f"  {a.name}: {a.last10_wins}-{a.last10_losses}",
        f"  {b.name}: {b.last10_wins}-{b.last10_losses}",
        "",
        "*Home/away splits*",
        f"  {a.name} home: {a.home_win_pct*100:.1f}%" if a.home_win_pct is not None else f"  {a.name} home: n/a",
        f"  {b.name} away: {b.away_win_pct*100:.1f}%" if b.away_win_pct is not None else f"  {b.name} away: n/a",
    ]

    if pred.h2h and pred.h2h.games_checked > 0:
        h = pred.h2h
        lines += [
            "",
            "*Head-to-head (recent seasons)*",
            f"  Record: {a.name} {h.a_wins} - {h.b_wins} {b.name} ({h.games_checked} games)",
            f"  Avg score in matchups: {a.name} {h.a_avg_pts:.1f} — {h.b_avg_pts:.1f} {b.name}",
        ]
        if h.last_meeting:
            lines.append(f"  Last meeting ({h.last_meeting}): {h.last_meeting_result}")
    else:
        lines += ["", "*Head-to-head*", "  No recent meetings found in the last few seasons."]

    lines += [
        "",
        "_Model: weighted blend of season win%, recent form, net scoring margin, "
        "home/away splits, and head-to-head history, run through a capped logistic "
        "curve (never above 95%)._",
        "_No prediction model hits a fixed accuracy rate like 8/10 — treat this as an "
        "informed estimate, not a guarantee._",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Telegram handlers
# --------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🏀 HoopsBot — data-backed basketball predictions.\n\n"
        "Usage:\n"
        "/predict Lakers vs Celtics\n"
        "/predict Barcelona vs Real Madrid euroleague\n"
        "/live — today's NBA scores (live + final)\n"
        "/leagues — see supported leagues\n\n"
        "Predictions include head-to-head history. This bot gives realistic "
        "probabilities (never claims a fixed accuracy rate — no model can back that up)."
    )


async def leagues_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(LEAGUE_HELP)


async def predict_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = " ".join(context.args)
    if not text or " vs " not in text.lower():
        await update.message.reply_text(
            "Usage: /predict <Team A> vs <Team B> [league]\n"
            "Example: /predict Lakers vs Celtics\n"
            "Example: /predict Gonzaga vs Duke ncaa"
        )
        return

    lower = text.lower()
    idx = lower.find(" vs ")
    team_a_raw = text[:idx].strip()
    rest = text[idx + 4:].strip()

    league = "nba"
    team_b_raw = rest
    for code in LEAGUE_IDS.keys():
        if rest.lower().endswith(code):
            league = code
            team_b_raw = rest[: -len(code)].strip()
            break

    await update.message.reply_text(f"Crunching numbers for {team_a_raw} vs {team_b_raw}...")

    try:
        if league == "nba":
            ta_info = _bdl_find_team(team_a_raw)
            tb_info = _bdl_find_team(team_b_raw)
            if not ta_info or not tb_info:
                missing = team_a_raw if not ta_info else team_b_raw
                await update.message.reply_text(f"Could not find NBA team matching '{missing}'.")
                return
            stats_a, stats_b = get_nba_matchup(team_a_raw, team_b_raw)
            h2h = get_nba_h2h(ta_info["id"], tb_info["id"], stats_a.name, stats_b.name)
        else:
            stats_a, stats_b = get_other_league_matchup(league, team_a_raw, team_b_raw)
            h2h = None

        if stats_a.games_played == 0 or stats_b.games_played == 0:
            await update.message.reply_text(
                "Not enough game data yet for one of these teams this season "
                "(too early in the season, or wrong team name)."
            )
            return

        pred = predict_matchup(stats_a, stats_b, a_is_home=True, h2h=h2h)
        await update.message.reply_markdown(format_prediction(pred))

    except ValueError as e:
        await update.message.reply_text(str(e))
    except requests.RequestException as e:
        logger.exception("Data source error")
        await update.message.reply_text(f"Data source error, try again shortly: {e}")


async def live_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not BALLDONTLIE_API_KEY:
        await update.message.reply_text(
            "Live scores need a free BALLDONTLIE_API_KEY. See README.md."
        )
        return
    try:
        games = get_nba_live_games()
    except requests.RequestException as e:
        logger.exception("Live scores error")
        await update.message.reply_text(f"Couldn't fetch live scores right now: {e}")
        return

    if not games:
        await update.message.reply_text("No NBA games scheduled today.")
        return

    lines = ["🏀 *Today's NBA games*", ""]
    for g in games:
        home = g["home_team"]["full_name"]
        away = g["visitor_team"]["full_name"]
        status = g.get("status", "")
        home_score = g.get("home_team_score", 0)
        away_score = g.get("visitor_team_score", 0)

        if status == "Final":
            lines.append(f"✅ {away} {away_score} — {home_score} {home} (Final)")
        elif status in ("1st Qtr", "2nd Qtr", "3rd Qtr", "4th Qtr", "Halftime", "OT"):
            lines.append(f"🔴 LIVE: {away} {away_score} — {home_score} {home} ({status})")
        else:
            lines.append(f"🕐 {away} @ {home} — {status}")

    await update.message.reply_markdown("\n".join(lines))


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)


def main():
    if not TELEGRAM_TOKEN:
        raise RuntimeError(
            "Set the TELEGRAM_BOT_TOKEN environment variable (get one from @BotFather on Telegram)."
        )

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("leagues", leagues_cmd))
    app.add_handler(CommandHandler("predict", predict_cmd))
    app.add_handler(CommandHandler("live", live_cmd))

    logger.info("BullBot starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
