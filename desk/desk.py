#!/usr/bin/env python3
"""desk.py - shared data layer for the LET'S GET RICH CRYPTO DESK (multi-bot research desk).

One SQLite file (WAL mode) that every bot reads and writes through this module or its CLI.
Stdlib only. Research/measurement only: NOTHING here trades, signs, sends, or touches a wallet or a secret.

  Paths   : $DESK_DIR (default: the folder holding desk.py) -> $DESK_DIR/desk.db (override with $DESK_DB)
            $DESK_TOOLS_DIR (default: <repo>/research)   $DESK_TOOLS_DATA (default: $BOT_DATA_DIR or <repo>/data)
  Times   :: UTC ISO-8601 "YYYY-MM-DDTHH:MM:SSZ"
  Rules   : append-only tables refuse UPDATE/DELETE (SQLite triggers). Unknown data = NULL, never a guess.
            A Rug Shield / Risk & Execution REJECT veto (with evidence) blocks ACTIONABLE decisions (trigger).
            The entry bar lives in settings.entry_bar, starts at 0.75 and can only go UP (trigger).

Quick CLI (python3 $DESK_DIR/desk.py <cmd> ...; add --json-out for machine output where supported):
  init | stats | tables
  add-report --json '{...}' | --file r.json      (Alpha Hunter 16-field report -> opportunity id)
  verdict --opp ID --bot rug-shield --verdict REJECT --evidence "..." [--veto] [--links URL ...]
  decide  --opp ID --class ACTIONABLE --confidence 0.8 --reasoning "..." [--disagreement "..."]
  watch   --json '{token,chain,contract,status,...}'        (upsert token_watchlist)
  add     --table TABLE --json '{...}'                      (generic validated insert, any table)
  query   watchlist|reports|verdicts|decisions|wallets|wallet-events|callers|trades|regime|narratives|
          catalysts|security|strategies|sources|bots|signals|events|watch-changes [--where k=v] [--limit N]
  opp ID                       full bundle: report + verdicts + decisions + veto status
  regime latest | regime add --json '{...}'
  staleness [--minutes 30]     market fields older than N minutes
  calibration [--min-n 30]     confidence buckets 50-59/60-69/70-79/80-89/90+ vs outcomes (signals + live trades)
  signal-value                 per-signal (yes vs no) win-rate lift from signal_log + signal_outcomes
  leaderboard strategies|sources|callers
  bot-scorecard [--week 2026-W39]
  ladder [--bankroll USD]      realized-loss ladder state (trading-safety-rules SKILL)
  guard --chain C --contract X [--bankroll USD]   pre-buy checks: veto, 24h crash re-buy ban, ladder, crash regime
  audit-bar                    after >=20 live closed trades at/above the bar, raise the bar if win rate < 55%
  seed-shadow                  import tools/data shadow-tracker calls/sources + smart wallets (read-only, idempotent)
  sql "SELECT ..."             read-only SQL
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(HERE)
DESK_DIR = os.path.abspath(os.path.expanduser(os.environ.get("DESK_DIR") or HERE))
DB_PATH = os.environ.get("DESK_DB", os.path.join(DESK_DIR, "desk.db"))
DESK_TOOLS_DIR = os.path.abspath(os.path.expanduser(os.environ.get("DESK_TOOLS_DIR") or os.path.join(REPO_DIR, "research")))
REPORTS_DIR = os.path.join(DESK_DIR, "reports")
SAFETY_RULES_REF = "trading-safety-rules skill (prompts/trading-safety-rules.md in the template repo)"
DEFAULT_STALE_MIN = 30
ENTRY_BAR_FLOOR = 0.75
ROLES = ["hq", "alpha-hunter", "smart-money", "crypto-radar", "rug-shield", "quant-lab", "risk-execution"]
VETO_BOTS = ("rug-shield", "risk-execution")
CLASSES = ["REJECT", "WATCH", "DEVELOPING", "ACTIONABLE", "HIGH_CONFIDENCE"]
VERDICTS = ["BULLISH", "NEUTRAL", "BEARISH", "CLEAR", "CAUTION", "HIGH_RISK", "REJECT", "POSITIVE", "NEGATIVE",
            "INSUFFICIENT_DATA", "APPROVE", "WATCH"]
TAGS = ["FACT", "INFERENCE", "RUMOR", "SPECULATION", "UNKNOWN"]
WALLET_LABELS = ["skilled", "market_maker", "developer", "insider", "influencer", "bundled", "wash", "lucky", "unknown"]


def _in(col, vals, null_ok=True):
    q = ",".join("'%s'" % v for v in vals)
    return f"CHECK ({col} IN ({q}){' OR ' + col + ' IS NULL' if null_ok else ''})"


def now_iso(dt: datetime | None = None) -> str:
    return (dt or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s) -> datetime | None:
    if s is None or s == "":
        return None
    if isinstance(s, (int, float)):
        return datetime.fromtimestamp(float(s), timezone.utc)
    s = str(s).strip().replace(" ", "T")
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def is_stale(ts, minutes: float = DEFAULT_STALE_MIN, now: datetime | None = None) -> bool:
    """True when ts is missing/unparseable or older than `minutes`. Missing time = stale (never assume fresh)."""
    d = parse_iso(ts)
    if d is None:
        return True
    return (now or datetime.now(timezone.utc)) - d > timedelta(minutes=minutes)


def age_minutes(ts, now: datetime | None = None) -> float | None:
    d = parse_iso(ts)
    return None if d is None else round(((now or datetime.now(timezone.utc)) - d).total_seconds() / 60, 1)


# ------------------------------------------------------------------------------------------------ schema
COMMON = "source_bot TEXT NOT NULL DEFAULT 'unknown', source_links TEXT"   # source_links = JSON list of URLs/ids
SCHEMA = {
    "settings": "key TEXT PRIMARY KEY, value TEXT, updated_at TEXT",
    "token_watchlist": f"""id INTEGER PRIMARY KEY, token TEXT, chain TEXT NOT NULL, contract TEXT NOT NULL,
        status TEXT NOT NULL {_in('status', CLASSES, False)}, price REAL, mcap REAL, liquidity REAL, volume REAL,
        age TEXT, holder_growth REAL, liq_growth REAL, why TEXT, catalyst TEXT, risks TEXT,
        confidence REAL CHECK (confidence IS NULL OR (confidence>=0 AND confidence<=1)),
        market_ts TEXT, opportunity_id INTEGER, {COMMON}, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        UNIQUE(chain, contract)""",
    "watchlist_changes": f"""id INTEGER PRIMARY KEY, ts TEXT NOT NULL, chain TEXT, contract TEXT, token TEXT,
        old_status TEXT, new_status TEXT, source_bot TEXT""",
    "opportunity_reports": f"""id INTEGER PRIMARY KEY, ts TEXT NOT NULL, token TEXT NOT NULL, chain TEXT NOT NULL,
        contract TEXT, price REAL, mcap REAL, liquidity REAL, volume REAL, token_age TEXT, holder_growth REAL,
        liquidity_growth REAL, why_detected TEXT NOT NULL, catalyst TEXT, risks TEXT,
        confidence REAL CHECK (confidence IS NULL OR (confidence>=0 AND confidence<=1)), market_ts TEXT,
        extra TEXT, {COMMON}""",
    "bot_verdicts": f"""id INTEGER PRIMARY KEY, ts TEXT NOT NULL, opportunity_id INTEGER NOT NULL
        REFERENCES opportunity_reports(id), bot TEXT NOT NULL, verdict TEXT NOT NULL {_in('verdict', VERDICTS, False)},
        veto INTEGER NOT NULL DEFAULT 0 CHECK (veto IN (0,1)), evidence TEXT,
        confidence REAL CHECK (confidence IS NULL OR (confidence>=0 AND confidence<=1)), {COMMON},
        CHECK (veto=0 OR (verdict='REJECT' AND length(trim(coalesce(evidence,'')))>=10))""",
    "hq_decisions": f"""id INTEGER PRIMARY KEY, ts TEXT NOT NULL, opportunity_id INTEGER NOT NULL
        REFERENCES opportunity_reports(id), classification TEXT NOT NULL {_in('classification', CLASSES, False)},
        confidence REAL CHECK (confidence IS NULL OR (confidence>=0 AND confidence<=1)),
        disagreement_summary TEXT, reasoning TEXT NOT NULL, strongest_case_against TEXT, trade_report TEXT,
        output_channel TEXT, {COMMON}""",
    "smart_money_wallets": f"""address TEXT NOT NULL, chain TEXT NOT NULL,
        label TEXT NOT NULL DEFAULT 'unknown' {_in('label', WALLET_LABELS, False)}, tier TEXT,
        pnl_basis TEXT NOT NULL DEFAULT 'realized_onchain' {_in('pnl_basis', ['realized_onchain', 'realized_exchange', 'unverified'], False)},
        realized_pnl REAL, pnl_unit TEXT, win_rate REAL, median_return REAL, avg_hold TEXT, n_trades INTEGER,
        preferred_chains TEXT, mcap_range TEXT, last_scored TEXT, evidence TEXT, {COMMON}, updated_at TEXT NOT NULL,
        PRIMARY KEY(address, chain)""",
    "wallet_events": f"""id INTEGER PRIMARY KEY, ts TEXT NOT NULL, address TEXT NOT NULL, chain TEXT NOT NULL,
        event_type TEXT NOT NULL, token TEXT, contract TEXT, amount REAL, usd_value REAL, tx_hash TEXT,
        block_time TEXT, notes TEXT, {COMMON}""",
    "caller_performance": f"""id INTEGER PRIMARY KEY, call_key TEXT UNIQUE, source TEXT NOT NULL, token TEXT, chain TEXT,
        contract TEXT, call_ts TEXT, entry_price REAL, entry_mode TEXT, mc_at_call REAL,
        ret_5m REAL, ret_15m REAL, ret_1h REAL, ret_4h REAL, ret_24h REAL, ret_3d REAL, ret_7d REAL,
        max_up REAL, max_down REAL, executable INTEGER, outcome TEXT, imported_from TEXT, {COMMON},
        updated_at TEXT NOT NULL""",
    "trade_history": f"""id INTEGER PRIMARY KEY, ts TEXT NOT NULL, trade_id TEXT NOT NULL,
        event TEXT NOT NULL {_in('event', ['OPEN', 'PARTIAL_CLOSE', 'CLOSE', 'REVIEW', 'NOTE'], False)},
        mode TEXT {_in('mode', ['manual_fomo', 'cdp_bot', 'paper', 'shadow'])}, is_live INTEGER NOT NULL DEFAULT 0,
        venue TEXT, token TEXT, chain TEXT, contract TEXT, side TEXT, strategy TEXT, signals TEXT,
        confidence REAL, opportunity_id INTEGER, hq_decision_id INTEGER, regime TEXT, narrative TEXT, thesis TEXT,
        entry_ts TEXT, entry_price REAL, size_usd REAL, stop REAL, target REAL, invalidation TEXT,
        exit_ts TEXT, exit_price REAL, exit_reason TEXT, fees_usd REAL, gas_usd REAL, slippage_pct REAL,
        price_impact_pct REAL, failed_tx INTEGER, latency_s REAL, gross_pnl_usd REAL, net_pnl_usd REAL,
        net_return_pct REAL, hold_minutes REAL, mfe_pct REAL, mae_pct REAL, crashed INTEGER,
        decision_quality TEXT {_in('decision_quality', ['GOOD', 'BAD'])},
        result TEXT {_in('result', ['WIN', 'LOSS', 'SCRATCH'])},
        quadrant TEXT {_in('quadrant', ['GOOD_DECISION_GOOD_RESULT', 'GOOD_DECISION_BAD_RESULT', 'BAD_DECISION_GOOD_RESULT', 'BAD_DECISION_BAD_RESULT'])},
        lessons TEXT, notes TEXT, {COMMON}""",
    "market_regime": f"""id INTEGER PRIMARY KEY, ts TEXT NOT NULL,
        regime TEXT NOT NULL {_in('regime', ['RISK_ON', 'NEUTRAL', 'RISK_OFF', 'CRASH', 'EUPHORIA', 'UNKNOWN'], False)},
        btc_price REAL, btc_24h_pct REAL, eth_price REAL, sol_price REAL, btc_dominance REAL, total_mcap REAL,
        stablecoin_mcap REAL, stablecoin_7d_pct REAL, btc_funding REAL, btc_oi_usd REAL, fear_greed INTEGER,
        dex_volume_24h REAL, us10y REAL, confidence REAL, notes TEXT, data TEXT, {COMMON}""",
    "narratives": f"""id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE,
        stage TEXT {_in('stage', ['EMERGING', 'ACCELERATING', 'PEAKING', 'FADING', 'DEAD'])}, assets TEXT,
        mention_velocity REAL, unique_authors INTEGER, onchain_confirmation TEXT, evidence TEXT,
        tag TEXT {_in('tag', TAGS)}, first_seen TEXT, {COMMON}, updated_at TEXT NOT NULL""",
    "catalysts": f"""id INTEGER PRIMARY KEY, ts TEXT NOT NULL, event_time TEXT, title TEXT NOT NULL, source TEXT,
        expected_impact TEXT, assets TEXT, reaction TEXT, tag TEXT NOT NULL DEFAULT 'UNKNOWN' {_in('tag', TAGS, False)},
        status TEXT, {COMMON}, updated_at TEXT NOT NULL""",
    "security_findings": f"""id INTEGER PRIMARY KEY, ts TEXT NOT NULL, token TEXT, chain TEXT NOT NULL, contract TEXT NOT NULL,
        classification TEXT NOT NULL {_in('classification', ['CLEAR', 'CAUTION', 'HIGH_RISK', 'REJECT'], False)},
        evidence TEXT NOT NULL, checks TEXT, opportunity_id INTEGER, {COMMON}""",
    "strategy_performance": f"""id INTEGER PRIMARY KEY, ts TEXT NOT NULL, strategy TEXT NOT NULL, period TEXT,
        stage TEXT {_in('stage', ['idea', 'backtest', 'validation', 'out_of_sample', 'paper', 'shadow', 'small_live', 'production', 'retired'])},
        n_trades INTEGER, net_return_pct REAL, win_rate REAL, profit_factor REAL, avg_win_pct REAL, avg_loss_pct REAL,
        max_dd_pct REAL, ev_per_trade_pct REAL, fees_pct REAL, slippage_pct REAL, notes TEXT, {COMMON}""",
    "source_reliability": f"""source TEXT PRIMARY KEY, source_type TEXT, hierarchy_tier INTEGER, n_calls INTEGER,
        n_priced INTEGER, n_resolved INTEGER, hit_rate REAL, median_max_mult_24h REAL, median_price_24h_ratio REAL,
        share_2x_24h REAL, notes TEXT, {COMMON}, updated_at TEXT NOT NULL""",
    "bot_performance": f"""bot TEXT NOT NULL, week TEXT NOT NULL, useful_discoveries INTEGER, false_positives INTEGER,
        latency_min REAL, accuracy REAL, duplicate_work INTEGER, compute_notes TEXT, notes TEXT, {COMMON},
        updated_at TEXT NOT NULL, PRIMARY KEY(bot, week)""",
    "signal_log": f"""id INTEGER PRIMARY KEY, ts TEXT NOT NULL, opportunity_id INTEGER, token TEXT, chain TEXT,
        contract TEXT, model TEXT, answers TEXT NOT NULL, correlated_groups TEXT,
        confidence REAL NOT NULL CHECK (confidence>=0 AND confidence<=1), bucket TEXT NOT NULL, horizon TEXT,
        target_pct REAL, stop_pct REAL, {COMMON}""",
    "signal_outcomes": f"""id INTEGER PRIMARY KEY, ts TEXT NOT NULL, signal_id INTEGER NOT NULL REFERENCES signal_log(id),
        outcome TEXT NOT NULL {_in('outcome', ['WIN', 'LOSS', 'EXPIRED', 'EXCLUDED'], False)}, return_pct REAL,
        notes TEXT, {COMMON}""",
    "desk_events": f"""id INTEGER PRIMARY KEY, ts TEXT NOT NULL, kind TEXT NOT NULL, message TEXT NOT NULL, data TEXT, {COMMON}""",
}
APPEND_ONLY = ["opportunity_reports", "bot_verdicts", "hq_decisions", "wallet_events", "trade_history", "market_regime",
               "security_findings", "strategy_performance", "signal_log", "signal_outcomes", "desk_events",
               "watchlist_changes"]
INDEXES = [
    "CREATE INDEX IF NOT EXISTS ix_rep_contract ON opportunity_reports(chain, contract)",
    "CREATE INDEX IF NOT EXISTS ix_verd_opp ON bot_verdicts(opportunity_id)",
    "CREATE INDEX IF NOT EXISTS ix_dec_opp ON hq_decisions(opportunity_id)",
    "CREATE INDEX IF NOT EXISTS ix_sec_contract ON security_findings(chain, contract)",
    "CREATE INDEX IF NOT EXISTS ix_trade_tid ON trade_history(trade_id)",
    "CREATE INDEX IF NOT EXISTS ix_trade_contract ON trade_history(chain, contract)",
    "CREATE INDEX IF NOT EXISTS ix_caller_src ON caller_performance(source)",
    "CREATE INDEX IF NOT EXISTS ix_wev_addr ON wallet_events(address, chain)",
    "CREATE INDEX IF NOT EXISTS ix_sigout ON signal_outcomes(signal_id)",
    "CREATE INDEX IF NOT EXISTS ix_regime_ts ON market_regime(ts)",
]
TRIGGERS = [
    # veto rule: a Rug Shield / Risk & Execution REJECT veto, or a REJECT security finding on the same contract,
    # cannot be outvoted into an ACTIONABLE decision.
    """CREATE TRIGGER IF NOT EXISTS trg_veto_blocks_actionable BEFORE INSERT ON hq_decisions
       WHEN NEW.classification IN ('ACTIONABLE','HIGH_CONFIDENCE') AND (
         EXISTS (SELECT 1 FROM bot_verdicts v WHERE v.opportunity_id=NEW.opportunity_id AND v.veto=1
                 AND v.bot IN ('rug-shield','risk-execution'))
         OR EXISTS (SELECT 1 FROM security_findings s JOIN opportunity_reports r ON r.id=NEW.opportunity_id
                 WHERE s.classification='REJECT' AND s.chain=r.chain AND s.contract=r.contract AND r.contract IS NOT NULL))
       BEGIN SELECT RAISE(ABORT, 'VETO: evidence-backed REJECT from rug-shield/risk-execution blocks ACTIONABLE'); END""",
    # entry bar: ACTIONABLE needs confidence >= settings.entry_bar
    """CREATE TRIGGER IF NOT EXISTS trg_entry_bar BEFORE INSERT ON hq_decisions
       WHEN NEW.classification IN ('ACTIONABLE','HIGH_CONFIDENCE') AND (NEW.confidence IS NULL OR NEW.confidence <
            (SELECT CAST(value AS REAL) FROM settings WHERE key='entry_bar'))
       BEGIN SELECT RAISE(ABORT, 'ENTRY BAR: ACTIONABLE requires confidence >= settings.entry_bar'); END""",
    """CREATE TRIGGER IF NOT EXISTS trg_bar_no_lower BEFORE UPDATE ON settings
       WHEN NEW.key='entry_bar' AND CAST(NEW.value AS REAL) < CAST(OLD.value AS REAL)
       BEGIN SELECT RAISE(ABORT, 'entry_bar can only be raised (owner-only loosening, outside this tool)'); END""",
    """CREATE TRIGGER IF NOT EXISTS trg_bar_floor BEFORE INSERT ON settings
       WHEN NEW.key='entry_bar' AND CAST(NEW.value AS REAL) < 0.75
       BEGIN SELECT RAISE(ABORT, 'entry_bar floor is 0.75'); END""",
    """CREATE TRIGGER IF NOT EXISTS trg_settings_nodel BEFORE DELETE ON settings WHEN OLD.key='entry_bar'
       BEGIN SELECT RAISE(ABORT, 'entry_bar cannot be deleted'); END""",
    # watchlist change log
    """CREATE TRIGGER IF NOT EXISTS trg_watch_ins AFTER INSERT ON token_watchlist BEGIN
       INSERT INTO watchlist_changes(ts, chain, contract, token, old_status, new_status, source_bot)
       VALUES (NEW.updated_at, NEW.chain, NEW.contract, NEW.token, NULL, NEW.status, NEW.source_bot); END""",
    """CREATE TRIGGER IF NOT EXISTS trg_watch_upd AFTER UPDATE OF status ON token_watchlist
       WHEN OLD.status IS NOT NEW.status BEGIN
       INSERT INTO watchlist_changes(ts, chain, contract, token, old_status, new_status, source_bot)
       VALUES (NEW.updated_at, NEW.chain, NEW.contract, NEW.token, OLD.status, NEW.status, NEW.source_bot); END""",
]
for _t in APPEND_ONLY:
    TRIGGERS.append(f"CREATE TRIGGER IF NOT EXISTS trg_{_t}_noupd BEFORE UPDATE ON {_t} "
                    f"BEGIN SELECT RAISE(ABORT, 'append-only table: {_t}'); END")
    TRIGGERS.append(f"CREATE TRIGGER IF NOT EXISTS trg_{_t}_nodel BEFORE DELETE ON {_t} "
                    f"BEGIN SELECT RAISE(ABORT, 'append-only table: {_t}'); END")
# market fields per table used by the staleness helper: (table, time column that dates the market fields)
MARKET_TS = {"token_watchlist": "market_ts", "opportunity_reports": "market_ts", "market_regime": "ts"}
DEFAULT_SETTINGS = {"entry_bar": "0.75", "stale_minutes": str(DEFAULT_STALE_MIN), "bankroll_usd": "",
                    "live_mode": "manual_fomo", "timezone": os.environ.get("DESK_TIMEZONE", "UTC")}


def connect(path: str | None = None, readonly: bool = False) -> sqlite3.Connection:
    p = path or DB_PATH
    if readonly:
        con = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=30)
    else:
        os.makedirs(os.path.dirname(os.path.abspath(p)), exist_ok=True)
        con = sqlite3.connect(p, timeout=30)
        con.execute("PRAGMA journal_mode=WAL")
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=30000")
    con.execute("PRAGMA foreign_keys=ON")
    if not readonly:
        init(con)
    return con


def init(con: sqlite3.Connection) -> None:
    for t, cols in SCHEMA.items():
        con.execute(f"CREATE TABLE IF NOT EXISTS {t} ({cols})")
    for s in INDEXES + TRIGGERS:
        con.execute(s)
    for k, v in DEFAULT_SETTINGS.items():
        con.execute("INSERT OR IGNORE INTO settings(key, value, updated_at) VALUES (?,?,?)", (k, v, now_iso()))
    con.commit()


def columns(con, table) -> list[str]:
    return [r[1] for r in con.execute(f"PRAGMA table_info({table})")]


def _norm(v):
    if isinstance(v, (list, dict)):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, bool):
        return int(v)
    return v


def insert(con, table: str, row: dict, commit: bool = True) -> int:
    """Validated insert. Unknown keys are refused (typos must not silently drop data). ts/updated_at auto-filled."""
    if table not in SCHEMA:
        raise ValueError(f"unknown table {table}")
    cols = columns(con, table)
    row = dict(row)
    bad = [k for k in row if k not in cols]
    if bad:
        raise ValueError(f"{table}: unknown field(s) {bad}; valid: {cols}")
    for c in ("ts", "updated_at", "created_at"):
        if c in cols and not row.get(c):
            row[c] = now_iso()
    if "source_bot" in row and row["source_bot"]:
        row["source_bot"] = str(row["source_bot"]).strip().lower()
    keys = list(row)
    cur = con.execute(f"INSERT INTO {table} ({','.join(keys)}) VALUES ({','.join('?' * len(keys))})",
                      [_norm(row[k]) for k in keys])
    if commit:
        con.commit()
    return cur.lastrowid


def upsert(con, table: str, row: dict, key: list[str], commit: bool = True) -> None:
    if table in APPEND_ONLY:
        raise ValueError(f"{table} is append-only; use insert")
    cols = columns(con, table)
    row = dict(row)
    bad = [k for k in row if k not in cols]
    if bad:
        raise ValueError(f"{table}: unknown field(s) {bad}; valid: {cols}")
    if "updated_at" in cols:
        row["updated_at"] = row.get("updated_at") or now_iso()
    if "created_at" in cols:
        row.setdefault("created_at", now_iso())
    keys = list(row)
    upd = [k for k in keys if k not in key and k != "created_at"]
    sql = (f"INSERT INTO {table} ({','.join(keys)}) VALUES ({','.join('?' * len(keys))}) "
           f"ON CONFLICT({','.join(key)}) DO UPDATE SET " + ",".join(f"{k}=excluded.{k}" for k in upd))
    con.execute(sql, [_norm(row[k]) for k in keys])
    if commit:
        con.commit()


def rows(con, sql, params=()) -> list[dict]:
    return [dict(r) for r in con.execute(sql, params)]


def setting(con, key, default=None):
    r = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r[0] if r and r[0] not in (None, "") else default


def log_event(con, kind, message, data=None, source_bot="desk"):
    return insert(con, "desk_events", {"kind": kind, "message": message, "data": data, "source_bot": source_bot})


# ------------------------------------------------------------------------------------------------ domain API
REPORT_FIELDS = ["token", "chain", "contract", "price", "mcap", "liquidity", "volume", "token_age", "holder_growth",
                 "liquidity_growth", "why_detected", "catalyst", "risks", "confidence", "source_links", "ts"]


def add_report(con, rep: dict, source_bot: str = "alpha-hunter") -> int:
    """Alpha Hunter 16-field report. Unknown values must be null (never invented). Extra keys go to `extra`."""
    rep = dict(rep)
    extra = {k: rep.pop(k) for k in list(rep) if k not in columns(con, "opportunity_reports")}
    if extra:
        rep["extra"] = {**(json.loads(rep["extra"]) if isinstance(rep.get("extra"), str) else rep.get("extra") or {}), **extra}
    rep.setdefault("source_bot", source_bot)
    for req in ("token", "chain", "why_detected"):
        if not rep.get(req):
            raise ValueError(f"report needs '{req}'")
    rep["chain"] = str(rep["chain"]).lower()
    if rep.get("price") is not None and not rep.get("market_ts"):
        rep["market_ts"] = rep.get("ts") or now_iso()
    return insert(con, "opportunity_reports", rep)


def add_verdict(con, opportunity_id: int, bot: str, verdict: str, evidence: str = "", veto: bool = False,
                confidence=None, source_links=None) -> int:
    bot = bot.strip().lower()
    verdict = verdict.strip().upper()
    if veto and bot not in VETO_BOTS:
        raise ValueError(f"only {VETO_BOTS} can veto; others record REJECT/BEARISH without veto")
    return insert(con, "bot_verdicts", {"opportunity_id": opportunity_id, "bot": bot, "verdict": verdict,
                                        "veto": 1 if veto else 0, "evidence": evidence, "confidence": confidence,
                                        "source_links": source_links, "source_bot": bot})


def veto_status(con, opportunity_id: int) -> dict:
    v = rows(con, "SELECT id, bot, verdict, evidence, ts FROM bot_verdicts WHERE opportunity_id=? AND veto=1 "
                  "AND bot IN ('rug-shield','risk-execution')", (opportunity_id,))
    r = con.execute("SELECT chain, contract FROM opportunity_reports WHERE id=?", (opportunity_id,)).fetchone()
    s = []
    if r and r["contract"]:
        s = rows(con, "SELECT id, classification, evidence, ts FROM security_findings WHERE chain=? AND contract=? "
                      "AND classification='REJECT'", (r["chain"], r["contract"]))
    return {"vetoed": bool(v or s), "verdict_vetoes": v, "security_rejects": s}


def decide(con, opportunity_id: int, classification: str, reasoning: str, confidence=None,
           disagreement_summary: str = "", strongest_case_against: str = "", trade_report=None,
           source_bot: str = "hq") -> int:
    classification = classification.strip().upper().replace("-", "_").replace(" ", "_")
    if classification == "HIGH_CONFIDENCE_ACTIONABLE":
        classification = "HIGH_CONFIDENCE"
    channel = None
    if classification in ("ACTIONABLE", "HIGH_CONFIDENCE"):
        mode = setting(con, "live_mode", "manual_fomo")
        channel = "cdp_bot" if mode == "cdp_bot_live_funded" else "fomo_move_card"
    return insert(con, "hq_decisions", {"opportunity_id": opportunity_id, "classification": classification,
                                        "confidence": confidence, "reasoning": reasoning,
                                        "disagreement_summary": disagreement_summary,
                                        "strongest_case_against": strongest_case_against,
                                        "trade_report": trade_report, "output_channel": channel,
                                        "source_bot": source_bot})


def upsert_watch(con, w: dict, source_bot: str | None = None) -> None:
    w = dict(w)
    w["chain"] = str(w.get("chain", "")).lower()
    if not w.get("contract") or not w.get("chain"):
        raise ValueError("watchlist needs chain + contract")
    w["status"] = str(w.get("status", "WATCH")).upper().replace("-", "_").replace("HIGH_CONFIDENCE_ACTIONABLE", "HIGH_CONFIDENCE")
    if source_bot:
        w["source_bot"] = source_bot
    if any(w.get(k) is not None for k in ("price", "mcap", "liquidity", "volume")) and not w.get("market_ts"):
        w["market_ts"] = now_iso()
    upsert(con, "token_watchlist", w, ["chain", "contract"])


def opportunity_bundle(con, oid: int) -> dict:
    rep = rows(con, "SELECT * FROM opportunity_reports WHERE id=?", (oid,))
    if not rep:
        raise ValueError(f"no opportunity {oid}")
    r = rep[0]
    r["market_stale"] = is_stale(r.get("market_ts"), float(setting(con, "stale_minutes", DEFAULT_STALE_MIN)))
    sec = rows(con, "SELECT * FROM security_findings WHERE chain=? AND contract=? ORDER BY id", (r["chain"], r["contract"])) if r["contract"] else []
    return {"report": r, "verdicts": rows(con, "SELECT * FROM bot_verdicts WHERE opportunity_id=? ORDER BY id", (oid,)),
            "security_findings": sec,
            "decisions": rows(con, "SELECT * FROM hq_decisions WHERE opportunity_id=? ORDER BY id", (oid,)),
            "veto": veto_status(con, oid)}


def bucket_of(conf: float) -> str:
    c = conf * 100 if conf <= 1 else conf
    if c < 50:
        return "<50"
    if c >= 90:
        return "90+"
    lo = int(c // 10 * 10)
    return f"{lo}-{lo + 9}"


BUCKETS = ["<50", "50-59", "60-69", "70-79", "80-89", "90+"]


def log_signal(con, answers: dict, confidence: float, source_bot: str, **kw) -> int:
    """answers: {question: 'yes'|'no'|'unknown'}. correlated_groups: {group: [questions]} - only one answer per group
    may carry weight in a score (no double counting); the log keeps them all so predictive value can be tested."""
    norm = {}
    for k, v in answers.items():
        s = str(v).strip().lower()
        norm[k] = "yes" if s in ("yes", "y", "true", "1") else "no" if s in ("no", "n", "false", "0") else "unknown"
    return insert(con, "signal_log", {"answers": norm, "confidence": confidence, "bucket": bucket_of(confidence),
                                      "source_bot": source_bot, **kw})


def wilson(k: int, n: int, z: float = 1.96):
    if n == 0:
        return None, None
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return round(max(0, c - h), 3), round(min(1, c + h), 3)


def _outcomes(con):
    """Latest outcome per signal (WIN/LOSS only; EXPIRED counts as not-a-win, EXCLUDED dropped)."""
    return rows(con, """SELECT s.id, s.confidence, s.bucket, s.answers, o.outcome, o.return_pct FROM signal_log s
                        JOIN signal_outcomes o ON o.id=(SELECT max(id) FROM signal_outcomes WHERE signal_id=s.id)
                        WHERE o.outcome != 'EXCLUDED'""")


def calibration(con, min_n: int = 30) -> dict:
    out = {"signals": [], "live_trades": [], "thresholds": [], "min_n": min_n}
    res = _outcomes(con)
    for b in BUCKETS:
        xs = [r for r in res if r["bucket"] == b]
        k = sum(1 for r in xs if r["outcome"] == "WIN")
        out["signals"].append({"bucket": b, "n": len(xs), "predicted_avg": round(sum(r["confidence"] for r in xs) / len(xs), 3) if xs else None,
                               "win_rate": round(k / len(xs), 3) if xs else None, "wilson95": wilson(k, len(xs)),
                               "enough": len(xs) >= min_n})
    for t in (0.75, 0.80, 0.85, 0.90):
        xs = [r for r in res if r["confidence"] >= t]
        k = sum(1 for r in xs if r["outcome"] == "WIN")
        out["thresholds"].append({"at_or_above": t, "n": len(xs), "win_rate": round(k / len(xs), 3) if xs else None,
                                  "wilson95": wilson(k, len(xs)), "enough": len(xs) >= min_n})
    tr = rows(con, "SELECT confidence, result FROM trade_history WHERE event='CLOSE' AND is_live=1 AND confidence IS NOT NULL")
    for b in BUCKETS:
        xs = [r for r in tr if bucket_of(r["confidence"]) == b]
        k = sum(1 for r in xs if r["result"] == "WIN")
        out["live_trades"].append({"bucket": b, "n": len(xs), "win_rate": round(k / len(xs), 3) if xs else None,
                                   "wilson95": wilson(k, len(xs))})
    out["brier_signals"] = round(sum((r["confidence"] - (r["outcome"] == "WIN")) ** 2 for r in res) / len(res), 4) if res else None
    return out


def signal_value(con, min_n: int = 20) -> list[dict]:
    res = _outcomes(con)
    base = sum(1 for r in res if r["outcome"] == "WIN") / len(res) if res else None
    qs = {}
    for r in res:
        for q, a in json.loads(r["answers"]).items():
            d = qs.setdefault(q, {"yes": [0, 0], "no": [0, 0], "unknown": [0, 0]})
            d[a][0] += r["outcome"] == "WIN"
            d[a][1] += 1
    out = []
    for q, d in sorted(qs.items()):
        wy = d["yes"][0] / d["yes"][1] if d["yes"][1] else None
        wn = d["no"][0] / d["no"][1] if d["no"][1] else None
        out.append({"signal": q, "n_yes": d["yes"][1], "win_yes": None if wy is None else round(wy, 3),
                    "n_no": d["no"][1], "win_no": None if wn is None else round(wn, 3), "n_unknown": d["unknown"][1],
                    "lift": None if wy is None or wn is None else round(wy - wn, 3),
                    "enough": d["yes"][1] >= min_n and d["no"][1] >= min_n, "base_rate": None if base is None else round(base, 3)})
    return out


def leaderboard(con, what: str) -> list[dict]:
    if what == "strategies":
        return rows(con, """SELECT sp.* FROM strategy_performance sp JOIN (SELECT strategy, max(id) mid FROM strategy_performance
                            GROUP BY strategy) l ON l.mid=sp.id
                            ORDER BY (coalesce(n_trades,0)>=30) DESC, ev_per_trade_pct DESC NULLS LAST""")
    if what == "sources":
        return rows(con, """SELECT source, source_type, hierarchy_tier, n_calls, n_priced, n_resolved, hit_rate,
                            median_max_mult_24h, median_price_24h_ratio, share_2x_24h, updated_at FROM source_reliability
                            ORDER BY (coalesce(n_resolved,0)>=30) DESC, hit_rate DESC NULLS LAST, n_calls DESC""")
    if what == "callers":
        return rows(con, """SELECT source, count(*) calls, sum(entry_price IS NOT NULL) priced,
                            sum(ret_24h IS NOT NULL) with_24h, round(avg(ret_24h),2) avg_ret_24h_pct,
                            round(avg(max_up),2) avg_max_up_pct, sum(executable) executable
                            FROM caller_performance GROUP BY source ORDER BY with_24h DESC, calls DESC""")
    raise ValueError("leaderboard strategies|sources|callers")


def iso_week(dt: datetime | None = None) -> str:
    y, w, _ = (dt or datetime.now(timezone.utc)).isocalendar()
    return f"{y}-W{w:02d}"


def bot_scorecard(con, week: str | None = None) -> dict:
    week = week or iso_week()
    wk = datetime.strptime(week + "-1", "%G-W%V-%u").replace(tzinfo=timezone.utc)
    lo, hi = now_iso(wk), now_iso(wk + timedelta(days=7))
    activity = {}
    for t, col in (("opportunity_reports", "source_bot"), ("bot_verdicts", "bot"), ("hq_decisions", "source_bot"),
                   ("security_findings", "source_bot"), ("signal_log", "source_bot"), ("catalysts", "source_bot"),
                   ("wallet_events", "source_bot"), ("market_regime", "source_bot")):
        for r in rows(con, f"SELECT {col} b, count(*) n FROM {t} WHERE ts>=? AND ts<? GROUP BY {col}", (lo, hi)):
            activity.setdefault(r["b"], {})[t] = r["n"]
    # duplicate discoveries: same contract reported by >1 bot within the week
    dups = rows(con, """SELECT chain, contract, count(DISTINCT source_bot) bots, group_concat(DISTINCT source_bot) who
                        FROM opportunity_reports WHERE ts>=? AND ts<? AND contract IS NOT NULL
                        GROUP BY chain, contract HAVING bots>1""", (lo, hi))
    # discoveries that HQ later rated ACTIONABLE+ (useful) vs REJECT (false positive) per reporting bot
    q = rows(con, """SELECT r.source_bot b, sum(d.classification IN ('ACTIONABLE','HIGH_CONFIDENCE','DEVELOPING')) useful,
                     sum(d.classification='REJECT') rejected FROM opportunity_reports r
                     JOIN hq_decisions d ON d.id=(SELECT max(id) FROM hq_decisions WHERE opportunity_id=r.id)
                     WHERE r.ts>=? AND r.ts<? GROUP BY r.source_bot""", (lo, hi))
    return {"week": week, "window_utc": [lo, hi], "activity": activity, "derived_quality": q, "duplicate_reports": dups,
            "manual_scores": rows(con, "SELECT * FROM bot_performance WHERE week=? ORDER BY bot", (week,))}


def staleness(con, minutes: float | None = None) -> dict:
    m = float(minutes if minutes is not None else setting(con, "stale_minutes", DEFAULT_STALE_MIN))
    out = {"threshold_min": m, "checked_at": now_iso(), "stale": {}}
    wl = rows(con, "SELECT token, chain, contract, status, market_ts FROM token_watchlist WHERE status != 'REJECT'")
    out["stale"]["token_watchlist"] = [dict(r, age_min=age_minutes(r["market_ts"])) for r in wl if is_stale(r["market_ts"], m)]
    reg = rows(con, "SELECT id, ts, regime FROM market_regime ORDER BY id DESC LIMIT 1")
    out["stale"]["market_regime_latest"] = [dict(r, age_min=age_minutes(r["ts"])) for r in reg if is_stale(r["ts"], m)]
    if not reg:
        out["stale"]["market_regime_latest"] = [{"note": "no regime snapshot yet (UNKNOWN)"}]
    return out


def realized_losses(con, bankroll: float, now: datetime | None = None) -> dict:
    """Loss ladder from LIVE realized closes (conservative rolling windows 1h / 24h / 7d; the SKILL says hour/day/week)."""
    now = now or datetime.now(timezone.utc)
    tr = rows(con, "SELECT ts, exit_ts, net_pnl_usd FROM trade_history WHERE is_live=1 AND event IN ('CLOSE','PARTIAL_CLOSE') "
                   "AND net_pnl_usd IS NOT NULL")

    def win(h):
        s = 0.0
        for r in tr:
            d = parse_iso(r["exit_ts"] or r["ts"])
            if d and now - d <= timedelta(hours=h):
                s += r["net_pnl_usd"]
        return s
    p1, p24, p7 = win(1), win(24), win(168)
    pct = lambda x: round(-x / bankroll * 100, 2) if bankroll and x < 0 else 0.0
    l1, l24, l7 = pct(p1), pct(p24), pct(p7)
    state, action = "OK", "normal rules"
    if l7 >= 20:
        state, action = "DONE_WEEK", "20%+ realized loss in a week: done for the week"
    elif l24 >= 12:
        state, action = "DONE_DAY", "12%+ realized loss in a day: done for the day"
    elif l7 >= 15:
        state, action = "COOLDOWN_24H_HALF_SIZE", "15%+ in a week: 24-hour cooldown, then half size"
    elif l24 >= 8:
        state, action = "COOLDOWN_3H_HALF_SIZE", "8%+ in a day: 3-hour cooldown, then half size"
    elif l1 >= 5:
        state, action = "PAUSE_1_2H", "5%+ in 1 hour: pause 1-2 hours"
    return {"bankroll_usd": bankroll, "loss_pct_1h": l1, "loss_pct_24h": l24, "loss_pct_7d": l7, "state": state,
            "action": action, "live_closed_trades": len(tr),
            "rule_source": SAFETY_RULES_REF}


def guard(con, chain: str, contract: str, bankroll: float | None = None, now: datetime | None = None) -> dict:
    """Pre-buy research checks. Returns allowed=False with reasons. It cannot loosen anything; it only blocks."""
    now = now or datetime.now(timezone.utc)
    chain = chain.lower()
    reasons = []
    sec = rows(con, "SELECT id, classification, ts FROM security_findings WHERE chain=? AND contract=? ORDER BY id DESC LIMIT 1",
               (chain, contract))
    if sec and sec[0]["classification"] == "REJECT":
        reasons.append(f"Rug Shield REJECT (security_findings #{sec[0]['id']})")
    if not sec:
        reasons.append("no Rug Shield finding yet (UNKNOWN security = no buy)")
    v = rows(con, """SELECT v.id, v.bot FROM bot_verdicts v JOIN opportunity_reports r ON r.id=v.opportunity_id
                     WHERE r.chain=? AND r.contract=? AND v.veto=1""", (chain, contract))
    if v:
        reasons.append(f"binding veto(s): {[(x['id'], x['bot']) for x in v]}")
    since = now_iso(now - timedelta(hours=24))
    crash = rows(con, "SELECT id FROM trade_history WHERE chain=? AND contract=? AND crashed=1 AND ts>=?", (chain, contract, since))
    if crash:
        reasons.append("token crashed within 24h: no re-buy (SKILL)")
    reg = rows(con, "SELECT regime, ts FROM market_regime ORDER BY id DESC LIMIT 1")
    if reg and reg[0]["regime"] == "CRASH":
        reasons.append("market-wide CRASH regime: exits only (SKILL)")
    if not reg or is_stale(reg[0]["ts"], 240, now):
        reasons.append("market regime unknown or older than 4h: refresh first")
    lad = None
    b = bankroll if bankroll is not None else float(setting(con, "bankroll_usd", 0) or 0)
    if b:
        lad = realized_losses(con, b, now)
        if lad["state"] != "OK":
            reasons.append(f"loss ladder: {lad['state']} ({lad['action']})")
    else:
        reasons.append("bankroll unknown: cannot evaluate loss ladder/sizing (set settings.bankroll_usd or --bankroll)")
    return {"chain": chain, "contract": contract, "allowed": not reasons, "reasons": reasons, "ladder": lad,
            "entry_bar": float(setting(con, "entry_bar", ENTRY_BAR_FLOOR)),
            "sizing_rule": "max 10% of bankroll or owner's $ cap (lower) per position until 20 trades; max 40% exposed"}


def audit_bar(con) -> dict:
    bar = float(setting(con, "entry_bar", ENTRY_BAR_FLOOR))
    tr = rows(con, "SELECT result FROM trade_history WHERE event='CLOSE' AND is_live=1 AND confidence>=?", (bar,))
    n, k = len(tr), sum(1 for r in tr if r["result"] == "WIN")
    out = {"entry_bar": bar, "n_at_or_above_bar": n, "wins": k, "win_rate": round(k / n, 3) if n else None, "action": "none"}
    if n >= 20 and k / n < 0.55:
        new = round(min(0.95, bar + 0.05), 2)
        if new > bar:
            con.execute("UPDATE settings SET value=?, updated_at=? WHERE key='entry_bar'", (str(new), now_iso()))
            log_event(con, "entry_bar_raised", f"entry bar {bar} -> {new}: {k}/{n} wins < 55% at/above bar", out, "quant-lab")
            out["action"] = f"raised to {new}"
    elif n < 20:
        out["action"] = f"insufficient sample ({n}/20 live closed trades at/above bar)"
    return out


def stats(con) -> dict:
    t = {name: con.execute(f"SELECT count(*) FROM {name}").fetchone()[0] for name in SCHEMA}
    live = con.execute("SELECT count(DISTINCT trade_id) FROM trade_history WHERE is_live=1 AND event='OPEN'").fetchone()[0]
    closed = con.execute("SELECT count(DISTINCT trade_id) FROM trade_history WHERE is_live=1 AND event='CLOSE'").fetchone()[0]
    return {"db": DB_PATH, "journal_mode": con.execute("PRAGMA journal_mode").fetchone()[0], "row_counts": t,
            "live_trades_opened": live, "live_trades_closed": closed, "live_trades_open_now": live - closed,
            "entry_bar": setting(con, "entry_bar"), "live_mode": setting(con, "live_mode")}


# ------------------------------------------------------------------------------------------------ seed (read-only import)
TOOLS_DATA = os.path.abspath(os.path.expanduser(os.environ.get("DESK_TOOLS_DATA") or os.environ.get("BOT_DATA_DIR")
                                                or os.path.join(REPO_DIR, "data")))


def _median(xs):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    m = len(xs) // 2
    return xs[m] if len(xs) % 2 else (xs[m - 1] + xs[m]) / 2


def seed_shadow(con, data_dir: str | None = None) -> dict:
    """Bridge shadow_tracker data into caller_performance/source_reliability, and smart_wallets.json into
    smart_money_wallets. Opens the source files READ-ONLY; never writes to the research tools' data folder. Idempotent (call_key)."""
    d = data_dir or TOOLS_DATA
    log = os.path.join(d, "shadow_log.jsonl")
    out = {"calls_upserted": 0, "sources": 0, "wallets": 0, "skipped_lines": 0}
    per_src = {}
    if os.path.exists(log):
        ts_now = now_iso()
        with open(log, encoding="utf-8") as f:
            batch = []
            for line in f:
                try:
                    e = json.loads(line)
                except ValueError:
                    out["skipped_lines"] += 1
                    continue
                src = e.get("source") or "scanner"
                if e.get("status") == "SUPERSEDED":
                    continue
                px = e.get("entry_price")
                r24 = e.get("price_24h_ratio")
                mx, mn = e.get("max_mult_24h"), e.get("min_mult_24h")
                oc = e.get("on_curve")
                ps = per_src.setdefault(src, {"n": 0, "priced": 0, "res": 0, "win": 0, "m24": [], "p24": []})
                ps["n"] += 1
                ps["priced"] += px is not None
                if e.get("status") == "RESOLVED" and e.get("outcome") not in ("BAD_PRICE", "SUPERSEDED"):
                    ps["res"] += 1
                    ps["win"] += e.get("outcome") == "WIN"
                if mx is not None:
                    ps["m24"].append(mx)
                if r24 is not None:
                    ps["p24"].append(r24)
                batch.append((
                    "shadow:" + e["id"], src if src != "scanner" else "scanner:" + (e.get("verdict") or "?"),
                    e.get("symbol"), e.get("chain"), e.get("address"), now_iso(datetime.fromtimestamp(e["entry_ts"], timezone.utc)) if e.get("entry_ts") else None,
                    px, e.get("entry_mode"), e.get("mc_at_call"),
                    None if r24 is None else round((r24 - 1) * 100, 2),
                    None if mx is None else round((mx - 1) * 100, 2), None if mn is None else round((mn - 1) * 100, 2),
                    None if oc is None else int(not oc), e.get("outcome") or e.get("status"),
                    "tools/data/shadow_log.jsonl", "shadow-tracker", json.dumps([f"shadow_log id {e['id']}"] +
                    ([f"https://t.me/{src[3:]}/{e['msg_id']}"] if src.startswith("tg:") and e.get("msg_id") else [])), ts_now))
            con.executemany("""INSERT INTO caller_performance(call_key, source, token, chain, contract, call_ts, entry_price,
                entry_mode, mc_at_call, ret_24h, max_up, max_down, executable, outcome, imported_from, source_bot, source_links,
                updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(call_key) DO UPDATE SET entry_price=excluded.entry_price, ret_24h=excluded.ret_24h,
                max_up=excluded.max_up, max_down=excluded.max_down, executable=excluded.executable, outcome=excluded.outcome,
                updated_at=excluded.updated_at""", batch)
            out["calls_upserted"] = len(batch)
        for src, ps in per_src.items():
            m24, p24 = ps["m24"], ps["p24"]
            upsert(con, "source_reliability", {
                "source": src, "source_type": "telegram_call_channel" if src.startswith("tg:") else "internal_scanner",
                "hierarchy_tier": 9 if src.startswith("tg:") else 5, "n_calls": ps["n"], "n_priced": ps["priced"],
                "n_resolved": ps["res"], "hit_rate": round(ps["win"] / ps["res"], 3) if ps["res"] else None,
                "median_max_mult_24h": _median(m24), "median_price_24h_ratio": _median(p24),
                "share_2x_24h": round(sum(1 for x in m24 if x >= 2) / len(m24), 3) if m24 else None,
                "notes": "imported from shadow_tracker (TP1-before-stop, 5m candles). Hit rate meaningless below 30 resolved.",
                "source_bot": "shadow-tracker", "source_links": [os.path.join(d, "shadow_report.md")]}, ["source"], commit=False)
            out["sources"] += 1
    sw = os.path.join(d, "smart_wallets.json")
    if os.path.exists(sw):
        with open(sw, encoding="utf-8") as fh:
            W = json.load(fh).get("wallets", {})
        for addr, w in W.items():
            st = w.get("stats", {})
            upsert(con, "smart_money_wallets", {
                "address": addr, "chain": "solana", "label": "unknown", "tier": w.get("tier"),
                "pnl_basis": "realized_onchain", "realized_pnl": st.get("realized_pnl_sol_30d"), "pnl_unit": "SOL_30d",
                "win_rate": st.get("win_rate_30d"), "median_return": st.get("median_pnl_pct_30d"),
                "avg_hold": f"median {st.get('median_hold_s')}s" if st.get("median_hold_s") is not None else None,
                "n_trades": st.get("closed_30d"), "preferred_chains": ["solana"], "last_scored": w.get("scored_at"),
                "evidence": {"reliability": w.get("reliability"), "discovered_via": w.get("discovered_via"),
                             "swaps_90d": st.get("swaps_90d"), "top_trade_share": st.get("top_trade_share"),
                             "max_drawdown_sol_30d": st.get("max_drawdown_sol_30d"),
                             "note": "label unknown until Smart Money classifies; 30d sample only"},
                "source_bot": "smart-money", "source_links": [os.path.join(d, "smart_wallets.json")]},
                ["address", "chain"], commit=False)
            out["wallets"] += 1
    con.commit()
    return out


