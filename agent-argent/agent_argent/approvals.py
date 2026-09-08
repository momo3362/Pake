"""Pending-approval queue for orders above the autonomous ceiling.

Flow: the strategy proposes, the guards say "needs approval", the request lands
here and a notification goes out. You answer `valider <id>` or `refuser <id>`.
The runner picks approved requests up on its next pass and executes them.

Two rules make this safe rather than merely convenient:

  * A request expires after approval_ttl_minutes. Approving a 300 EUR buy at a
    price that is twenty minutes stale is approving a different trade.
  * A request records the price it was created at. Execution re-checks that the
    market has not moved past a tolerance, and refuses if it has.
"""

from __future__ import annotations

import json
import subprocess
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

PENDING = "en_attente"
APPROVED = "validee"
REFUSED = "refusee"
EXPIRED = "expiree"
DONE = "executee"


@dataclass
class Request:
    id: str
    at: str
    symbol: str
    asset: str
    side: str
    amount_display: float
    reference_price: float
    reason: str
    status: str = PENDING
    decided_at: str | None = None
    note: str | None = None

    def age_minutes(self, now: datetime) -> float:
        created = datetime.fromisoformat(self.at)
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        return (now - created).total_seconds() / 60.0


class ApprovalQueue:
    def __init__(self, path: Path, ttl_minutes: int) -> None:
        self.path = path
        self.ttl_minutes = ttl_minutes
        self.requests: list[Request] = []
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise RuntimeError(
                f"File de validation illisible ({self.path}): {exc}. "
                "Corrigez ou supprimez explicitement le fichier."
            ) from exc
        self.requests = [Request(**row) for row in raw]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps([asdict(r) for r in self.requests], indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        temporary.replace(self.path)

    def expire_stale(self, now: datetime | None = None) -> list[Request]:
        """Mark timed-out requests expired. Called before every read of the queue."""
        now = now or datetime.now(timezone.utc)
        expired = []
        for request in self.requests:
            if request.status in (PENDING, APPROVED) and request.age_minutes(now) > self.ttl_minutes:
                request.status = EXPIRED
                request.decided_at = now.isoformat(timespec="seconds")
                request.note = f"expiree apres {self.ttl_minutes} min sans execution"
                expired.append(request)
        return expired

    def submit(
        self, *, symbol: str, asset: str, side: str, amount: float, price: float, reason: str
    ) -> Request:
        request = Request(
            id=uuid.uuid4().hex[:8],
            at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            symbol=symbol,
            asset=asset,
            side=side,
            amount_display=round(amount, 2),
            reference_price=price,
            reason=reason,
        )
        self.requests.append(request)
        return request

    def find(self, request_id: str) -> Request | None:
        for request in self.requests:
            if request.id == request_id:
                return request
        return None

    def decide(self, request_id: str, approve: bool, note: str | None = None) -> Request:
        request = self.find(request_id)
        if request is None:
            raise KeyError(f"Demande inconnue: {request_id}")
        if request.status != PENDING:
            raise ValueError(
                f"Demande {request_id} deja {request.status}, aucune decision possible."
            )
        request.status = APPROVED if approve else REFUSED
        request.decided_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        request.note = note
        return request

    def pending(self) -> list[Request]:
        return [r for r in self.requests if r.status == PENDING]

    def approved(self) -> list[Request]:
        return [r for r in self.requests if r.status == APPROVED]

    def prune(self, keep: int = 200) -> None:
        self.requests = self.requests[-keep:]


def render_notification(request: Request, display_asset: str) -> str:
    return (
        f"[agent-argent] VALIDATION REQUISE — {request.symbol}\n"
        f"  {request.side} {request.amount_display:.2f} {display_asset} "
        f"@ {request.reference_price:.6f}\n"
        f"  Motif : {request.reason}\n"
        f"  Repondre : valider {request.id}   |   refuser {request.id}\n"
        f"  Sans reponse, la demande expire et rien n'est execute."
    )


def notify(message: str, command: str | None, log_path: Path) -> None:
    """Send a notification, and always leave a trace on disk.

    The disk trace is written first and unconditionally: a notification channel
    that is down must never mean a silent pending request.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}\n{message}\n\n")

    if not command:
        return
    try:
        subprocess.run(
            command, shell=True, input=message, text=True, timeout=20, check=True,
            capture_output=True,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        # Surfaced, never swallowed: you need to know the channel is broken.
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"ECHEC NOTIFICATION via '{command}': {exc}\n\n")
