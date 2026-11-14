"""Valio Aimo's own records: offers it issued and orders it accepted. A separate database from the buyer's."""
from __future__ import annotations

from sqlalchemy import Float, Integer, String, Text, create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

import settings as config


class Base(DeclarativeBase):
    pass


class OfferRecord(Base):
    __tablename__ = "offers"

    offer_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    issued_at: Mapped[str] = mapped_column(String(40))
    expires_at: Mapped[str] = mapped_column(String(40))
    buyer_org: Mapped[str | None] = mapped_column(String(200), nullable=True)
    offer_json: Mapped[str] = mapped_column(Text)                      # the signed offer, as sent
    used_by: Mapped[str | None] = mapped_column(String(40), nullable=True)   # order id; an offer is used once


class OrderRecord(Base):
    __tablename__ = "orders"

    order_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    offer_id: Mapped[str] = mapped_column(String(40), unique=True)
    mandate_id: Mapped[str] = mapped_column(String(80), index=True)
    buyer_org: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20))                    # CONFIRMED | DELIVERED | CANCELLED
    total_net: Mapped[float] = mapped_column(Float)
    confirmed_at: Mapped[str] = mapped_column(String(40))              # ISO-8601, Helsinki time
    delivery_date: Mapped[str] = mapped_column(String(10))
    approval_signature: Mapped[str | None] = mapped_column(String(120), nullable=True, unique=True)  # used once
    confirmation_json: Mapped[str] = mapped_column(Text)               # the signed confirmation, as sent
    buyer_reference: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)  # resends match it


class MandateRecord(Base):
    """The newest version of each buyer mandate Valio Aimo has seen. Older versions are refused."""

    __tablename__ = "mandates"

    mandate_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    version: Mapped[int] = mapped_column(Integer)
    buyer_org: Mapped[str] = mapped_column(String(200))
    recorded_at: Mapped[str] = mapped_column(String(40))
    mandate_json: Mapped[str] = mapped_column(Text)


def make_sessionmaker(url: str | None = None):
    url = url or config.GATEWAY_DATABASE_URL
    connect_args = {"timeout": 30, "check_same_thread": False} if url.startswith("sqlite") else {}
    engine = create_engine(url, connect_args=connect_args)
    Base.metadata.create_all(engine)
    with engine.begin() as conn:                    # a database from before resends: add the column
        if "buyer_reference" not in {c["name"] for c in inspect(conn).get_columns("orders")}:
            conn.execute(text("ALTER TABLE orders ADD COLUMN buyer_reference VARCHAR(80)"))
    return sessionmaker(engine, expire_on_commit=False)
