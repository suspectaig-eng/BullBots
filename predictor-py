"""
Prediction engine.

Produces a probability estimate for a fixture from several real signals:
  - each team's recent form (points per game, last 10)
  - goals for / against (attack & defense strength)
  - head-to-head history between the two teams
  - API-Football's own model prediction, as one additional input

This is a transparent, explainable blend — not a black box, and not a
claim of fixed accuracy. Every number returned traces back to real data
pulled at request time.
"""

import api_football as af


def _points_per_game(fixtures, team_id):
    if not fixtures:
        return None, 0
    pts = 0
    for fx in fixtures:
        home = fx["teams"]["home"]
        away = fx["teams"]["away"]
        home_goals = fx["goals"]["home"]
        away_goals = fx["goals"]["away"]
        if home_goals is None or away_goals is None:
            continue
        is_home = home["id"] == team_id
        gf = home_goals if is_home else away_goals
        ga = away_goals if is_home else home_goals
        if gf > ga:
            pts += 3
        elif gf == ga:
            pts += 1
    return round(pts / len(fixtures), 2), len(fixtures)


def _goal_rates(fixtures, team_id):
    gf_total, ga_total, n = 0, 0, 0
    for fx in fixtures:
        home = fx["teams"]["home"]
        home_goals = fx["goals"]["home"]
        away_goals = fx["goals"]["away"]
        if home_goals is None or away_goals is None:
            continue
        is_home = home["id"] == team_id
        gf = home_goals if is_home else away_goals
        ga = away_goals if is_home else home_goals
        gf_total += gf
        ga_total += ga
        n += 1
    if n == 0:
        return 0, 0
    return round(gf_total / n, 2), round(ga_total / n, 2)


def _h2h_lean(h2h_fixtures, team1_id, team2_id):
    """Returns (team1_wins, draws, team2_wins) over the sample."""
    t1w = t2w = draws = 0
    for fx in h2h_fixtures:
        home = fx["teams"]["home"]
        home_goals = fx["goals"]["home"]
        away_goals = fx["goals"]["away"]
        if home_goals is None or away_goals is None:
            continue
        home_id = home["id"]
        if home_goals == away_goals:
            draws += 1
        else:
            winner_id = home_id if home_goals > away_goals else (
                fx["teams"]["away"]["id"]
            )
            if winner_id == team1_id:
                t1w += 1
            else:
                t2w += 1
    return t1w, draws, t2w


