"""The "try to cheat" test: a misbehaving purchasing agent tries seven tricks against Valio Aimo's real gateway.

Everything goes over HTTP through the gateway client, exactly as the real agent's orders do; nothing here imports
the gateway. Each attempt reports what Valio Aimo actually answered. A trick that gets through is reported as a
failure, never hidden. Tricks use one sales unit of the cheapest offered product, so a wrong acceptance costs
little; the two over-the-limit tricks have to go just past the agent's own limit.
"""
from __future__ import annotations

import copy
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime

import settings as config
from app import keys
from app.gateway_client import GatewayClient, GatewayError
from protocol.mandate import Mandate
from protocol.signing import Signer, sign_document, verify_document

SEARCH_TERMS = ["milk"]


@dataclass
class Attempt:
    title: str
    tried: str                        # what the cheating agent did, in plain words
    outcome: str = "error"            # refused | accepted | skipped | error
    reason: str = ""                  # Valio Aimo's own words (or why the attempt could not run)
    source: str = "Valio Aimo"        # who gave the reason
    raw: list[dict] = field(default_factory=list)    # the exchanges, one click deeper

    @property
    def safe(self) -> bool:
        return self.outcome == "refused"


class _Run:
    def __init__(self, client: GatewayClient, mandate: Mandate, delivery_by: date, now: datetime):
        self.client, self.m, self.delivery_by, self.now = client, mandate, delivery_by, now
        self.product_id: str | None = None
        self.raw: list[dict] = []

    def call(self, method: str, path: str, *, json: dict | None = None) -> tuple[int, dict]:
        status, body = self.client._call(method, path, json=json)
        self.raw.append({"request": f"{method} {path}", "sent": json, "status": status, "answer": body})
        return status, body

    def offer(self, products: list[str] | None = None) -> dict:
        status, body = self.call("POST", "/v1/offers", json={
            "products": products or [self.product_id], "delivery_by": self.delivery_by.isoformat(),
            "buyer_org": self.m.buyer_org})
        if status != 201:
            raise GatewayError(f"Valio Aimo gave no offer (HTTP {status})")
        return body

    def orderable(self, offer: dict, units: int = 1) -> list[dict]:
        return [i for i in offer["items"] if not i["sales_block"] and i["available_date"]
                and i["available_units"] >= units
                and (not i["closing_time"] or datetime.fromisoformat(i["closing_time"]) > self.now)]

    def order(self, offer_id: str, units: int, *, approval: dict | None = None, mandate: dict | None = None,
              signer: Signer | None = None, as_mandate: Mandate | None = None) -> tuple[int, dict]:
        m = as_mandate or self.m
        order = {"offer_id": offer_id, "mandate_id": m.mandate_id, "buyer_reference": f"safety-{uuid.uuid4().hex[:8]}",
                 "lines": [{"product_id": self.product_id, "sales_units": units}], "approval": approval}
        signed = sign_document(order, signer) if signer else keys.sign_order(order, m)
        return self.call("POST", "/v1/orders", json={"order": signed, "mandate": mandate or m.model_dump(mode="json")})


def _judge(attempt: Attempt, status: int, body: dict) -> None:
    if status == 201:
        attempt.outcome = "accepted"
        attempt.reason = f"Valio Aimo accepted it as order {body.get('order_id', '?')}. This is a failure."
    elif isinstance(body, dict) and "error" in body:
        attempt.outcome = "refused"
        failed = [c["message"] for c in body.get("checks", []) if not c["ok"]]
        attempt.reason = "; ".join(failed) or body["error"]
    else:
        attempt.outcome = "error"
        attempt.reason = f"Valio Aimo answered HTTP {status} without a decision"


