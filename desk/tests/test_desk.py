"""Offline tests for desk.py. Run: python3 -m unittest discover -s "${DESK_DIR:-desk}/tests" -v  (from the repo root: python3 -m unittest discover -s desk/tests -v)"""
import json, os, sqlite3, subprocess, sys, tempfile, unittest
from datetime import datetime, timedelta, timezone
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import desk  # noqa: E402

REP = {"token": "TEST", "chain": "solana", "contract": "Mint111", "price": 0.01, "mcap": 1e6, "liquidity": 2e5,
       "volume": 5e5, "token_age": "3d", "holder_growth": 4.2, "liquidity_growth": 1.1, "why_detected": "volume + holders up",
       "catalyst": None, "risks": "top10 34%", "confidence": 0.6, "source_links": ["https://dexscreener.com/solana/x"]}


class DeskTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "t.db")
        self.con = desk.connect(self.db)

    def tearDown(self):
        self.con.close()

    def test_wal_and_tables(self):
        self.assertEqual(self.con.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        for t in desk.SCHEMA:
            self.con.execute(f"SELECT * FROM {t} LIMIT 1")

    def test_append_only(self):
        oid = desk.add_report(self.con, REP)
        for sql in ("UPDATE opportunity_reports SET token='X' WHERE id=?", "DELETE FROM opportunity_reports WHERE id=?"):
            with self.assertRaises(sqlite3.DatabaseError):
                self.con.execute(sql, (oid,))
        vid = desk.add_verdict(self.con, oid, "smart-money", "NEUTRAL", "no tracked wallets")
        with self.assertRaises(sqlite3.DatabaseError):
            self.con.execute("DELETE FROM bot_verdicts WHERE id=?", (vid,))
        trig = {r[0] for r in self.con.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
        for t in desk.APPEND_ONLY:
            self.assertIn(f"trg_{t}_noupd", trig)
            self.assertIn(f"trg_{t}_nodel", trig)

    def test_report_fields_and_extra(self):
        oid = desk.add_report(self.con, dict(REP, dex="raydium"))
        r = desk.rows(self.con, "SELECT * FROM opportunity_reports WHERE id=?", (oid,))[0]
        for f in desk.REPORT_FIELDS:
            self.assertIn(f, r)
        self.assertEqual(json.loads(r["extra"])["dex"], "raydium")
        self.assertTrue(r["market_ts"])
        self.assertTrue(r["ts"].endswith("Z"))
        with self.assertRaises(ValueError):
            desk.add_report(self.con, {"token": "x", "chain": "solana"})  # no why_detected

    def test_veto_blocks_actionable(self):
        oid = desk.add_report(self.con, REP)
        desk.add_verdict(self.con, oid, "alpha-hunter", "BULLISH", "momentum")
        desk.add_verdict(self.con, oid, "rug-shield", "REJECT", "mint authority not revoked (RugCheck report)", veto=True)
        with self.assertRaises(sqlite3.DatabaseError):
            desk.decide(self.con, oid, "ACTIONABLE", "everyone else bullish", confidence=0.9)
        desk.decide(self.con, oid, "REJECT", "veto stands")
        self.assertTrue(desk.veto_status(self.con, oid)["vetoed"])

    def test_veto_needs_evidence_and_role(self):
        oid = desk.add_report(self.con, REP)
        with self.assertRaises(sqlite3.DatabaseError):
            desk.add_verdict(self.con, oid, "rug-shield", "REJECT", "", veto=True)
        with self.assertRaises(ValueError):
            desk.add_verdict(self.con, oid, "alpha-hunter", "REJECT", "looks bad to me really", veto=True)

    def test_security_reject_blocks(self):
        oid = desk.add_report(self.con, REP)
        desk.insert(self.con, "security_findings", {"chain": "solana", "contract": "Mint111", "classification": "REJECT",
                                                    "evidence": "freeze authority live", "source_bot": "rug-shield"})
        with self.assertRaises(sqlite3.DatabaseError):
            desk.decide(self.con, oid, "HIGH_CONFIDENCE", "x", confidence=0.95)

    def test_entry_bar(self):
        oid = desk.add_report(self.con, REP)
        with self.assertRaises(sqlite3.DatabaseError):
            desk.decide(self.con, oid, "ACTIONABLE", "x", confidence=0.7)
        did = desk.decide(self.con, oid, "ACTIONABLE", "clean", confidence=0.8)
        self.assertEqual(desk.rows(self.con, "SELECT output_channel FROM hq_decisions WHERE id=?", (did,))[0]["output_channel"], "fomo_move_card")
        with self.assertRaises(sqlite3.DatabaseError):
            self.con.execute("UPDATE settings SET value='0.6' WHERE key='entry_bar'")
        self.con.execute("UPDATE settings SET value='0.8' WHERE key='entry_bar'")
        with self.assertRaises(sqlite3.DatabaseError):
            self.con.execute("UPDATE settings SET value='0.75' WHERE key='entry_bar'")

    def test_watchlist_upsert_and_changes(self):
        desk.upsert_watch(self.con, {"token": "T", "chain": "Solana", "contract": "M", "status": "watch", "price": 1}, "alpha-hunter")
        desk.upsert_watch(self.con, {"token": "T", "chain": "solana", "contract": "M", "status": "DEVELOPING", "price": 2}, "hq")
        w = desk.rows(self.con, "SELECT * FROM token_watchlist")
        self.assertEqual(len(w), 1)
        self.assertEqual(w[0]["status"], "DEVELOPING")
        ch = desk.rows(self.con, "SELECT old_status, new_status FROM watchlist_changes ORDER BY id")
        self.assertEqual([(c["old_status"], c["new_status"]) for c in ch], [(None, "WATCH"), ("WATCH", "DEVELOPING")])
        with self.assertRaises(sqlite3.DatabaseError):
            desk.upsert_watch(self.con, {"chain": "solana", "contract": "Z", "status": "MAYBE"})

    def test_unknown_field_refused(self):
        with self.assertRaises(ValueError):
            desk.insert(self.con, "catalysts", {"title": "x", "tagg": "FACT"})
        with self.assertRaises(sqlite3.DatabaseError):
            desk.insert(self.con, "catalysts", {"title": "x", "tag": "VIBES"})

    def test_staleness(self):
        now = datetime.now(timezone.utc)
        self.assertTrue(desk.is_stale(None))
        self.assertTrue(desk.is_stale(desk.now_iso(now - timedelta(minutes=31))))
        self.assertFalse(desk.is_stale(desk.now_iso(now - timedelta(minutes=5))))
        self.assertFalse(desk.is_stale(desk.now_iso(now - timedelta(minutes=50)), minutes=60))
        desk.upsert_watch(self.con, {"chain": "solana", "contract": "OLD", "status": "WATCH", "price": 1,
                                     "market_ts": desk.now_iso(now - timedelta(hours=2))})
        s = desk.staleness(self.con)
        self.assertEqual(s["stale"]["token_watchlist"][0]["contract"], "OLD")

    def test_buckets_calibration_signal_value(self):
        self.assertEqual([desk.bucket_of(x) for x in (0.49, 0.5, 0.59, 0.6, 0.75, 0.89, 0.9, 0.99)],
                         ["<50", "50-59", "50-59", "60-69", "70-79", "80-89", "90+", "90+"])
        for i in range(40):
            yes = i % 2 == 0
            sid = desk.log_signal(self.con, {"liq_growing": "yes" if yes else "no", "sm_entry": "unknown"},
                                  0.82 if yes else 0.55, "quant-lab")
            desk.insert(self.con, "signal_outcomes", {"signal_id": sid, "outcome": "WIN" if (yes and i % 4 == 0) or (not yes and i % 10 == 1) else "LOSS"})
        cal = desk.calibration(self.con, min_n=10)
        b = {x["bucket"]: x for x in cal["signals"]}
        self.assertEqual(b["80-89"]["n"], 20)
        self.assertEqual(b["80-89"]["win_rate"], 0.5)
        self.assertIsNotNone(cal["brier_signals"])
        sv = {x["signal"]: x for x in desk.signal_value(self.con, min_n=10)}
        self.assertGreater(sv["liq_growing"]["lift"], 0)
        self.assertEqual(sv["sm_entry"]["n_unknown"], 40)

    def _close(self, pnl, conf=0.8, result="LOSS", hours_ago=0.2, crashed=0, contract="C"):
        t = desk.now_iso(datetime.now(timezone.utc) - timedelta(hours=hours_ago))
        desk.insert(self.con, "trade_history", {"trade_id": f"t{pnl}{hours_ago}", "event": "CLOSE", "is_live": 1, "mode": "manual_fomo",
                                                "chain": "solana", "contract": contract, "net_pnl_usd": pnl, "exit_ts": t,
                                                "confidence": conf, "result": result, "crashed": crashed})

    def test_ladder_and_guard(self):
        self._close(-6, hours_ago=0.5)
        lad = desk.realized_losses(self.con, 100)
        self.assertEqual(lad["state"], "PAUSE_1_2H")
        self._close(-7, hours_ago=5, crashed=1)
        self.assertEqual(desk.realized_losses(self.con, 100)["state"], "DONE_DAY")
        g = desk.guard(self.con, "solana", "C", 100)
        self.assertFalse(g["allowed"])
        self.assertTrue(any("crashed within 24h" in r for r in g["reasons"]))
        self.assertTrue(any("no Rug Shield finding" in r for r in g["reasons"]))

    def test_guard_clean(self):
        desk.insert(self.con, "security_findings", {"chain": "solana", "contract": "OK1", "classification": "CLEAR", "evidence": "rugcheck ok"})
        desk.insert(self.con, "market_regime", {"regime": "NEUTRAL"})
        self.assertTrue(desk.guard(self.con, "solana", "OK1", 100)["allowed"])
        desk.insert(self.con, "market_regime", {"regime": "CRASH"})
        self.assertFalse(desk.guard(self.con, "solana", "OK1", 100)["allowed"])

    def test_audit_bar_raises(self):
        for i in range(20):
            self._close(-1 if i % 2 else 1, conf=0.8, result="LOSS" if i % 2 else "WIN", hours_ago=200 + i)
        r = desk.audit_bar(self.con)
        self.assertEqual(r["action"], "raised to 0.8")
        self.assertEqual(desk.setting(self.con, "entry_bar"), "0.8")

    def test_leaderboards_scorecard_stats(self):
        desk.insert(self.con, "strategy_performance", {"strategy": "new-pair momentum", "stage": "backtest", "n_trades": 12, "ev_per_trade_pct": -1.2})
        desk.insert(self.con, "strategy_performance", {"strategy": "new-pair momentum", "stage": "paper", "n_trades": 40, "ev_per_trade_pct": 0.4})
        lb = desk.leaderboard(self.con, "strategies")
        self.assertEqual(len(lb), 1)
        self.assertEqual(lb[0]["stage"], "paper")
        self.assertEqual(len(desk.rows(self.con, "SELECT * FROM strategy_performance")), 2)  # bad results never deleted
        desk.add_report(self.con, REP, "alpha-hunter"); desk.add_report(self.con, REP, "crypto-radar")
        sc = desk.bot_scorecard(self.con)
        self.assertEqual(sc["duplicate_reports"][0]["bots"], 2)
        self.assertEqual(desk.stats(self.con)["live_trades_open_now"], 0)

    def test_seed_shadow(self):
        d = os.path.join(self.tmp, "data"); os.makedirs(d)
        with open(os.path.join(d, "shadow_log.jsonl"), "w") as f:
            f.write(json.dumps({"id": "a1", "source": "tg:Chan", "msg_id": 5, "entry_ts": 1790000000, "symbol": "X", "chain": "solana",
                                "address": "M1", "entry_price": 0.1, "max_mult_24h": 2.5, "price_24h_ratio": 0.5, "on_curve": False,
                                "status": "RESOLVED", "outcome": "WIN"}) + "\n")
            f.write(json.dumps({"id": "a2", "source": "tg:Chan", "entry_ts": 1790000100, "address": "M2", "status": "OPEN"}) + "\n")
            f.write("{torn\n")
        with open(os.path.join(d, "smart_wallets.json"), "w") as fh:
            json.dump({"wallets": {"W1": {"tier": "tracked", "stats": {"win_rate_30d": 0.6, "closed_30d": 20}}}}, fh)
        r1 = desk.seed_shadow(self.con, d); r2 = desk.seed_shadow(self.con, d)
        self.assertEqual(r1["calls_upserted"], 2); self.assertEqual(r1["skipped_lines"], 1)
        self.assertEqual(self.con.execute("SELECT count(*) FROM caller_performance").fetchone()[0], 2)  # idempotent
        c = desk.rows(self.con, "SELECT * FROM caller_performance WHERE call_key='shadow:a1'")[0]
        self.assertEqual(c["ret_24h"], -50.0); self.assertEqual(c["max_up"], 150.0); self.assertEqual(c["executable"], 1)
        s = desk.rows(self.con, "SELECT * FROM source_reliability WHERE source='tg:Chan'")[0]
        self.assertEqual(s["n_calls"], 2); self.assertEqual(s["hit_rate"], 1.0)
        self.assertEqual(desk.rows(self.con, "SELECT label FROM smart_money_wallets")[0]["label"], "unknown")

    def test_cli_roundtrip(self):
        env = dict(os.environ, DESK_DB=self.db)
        py = [sys.executable, os.path.join(os.path.dirname(HERE), "desk.py")]
        out = subprocess.run(py + ["add-report", "--json", json.dumps(REP)], env=env, capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        oid = json.loads(out.stdout)["opportunity_id"]
        out = subprocess.run(py + ["verdict", "--opp", str(oid), "--bot", "risk-execution", "--verdict", "REJECT", "--veto",
                                   "--evidence", "round-trip cost 7.9% > 3% cap (Jupiter quote)"], env=env, capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        out = subprocess.run(py + ["decide", "--opp", str(oid), "--class", "ACTIONABLE", "--confidence", "0.9", "--reasoning", "x"],
                             env=env, capture_output=True, text=True)
        self.assertEqual(out.returncode, 2); self.assertIn("VETO", out.stderr)
        for cmd in (["stats"], ["query", "reports"], ["regime", "latest"], ["calibration"], ["leaderboard", "strategies"],
                    ["leaderboard", "sources"], ["leaderboard", "callers"], ["bot-scorecard"], ["staleness"], ["opp", str(oid)],
                    ["sql", "SELECT count(*) n FROM bot_verdicts"], ["signal-value"], ["ladder", "--bankroll", "100"]):
            out = subprocess.run(py + cmd, env=env, capture_output=True, text=True)
            self.assertEqual(out.returncode, 0, (cmd, out.stderr))
        out = subprocess.run(py + ["sql", "DELETE FROM settings"], env=env, capture_output=True, text=True)
        self.assertNotEqual(out.returncode, 0)  # read-only SQL


if __name__ == "__main__":
    unittest.main()