def build_prediction(team1, team2, league_id, season, fixture_id=None):
    """
    team1, team2: dicts with 'id' and 'name' from api_football.search_team
    Returns a dict ready for formatting into a Telegram message.
    """
    t1_recent = af.recent_fixtures(team1["id"], count=10)
    t2_recent = af.recent_fixtures(team2["id"], count=10)
    h2h = af.head_to_head(team1["id"], team2["id"], count=10)

    t1_ppg, t1_n = _points_per_game(t1_recent, team1["id"])
    t2_ppg, t2_n = _points_per_game(t2_recent, team2["id"])
    t1_gf, t1_ga = _goal_rates(t1_recent, team1["id"])
    t2_gf, t2_ga = _goal_rates(t2_recent, team2["id"])
    t1_h2h_w, draws_h2h, t2_h2h_w = _h2h_lean(h2h, team1["id"], team2["id"])

    # --- Blend into a win/draw/win probability estimate ---
    # Base signal: relative "strength" from form (ppg) + attacking/defensive goal rates.
    t1_strength = (t1_ppg or 1.0) + (t1_gf - t1_ga)
    t2_strength = (t2_ppg or 1.0) + (t2_gf - t2_ga)

    # Small home advantage bump (team1 assumed home side by convention here)
    t1_strength += 0.25

    # H2H nudges the raw strength slightly if there's a real sample
    h2h_total = t1_h2h_w + draws_h2h + t2_h2h_w
    if h2h_total >= 3:
        t1_strength += 0.15 * (t1_h2h_w - t2_h2h_w) / h2h_total
        t2_strength += 0.15 * (t2_h2h_w - t1_h2h_w) / h2h_total

    total = max(t1_strength, 0.01) + max(t2_strength, 0.01)
    raw_t1 = max(t1_strength, 0.01) / total
    raw_t2 = max(t2_strength, 0.01) / total

    # Reserve a draw probability band based on how close the two sides are
    closeness = 1 - abs(raw_t1 - raw_t2)  # 1.0 = dead even, 0 = huge mismatch
    draw_prob = 0.18 + 0.12 * closeness  # roughly 18-30%

    remaining = 1 - draw_prob
    win1_prob = raw_t1 * remaining
    win2_prob = raw_t2 * remaining

    # Optionally blend in API-Football's own model prediction as a cross-check
    api_pred = None
    if fixture_id:
        try:
            api_pred = af.api_prediction(fixture_id)
        except Exception:
            api_pred = None

    if api_pred and api_pred.get("predictions", {}).get("percent"):
        pct = api_pred["predictions"]["percent"]
        try:
            api_home = float(pct["home"].strip("%")) / 100
            api_draw = float(pct["draw"].strip("%")) / 100
            api_away = float(pct["away"].strip("%")) / 100
            # 60/40 blend: our transparent model + API's own model
            win1_prob = 0.6 * win1_prob + 0.4 * api_home
            draw_prob = 0.6 * draw_prob + 0.4 * api_draw
            win2_prob = 0.6 * win2_prob + 0.4 * api_away
        except (KeyError, ValueError):
            pass

    # Normalize to 100%
    s = win1_prob + draw_prob + win2_prob
    win1_prob, draw_prob, win2_prob = win1_prob / s, draw_prob / s, win2_prob / s

    return {
        "team1": team1["name"],
        "team2": team2["name"],
        "win1_pct": round(win1_prob * 100, 1),
        "draw_pct": round(draw_prob * 100, 1),
        "win2_pct": round(win2_prob * 100, 1),
        "stats": {
            "t1_form_ppg": t1_ppg, "t1_sample": t1_n,
            "t2_form_ppg": t2_ppg, "t2_sample": t2_n,
            "t1_goals_for": t1_gf, "t1_goals_against": t1_ga,
            "t2_goals_for": t2_gf, "t2_goals_against": t2_ga,
            "h2h_t1_wins": t1_h2h_w, "h2h_draws": draws_h2h, "h2h_t2_wins": t2_h2h_w,
            "h2h_sample": h2h_total,
        },
        "used_api_model": api_pred is not None,
    }


def format_prediction(pred, expanded=False):
    """Format a prediction dict into a Telegram message (Markdown)."""
    t1, t2 = pred["team1"], pred["team2"]
    lines = [
        f"⚽ *{t1} vs {t2}*",
        "",
        f"🏠 {t1} win: *{pred['win1_pct']}%*",
        f"🤝 Draw: *{pred['draw_pct']}%*",
        f"✈️ {t2} win: *{pred['win2_pct']}%*",
    ]

    if not expanded:
        lines.append("")
        lines.append("_Tap /stats for the numbers behind this._")
        return "\n".join(lines)

    s = pred["stats"]
    lines += [
        "",
        "📊 *Behind the numbers:*",
        f"• {t1} form: {s['t1_form_ppg']} pts/game (last {s['t1_sample']})",
        f"• {t2} form: {s['t2_form_ppg']} pts/game (last {s['t2_sample']})",
        f"• {t1} goals: {s['t1_goals_for']} for / {s['t1_goals_against']} against per game",
        f"• {t2} goals: {s['t2_goals_for']} for / {s['t2_goals_against']} against per game",
        f"• Head-to-head (last {s['h2h_sample']}): {t1} {s['h2h_t1_wins']}-{s['h2h_draws']}-{s['h2h_t2_wins']} {t2}",
    ]
    if pred["used_api_model"]:
        lines.append("• Cross-checked against API-Football's own model")

    lines += [
        "",
        "_This is a probability estimate from real data, not a guarantee — "
        "no model predicts football with fixed accuracy._",
    ]
    return "\n".join(lines)
