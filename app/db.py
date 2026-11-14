"""The buyer's database. SQLite locally, PostgreSQL by changing DATABASE_URL only.

Timestamps on runs, steps and orders are ISO-8601 in Helsinki time (the buyer's business clock, used for weekly
spend). Audit events are stamped in UTC.
"""
from sqlalchemy import Boolean, Float, Integer, String, Text, create_engine, inspect, text
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
    # RESERVED (being placed) | PLACED (confirmed by Valio Aimo) | DELIVERED | REJECTED (refused or failed)
    status: Mapped[str] = mapped_column(String(30))
    total_net: Mapped[float] = mapped_column(Float)
    created_at: Mapped[str] = mapped_column(String(40))
    lines_json: Mapped[str] = mapped_column(Text)
    receipt: Mapped[str] = mapped_column(Text, default="")
    supplier_order_id: Mapped[str | None] = mapped_column(String(40), nullable=True)   # Valio Aimo's order id
    delivery_date: Mapped[str | None] = mapped_column(String(10), nullable=True)
    confirmation_json: Mapped[str] = mapped_column(Text, default="")   # Valio Aimo's signed confirmation
    arrives_on_day: Mapped[int | None] = mapped_column(Integer, nullable=True)   # simulated kitchen day
    request_json: Mapped[str] = mapped_column(Text, default="")      # the signed order as sent, for resending


class Supply(Base):
    """One item a kitchen keeps in stock, from any supplier."""

    __tablename__ = "supplies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    org_key: Mapped[str] = mapped_column(String(20), index=True)     # hotel | school
    name: Mapped[str] = mapped_column(String(120))
    category: Mapped[str] = mapped_column(String(40), default="")
    storage: Mapped[str] = mapped_column(String(20), default="")     # cold | dry | clean: where it is counted
    unit_cost: Mapped[float] = mapped_column(Float, default=0.0)    # € per unit before VAT, for stock value
    shelf_days: Mapped[int] = mapped_column(Integer, default=0)     # days from delivery to use-by; 0 = keeps
    supplier: Mapped[str] = mapped_column(String(120), default="")
    ask_as: Mapped[str] = mapped_column(String(120), default="")     # how the agent asks for it; "" = not orderable by it
    unit: Mapped[str] = mapped_column(String(4))                     # L | KG | PCS
    on_hand: Mapped[float] = mapped_column(Float)
    daily_use: Mapped[float] = mapped_column(Float)                  # on a typical day; the week pattern scales it
    reorder_below: Mapped[float] = mapped_column(Float)
    order_up_to: Mapped[float] = mapped_column(Float)
    rule: Mapped[str] = mapped_column(String(10), default="off")    # off | auto | ask: automatic reordering by the agent
    updated_by: Mapped[str] = mapped_column(String(80), default="")
    position: Mapped[int] = mapped_column(Integer, default=0)


class StockLevel(Base):
    """One item's stock at the end of one kitchen day: what was used, what arrived, and where the count came from."""

    __tablename__ = "stock_levels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    supply_id: Mapped[int] = mapped_column(Integer, index=True)
    day: Mapped[int] = mapped_column(Integer, index=True)
    on_hand: Mapped[float] = mapped_column(Float)
    used: Mapped[float] = mapped_column(Float, default=0.0)
    received: Mapped[float] = mapped_column(Float, default=0.0)
    source: Mapped[str] = mapped_column(String(20), default="day")  # day | count | import | api | history
    by: Mapped[str] = mapped_column(String(80), default="")


class Delivery(Base):
    """An order placed outside the agent (by phone, a supplier's webshop, another system), expected or received."""

    __tablename__ = "deliveries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    supply_id: Mapped[int] = mapped_column(Integer, index=True)
    supplier: Mapped[str] = mapped_column(String(120), default="")
    quantity: Mapped[float] = mapped_column(Float)
    ordered_day: Mapped[int] = mapped_column(Integer)
    due_day: Mapped[int] = mapped_column(Integer)
    received_day: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(12), default="EXPECTED")   # EXPECTED | RECEIVED
    reference: Mapped[str] = mapped_column(String(80), default="")
    created_by: Mapped[str] = mapped_column(String(80), default="")


class PriceRecord(Base):
    """A supplier's price for one product that can fill a stock item, seen on one kitchen day.

    `in_use` marks the product the kitchen buys; other rows are alternatives Valio Aimo offered for the same item.
    `source` says where the price came from: "valio" (a signed offer) or "history" (simulated past, see ASSUMPTIONS.md).
    """

    __tablename__ = "price_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    supply_id: Mapped[int] = mapped_column(Integer, index=True)
    day: Mapped[int] = mapped_column(Integer, index=True)
    product_id: Mapped[str] = mapped_column(String(40), default="")
    product_name: Mapped[str] = mapped_column(String(200), default="")
    unit_price: Mapped[float] = mapped_column(Float)                     # € per stock unit (L, KG, PCS), before VAT
    in_use: Mapped[bool] = mapped_column(Boolean, default=False)
    source: Mapped[str] = mapped_column(String(20), default="history")

