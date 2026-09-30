"""One auditable decision record per proposed setup."""
from dataclasses import dataclass, field
from typing import List, Optional

from utils.logger import setup_logger
logger = setup_logger("decision")   # bot's own logger -> visible in Railway


@dataclass
class Verdict:
    agent: str
    approved: bool
    reason: str = ""


@dataclass
class DecisionRecord:
    symbol: str
    side: str
    entry: float
    sl: float
    verdicts: List[Verdict] = field(default_factory=list)

    def add(self, verdict: Verdict) -> Verdict:
        self.verdicts.append(verdict)
        return verdict

    @property
    def vetoed_by(self) -> Optional[Verdict]:
        return next((v for v in self.verdicts if not v.approved), None)

    def log(self, outcome: str) -> None:
        chain = " -> ".join(
            f"{v.agent}:{'OK' if v.approved else 'VETO'}"
            + (f"({v.reason})" if v.reason else "")
            for v in self.verdicts)
        logger.info(f"DECISION | {self.symbol} {self.side} entry={self.entry:.4f} "
                    f"sl={self.sl:.4f} | Structure:PROPOSED -> {chain} => {outcome}")
