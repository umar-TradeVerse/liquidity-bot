"""Pattern Memory Agent — learns which setup conditions keep losing.

SHADOW MODE by default: it never blocks a trade. For every setup it
writes what it sees into the DECISION record, e.g.
    Pattern:OK(shadow — matches loser pattern side=SELL & sess=Asia, 38% on 21)

Why shadow: walk-forward testing on 316 historical setups (2026-09-28)
showed winner patterns had NO edge on unseen data (= baseline) and loser
patterns only 1-3 points below baseline, while vetoing them would cut ~60%
of trades. The features available then were too thin. This agent now
records richer features (incl. OBI, walls, gaps) so the question can be
answered properly as live data accumulates.

How it earns veto power (only if PATTERN_VETO_ENABLED is switched on):
  a loser pattern must have >= PROMOTE_MIN_UNSEEN trades that happened
  AFTER it was first identified, with a win rate <= PROMOTE_MAX_WINRATE
  on those. Judged only on unseen trades — never on the data that
  created the pattern.

Memory sources: core/agents/pattern_seed.json (316 simulated historical
setups) + PERSIST_DIR/pattern_memory.jsonl (every real closed trade,
appended automatically). Winner patterns are tracked for information
only and can never create or force a trade.
"""
import itertools, json, logging, os
from datetime import datetime, timezone
from core.agents.decision import Verdict

logger = logging.getLogger("pattern_agent")
AGENT = "Pattern"

PATTERN_VETO_ENABLED = False     # stays False until the data justifies it
LEARN_MIN_TRADES = 8             # a combination needs this many trades to count
LOSER_MAX_WINRATE = 0.35         # candidate loser pattern threshold
WINNER_MIN_WINRATE = 0.65        # winner patterns: information only
PROMOTE_MIN_UNSEEN = 20
PROMOTE_MAX_WINRATE = 0.30
FEATURES = ("sym", "side", "regime", "bias", "sl", "sess", "obi")

_SEED = os.path.join(os.path.dirname(__file__), "pattern_seed.json")
_MEM = os.path.join(os.getenv("PERSIST_DIR", "/data"), "pattern_memory.jsonl")


def _obi_bucket(obi):
    if obi is None:
        return "?"
    return "bid-heavy" if obi >= 0.3 else "ask-heavy" if obi <= -0.3 else "balanced"


def build_features(symbol, side, entry, sl, regime, bias, obi=None, now=None) -> dict:
    """Only information available AT ENTRY TIME — never anything later."""
    now = now or datetime.now(timezone.utc)
    slp = abs(entry - sl) / entry * 100 if entry else 0
    hr = now.hour
    return {"sym": symbol, "side": side, "regime": regime or "?", "bias": bias or "?",
            "sl": "tight<1%" if slp < 1 else "mid1-2%" if slp < 2 else "wide>2%",
            "sess": "Asia" if hr < 7 else "EU" if hr < 13 else "US",
            "obi": _obi_bucket(obi)}


class PatternAgent:
    def __init__(self):
        self.rows = []          # [{"ts":..., "f":{...}, "win":bool}]
        self._load()
        self.learn()

    def _load(self):
        for path in (_SEED, _MEM):
            try:
                with open(path) as fh:
                    data = json.load(fh) if path == _SEED else [json.loads(x) for x in fh if x.strip()]
                self.rows.extend(data)
            except FileNotFoundError:
                pass
            except Exception as e:
                logger.error(f"Pattern memory load failed ({path}): {e}")
        self.rows.sort(key=lambda r: r["ts"])

    def learn(self):
        """Group closed trades by every 2-feature combination."""
        stats = {}
        for r in self.rows:
            f = r["f"]
            for combo in itertools.combinations(FEATURES, 2):
                if f.get(combo[0], "?") == "?" or f.get(combo[1], "?") == "?":
                    continue
                k = tuple((c, f[c]) for c in combo)
                s = stats.setdefault(k, {"n": 0, "w": 0, "first": r["ts"], "rows": []})
                s["n"] += 1; s["w"] += int(r["win"]); s["rows"].append(r)
        self.losers, self.winners = {}, {}
        for k, s in stats.items():
            if s["n"] < LEARN_MIN_TRADES:
                continue
            wr = s["w"] / s["n"]
            if wr <= LOSER_MAX_WINRATE:
                # identified once the first LEARN_MIN_TRADES were seen;
                # everything after that point is "unseen" evidence
                cutoff = s["rows"][LEARN_MIN_TRADES - 1]["ts"]
                unseen = [x for x in s["rows"] if x["ts"] > cutoff]
                uw = sum(x["win"] for x in unseen) / len(unseen) if unseen else None
                promoted = (len(unseen) >= PROMOTE_MIN_UNSEEN and uw is not None
                            and uw <= PROMOTE_MAX_WINRATE)
                self.losers[k] = {"n": s["n"], "wr": wr, "unseen_n": len(unseen),
                                  "unseen_wr": uw, "promoted": promoted}
            elif wr >= WINNER_MIN_WINRATE:
                self.winners[k] = {"n": s["n"], "wr": wr}
        prom = sum(v["promoted"] for v in self.losers.values())
        logger.info(f"Pattern memory: {len(self.rows)} trades, {len(self.losers)} loser / "
                    f"{len(self.winners)} winner patterns, {prom} promoted to veto-eligible")

    @staticmethod
    def _fmt(k):
        return " & ".join(f"{a}={b}" for a, b in k)

    def evaluate(self, features: dict) -> Verdict:
        hit = lambda P: [(k, v) for k, v in P.items() if all(features.get(a) == b for a, b in k)]
        lose, win = hit(self.losers), hit(self.winners)
        notes = []
        if lose:
            k, v = min(lose, key=lambda kv: kv[1]["wr"])
            notes.append(f"loser {self._fmt(k)} {v['wr']*100:.0f}% on {v['n']}"
                         + (" [PROMOTED]" if v["promoted"] else ""))
        if win:
            k, v = max(win, key=lambda kv: kv[1]["wr"])
            notes.append(f"winner {self._fmt(k)} {v['wr']*100:.0f}% on {v['n']} (info only)")
        promoted = [kv for kv in lose if kv[1]["promoted"]]
        if PATTERN_VETO_ENABLED and promoted:
            return Verdict(AGENT, False, "; ".join(notes))
        return Verdict(AGENT, True, ("shadow — " + "; ".join(notes)) if notes else "no known pattern")

    def record(self, features: dict, win: bool):
        """Append a real closed trade to memory and re-learn."""
        row = {"ts": datetime.now(timezone.utc).isoformat()[:19], "f": features, "win": bool(win)}
        self.rows.append(row)
        try:
            os.makedirs(os.path.dirname(_MEM), exist_ok=True)
            with open(_MEM, "a") as fh:
                fh.write(json.dumps(row) + "\n")
        except Exception as e:
            logger.error(f"Pattern memory write failed: {e}")
        self.learn()
