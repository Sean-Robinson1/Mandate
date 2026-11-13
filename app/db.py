"""Database setup. SQLite locally, PostgreSQL by changing DATABASE_URL only.

Timestamps on runs and orders are ISO-8601 in Helsinki time (the buyer's business clock, used for weekly
spend). Audit events are stamped in UTC.
"""
from sqlalchemy import Float, String, Text, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from app import config


class Base(DeclarativeBase):
    pass


class Order(Base):
    __tablename__ = "orders"

    order_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    trace_id: Mapped[str] = mapped_column(String(40), index=True, unique=True)   # at most one order per request
    mandate_id: Mapped[str] = mapped_column(String(80), index=True)
    buyer_org: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(30))            # PLACED | CANCELLED
    total_net: Mapped[float] = mapped_column(Float)
    created_at: Mapped[str] = mapped_column(String(40))        # ISO-8601, Helsinki time
    lines_json: Mapped[str] = mapped_column(Text)
    receipt: Mapped[str] = mapped_column(Text, default="")


class Run(Base):
    """One buyer request and everything the agent did with it. Feeds the UI and the approval queue."""

    __tablename__ = "runs"

    trace_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    created_at: Mapped[str] = mapped_column(String(40), index=True)   # ISO-8601, Helsinki time
    request_text: Mapped[str] = mapped_column(Text)
    mandate_id: Mapped[str] = mapped_column(String(80))
    buyer_org: Mapped[str] = mapped_column(String(200))
    # ORDERED | NEEDS_HUMAN | REFUSED | NEEDS_CLARIFICATION | APPROVED | DECLINED | FAILED
    status: Mapped[str] = mapped_column(String(30), index=True)
    total_net: Mapped[float] = mapped_column(Float, default=0.0)
    intent_json: Mapped[str] = mapped_column(Text, default="")
    plan_json: Mapped[str] = mapped_column(Text, default="")
    policy_json: Mapped[str] = mapped_column(Text, default="")
    receipt: Mapped[str] = mapped_column(Text, default="")
    order_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    approval_json: Mapped[str] = mapped_column(Text, default="")   # signed approval record, if any


def make_engine(url: str | None = None):
    url = url or config.DATABASE_URL
    if url.startswith("postgres://"):                           # Aiven-style URL
        url = url.replace("postgres://", "postgresql://", 1)
    connect_args = {}
    if url.startswith("sqlite"):
        # Writers queue on SQLite's database lock; wait for it rather than failing with "database is locked".
        connect_args = {"timeout": 30, "check_same_thread": False}
    return create_engine(url, connect_args=connect_args)


engine = make_engine()
SessionLocal = sessionmaker(engine, expire_on_commit=False)


def init_db(bind=None):
    import app.audit  # noqa: F401  registers the audit tables on Base.metadata

    Base.metadata.create_all(bind or engine)
