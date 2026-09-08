"""Signed Binance REST clients with a hard capability boundary.

Two clients live here:

  BinanceReadOnlyClient  reaches only read endpoints, and refuses to start
                         against a key that carries trading rights.
  BinanceTradingClient   additionally reaches the spot order endpoints, and
                         refuses to start against a key that can WITHDRAW.

The withdrawal ban is the property that survives every other bug in this
codebase: whatever the strategy does wrong, it cannot move funds off Binance.
Paths are checked against a per-client allowlist AND against FORBIDDEN_FRAGMENTS,
so an endpoint that takes money out stays unreachable even if someone later adds
it to an allowlist by mistake.
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

# Endpoints the trading client may additionally reach. All are spot order
# operations; none of them can move funds off the exchange.
TRADING_ENDPOINTS = frozenset(
    {
        "/api/v3/order",
        "/api/v3/order/oco",
        "/api/v3/openOrders",
    }
)

# Substrings that must never appear in a requested path, whatever any allowlist
# says. These are the operations that take money out of the account.
FORBIDDEN_FRAGMENTS = ("withdraw", "transfer", "borrow", "repay", "redeem", "sub-account")


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

    # Capability boundary. Subclasses widen these; nothing else may.
    allowed_endpoints: frozenset[str] = READ_ONLY_ENDPOINTS
    allowed_methods: frozenset[str] = frozenset({"GET"})

    def _check_path(self, path: str, method: str) -> None:
        lowered = path.lower()
        for fragment in FORBIDDEN_FRAGMENTS:
            if fragment in lowered:
                raise ReadOnlyViolation(
                    f"Endpoint refuse: {path} contient le fragment interdit '{fragment}'. "
                    "Aucun code de cet agent ne peut sortir de fonds de Binance."
                )
        if path not in self.allowed_endpoints:
            raise ReadOnlyViolation(
                f"Endpoint refuse: {path}. Autorises pour {type(self).__name__}: "
                f"{', '.join(sorted(self.allowed_endpoints))}."
            )
        if method not in self.allowed_methods:
            raise ReadOnlyViolation(
                f"Methode refusee: {method} sur {path} pour {type(self).__name__}."
            )

    def _get(self, path: str, params: dict | None = None, signed: bool = False):
        return self._request("GET", path, params, signed)

    def _request(
        self, method: str, path: str, params: dict | None = None, signed: bool = False
    ):
        self._check_path(path, method)

        query_params = dict(params or {})
        if signed:
            query_params["timestamp"] = int(time.time() * 1000)
            query_params["recvWindow"] = self._recv_window_ms

        query = urllib.parse.urlencode(query_params, doseq=True)
        if signed:
            query = f"{query}&signature={self._sign(query)}"

        url = f"{self._base_url}{path}"
        data = None
        if method == "GET":
            if query:
                url = f"{url}?{query}"
        else:
            # Binance accepts signed parameters as a form body on write calls.
            data = query.encode("utf-8")

        request = urllib.request.Request(url, data=data, method=method)
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

    def assert_no_withdrawal(self) -> dict:
        """The floor every mode enforces: the key must not be able to withdraw.

        Monitoring and autonomous trading disagree about the trading right, but
        neither ever needs to move funds off the exchange.
        """
        account = self.account()
        if account.get("canWithdraw"):
            raise BinanceError(
                "REFUS DE DEMARRER: la cle API autorise les RETRAITS. "
                "Binance > Gestion API > editez la cle et decochez "
                "'Activer les retraits', puis restreignez l'acces a l'IP du VPS."
            )
        return account

    def assert_read_only_key(self) -> dict:
        """Stricter check for monitor-only deployments: no trading either.

        Only call this when the operator has said they want a key that cannot
        trade. In autonomous mode the trading right is required, so this check
        would reject a correctly configured key.
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


class BinanceTradingClient(BinanceReadOnlyClient):
    """Read endpoints plus spot orders. Cannot withdraw, transfer or borrow.

    Placing an order is deliberately awkward to reach: callers go through
    execution.py, which applies the hard caps first. Nothing in this class
    enforces position sizing; that is not its job.
    """

    allowed_endpoints = READ_ONLY_ENDPOINTS | TRADING_ENDPOINTS
    allowed_methods = frozenset({"GET", "POST", "DELETE"})

    def assert_trading_key(self) -> dict:
        """Refuse a key that can withdraw. Trading rights are expected here."""
        account = self.account()
        if account.get("canWithdraw"):
            raise BinanceError(
                "REFUS DE DEMARRER: la cle API autorise les RETRAITS. "
                "Un bot ne doit jamais pouvoir sortir de fonds. "
                "Binance > Gestion API > editez la cle, decochez 'Activer les retraits', "
                "gardez 'Activer le Trading Spot & Margin', "
                "et restreignez l'acces a l'IP du VPS."
            )
        if not account.get("canTrade"):
            raise BinanceError(
                "La cle API ne porte pas le droit de trading, requis en mode automatique. "
                "Binance > Gestion API > cochez 'Activer le Trading Spot & Margin'."
            )
        return account

    def market_buy(self, symbol: str, quote_amount: float, client_id: str) -> dict:
        """Spend exactly `quote_amount` of the quote asset. Binance sizes the base."""
        return self._request(
            "POST",
            "/api/v3/order",
            {
                "symbol": symbol,
                "side": "BUY",
                "type": "MARKET",
                "quoteOrderQty": f"{quote_amount:.8f}".rstrip("0").rstrip("."),
                "newClientOrderId": client_id,
                "newOrderRespType": "FULL",
            },
            signed=True,
        )

    def market_sell(self, symbol: str, quantity: str, client_id: str) -> dict:
        return self._request(
            "POST",
            "/api/v3/order",
            {
                "symbol": symbol,
                "side": "SELL",
                "type": "MARKET",
                "quantity": quantity,
                "newClientOrderId": client_id,
                "newOrderRespType": "FULL",
            },
            signed=True,
        )

    def protective_oco_sell(
        self,
        symbol: str,
        quantity: str,
        take_profit: str,
        stop_trigger: str,
        stop_limit: str,
        client_id: str,
    ) -> dict:
        """One-cancels-the-other exit: take profit above, stop loss below.

        OCO matters here. Two independent orders could both fill on a wick and
        leave a short position the spot account cannot hold.
        """
        return self._request(
            "POST",
            "/api/v3/order/oco",
            {
                "symbol": symbol,
                "side": "SELL",
                "quantity": quantity,
                "price": take_profit,
                "stopPrice": stop_trigger,
                "stopLimitPrice": stop_limit,
                "stopLimitTimeInForce": "GTC",
                "listClientOrderId": client_id,
            },
            signed=True,
        )

    def open_orders(self, symbol: str | None = None) -> list[dict]:
        return self._request(
            "GET", "/api/v3/openOrders", {"symbol": symbol} if symbol else {}, signed=True
        )
