"""The buyer agent's client for Valio Aimo's agent gateway. HTTP only: no shared code paths, no shared database.

Every call can be recorded (method, path, request, response, timing) so the console can show the exchange.
Offers and order confirmations are checked against Valio Aimo's published key before anything relies on them.
"""
from __future__ import annotations

import copy
import time
from dataclasses import dataclass
from datetime import date
from typing import Callable

import httpx

import settings as config
from protocol.signing import verify_document
from protocol.wire import Offer, OrderConfirmation, OrderRefused, SearchResult


@dataclass
class Exchange:
    method: str
    path: str
    request: dict | None
    status: int
    response: dict | list | None
    ms: int


class GatewayError(RuntimeError):
    """Valio Aimo could not be reached or answered with an error."""


class UntrustedResponse(GatewayError):
    """A document claiming to be from Valio Aimo does not carry Valio Aimo's signature."""


class OrderRefusedError(GatewayError):
    def __init__(self, refusal: OrderRefused, status: int):
        super().__init__(refusal.error)
        self.refusal, self.status = refusal, status


class GatewayClient:
    def __init__(self, base_url: str | None = None, http: httpx.Client | None = None, timeout: float = 30.0):
        self.http = http or httpx.Client(base_url=base_url or config.GATEWAY_URL, timeout=timeout)
        self.on_exchange: Callable[[Exchange], None] | None = None
        self._cache: dict[str, str] = {}          # shared with recording() copies

    def recording(self, on_exchange: Callable[[Exchange], None]) -> "GatewayClient":
        """A copy of this client that reports every exchange to on_exchange (one per run, so threads don't mix)."""
        clone = copy.copy(self)
        clone.on_exchange = on_exchange
        return clone

    # ------------------------------------------------------------ transport

    def _call(self, method: str, path: str, *, json: dict | None = None, params: dict | None = None):
        started = time.monotonic()
        try:
            r = self.http.request(method, path, json=json, params=params)
        except httpx.HTTPError as e:
            raise GatewayError(f"Valio Aimo could not be reached ({type(e).__name__})") from e
        try:
            body = r.json()
        except ValueError:
            body = {"detail": r.text[:500]}
        if self.on_exchange:
            self.on_exchange(Exchange(method, r.request.url.raw_path.decode(), json, r.status_code, body,
                                      int((time.monotonic() - started) * 1000)))
        return r.status_code, body

    def _ok(self, status: int, body, what: str):
        if status >= 400:
            detail = body.get("detail") if isinstance(body, dict) else None
            raise GatewayError(f"{what} failed: HTTP {status}" + (f", {detail}" if detail else ""))
        return body

    # ------------------------------------------------------------ calls

    def public_key(self) -> str:
        """Valio Aimo's Ed25519 key, from its published key set (fetched once, then cached)."""
        if "key" not in self._cache:
            body = self._ok(*self._call("GET", "/.well-known/jwks.json"), "fetching Valio Aimo's key")
            keys = [k for k in body.get("keys", []) if k.get("kty") == "OKP" and k.get("crv") == "Ed25519"]
            if not keys:
                raise UntrustedResponse("Valio Aimo publishes no Ed25519 key")
            self._cache["key"] = keys[0]["x"]
        return self._cache["key"]

    def search(self, terms: list[str], category: str | None, exclude: list[str], claims: list[str],
               allergens: list[str]) -> SearchResult:
        params = {"q": " ".join(terms), "exclude": exclude, "claims": claims, "allergens": allergens}
        if category:
            params["category"] = category
        return SearchResult.model_validate(self._ok(*self._call("GET", "/v1/products", params=params), "search"))

    def offer(self, products: list[str], delivery_by: date, buyer_org: str) -> tuple[Offer, dict]:
        body = self._ok(*self._call("POST", "/v1/offers", json={
            "products": products, "delivery_by": delivery_by.isoformat(), "buyer_org": buyer_org}), "offer request")
        if not verify_document(body, self.public_key()):
            raise UntrustedResponse("the offer is not signed by Valio Aimo")
        return Offer.model_validate(body), body

    def place_order(self, signed_order: dict, mandate: dict) -> tuple[OrderConfirmation, dict]:
        status, body = self._call("POST", "/v1/orders", json={"order": signed_order, "mandate": mandate})
        if status in (403, 409, 422) and isinstance(body, dict) and "error" in body:
            raise OrderRefusedError(OrderRefused.model_validate(body), status)
        body = self._ok(status, body, "order")
        if not verify_document(body, self.public_key()):
            raise UntrustedResponse("the order confirmation is not signed by Valio Aimo")
        return OrderConfirmation.model_validate(body), body

    def get_order(self, order_id: str) -> dict:
        return self._ok(*self._call("GET", f"/v1/orders/{order_id}"), "order status")
