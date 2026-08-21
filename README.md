FootyBot
A Telegram bot for football (soccer) predictions, live scores, and
accumulator booking codes — built on real data, with honest probabilities
instead of fixed “accuracy” claims.
What it does
	•	/predict Team A vs Team B — probability breakdown (win/draw/win) built
from recent form, goal rates, and head-to-head history, cross-checked
against API-Football’s own prediction model
	•	/stats — expands the numbers behind your last prediction
	•	/live — live scores right now, or today’s finished results if nothing’s live
	•	/leagues <name> — look up a league to disambiguate a /predict call
	•	/combo → /addleg → /finish — build a 2-5 leg accumulator, get
combined odds and the honest combined probability (which drops fast
as you add legs)
	•	/code ABC123 — anyone can pull up a shared booking slip by code
Setup
1. Telegram bot token
You already have this from @BotFather.
2. API-Football key
This bot uses API-Football (by API-Sports)
for teams, fixtures, stats, and live scores.
	•	Free tier: sign up at api-football.com or via
RapidAPI — 100
requests/day free, enough to develop and test with.
	•	Paid tiers start around $10-40/month once you want higher request
volume (e.g. serving many users’ /live calls).
Two ways to authenticate — pick one:
Direct (api-sports.io) — sign up at api-football.com, key comes from
your dashboard:
