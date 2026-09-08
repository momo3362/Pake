"""Configuration loading: credentials from the environment, risk rules from a file."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


class ConfigError(Exception):
    """Raised when configuration is missing or internally inconsistent."""


def load_dotenv(path: Path) -> None:
    """Load KEY=VALUE lines into os.environ without overwriting existing values.

    Kept dependency-free on purpose: this agent runs on the VPS with the system
    Python and no virtualenv guarantees.
    """
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


@dataclass(frozen=True)
class Credentials:
    api_key: str
    api_secret: str

    @classmethod
    def from_env(cls, env_path: Path | None = None) -> "Credentials":
        """Read credentials, and say precisely which setup step is missing.

        "Copied .env.example but never filled it in" is by far the most common
        way this fails, and it deserves a different message from "no .env at
        all": the remedy is not the same.
        """
        key = os.environ.get("BINANCE_API_KEY", "").strip()
        secret = os.environ.get("BINANCE_API_SECRET", "").strip()
        if key and secret:
            return cls(api_key=key, api_secret=secret)

        missing = [
            name
            for name, value in (("BINANCE_API_KEY", key), ("BINANCE_API_SECRET", secret))
            if not value
        ]

        if env_path is None or not env_path.exists():
            location = env_path or Path(".env")
            raise ConfigError(
                f"Aucun fichier {location} trouve.\n"
                f"  1. cp {location.parent / '.env.example'} {location}\n"
                f"  2. ouvrez {location} et collez vos deux cles Binance\n"
                "  3. relancez cette commande"
            )

        verb = "n'ont pas de valeur" if len(missing) > 1 else "n'a pas de valeur"
        raise ConfigError(
            f"Le fichier {env_path} existe mais {' et '.join(missing)} {verb}.\n"
            "  Vous avez probablement copie .env.example sans le remplir.\n"
            f"  Ouvrez {env_path} et collez vos cles apres le signe '=', "
            "sans guillemets ni espace:\n"
            "    BINANCE_API_KEY=abc123...\n"
            "    BINANCE_API_SECRET=def456...\n"
            "  Ces cles ne doivent jamais etre commitees ni collees dans un message."
        )


@dataclass(frozen=True)
class RiskRules:
    """Money-management parameters. Every value is a hard ceiling, never a target."""

    # Fraction of total equity risked on a single idea, i.e. the loss taken if
    # the stop is hit. 0.01 == 1%.
    risk_per_trade: float = 0.01
    # Ceiling on what one asset may represent in the portfolio.
    max_position_pct: float = 0.20
    # Ceiling on the total non-stablecoin exposure.
    max_total_exposure_pct: float = 0.60
    # Drawdown from the equity high-water mark past which no new risk is proposed.
    max_drawdown_pct: float = 0.20
    # Stop distance expressed in ATR multiples.
    atr_period: int = 14
    atr_multiple: float = 2.0
    # Assets treated as cash rather than exposure.
    stable_assets: tuple[str, ...] = ("USDT", "USDC", "FDUSD", "BUSD", "TUSD", "EUR")
    # Asset every position is valued in before optional display conversion.
    valuation_asset: str = "USDT"
    # Display currency; set to USDT to disable conversion.
    display_asset: str = "EUR"
    # Balances worth less than this in valuation_asset are ignored as dust.
    dust_threshold: float = 1.0

    def validate(self) -> None:
        if not 0 < self.risk_per_trade <= 0.05:
            raise ConfigError(
                f"risk_per_trade={self.risk_per_trade} hors bornes: "
                "attendu >0 et <=0.05 (5% par idee est deja tres agressif)."
            )
        if not 0 < self.max_position_pct <= 1:
            raise ConfigError(f"max_position_pct={self.max_position_pct} hors bornes ]0,1].")
        if not 0 < self.max_total_exposure_pct <= 1:
            raise ConfigError(
                f"max_total_exposure_pct={self.max_total_exposure_pct} hors bornes ]0,1]."
            )
        if self.max_position_pct > self.max_total_exposure_pct:
            raise ConfigError(
                "max_position_pct ne peut pas depasser max_total_exposure_pct: "
                f"{self.max_position_pct} > {self.max_total_exposure_pct}."
            )
        if not 0 < self.max_drawdown_pct < 1:
            raise ConfigError(f"max_drawdown_pct={self.max_drawdown_pct} hors bornes ]0,1[.")
        if self.atr_period < 2:
            raise ConfigError(f"atr_period={self.atr_period} doit valoir au moins 2.")
        if self.atr_multiple <= 0:
            raise ConfigError(f"atr_multiple={self.atr_multiple} doit etre strictement positif.")

    @classmethod
    def load(cls, path: Path | None) -> "RiskRules":
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
        if "stable_assets" in raw:
            raw["stable_assets"] = tuple(raw["stable_assets"])
        rules = cls(**raw)
        rules.validate()
        return rules
