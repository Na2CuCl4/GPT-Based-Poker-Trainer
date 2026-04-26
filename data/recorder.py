"""SQLite-based game history recorder."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

DB_PATH = Path("data/poker_history.db")


@contextmanager
def _conn():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _conn() as con:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                start_time  TEXT NOT NULL,
                end_time    TEXT,
                config_json TEXT
            );
            CREATE TABLE IF NOT EXISTS hands (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id          INTEGER NOT NULL,
                hand_num            INTEGER NOT NULL,
                pot                 INTEGER,
                winner              TEXT,
                community_cards_json TEXT,
                result_json         TEXT,
                FOREIGN KEY (session_id) REFERENCES sessions(id)
            );
            CREATE TABLE IF NOT EXISTS decisions (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                hand_id             INTEGER NOT NULL,
                player              TEXT,
                street              TEXT,
                action              TEXT,
                amount              INTEGER,
                hand_strength_rank  INTEGER,
                FOREIGN KEY (hand_id) REFERENCES hands(id)
            );
        """)


def create_session(config: dict) -> int:
    with _conn() as con:
        cur = con.execute(
            "INSERT INTO sessions (start_time, config_json) VALUES (?, ?)",
            (datetime.utcnow().isoformat(), json.dumps(config)),
        )
        return cur.lastrowid


def record_hand(
    session_id: int,
    hand_num: int,
    pot: int,
    winner: str,
    community_cards: list[dict],
    result: dict,
) -> int:
    with _conn() as con:
        cur = con.execute(
            """INSERT INTO hands
               (session_id, hand_num, pot, winner, community_cards_json, result_json)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                session_id,
                hand_num,
                pot,
                winner,
                json.dumps(community_cards, ensure_ascii=False),
                json.dumps(result, ensure_ascii=False),
            ),
        )
        return cur.lastrowid


def record_decision(
    hand_id: int,
    player: str,
    street: str,
    action: str,
    amount: int,
    hand_strength_rank: int = 0,
) -> None:
    with _conn() as con:
        con.execute(
            """INSERT INTO decisions
               (hand_id, player, street, action, amount, hand_strength_rank)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (hand_id, player, street, action, amount, hand_strength_rank),
        )


def get_session_hands(session_id: int) -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM hands WHERE session_id = ? ORDER BY hand_num",
            (session_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_hand_decisions(hand_id: int) -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM decisions WHERE hand_id = ? ORDER BY id",
            (hand_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def update_hand_analysis(hand_id: int, analysis: dict) -> None:
    """Append analysis dict to an existing hand's result_json."""
    with _conn() as con:
        row = con.execute(
            "SELECT result_json FROM hands WHERE id=?", (hand_id,)
        ).fetchone()
        if row:
            existing = json.loads(row["result_json"] or "{}")
            existing["analysis"] = analysis
            con.execute(
                "UPDATE hands SET result_json=? WHERE id=?",
                (json.dumps(existing, ensure_ascii=False), hand_id),
            )


def update_hand(
    hand_id: int,
    pot: int,
    winner: str,
    community_cards: list[dict],
    result: dict,
) -> None:
    with _conn() as con:
        con.execute(
            """UPDATE hands SET pot=?, winner=?, community_cards_json=?, result_json=?
               WHERE id=?""",
            (
                pot,
                winner,
                json.dumps(community_cards, ensure_ascii=False),
                json.dumps(result, ensure_ascii=False),
                hand_id,
            ),
        )


def get_hand_detail(hand_id: int) -> dict | None:
    with _conn() as con:
        row = con.execute("SELECT * FROM hands WHERE id = ?", (hand_id,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["result"] = json.loads(d.pop("result_json", "{}"))
    d["community_cards"] = json.loads(d.pop("community_cards_json", "[]"))
    return d
