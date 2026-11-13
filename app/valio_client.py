"""Thin typed client for the Valio Aimo mock API (price + availability).

The mock has no order endpoint; ordering is mocked locally in app/orders.py (see ASSUMPTIONS.md).
"""
from __future__ import annotations

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
        for _ in range(3):                                      # undisclosed rate limits: small retry
            try:
                r = self._http.post(path, json=body)
                if r.status_code in (429, 502, 503):
                    last = httpx.HTTPStatusError("retryable", request=r.request, response=r)
                    continue
                r.raise_for_status()
                return r.json()
            except httpx.TransportError as e:
                last = e
        raise RuntimeError(f"Valio API {path} failed: {last}")

    def ping(self) -> bool:
        return self._http.get("/api/ping").json().get("status") == "ok"

    def prices(self, product_ids: list[str], requested: date) -> dict[str, ProductPrice]:
        data = self._post("/api/price", product_ids, requested)
        return {p["productId"]: ProductPrice.model_validate(p) for p in data["products"]}

    def availability(self, product_ids: list[str], requested: date) -> dict[str, ProductAvailability]:
        data = self._post("/api/availability", product_ids, requested)
        return {p["productId"]: ProductAvailability.model_validate(p) for p in data["products"]}
