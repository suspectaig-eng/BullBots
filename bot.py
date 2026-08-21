"""
FootyBot — data-backed football predictions, live scores, and accumulator
booking codes for Telegram.

Setup:
  1. pip install -r requirements.txt
  2. Set environment variables:
       TELEGRAM_BOT_TOKEN   - from @BotFather
       API_FOOTBALL_KEY     - from api-football.com or RapidAPI
       API_FOOTBALL_PROVIDER (optional) - "direct" (default) or "rapidapi"
  3. python bot.py

Commands:
  /start                        - intro
  /predict Team A vs Team B     - probability breakdown for a fixture
  /stats                        - expand the numbers behind the last prediction
  /live                         - today's live + finished scores
  /leagues <name>               - look up a league by name (needed to disambiguate)
  /combo                        - start building an accumulator (2-5 legs)
  /code ABC123                  - look up a shared booking code
"""

import os
import re
import logging
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes, filters
)

import api_football as af
import predictor
import accumulator as acc

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")

# Per-user scratch state (last prediction, in-progress combo). Fine for a
# single-process bot; swap for Redis/DB if you scale to multiple workers.
USER_STATE = {}


def get_state(user_id):
    return USER_STATE.setdefault(user_id, {"last_prediction": None, "combo_legs": []})


# ---------- Commands ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "⚽ *FootyBot* — data-backed football predictions.\n\n"
        "*Usage:*\n"
        "`/predict Arsenal vs Chelsea`\n"
        "`/predict Arsenal vs Chelsea | Premier League`  _(add league if ambiguous)_\n"
        "`/live` — today's scores (live + final), all leagues\n"
        "`/leagues Premier League` — look up a league\n"
        "`/combo` — build a 2-5 leg accumulator with combined odds\n"
        "`/code ABC123` — open a shared booking slip\n\n"
        "Predictions include recent form, goal rates, and head-to-head history. "
        "This bot gives *realistic probabilities* — it never claims a fixed "
        "accuracy rate, because no model can back that up."
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)


async def leagues_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = " ".join(context.args)
    if not query:
        await update.message.reply_text("Usage: /leagues Premier League")
        return
    try:
        results = af.search_league(query)
    except RuntimeError as e:
        await update.message.reply_text(f"⚠️ {e}")
        return
    if not results:
        await update.message.reply_text(f"No leagues found matching '{query}'.")
        return
    lines = ["*Matching leagues:*"]
    for r in results[:10]:
        lines.append(f"• {r['name']} ({r['country']}) — id `{r['id']}`, season {r['season']}")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


def _parse_predict_args(text):
    """Parses 'Team A vs Team B | League Name' (league optional)."""
    league = None
    if "|" in text:
        text, league = text.split("|", 1)
        league = league.strip()
    m = re.split(r"\s+vs\.?\s+", text.strip(), flags=re.IGNORECASE)
    if len(m) != 2:
        return None, None, None
    return m[0].strip(), m[1].strip(), league


async def predict_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = " ".join(context.args)
    if not text or " vs " not in text.lower():
        await update.message.reply_text(
            "Usage: /predict Team A vs Team B\n"
            "Optionally add a league: /predict Team A vs Team B | Premier League"
        )
        return

    t1_name, t2_name, league_hint = _parse_predict_args(text)
    if not t1_name:
        await update.message.reply_text("Couldn't parse that. Try: /predict Arsenal vs Chelsea")
        return

    await update.message.reply_chat_action("typing")

    try:
        t1_matches = af.search_team(t1_name)
        t2_matches = af.search_team(t2_name)
    except RuntimeError as e:
        await update.message.reply_text(f"⚠️ {e}")
        return

    if not t1_matches:
        await update.message.reply_text(f"Couldn't find a team matching '{t1_name}'.")
        return
    if not t2_matches:
        await update.message.reply_text(f"Couldn't find a team matching '{t2_name}'.")
        return

    team1, team2 = t1_matches[0], t2_matches[0]

    league_id, season, fixture_id = None, None, None
    if league_hint:
        leagues = af.search_league(league_hint)
        if leagues:
            league_id, season = leagues[0]["id"], leagues[0]["season"]

    try:
        fx = af.upcoming_fixture_between(team1["id"], team2["id"])
        if fx:
            fixture_id = fx["fixture"]["id"]
            if not league_id:
                league_id = fx["league"]["id"]
                season = fx["league"]["season"]
    except RuntimeError:
        pass

    try:
        pred = predictor.build_prediction(team1, team2, league_id, season, fixture_id)
    except RuntimeError as e:
        await update.message.reply_text(f"⚠️ {e}")
        return

    get_state(update.effective_user.id)["last_prediction"] = pred
    await update.message.reply_text(
        predictor.format_prediction(pred, expanded=False), parse_mode=ParseMode.MARKDOWN
    )


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = get_state(update.effective_user.id)
    pred = state.get("last_prediction")
    if not pred:
        await update.message.reply_text("Run /predict Team A vs Team B first.")
        return
    await update.message.reply_text(
        predictor.format_prediction(pred, expanded=True), parse_mode=ParseMode.MARKDOWN
    )


