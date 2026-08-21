"""
Accumulator ("combo") builder + shareable booking codes.

Combined odds are exact multiplication (that part is just math, always
correct). Combined *probability* of every leg landing is the product of
each leg's individual probability — shown honestly, because it drops
fast as legs are added. A 4-leg combo where each leg is a genuine ~65%
chance is realistically an ~18% chance for the whole slip, not a "sure
thing," and the bot says so.
"""

import sqlite3
import random
import string
import os
import json

DB_PATH = os.path.join(os.path.dirname(__file__), "cardless_bot.db")  # reused for bookings


def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS booking_codes (
            code TEXT PRIMARY KEY,
            user_id INTEGER,
            legs_json TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    return conn


def _gen_code(length=6):
    chars = string.ascii_uppercase + string.digits
    return "".join(random.choice(chars) for _ in range(length))


def combine_odds(legs):
    """legs: list of dicts with at least 'odds' (decimal, e.g. 1.85) and 'probability_pct'."""
    total_odds = 1.0
    total_prob = 1.0
    for leg in legs:
        total_odds *= leg["odds"]
        total_prob *= leg["probability_pct"] / 100
    return round(total_odds, 2), round(total_prob * 100, 1)


def save_booking(user_id, legs):
    """legs: list of dicts, e.g. {'match': 'Arsenal vs Chelsea', 'market': 'Over 2.5 Goals',
    'odds': 1.85, 'probability_pct': 58.0}"""
    conn = _conn()
    code = _gen_code()
    # Ensure uniqueness (astronomically unlikely to collide, but cheap to check)
    while conn.execute("SELECT 1 FROM booking_codes WHERE code=?", (code,)).fetchone():
        code = _gen_code()
    conn.execute(
        "INSERT INTO booking_codes (code, user_id, legs_json) VALUES (?, ?, ?)",
        (code, user_id, json.dumps(legs)),
    )
    conn.commit()
    conn.close()
    return code


def load_booking(code):
    conn = _conn()
    row = conn.execute("SELECT legs_json FROM booking_codes WHERE code=?", (code.upper(),)).fetchone()
    conn.close()
    if not row:
        return None
    return json.loads(row[0])


def format_booking_slip(code, legs):
    total_odds, total_prob = combine_odds(legs)
    lines = [f"🎫 *Booking Code: {code}*", ""]
    for i, leg in enumerate(legs, 1):
        lines.append(f"{i}. {leg['match']} — {leg['market']}")
        lines.append(f"   Odds: {leg['odds']} | Model: {leg['probability_pct']}%")
    lines += [
        "",
        f"*Combined odds: {total_odds}*",
        f"*Combined probability: ~{total_prob}%*",
        "",
        "_Combined probability drops fast with more legs — this is the honest "
        "math, not a guarantee any single leg (or the slip) lands._",
    ]
    return "\n".join(lines)
