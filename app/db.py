"""The buyer's database. SQLite locally, PostgreSQL by changing DATABASE_URL only.

Timestamps on runs, steps and orders are ISO-8601 in Helsinki time (the buyer's business clock, used for weekly
spend). Audit events are stamped in UTC.
"""
from sqlalchemy import Float, Integer, String, Text, create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

import settings as config


class Base(DeclarativeBase):
    pass


class Order(Base):
    """The buyer's purchase order. Counts towards the week's spend from the moment it is reserved."""

    __tablename__ = "orders"

    order_id: Mapped[str] = mapped_column(String(40), primary_key=True)        # buyer's PO number
    trace_id: Mapped[str] = mapped_column(String(40), index=True, unique=True)   # at most one order per request
    mandate_id: Mapped[str] = mapped_column(String(80), index=True)
    buyer_org: Mapped[str] = mapped_column(String(200))
    # RESERVED (being placed) | PLACED (confirmed by Valio Aimo) | REJECTED (refused or failed; spends nothing)
    status: Mapped[str] = mapped_column(String(30))
    total_net: Mapped[float] = mapped_column(Float)
    created_at: Mapped[str] = mapped_column(String(40))
    lines_json: Mapped[str] = mapped_column(Text)
    receipt: Mapped[str] = mapped_column(Text, default="")
    supplier_order_id: Mapped[str | None] = mapped_column(String(40), nullable=True)   # Valio Aimo's order id
    delivery_date: Mapped[str | None] = mapped_column(String(10), nullable=True)
    confirmation_json: Mapped[str] = mapped_column(Text, default="")   # Valio Aimo's signed confirmation


class Run(Base):
    """One buyer request and everything the agent did with it. Feeds the UI and the approval queue."""

    __tablename__ = "runs"

    trace_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    created_at: Mapped[str] = mapped_column(String(40), index=True)
    request_text: Mapped[str] = mapped_column(Text)
    mandate_id: Mapped[str] = mapped_column(String(80))
    buyer_org: Mapped[str] = mapped_column(String(200))
    # WORKING | ORDERED | NEEDS_HUMAN | DECIDING | APPROVED | DECLINED | REFUSED | EXPIRED | NEEDS_CLARIFICATION
    # | FAILED
    status: Mapped[str] = mapped_column(String(30), index=True)
    total_net: Mapped[float] = mapped_column(Float, default=0.0)
    intent_json: Mapped[str] = mapped_column(Text, default="")
    plan_json: Mapped[str] = mapped_column(Text, default="")
    policy_json: Mapped[str] = mapped_column(Text, default="")
    offer_json: Mapped[str] = mapped_column(Text, default="")        # Valio Aimo's signed offer the plan uses
    receipt: Mapped[str] = mapped_column(Text, default="")
    order_id: Mapped[str | None] = mapped_column(String(40), nullable=True)      # Valio Aimo's order id
    approval_json: Mapped[str] = mapped_column(Text, default="")     # signed approval record, if any
    requested_by: Mapped[str] = mapped_column(String(80), default="")   # person id, or "" for the agent itself


class Step(Base):
    """One visible step of a run: something the agent did, an exchange with Valio Aimo, or a person's decision."""

    __tablename__ = "steps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trace_id: Mapped[str] = mapped_column(String(40), index=True)
    at: Mapped[str] = mapped_column(String(40))
    lane: Mapped[str] = mapped_column(String(10))                    # agent | exchange | person
    title: Mapped[str] = mapped_column(Text, default="")
    detail: Mapped[str] = mapped_column(Text, default="")
    tone: Mapped[str] = mapped_column(String(10), default="")        # ok | warn | bad | ""
    method: Mapped[str] = mapped_column(String(8), default="")
    path: Mapped[str] = mapped_column(Text, default="")
    status_code: Mapped[int] = mapped_column(Integer, default=0)
    ms: Mapped[int] = mapped_column(Integer, default=0)
    request_json: Mapped[str] = mapped_column(Text, default="")
    response_json: Mapped[str] = mapped_column(Text, default="")


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


def _add_missing_columns(bind) -> None:
    """Databases created by an earlier version: add new columns (all nullable or with a default)."""
    insp = inspect(bind)
    with bind.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue
            existing = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in existing:
                    continue
                default = col.default.arg if col.default is not None and not callable(col.default.arg) else None
                ddl = f'ALTER TABLE {table.name} ADD COLUMN {col.name} {col.type.compile(bind.dialect)}'
                if default is not None:
                    ddl += " DEFAULT " + (f"'{default}'" if isinstance(default, str) else str(default))
                conn.execute(text(ddl))


def init_db(bind=None):
    import app.audit  # noqa: F401  registers the audit tables on Base.metadata

    bind = bind or engine
    Base.metadata.create_all(bind)
    _add_missing_columns(bind)
