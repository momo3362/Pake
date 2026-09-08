"""Persistent equity history: the high-water mark drawdown rules are measured against."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

MAX_HISTORY = 400


class State:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.high_water_mark: float = 0.0
        self.history: list[dict] = []
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            # A corrupt state file must not silently reset the high-water mark:
            # that would erase a real drawdown and unblock trading proposals.
            raise RuntimeError(
                f"Etat illisible ({self.path}): {exc}. "
                "Corrigez ou supprimez explicitement le fichier avant de relancer."
            ) from exc
        self.high_water_mark = float(raw.get("high_water_mark", 0.0))
        self.history = list(raw.get("history", []))

    def record(self, equity: float, valuation_asset: str) -> None:
        self.high_water_mark = max(self.high_water_mark, equity)
        self.history.append(
            {
                "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "equity": round(equity, 2),
                "asset": valuation_asset,
            }
        )
        self.history = self.history[-MAX_HISTORY:]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"high_water_mark": round(self.high_water_mark, 2), "history": self.history}
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        temporary.replace(self.path)

    def previous_equity(self) -> float | None:
        return self.history[-2]["equity"] if len(self.history) >= 2 else None