# ------------------------------------------------------------------------------------------------ CLI
QUERY = {
    "watchlist": ("token_watchlist", "updated_at DESC"), "reports": ("opportunity_reports", "id DESC"),
    "verdicts": ("bot_verdicts", "id DESC"), "decisions": ("hq_decisions", "id DESC"),
    "wallets": ("smart_money_wallets", "updated_at DESC"), "wallet-events": ("wallet_events", "id DESC"),
    "callers": ("caller_performance", "call_ts DESC"), "trades": ("trade_history", "id DESC"),
    "regime": ("market_regime", "id DESC"), "narratives": ("narratives", "updated_at DESC"),
    "catalysts": ("catalysts", "event_time DESC"), "security": ("security_findings", "id DESC"),
    "strategies": ("strategy_performance", "id DESC"), "sources": ("source_reliability", "n_calls DESC"),
    "bots": ("bot_performance", "week DESC"), "signals": ("signal_log", "id DESC"),
    "signal-outcomes": ("signal_outcomes", "id DESC"), "events": ("desk_events", "id DESC"),
    "watch-changes": ("watchlist_changes", "id DESC"), "settings": ("settings", "key"),
}


def _load_json_arg(a):
    if getattr(a, "file", None):
        with open(a.file, encoding="utf-8") as fh:
            return json.load(fh)
    return json.loads(a.json)


