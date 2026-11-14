"""The visible story of a run: what the agent did, what it exchanged with Valio Aimo, and who decided.

Steps are written as they happen (the console shows them live). Exchanges are stored raw and described at
display time, so the wording can improve without touching the record.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import date, datetime
from urllib.parse import parse_qs, urlsplit

from sqlalchemy.orm import Session

import settings as config
from app.db import Step
from app.gateway_client import Exchange

ALLERGEN_NAMES = {"AN": "tree nuts", "AP": "peanuts", "AM": "milk", "AE": "egg", "AF": "fish",
                  "AW": "gluten cereals", "AY": "soy", "AS": "sesame", "AC": "crustaceans"}


class StepLog:
    """Writes steps for one run. Each step is committed at once, except inside held() (a larger transaction)."""

    def __init__(self, session: Session, trace_id: str):
        self.session, self.trace_id, self._held = session, trace_id, False

    @contextmanager
    def held(self):
        """Steps added inside are committed with the caller's transaction, not on their own."""
        self._held = True
        try:
            yield
        finally:
            self._held = False

    def _add(self, **fields) -> None:
        self.session.add(Step(trace_id=self.trace_id, at=datetime.now(config.HELSINKI).isoformat(timespec="seconds"),
                              **fields))
        if self._held:
            self.session.flush()
        else:
            self.session.commit()

    def agent(self, title: str, detail: str = "", tone: str = "") -> None:
        self._add(lane="agent", title=title, detail=detail, tone=tone)

    def person(self, title: str, detail: str = "", tone: str = "") -> None:
        self._add(lane="person", title=title, detail=detail, tone=tone)

    def exchange(self, ex: Exchange) -> None:
        self._add(lane="exchange", method=ex.method, path=ex.path, status_code=ex.status, ms=ex.ms,
                  request_json=json.dumps(ex.request) if ex.request is not None else "",
                  response_json=json.dumps(ex.response) if ex.response is not None else "")


# ---------------------------------------------------------------- display

def _day(iso: str | None) -> str:
    return date.fromisoformat(iso[:10]).strftime("%a %d %b") if iso else "?"


def _time(iso: str | None) -> str:
    if not iso:
        return "?"
    dt = datetime.fromisoformat(iso)
    return (dt.astimezone(config.HELSINKI) if dt.tzinfo else dt).strftime("%H:%M")


def _eur(v) -> str:
    return f"€{v:,.2f}" if v is not None else "–"


def _claim(c: str) -> str:
    return c[10:].replace("_", " ").lower() + "-free" if c.startswith("FREE_FROM_") else c.replace("_", " ").lower()


def describe(step: Step, names: dict[str, str] | None = None) -> dict:
    """Plain-language view of a step for the timeline."""
    names = names or {}
    view = {"lane": step.lane, "title": step.title, "detail": step.detail, "tone": step.tone, "at": step.at,
            "id": step.id}
    if step.lane != "exchange":
        return view
    req = json.loads(step.request_json) if step.request_json else None
    res = json.loads(step.response_json) if step.response_json else None
    path = urlsplit(step.path).path
    ok = 200 <= step.status_code < 300
    view.update(ask="", ask_detail="", answer="", answer_detail="", checks=[], ok=ok,
                http=f"{step.method} {path} · {step.status_code} · {step.ms} ms",
                request_pretty=json.dumps(req, indent=2, ensure_ascii=False) if req is not None else
                f"{step.method} {step.path}",
                response_pretty=json.dumps(res, indent=2, ensure_ascii=False) if res is not None else "")
    error = (res or {}).get("detail") or (res or {}).get("error") if isinstance(res, dict) else None

    if path == "/.well-known/jwks.json":
        view["ask"] = "Fetch Valio Aimo's public signing key"
        key = (res or {}).get("keys", [{}])[0] if ok else {}
        view["answer"] = f"Key {key.get('kid', '?')}" if ok else "No key"
        view["answer_detail"] = "Ed25519 · used to check every offer and confirmation" if ok else str(error)
    elif path == "/v1/products":
        q = parse_qs(urlsplit(step.path).query)
        filters = [_claim(c) for c in q.get("claims", [])] + [f"no {ALLERGEN_NAMES.get(a, a)}" for a in q.get("allergens", [])]
        view["ask"] = f"Search the catalog for “{' '.join(q.get('q', ['?']))}”"
        view["ask_detail"] = ", ".join(filters)
        if ok:
            view["answer"] = f"{len(res['products'])} products match"
            if res.get("not_matching"):
                view["answer_detail"] = f"{len(res['not_matching'])} ruled out, e.g. {res['not_matching'][0]['name']} " \
                                        f"{res['not_matching'][0]['reason']}"
        else:
            view["answer"] = f"Search failed: {error}"
    elif path == "/v1/offers":
        view["ask"] = f"Ask for prices and availability of {len((req or {}).get('products', []))} products"
        view["ask_detail"] = f"Delivery by {_day((req or {}).get('delivery_by'))}"
        if ok:
            by = res.get("delivery_by")
            ready = [i for i in res["items"] if i["available_units"] > 0 and i.get("available_date")
                     and i["available_date"] <= by and not i.get("sales_block")]
            view["answer"] = f"Signed offer {res['offer_id']}"
            view["answer_detail"] = f"{len(ready)} of {len(res['items'])} deliverable in time · " \
                                    f"prices fixed until {_time(res['expires_at'])}"
        else:
            view["answer"] = f"No offer: {error}"
    elif path == "/v1/orders":
        order = (req or {}).get("order", {})
        lines = ", ".join(f"{l['sales_units']:,} × {names.get(l['product_id'], l['product_id'])}"
                          for l in order.get("lines", []))
        view["ask"] = f"Place the order: {lines}"
        view["ask_detail"] = "Signed by the agent · signed mandate attached" + \
                             (" · signed approval attached" if order.get("approval") else "")
        view["checks"] = (res or {}).get("checks", []) if isinstance(res, dict) else []
        if ok:
            view["answer"] = f"Order {res['order_id']} confirmed"
            view["answer_detail"] = f"{_eur(res['total_net'])} before VAT · delivery {_day(res['delivery_date'])} · " \
                                    "signed by Valio Aimo"
        else:
            view["answer"] = f"Refused: {error}"
    elif path.startswith("/v1/orders/"):
        view["ask"] = "Check the order's status"
        view["answer"] = (res or {}).get("status", "?").capitalize() if ok else f"Not found: {error}"
    else:
        view["ask"], view["answer"] = f"{step.method} {path}", f"HTTP {step.status_code}"
    return view