def run(client: GatewayClient, mandate: Mandate, delivery_by: date, *, used_offer_id: str | None = None,
        older: Mandate | None = None, now: datetime | None = None) -> list[Attempt]:
    """Run every trick in turn. used_offer_id: an offer this agent already ordered; older: a replaced mandate."""
    r = _Run(client, mandate, delivery_by, now or datetime.now(config.HELSINKI))
    auto = mandate.limits.per_order_auto_eur
    attempts = [
        Attempt("Edited price", "Took a signed offer from Valio Aimo and changed a price in it to 0.01 EUR, "
                                "hoping to order at that price."),
        Attempt("Reused offer", "Ordered again against an offer it had already used."),
        Attempt("Old rules", "Sent an order under an earlier version of its purchasing rules, from before they "
                             "were changed."),
        Attempt("Raised its own limits", "Edited its signed mandate after signing, raising the amount it may order "
                                         "alone to 1,000,000 EUR."),
        Attempt("Approved its own order", f"Ordered above its {auto:,.0f} EUR limit and attached an approval it "
                                          f"signed itself instead of {mandate.approver_name}'s."),
        Attempt("Over its limit", f"Ordered above its {auto:,.0f} EUR limit with no approval at all."),
        Attempt("Someone else's key", "Signed the order with a key that is not the agent's registered key."),
    ]
    steps = [_edited_price, _reused_offer, _old_rules, _forged_mandate, _self_approval, _over_limit, _wrong_key]
    try:
        hc = mandate.hard_constraints
        found = client.search(SEARCH_TERMS, None, [], hc.required_claims, hc.excluded_allergens)
        first = r.offer([p.product_id for p in found.products[:10]])
        items = sorted(r.orderable(first), key=lambda i: i["unit_price"])
        if not items:
            raise GatewayError("Valio Aimo has nothing orderable to test with right now")
        cheapest = items[0]
        over = next((i for i in reversed(items)                 # dearest first: the fewest units over the limit
                     if i["available_units"] >= int(auto // i["unit_price"]) + 1
                     and (int(auto // i["unit_price"]) + 1) * i["unit_price"] <= mandate.limits.per_order_max_eur),
                    None)
    except GatewayError as e:
        for a in attempts:
            a.reason = f"Not tested: {e}"
        return attempts
    for attempt, step in zip(attempts, steps):
        r.raw = []
        r.product_id = (over or cheapest)["product_id"] if step in (_self_approval, _over_limit) else \
            cheapest["product_id"]
        try:
            step(r, attempt, over=over, used_offer_id=used_offer_id, older=older)
        except GatewayError as e:
            attempt.outcome, attempt.reason = "error", f"Not tested: {e}"
        attempt.raw = r.raw
    return attempts


def _edited_price(r: _Run, a: Attempt, **_) -> None:
    offer = r.offer()
    edited = copy.deepcopy(offer)
    edited["items"][0]["unit_price"] = 0.01
    genuine = verify_document(offer, r.client.public_key())
    still_valid = verify_document(edited, r.client.public_key())
    r.raw.append({"request": "Signature check against Valio Aimo's published key (/.well-known/jwks.json)",
                  "sent": edited, "status": None,
                  "answer": {"original_offer_signature_valid": genuine, "edited_offer_signature_valid": still_valid}})
    a.source = "Signature check with Valio Aimo's published key"
    if still_valid:
        a.outcome, a.reason = "accepted", "The edited offer still passed Valio Aimo's signature check. This is a failure."
    elif not genuine:
        a.outcome, a.reason = "error", "Not tested: even the unedited offer failed the signature check"
    else:
        a.outcome = "refused"
        a.reason = ("The edited offer no longer matches Valio Aimo's signature. An order also names only the offer "
                    "number: Valio Aimo prices it from its own signed copy, so an edited price never reaches it.")


def _reused_offer(r: _Run, a: Attempt, used_offer_id: str | None, **_) -> None:
    if not used_offer_id:
        status, body = r.order(r.offer()["offer_id"], 1)
        if status != 201:
            a.outcome, a.reason = "error", "Not tested: the genuine first order needed to use up an offer was refused"
            return
        used_offer_id = body["offer_id"]
        a.tried += " (A genuine 1-unit order was placed first to use up the offer.)"
    _judge(a, *r.order(used_offer_id, 1))


def _old_rules(r: _Run, a: Attempt, older: Mandate | None, **_) -> None:
    if older is None:
        a.outcome = "skipped"
        a.reason = ("Not tested: this kitchen's purchasing rules have only one signed version so far. Change a limit "
                    "on Purchasing rules, then run the test again.")
        return
    held = r.client.mandate_version(r.m.mandate_id)
    if held is None or held < r.m.version:              # make sure Valio Aimo knows the rules were replaced
        r.call("PUT", "/v1/mandates", json=r.m.model_dump(mode="json"))
    a.tried = f"Sent an order under version {older.version} of its purchasing rules, replaced by version {r.m.version}."
    _judge(a, *r.order(r.offer()["offer_id"], 1, as_mandate=older))


def _forged_mandate(r: _Run, a: Attempt, **_) -> None:
    forged = r.m.model_dump(mode="json")
    forged["limits"]["per_order_auto_eur"] = 1_000_000
    _judge(a, *r.order(r.offer()["offer_id"], 1, mandate=forged))


def _over(r: _Run, a: Attempt, over: dict | None) -> tuple[str, int, float] | None:
    if over is None:
        a.outcome, a.reason = "error", "Not tested: no offered product has enough stock to go just over the limit"
        return None
    offer = r.offer()
    item = next(i for i in offer["items"] if i["product_id"] == r.product_id)
    units = int(r.m.limits.per_order_auto_eur // item["unit_price"]) + 1
    return offer["offer_id"], units, round(units * item["unit_price"], 2)


def _self_approval(r: _Run, a: Attempt, over: dict | None, **_) -> None:
    if not (got := _over(r, a, over)):
        return
    offer_id, units, total = got
    record = {"trace_id": "safety-test", "mandate_id": r.m.mandate_id, "approver": r.m.approver,
              "approver_name": r.m.approver_name, "decision": "APPROVED",
              "decided_at": r.now.isoformat(timespec="seconds"),
              "lines": [{"product_id": r.product_id, "sales_units": units}], "total_net": total}
    approval = sign_document(record, keys.signer(r.m.agent_id))       # the agent's own key, not the approver's
    _judge(a, *r.order(offer_id, units, approval=approval))


def _over_limit(r: _Run, a: Attempt, over: dict | None, **_) -> None:
    if got := _over(r, a, over):
        offer_id, units, _total = got
        _judge(a, *r.order(offer_id, units))


def _wrong_key(r: _Run, a: Attempt, **_) -> None:
    _judge(a, *r.order(r.offer()["offer_id"], 1, signer=Signer.generate(r.m.agent_id)))