async def live_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_chat_action("typing")
    try:
        live = af.live_fixtures()
    except RuntimeError as e:
        await update.message.reply_text(f"⚠️ {e}")
        return

    if not live:
        try:
            today = af.fixtures_today()
        except RuntimeError as e:
            await update.message.reply_text(f"⚠️ {e}")
            return
        finished = [f for f in today if f["fixture"]["status"]["short"] in ("FT", "AET", "PEN")]
        if not finished:
            await update.message.reply_text("No live matches right now, and no finished matches found for today.")
            return
        lines = ["📅 *Today's results:*", ""]
        for fx in finished[:20]:
            h, a = fx["teams"]["home"]["name"], fx["teams"]["away"]["name"]
            hg, ag = fx["goals"]["home"], fx["goals"]["away"]
            lines.append(f"{h} {hg} - {ag} {a} (FT)")
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)
        return

    lines = ["🔴 *Live now:*", ""]
    for fx in live[:20]:
        h, a = fx["teams"]["home"]["name"], fx["teams"]["away"]["name"]
        hg, ag = fx["goals"]["home"], fx["goals"]["away"]
        minute = fx["fixture"]["status"].get("elapsed", "?")
        lines.append(f"{h} {hg} - {ag} {a}  ⏱ {minute}'")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


# ---------- Combo / booking code ----------

async def combo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = get_state(update.effective_user.id)
    state["combo_legs"] = []
    await update.message.reply_text(
        "🧩 *Building a combo.* Add legs one at a time:\n"
        "`/addleg Arsenal vs Chelsea | Over 2.5 Goals | 1.85`\n"
        "(format: match | market | decimal odds — probability comes from /predict "
        "if you've run it for that match, otherwise estimate it yourself)\n\n"
        "When you've added 2-5 legs, send `/finish` to get combined odds + a booking code.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def addleg_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = get_state(update.effective_user.id)
    text = " ".join(context.args)
    parts = [p.strip() for p in text.split("|")]
    if len(parts) != 3:
        await update.message.reply_text(
            "Format: /addleg Arsenal vs Chelsea | Over 2.5 Goals | 1.85"
        )
        return
    match, market, odds_str = parts
    try:
        odds = float(odds_str)
    except ValueError:
        await update.message.reply_text("Odds must be a number, e.g. 1.85")
        return

    # Try to reuse a real model probability if this leg matches the last /predict
    pred = state.get("last_prediction")
    probability_pct = 50.0  # fallback if we have no model estimate for this leg
    if pred and pred["team1"].lower() in match.lower() and pred["team2"].lower() in match.lower():
        # crude heuristic: pick the relevant side of the prediction if the market says so
        if "over" in market.lower() or "under" in market.lower() or "goal" in market.lower():
            probability_pct = 55.0  # goals markets aren't covered by the win/draw/win model
        else:
            probability_pct = max(pred["win1_pct"], pred["draw_pct"], pred["win2_pct"])

    if len(state["combo_legs"]) >= 5:
        await update.message.reply_text("Max 5 legs per combo. Send /finish to complete it.")
        return

    state["combo_legs"].append({
        "match": match, "market": market, "odds": odds, "probability_pct": probability_pct
    })
    await update.message.reply_text(
        f"✅ Added: {match} — {market} @ {odds}\n"
        f"Legs so far: {len(state['combo_legs'])}/5. Send another /addleg or /finish."
    )


async def finish_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = get_state(update.effective_user.id)
    legs = state.get("combo_legs", [])
    if len(legs) < 2:
        await update.message.reply_text("Add at least 2 legs first with /addleg.")
        return
    code = acc.save_booking(update.effective_user.id, legs)
    await update.message.reply_text(
        acc.format_booking_slip(code, legs), parse_mode=ParseMode.MARKDOWN
    )
    state["combo_legs"] = []


async def code_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /code ABC123")
        return
    code = context.args[0]
    legs = acc.load_booking(code)
    if not legs:
        await update.message.reply_text(f"No booking found for code {code.upper()}.")
        return
    await update.message.reply_text(
        acc.format_booking_slip(code.upper(), legs), parse_mode=ParseMode.MARKDOWN
    )


def main():
    if not BOT_TOKEN:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN as an environment variable before running.")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("predict", predict_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("live", live_cmd))
    app.add_handler(CommandHandler("leagues", leagues_cmd))
    app.add_handler(CommandHandler("combo", combo_cmd))
    app.add_handler(CommandHandler("addleg", addleg_cmd))
    app.add_handler(CommandHandler("finish", finish_cmd))
    app.add_handler(CommandHandler("code", code_cmd))

    logger.info("FootyBot starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