class Dish(Base):
    """One dish on a kitchen's menu."""

    __tablename__ = "dishes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    org_key: Mapped[str] = mapped_column(String(20), index=True)
    name: Mapped[str] = mapped_column(String(120))
    category: Mapped[str] = mapped_column(String(20))               # starter | main | dessert | drink
    price: Mapped[float] = mapped_column(Float)                       # menu price, € with VAT
    weight: Mapped[float] = mapped_column(Float, default=1.0)         # how often it is chosen within its category
    extras: Mapped[float] = mapped_column(Float, default=0.0)         # € per portion: oil, seasoning, garnish (untracked)
    target_margin_pct: Mapped[float] = mapped_column(Float, default=70.0)   # gross margin the kitchen aims for
    updated_by: Mapped[str] = mapped_column(String(80), default="")  # who last changed the menu price
    active: Mapped[int] = mapped_column(Integer, default=1)
    position: Mapped[int] = mapped_column(Integer, default=0)


class RecipeLine(Base):
    """One ingredient of one portion: a kitchen supply, in that supply's unit, before trimming losses."""

    __tablename__ = "recipe_lines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    dish_id: Mapped[int] = mapped_column(Integer, index=True)
    supply_id: Mapped[int] = mapped_column(Integer, index=True)
    quantity: Mapped[float] = mapped_column(Float)                    # on the plate, per portion
    yield_pct: Mapped[float] = mapped_column(Float, default=100.0)    # usable share after peeling and trimming


class Sale(Base):
    """One dish on one ticket during a kitchen day's service. served=0: it had sold out when ordered."""

    __tablename__ = "sales"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    org_key: Mapped[str] = mapped_column(String(20), index=True)
    day: Mapped[int] = mapped_column(Integer, index=True)
    ticket: Mapped[int] = mapped_column(Integer)
    minute: Mapped[int] = mapped_column(Integer)                      # minutes after midnight
    table_no: Mapped[int] = mapped_column(Integer)
    covers: Mapped[int] = mapped_column(Integer, default=1)          # guests on the ticket
    dish_id: Mapped[int] = mapped_column(Integer, index=True)
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    price: Mapped[float] = mapped_column(Float, default=0.0)          # € with VAT, per portion, at the time
    cost: Mapped[float] = mapped_column(Float, default=0.0)           # ingredients per portion, at the time
    served: Mapped[int] = mapped_column(Integer, default=1)


class Waste(Base):
    """Stock thrown away: how much, why, what it cost, and who logged it."""

    __tablename__ = "waste"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    org_key: Mapped[str] = mapped_column(String(20), index=True)
    supply_id: Mapped[int] = mapped_column(Integer, index=True)
    day: Mapped[int] = mapped_column(Integer, index=True)
    quantity: Mapped[float] = mapped_column(Float)                    # in the supply's unit
    cost: Mapped[float] = mapped_column(Float, default=0.0)           # € before VAT, at the price of the day
    reason: Mapped[str] = mapped_column(String(20))                   # see costs.REASONS
    note: Mapped[str] = mapped_column(String(200), default="")
    by: Mapped[str] = mapped_column(String(80), default="")           # person id, or "closing check"
    at: Mapped[str] = mapped_column(String(40), default="")


class SimState(Base):
    """The demo's calendar: day 1 is `start`, and the 'next day' button moves it on."""

    __tablename__ = "sim_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    day: Mapped[int] = mapped_column(Integer, default=1)
    start: Mapped[str] = mapped_column(String(10), default="")      # ISO date of day 1


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
    supply_id: Mapped[int | None] = mapped_column(Integer, nullable=True)  # the sheet row this restocks, if any
    trigger: Mapped[str] = mapped_column(Text, default="")              # why the agent started by itself


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


def wipe(bind=None):
    """Drop and recreate the buyer's tables: the demo's 'start again'. Mandates (files) are not touched."""
    import app.audit  # noqa: F401
    bind = bind or engine
    Base.metadata.drop_all(bind)
    Base.metadata.create_all(bind)


def init_db(bind=None):
    import app.audit  # noqa: F401  registers the audit tables on Base.metadata

    bind = bind or engine
    Base.metadata.create_all(bind)
    _add_missing_columns(bind)
