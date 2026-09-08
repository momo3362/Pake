"""Minimal signed Binance REST client, structurally incapable of trading.

Design note: the safety property here is not "we chose not to call the order
endpoints". It is that `_get` refuses any path outside READ_ONLY_ENDPOINTS and
the transport only ever issues GET. A future edit that tries to place an order
fails loudly at the client boundary instead of quietly sending money.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_BASE = "https://api.binance.com"

# Every endpoint this agent is permitted to reach. All are read-only.
READ_ONLY_ENDPOINTS = frozenset(
    {
        "/api/v3/ping",
        "/api/v3/time",
        "/api/v3/exchangeInfo",
        "/api/v3/ticker/price",
        "/api/v3/klines",
        "/api/v3/account",
        "/api/v3/myTrades",
    }
)

# Substrings that must never appear in a requested path, as defence in depth
# against an endpoint being added to the allowlist by mistake.
FORBIDDEN_FRAGMENTS = ("order", "withdraw", "transfer", "borrow", "repay", "redeem")


class BinanceError(Exception):
    """A Binance API call failed or was refused locally."""


class ReadOnlyViolation(BinanceError):
    """A caller attempted an endpoint outside the read-only allowlist."""


class BinanceReadOnlyClient:
    def __init__(
        self,
        api_key: str,
        api_secret: str,
        base_url: str = DEFAULT_BASE,
        timeout: float = 15.0,
        recv_window_ms: int = 5000,
    ) -> None:
        self._api_key = api_key
        self._api_secret = api_secret.encode("utf-8")
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._recv_window_ms = recv_window_ms

    # -- transport -------------------------------------------------------

    def _sign(self, query: str) -> str:
        return hmac.new(self._api_secret, query.encode("utf-8"), hashlib.sha256).hexdigest()

    def _get(self, path: str, params: dict | None = None, signed: bool = False):
        if path not in READ_ONLY_ENDPOINTS:
            raise ReadOnlyViolation(
                f"Endpoint refuse: {path}. Cet agent est en lecture seule; "
                f"endpoints autorises: {', '.join(sorted(READ_ONLY_ENDPOINTS))}."
            )
        lowered = path.lower()
        for fragment in FORBIDDEN_FRAGMENTS:
            if fragment in lowered:
                raise ReadOnlyViolation(
                    f"Endpoint refuse: {path} contient le fragment interdit '{fragment}'."
                )

        query_params = dict(params or {})
        if signed:
            query_params["timestamp"] = int(time.time() * 1000)
            query_params["recvWindow"] = self._recv_window_ms

        query = urllib.parse.urlencode(query_params, doseq=True)
        if signed:
            query = f"{query}&signature={self._sign(query)}"

        url = f"{self._base_url}{path}"
        if query:
            url = f"{url}?{query}"

        request = urllib.request.Request(url, method="GET")
        request.add_header("X-MBX-APIKEY", self._api_key)
        request.add_header("User-Agent", "xr-agent-argent/0.1 (read-only)")

        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:500]
            raise BinanceError(self._explain(exc.code, body, signed)) from exc
        except urllib.error.URLError as exc:
            raise BinanceError(f"Binance injoignable ({path}): {exc.reason}") from exc

    @staticmethod
    def _explain(status: int, body: str, signed: bool) -> str:
        """Turn Binance's terse error codes into something actionable."""
        hints = {
            401: "cle API invalide ou revoquee",
            403: "requete bloquee par le WAF Binance",
            418: "IP bannie temporairement pour exces de requetes",
            429: "quota de requetes depasse, ralentir",
        }
        hint = hints.get(status, "")
        if status == 401 and signed:
            hint = (
                "cle API invalide, revoquee, ou IP non autorisee. "
                "Verifiez la liste blanche d'IP dans Binance > Gestion API"
            )
        suffix = f" ({hint})" if hint else ""
        return f"Binance a repondu HTTP {status}{suffix}: {body}"

    # -- read-only endpoints ---------------------------------------------

    def server_time(self) -> int:
        return int(self._get("/api/v3/time")["serverTime"])

    def account(self) -> dict:
        """Balances and permissions. Signed; requires only the 'read' permission."""
        return self._get("/api/v3/account", signed=True)

    def prices(self) -> dict[str, float]:
        rows = self._get("/api/v3/ticker/price")
        return {row["symbol"]: float(row["price"]) for row in rows}

    def exchange_info(self, symbol: str) -> dict:
        info = self._get("/api/v3/exchangeInfo", {"symbol": symbol})
        symbols = info.get("symbols") or []
        if not symbols:
            raise BinanceError(f"Paire inconnue sur Binance: {symbol}")
        return symbols[0]

    def klines(self, symbol: str, interval: str = "1d", limit: int = 100) -> list[list]:
        return self._get(
            "/api/v3/klines", {"symbol": symbol, "interval": interval, "limit": limit}
        )

    def my_trades(self, symbol: str, limit: int = 500) -> list[dict]:
        return self._get("/api/v3/myTrades", {"symbol": symbol, "limit": limit}, signed=True)

    # -- preflight --------------------------------------------------------

    def assert_read_only_key(self) -> dict:
        """Fail fast if the configured key carries trading or withdrawal rights.

        A key that *can* trade is a key that can lose money to a bug. We refuse
        to run with one even though we would never call an order endpoint.
        """
        account = self.account()
        dangerous = []
        if account.get("canTrade"):
            dangerous.append("trading")
        if account.get("canWithdraw"):
            dangerous.append("retrait")
        if dangerous:
            raise BinanceError(
                "REFUS DE DEMARRER: la cle API porte les droits suivants: "
                + ", ".join(dangerous)
                + ". Cet agent exige une cle en lecture seule. "
                "Binance > Gestion API > editez la cle, decochez 'Activer le Trading Spot & Margin' "
                "et 'Activer les retraits', puis restreignez l'acces aux IP de confiance."
            )
        return account
