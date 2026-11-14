"""A small world for end-to-end tests: Valio Aimo's gateway (with a fake backend) and a buyer, each with its own
database and keys, talking over HTTP (in-process). No network, no LLM.

Prices are 1.00 EUR per litre in 1-litre packs, so an order's total in EUR equals its litres.
Hotel mandate: the agent may order up to 2,500 EUR alone and 8,000 EUR a week; 10,000 EUR per order is the cap.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

import settings
from app import keys
from app.db import init_db, make_engine
from app.gateway_client import GatewayClient
from gateway import catalog
from gateway.api import create_app
from gateway.backend import Money, ProductAvailability, ProductPrice, ScheduleLine
from gateway.keys import gateway_signer, registry
from gateway.service import Gateway
from gateway.store import make_sessionmaker

NOW = datetime(2026, 11, 13, 12, 0, tzinfo=settings.HELSINKI)        # Friday
CLOSING = datetime(2026, 11, 15, 17, 0, tzinfo=settings.HELSINKI)    # Sunday 17:00 cut-off for Monday delivery


def product(pid: str, name: str = "Lactose-free milk 1l", contains=("AM",), may_contain=()) -> dict:
    return {"id": pid, "gtin": None, "names": {"en": f"{name} {pid}"}, "brand": "Valio", "vendor": "VALIO OY",
            "category": "10400", "sales_unit": "ST", "contains": list(contains), "may_contain": list(may_contain),
            "free_from": [], "claims": ["FREE_FROM_LACTOSE"], "labels": [], "has_allergen_data": True,
            "reliability": 0.99, "co2_per_kg": None, "description_en": None}


INDEX = [product("100001"), product("100002")]


class FakeValio:
    """The challenge's price and availability backend, with knobs."""

    def __init__(self):
        self.price, self.delay, self.fail = 1.0, 0.0, False

    def prices(self, ids, requested):
        time.sleep(self.delay)
        if self.fail:
            raise RuntimeError("HTTP 503")
        return {i: ProductPrice(productId=i, catalogPrice=Money(value=self.price, valueWithTax=self.price * 1.14),
                                comparisonPrice=Money(value=self.price), comparisonUnit="L") for i in ids}

    def availability(self, ids, requested):
        # Cut-off at 17:00 the day before delivery (CLOSING for a Monday delivery).
        closing = datetime.combine(requested - timedelta(days=1), CLOSING.timetz())
        return {i: ProductAvailability(productId=i, closingTime=closing, logisticModel="Stock",
                                       scheduleLines=[ScheduleLine(availableAmount=100_000, availableDate=requested)])
                for i in ids}


class Clock:
    def __init__(self, now: datetime):
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@dataclass
class World:
    backend: FakeValio
    clock: Clock
    gateway: Gateway
    client: GatewayClient
    buyer_db: sessionmaker
    mandates: dict


@pytest.fixture
def world(tmp_path, monkeypatch) -> World:
    monkeypatch.setattr(settings, "KEYS_DIR", tmp_path / "keys")
    monkeypatch.setattr(catalog, "load_index", lambda: INDEX)
    backend, clock = FakeValio(), Clock(NOW)
    gw = Gateway(backend, make_sessionmaker(f"sqlite:///{tmp_path / 'gateway.db'}"), gateway_signer(), registry(),
                 clock=clock)
    engine = make_engine(f"sqlite:///{tmp_path / 'buyer.db'}")
    init_db(engine)
    with TestClient(create_app(gw)) as http:
        yield World(backend, clock, gw, GatewayClient(http=http), sessionmaker(engine, expire_on_commit=False),
                    keys.load_mandates())
    engine.dispose()