def _print(obj, as_json=True):
    if isinstance(obj, list) and not as_json:
        if not obj:
            print("(no rows)")
            return
        keys = list(obj[0])
        print(" | ".join(keys))
        for r in obj:
            print(" | ".join("" if r[k] is None else str(r[k])[:60] for k in keys))
    else:
        print(json.dumps(obj, indent=1, default=str, ensure_ascii=False))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="LET'S GET RICH CRYPTO DESK shared data layer", epilog=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", help="override DB path (default $DESK_DB or desk/desk.db)")
    sp = ap.add_subparsers(dest="cmd", required=True)
    sp.add_parser("init"); sp.add_parser("stats"); sp.add_parser("tables"); sp.add_parser("seed-shadow")
    sp.add_parser("audit-bar"); sp.add_parser("signal-value")
    p = sp.add_parser("add-report"); p.add_argument("--json"); p.add_argument("--file"); p.add_argument("--bot", default="alpha-hunter")
    p = sp.add_parser("verdict"); p.add_argument("--opp", type=int, required=True); p.add_argument("--bot", required=True)
    p.add_argument("--verdict", required=True); p.add_argument("--evidence", default=""); p.add_argument("--veto", action="store_true")
    p.add_argument("--confidence", type=float); p.add_argument("--links", nargs="*")
    p = sp.add_parser("decide"); p.add_argument("--opp", type=int, required=True); p.add_argument("--class", dest="cls", required=True)
    p.add_argument("--confidence", type=float); p.add_argument("--reasoning", required=True)
    p.add_argument("--disagreement", default=""); p.add_argument("--against", default="", help="strongest case against")
    p.add_argument("--trade-report", help="JSON (section-33 trade report) or text")
    p = sp.add_parser("watch"); p.add_argument("--json"); p.add_argument("--file"); p.add_argument("--bot")
    p = sp.add_parser("add"); p.add_argument("--table", required=True); p.add_argument("--json"); p.add_argument("--file")
    p = sp.add_parser("upsert"); p.add_argument("--table", required=True); p.add_argument("--key", required=True, help="comma list")
    p.add_argument("--json"); p.add_argument("--file")
    p = sp.add_parser("query"); p.add_argument("what", choices=sorted(QUERY)); p.add_argument("--where", nargs="*", default=[])
    p.add_argument("--limit", type=int, default=20); p.add_argument("--table-out", action="store_true")
    p = sp.add_parser("opp"); p.add_argument("id", type=int)
    p = sp.add_parser("regime"); p.add_argument("action", choices=["latest", "add"]); p.add_argument("--json"); p.add_argument("--file")
    p = sp.add_parser("staleness"); p.add_argument("--minutes", type=float)
    p = sp.add_parser("calibration"); p.add_argument("--min-n", type=int, default=30)
    p = sp.add_parser("leaderboard"); p.add_argument("what", choices=["strategies", "sources", "callers"])
    p = sp.add_parser("bot-scorecard"); p.add_argument("--week")
    p = sp.add_parser("ladder"); p.add_argument("--bankroll", type=float)
    p = sp.add_parser("guard"); p.add_argument("--chain", required=True); p.add_argument("--contract", required=True)
    p.add_argument("--bankroll", type=float)
    p = sp.add_parser("sql"); p.add_argument("query")
    p = sp.add_parser("log-signal"); p.add_argument("--json", required=True, help='{"answers":{...},"confidence":0.7,"source_bot":"quant-lab",...}')
    p = sp.add_parser("signal-outcome"); p.add_argument("--signal", type=int, required=True)
    p.add_argument("--outcome", required=True, choices=["WIN", "LOSS", "EXPIRED", "EXCLUDED"]); p.add_argument("--return-pct", type=float)
    p.add_argument("--bot", default="quant-lab"); p.add_argument("--notes")
    a = ap.parse_args(argv)
    global DB_PATH
    if a.db:
        DB_PATH = a.db
    if a.cmd == "sql":
        con = connect(readonly=True)
        _print(rows(con, a.query))
        return 0
    con = connect()
    try:
        c = a.cmd
        if c == "init":
            _print({"ok": True, "db": DB_PATH})
        elif c == "stats":
            _print(stats(con))
        elif c == "tables":
            _print({t: columns(con, t) for t in SCHEMA})
        elif c == "add-report":
            oid = add_report(con, _load_json_arg(a), a.bot)
            _print({"opportunity_id": oid})
        elif c == "verdict":
            _print({"verdict_id": add_verdict(con, a.opp, a.bot, a.verdict, a.evidence, a.veto, a.confidence, a.links)})
        elif c == "decide":
            tr = a.trade_report
            if tr:
                try:
                    tr = json.loads(tr)
                except ValueError:
                    pass
            _print({"decision_id": decide(con, a.opp, a.cls, a.reasoning, a.confidence, a.disagreement, a.against, tr)})
        elif c == "watch":
            upsert_watch(con, _load_json_arg(a), a.bot)
            _print({"ok": True})
        elif c == "add":
            _print({"id": insert(con, a.table, _load_json_arg(a))})
        elif c == "upsert":
            upsert(con, a.table, _load_json_arg(a), a.key.split(","))
            _print({"ok": True})
        elif c == "query":
            t, order = QUERY[a.what]
            cols = columns(con, t)
            conds, params = [], []
            for w in a.where:
                k, _, v = w.partition("=")
                if k not in cols:
                    raise ValueError(f"unknown column {k}")
                conds.append(f"{k}=?"); params.append(v)
            rs = rows(con, f"SELECT * FROM {t} {'WHERE ' + ' AND '.join(conds) if conds else ''} ORDER BY {order} LIMIT ?",
                      params + [a.limit])
            mts = MARKET_TS.get(t)
            if mts:
                m = float(setting(con, "stale_minutes", DEFAULT_STALE_MIN))
                for r in rs:
                    r["stale"] = is_stale(r.get(mts), m)
            _print(rs, not a.table_out)
        elif c == "opp":
            _print(opportunity_bundle(con, a.id))
        elif c == "regime":
            if a.action == "add":
                _print({"id": insert(con, "market_regime", _load_json_arg(a))})
            else:
                r = rows(con, "SELECT * FROM market_regime ORDER BY id DESC LIMIT 1")
                if r:
                    r[0]["stale"] = is_stale(r[0]["ts"], float(setting(con, "stale_minutes", DEFAULT_STALE_MIN)))
                    r[0]["age_min"] = age_minutes(r[0]["ts"])
                _print(r[0] if r else {"regime": "UNKNOWN", "note": "no snapshot yet"})
        elif c == "staleness":
            _print(staleness(con, a.minutes))
        elif c == "calibration":
            _print(calibration(con, a.min_n))
        elif c == "signal-value":
            _print(signal_value(con))
        elif c == "leaderboard":
            _print(leaderboard(con, a.what))
        elif c == "bot-scorecard":
            _print(bot_scorecard(con, a.week))
        elif c == "ladder":
            b = a.bankroll or float(setting(con, "bankroll_usd", 0) or 0)
            _print(realized_losses(con, b) if b else {"state": "UNKNOWN", "note": "bankroll unknown; pass --bankroll"})
        elif c == "guard":
            g = guard(con, a.chain, a.contract, a.bankroll)
            _print(g)
            return 0 if g["allowed"] else 3
        elif c == "audit-bar":
            _print(audit_bar(con))
        elif c == "seed-shadow":
            _print(seed_shadow(con))
        elif c == "log-signal":
            j = json.loads(a.json)
            _print({"signal_id": log_signal(con, j.pop("answers"), j.pop("confidence"), j.pop("source_bot", "unknown"), **j)})
        elif c == "signal-outcome":
            _print({"id": insert(con, "signal_outcomes", {"signal_id": a.signal, "outcome": a.outcome,
                                                            "return_pct": a.return_pct, "source_bot": a.bot, "notes": a.notes})})
    except (ValueError, sqlite3.DatabaseError, json.JSONDecodeError) as ex:
        print(json.dumps({"error": str(ex)}), file=sys.stderr)
        return 2
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
