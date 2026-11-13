"""Thin typed client for the Valio Aimo mock API (price + availability).

The mock has no order endpoint; ordering is mocked locally in app.agent.place_order (see ASSUMPTIONS.md).
"""
from __future__ import annotations

import time
from datetime import date, datetime
from typing import Protocol

import httpx
from pydantic import BaseModel

from app import config


class Money(BaseModel):
    value: float
    valueWithTax: float | None = None
    currency: str = "EUR"


class ProductPrice(BaseModel):
    productId: str
    catalogPrice: Money | None = None
    comparisonPrice: Money | None = None
    comparisonUnit: str | None = None
    listPrice: Money | None = None
    netPriceFound: bool = False
    error: bool = False


class ScheduleLine(BaseModel):
    availableAmount: float = 0.0
    availableDate: date | None = None      # absent when nothing is available
    salesUnit: str | None = None
    salesBlock: bool = False


class ProductAvailability(BaseModel):
    productId: str
    closingTime: datetime | None = None
    salesBlock: bool = False
    logisticModel: str | None = None
    scheduleLines: list[ScheduleLine] = []


class ValioAPI(Protocol):
    """The interface the rest of the app depends on. Tests and the offline demo use a fake."""

    def prices(self, product_ids: list[str], requested: date) -> dict[str, ProductPrice]: ...

    def availability(self, product_ids: list[str], requested: date) -> dict[str, ProductAvailability]: ...


class ValioClient:
    MAX_PRODUCTS = 1000
    ATTEMPTS = 3
    RETRYABLE = (429, 502, 503, 504)

    def __init__(self, base_url: str | None = None, api_key: str | None = None, customer: str = "123"):
        self.customer = customer
        self._http = httpx.Client(
            base_url=base_url or config.VALIO_API_BASE_URL,
            headers={"X-API-Key": api_key or config.VALIO_API_KEY},
            timeout=20.0,
        )

    def _post(self, path: str, product_ids: list[str], requested: date) -> dict:
        if not 1 <= len(product_ids) <= self.MAX_PRODUCTS:
            raise ValueError(f"need 1-{self.MAX_PRODUCTS} product ids, got {len(product_ids)}")
        body = {"customer": self.customer, "requestedDate": requested.isoformat(), "products": product_ids}
        last: Exception | None = None
        for attempt in range(self.ATTEMPTS):                    # undisclosed rate limits: retry with backoff
            if attempt:
                time.sleep(self._delay(attempt, last))
            try:
                r = self._http.post(path, json=body)
            except httpx.TransportError as e:
                last = e
                continue
            if r.status_code in self.RETRYABLE:
                last = httpx.HTTPStatusError(f"HTTP {r.status_code}", request=r.request, response=r)
                continue
            r.raise_for_status()
            return r.json()
        raise RuntimeError(f"Valio API {path} failed after {self.ATTEMPTS} attempts: {last}")

    @staticmethod
    def _delay(attempt: int, last: Exception | None) -> float:
        """Honour Retry-After (seconds) when the server sends it, else exponential backoff; at most 5 s."""
        if isinstance(last, httpx.HTTPStatusError):
            try:
                return min(5.0, max(0.0, float(last.response.headers.get("Retry-After", ""))))
            except ValueError:
                pass
        return min(5.0, 0.5 * 2 ** (attempt - 1))

    def ping(self) -> bool:
        return self._http.get("/api/ping").json().get("status") == "ok"

    def prices(self, product_ids: list[str], requested: date) -> dict[str, ProductPrice]:
        data = self._post("/api/price", product_ids, requested)
        return {p["productId"]: ProductPrice.model_validate(p) for p in data["products"]}

    def availability(self, product_ids: list[str], requested: date) -> dict[str, ProductAvailability]:
        data = self._post("/api/availability", product_ids, requested)
        return {p["productId"]: ProductAvailability.model_validate(p) for p in data["products"]}
