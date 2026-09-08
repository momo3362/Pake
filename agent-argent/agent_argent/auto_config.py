"""Autonomous-mode parameters: the hard ceilings the bot may never cross.

Every limit here is expressed in the display currency (EUR by default) because
that is how the ceilings were specified. They are converted to the valuation
asset at runtime using the live EURUSDT rate, so a limit of 300 EUR stays 300
EUR whatever the dollar does.

Two independent gates protect autonomous trading:

  1. `armed` must be true in autonomous.json AND the ARMER env var must be set.
     A config file alone cannot arm the bot: an accidental commit of
     `"armed": true` is inert without the environment variable on the VPS.
  2. Every ceiling below is re-checked immediately before each order, against
     freshly fetched balances, never against cached state.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from .config import ConfigError


@dataclass(frozen=True)
class AutoRules:
    # --- the two arming gates -------------------------------------------
    armed: bool = False
    arm_env_var: str = "AGENT_ARGENT_ARME"

    # --- per-order and per-asset ceilings, in display currency ----------
    # Maximum spend on a single autonomous order.
    max_order: float = 300.0
    # Maximum autonomous exposure held in one crypto at any time.
    max_per_asset: float = 300.0
    # Maximum autonomous exposure across all cryptos combined.
    max_total_auto: float = 1500.0

    # --- circuit breakers ------------------------------------------------
    # Realised loss over a rolling day past which the bot stops for the day.
    daily_loss_limit: float = 100.0
    # Cap on order count, so a signal bug cannot churn fees all night.
    max_trades_per_day: int = 20
    # Cap on concurrent autonomous positions.
    max_open_positions: int = 5

    # --- what may be traded autonomously ---------------------------------
    # Empty means nothing. There is no implicit "all symbols" mode.
    whitelist: tuple[str, ...] = ()

    # --- the quick round trip --------------------------------------------
    take_profit_pct: float = 0.015
    stop_loss_pct: float = 0.010
    # A position still open after this long is closed at market, win or lose.
    max_hold_minutes: int = 240

    # --- anything above this goes to you instead of being executed -------
    approval_threshold: float = 300.0
    # Pending approvals older than this expire unexecuted: a stale approval is
    # a decision made against a price that no longer exists.
    approval_ttl_minutes: int = 30

    def validate(self) -> None:
        if self.max_order <= 0:
            raise ConfigError(f"max_order={self.max_order} doit etre positif.")
        if self.max_per_asset < self.max_order:
            raise ConfigError(
                f"max_per_asset={self.max_per_asset} est inferieur a "
                f"max_order={self.max_order}: un seul ordre depasserait deja le "
                "plafond par crypto."
            )
        if self.max_total_auto < self.max_per_asset:
            raise ConfigError(
                f"max_total_auto={self.max_total_auto} est inferieur a "
                f"max_per_asset={self.max_per_asset}."
            )
        if self.daily_loss_limit <= 0:
            raise ConfigError("daily_loss_limit doit etre positif (coupe-circuit).")
        if self.max_trades_per_day < 1:
            raise ConfigError("max_trades_per_day doit valoir au moins 1.")
        if self.max_open_positions < 1:
            raise ConfigError("max_open_positions doit valoir au moins 1.")
        if not 0 < self.stop_loss_pct < 0.5:
            raise ConfigError(f"stop_loss_pct={self.stop_loss_pct} hors bornes ]0,0.5[.")
        if not 0 < self.take_profit_pct < 1:
            raise ConfigError(f"take_profit_pct={self.take_profit_pct} hors bornes ]0,1[.")
        if self.take_profit_pct <= self.stop_loss_pct:
            raise ConfigError(
                f"take_profit_pct={self.take_profit_pct} <= stop_loss_pct="
                f"{self.stop_loss_pct}: chaque trade perdrait de l'argent en esperance "
                "meme avec un taux de reussite de 50%."
            )
        if self.armed and not self.whitelist:
            raise ConfigError(
                "Mode automatique arme avec une liste blanche vide. "
                "Renseignez explicitement les paires autorisees, ex. "
                '"whitelist": ["BTCUSDT", "ETHUSDT"].'
            )
        if self.max_hold_minutes < 1:
            raise ConfigError("max_hold_minutes doit valoir au moins 1.")

    def is_armed(self) -> tuple[bool, str]:
        """Both gates must agree. Returns (armed, reason when not)."""
        if not self.armed:
            return False, 'autonomous.json contient "armed": false'
        if not os.environ.get(self.arm_env_var):
            return False, (
                f"la variable d'environnement {self.arm_env_var} n'est pas definie sur "
                "cette machine (second verrou, volontairement hors du depot)"
            )
        return True, ""

    @classmethod
    def load(cls, path: Path | None) -> "AutoRules":
        if path is None or not path.is_file():
            rules = cls()
            rules.validate()
            return rules
        raw = json.loads(path.read_text(encoding="utf-8"))
        known = set(cls.__dataclass_fields__)
        unknown = set(raw) - known
        if unknown:
            raise ConfigError(
                f"Cles inconnues dans {path}: {', '.join(sorted(unknown))}. "
                f"Cles valides: {', '.join(sorted(known))}."
            )
        if "whitelist" in raw:
            raw["whitelist"] = tuple(s.upper() for s in raw["whitelist"])
        rules = cls(**raw)
        rules.validate()
        return rules
