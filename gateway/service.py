"""Valio Aimo's agent gateway: what an external purchasing agent may see and do.

  search  -> static catalog facts
  offer   -> live price and availability from the backend, frozen and signed, valid for 15 minutes
  order   -> accepted only if every check passes; answered with a signed confirmation

The gateway does not trust the buyer's agent. Before accepting an order it verifies, itself:
the offer (its own signature, unexpired, unused, enough stock, cut-off not passed), the mandate (signed by a
registered buyer organisation, in date), the agent (the order is signed with the key the mandate names),
the mandate's hard limits (allergens, claims, categories, absolute spend cap) and the agent's authority
(over its own spend limits, a person's signed approval of exactly these lines is required).
Softer judgement (price bands, preferences, confidence) stays with the buyer's own policy engine.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

from pydantic import ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.orm import sessionmaker

import settings as config
from gateway import catalog
from gateway.backend import ValioAPI
from gateway.store import MandateRecord, OfferRecord, OrderRecord, PausedAgent
from protocol.mandate import Mandate
from protocol.registry import TrustRegistry
from protocol.signing import Signer, sign_document, verify_document
from protocol.wire import (
    OFFER_TTL_MINUTES,
    Approval,
    Check,
    ConfirmedLine,
    NotMatching,
    Offer,
    OfferItem,
    OfferRequest,
    OrderConfirmation,
    OrderRefused,
    OrderRequest,
    PlaceOrder,
    SearchResult,
)

UNLIMITED = 999_999          # Terminal products report this; some Stock products report 9999999999
IDENTITY_CHECKS = {"mandate.signature", "mandate.validity", "mandate.current", "order.signature", "agent.active"}


@dataclass
class Result:
    status: int                  # HTTP status
    body: dict


class Gateway:
    def __init__(self, backend: ValioAPI, sessions: sessionmaker, signer: Signer, registry: TrustRegistry,
                 clock: Callable[[], datetime] | None = None):
        self.backend, self.sessions, self.signer, self.registry = backend, sessions, signer, registry
        self.clock = clock or (lambda: datetime.now(config.HELSINKI))

    # ------------------------------------------------------------ discovery

    def jwks(self) -> dict:
        return {"keys": [self.signer.public_jwk()]}

    def search(self, terms: list[str], category: str | None, exclude: list[str], claims: list[str],
               allergens: list[str]) -> SearchResult:
        return catalog.search(terms, category, exclude, claims, allergens)

    # ------------------------------------------------------------ offer

    def offer(self, req: OfferRequest) -> dict:
        ids = list(dict.fromkeys(req.products))
        known = catalog.products_by_id(ids)
        unavailable = [NotMatching(product_id=i, name=i, reason="not in the catalog") for i in ids if i not in known]
        priced_ids = [i for i in ids if i in known]
        prices = self.backend.prices(priced_ids, req.delivery_by) if priced_ids else {}
        avail = self.backend.availability(priced_ids, req.delivery_by) if priced_ids else {}

        items: list[OfferItem] = []
        for pid in priced_ids:
            name = catalog.display_name(known[pid])
            pr, av = prices.get(pid), avail.get(pid)
            if pr is None or pr.error or pr.catalogPrice is None:
                unavailable.append(NotMatching(product_id=pid, name=name, reason="no price available"))
                continue
            line = av.scheduleLines[0] if av and av.scheduleLines else None
            items.append(OfferItem(
                product_id=pid, name=name, sales_unit=known[pid]["sales_unit"],
                unit_price=pr.catalogPrice.value,
                unit_price_with_tax=pr.catalogPrice.valueWithTax or pr.catalogPrice.value,
                comparison_unit=pr.comparisonUnit,
                comparison_price=pr.comparisonPrice.value if pr.comparisonPrice else None,
                available_units=min(line.availableAmount, UNLIMITED) if line else 0.0,
                available_date=line.availableDate if line else None,
                closing_time=av.closingTime if av else None,
                logistic_model=av.logisticModel if av else None,
                quantity_confirmed=bool(av and av.logisticModel == "Stock"),
                sales_block=bool((av and av.salesBlock) or (line and line.salesBlock)),
            ))

        now = self.clock()
        offer = Offer(offer_id=f"OF-{uuid.uuid4().hex[:10].upper()}", issued_at=now,
                      expires_at=now + timedelta(minutes=OFFER_TTL_MINUTES), delivery_by=req.delivery_by,
                      buyer_org=req.buyer_org, items=items, unavailable=unavailable)
        signed = sign_document(offer.model_dump(mode="json"), self.signer)
        with self.sessions() as s:
            s.add(OfferRecord(offer_id=offer.offer_id, issued_at=now.isoformat(), expires_at=offer.expires_at.isoformat(),
                              buyer_org=req.buyer_org, offer_json=json.dumps(signed)))
            s.commit()
        return signed

    # ------------------------------------------------------------ order

    def place_order(self, body: PlaceOrder) -> Result:
        checks: list[Check] = []

        def check(name: str, ok: bool, message: str) -> bool:
            checks.append(Check(check=name, ok=ok, message=message))
            return ok

        def refused(error: str) -> Result:
            failed = {c.check for c in checks if not c.ok}
            status = 403 if failed & IDENTITY_CHECKS else 409
            return Result(status, OrderRefused(error=error, checks=checks).model_dump(mode="json"))

        try:
            order = OrderRequest.model_validate(body.order)
            mandate = Mandate.model_validate(body.mandate)
        except ValidationError as e:
            return Result(422, OrderRefused(error=f"malformed order or mandate: {e.errors()[0]['msg']}").model_dump())
        now = self.clock()

        # ---- who is asking, and on whose authority
        org_key = self.registry.public_key(mandate.signer_key_id, org=mandate.buyer_org)
        mandate_ok = bool(org_key and verify_document(body.mandate, org_key))
        if not check("mandate.signature", mandate_ok,
                     f"Mandate {mandate.mandate_id} is signed by {mandate.buyer_org}'s registered key" if mandate_ok else
                     f"The mandate is not signed by {mandate.buyer_org}'s registered key, or was changed after signing"):
            return refused("mandate signature invalid")
        if not check("mandate.match", order.mandate_id == mandate.mandate_id,
                     "The order names the mandate it was sent with"):
            return refused("order and mandate do not match")
        today = now.date()
        check("mandate.validity", mandate.valid_from <= today <= mandate.valid_until,
              f"Mandate valid {mandate.valid_from} to {mandate.valid_until}; today is {today}")
        agent_ok = bool(mandate.agent_public_key and verify_document(body.order, mandate.agent_public_key))
        check("order.signature", agent_ok,
              f"Order signed by the agent the mandate names ({mandate.agent_id})" if agent_ok else
              "The order is not signed by the agent the mandate names")
        if any(not c.ok for c in checks):
            return refused(next(c.message for c in checks if not c.ok))
        # Only now is it certain which agent is asking, so only now can a pause be applied to it.
        with self.sessions() as s:
            paused = s.get(PausedAgent, mandate.agent_id)
        if not check("agent.active", paused is None,
                     f"Agent {mandate.agent_id} is active" if paused is None else
                     f"Valio Aimo paused this agent on {paused.paused_at[:16].replace('T', ' at ')}"
                     + (f": {paused.note}" if paused.note else "") + ". No orders are accepted until it is resumed"):
            return refused(checks[-1].message)

        with self.sessions() as s:
            # A resend of an order we already confirmed (its answer was lost on the way): same signed answer again.
            earlier = s.scalar(select(OrderRecord).where(OrderRecord.mandate_id == order.mandate_id,
                                                         OrderRecord.buyer_reference == order.buyer_reference,
                                                         OrderRecord.offer_id == order.offer_id))
            if earlier is not None:
                answer = json.loads(earlier.confirmation_json)
                same = sorted((l["product_id"], l["sales_units"]) for l in answer["lines"]) ==                     sorted((l.product_id, l.sales_units) for l in order.lines)
                if same:
                    return Result(201, answer)
            # Claim the offer first: an UPDATE takes the write lock, so concurrent orders are checked one at a time.
            order_id = f"AIMO-{uuid.uuid4().hex[:8].upper()}"
            claimed = s.execute(update(OfferRecord).where(OfferRecord.offer_id == order.offer_id,
                                                          OfferRecord.used_by.is_(None))
                                .values(used_by=order_id)).rowcount
            record = s.get(OfferRecord, order.offer_id)
            if record is None:
                check("offer.exists", False, f"No offer {order.offer_id} was issued")
                return refused("unknown offer")
            if not check("offer.unused", claimed == 1, "Each offer can be ordered once"):
                return refused("offer already used")
            current, message = self._note_version(s, mandate, body.mandate, now)
            if not check("mandate.current", current, message):
                return refused(message)
            signed_offer = json.loads(record.offer_json)
            if not check("offer.signature", verify_document(signed_offer, self.signer.public_key_b64()),
                         "The offer is Valio Aimo's own, unchanged"):
                return refused("offer record altered")
            offer = Offer.model_validate(signed_offer)
            check("offer.expiry", now <= offer.expires_at,
                  f"Offer valid until {offer.expires_at:%H:%M}" if now <= offer.expires_at else
                  f"The offer expired at {offer.expires_at:%H:%M}; ask for a new one")

            # ---- the goods
            items = {i.product_id: i for i in offer.items}
            static = catalog.products_by_id([l.product_id for l in order.lines])
            problems = []
            for line in order.lines:
                item = items.get(line.product_id)
                if item is None:
                    problems.append(f"{line.product_id} is not in offer {offer.offer_id}")
                elif item.sales_block or item.available_date is None:
                    problems.append(f"{item.name} cannot be ordered")
                elif line.sales_units > item.available_units:
                    problems.append(f"{item.name}: {line.sales_units} wanted, {item.available_units:g} available")
                elif item.closing_time is not None and _is_past(item.closing_time, now):
                    problems.append(f"{item.name}: order cut-off passed at {item.closing_time:%a %H:%M}")
            check("offer.lines", not problems, "; ".join(problems) or "Every line is in the offer, in stock, before cut-off")

            hc = mandate.hard_constraints
            unsafe = []
            for line in order.lines:
                p = static.get(line.product_id)
                if p is None:
                    continue
                name = catalog.display_name(p)
                unsafe += [f"{name} lacks {c}" for c in hc.required_claims if c not in p["claims"]]
                unsafe += [f"{name} contains {a}" for a in hc.excluded_allergens if a in p["contains"]]
                if hc.exclude_may_contain:
                    unsafe += [f"{name} may contain {a}" for a in hc.excluded_allergens if a in p["may_contain"]]
                if hc.require_allergen_data and not p["has_allergen_data"]:
                    unsafe.append(f"{name} has no allergen data")
                if mandate.allowed_categories is not None and \
                        not any(p["category"].startswith(c) for c in mandate.allowed_categories):
                    unsafe.append(f"{name} is outside the allowed categories")
            check("mandate.hard_constraints", not unsafe,
                  "; ".join(unsafe) or "Meets the mandate's allergen, claim and category rules")

            # ---- money and authority
            total = round(sum(l.sales_units * items[l.product_id].unit_price
                              for l in order.lines if l.product_id in items), 2)
            check("mandate.order_cap", total <= mandate.limits.per_order_max_eur,
                  f"{total:,.2f} EUR is within the {mandate.limits.per_order_max_eur:,.2f} EUR cap per order"
                  if total <= mandate.limits.per_order_max_eur else
                  f"{total:,.2f} EUR is above the {mandate.limits.per_order_max_eur:,.2f} EUR cap per order")
            week = self._week_spend(s, mandate.mandate_id, now)
            approved_by = self._check_authority(order, mandate, total, week, check)
            approval_sig = (order.approval or {}).get("signature") if approved_by else None
            if approval_sig:
                reused = s.scalar(select(OrderRecord.order_id).where(OrderRecord.approval_signature == approval_sig))
                check("approval.unused", reused is None,
                      "Each approval can be used for one order" if reused is None else
                      f"This approval was already used for order {reused}")

            if any(not c.ok for c in checks):
                s.rollback()                                   # releases the offer claim
                return refused(next(c.message for c in checks if not c.ok))

            confirmation = OrderConfirmation(
                order_id=order_id, offer_id=offer.offer_id, status="CONFIRMED", buyer_org=mandate.buyer_org,
                mandate_id=mandate.mandate_id, buyer_reference=order.buyer_reference,
                lines=[ConfirmedLine(product_id=l.product_id, name=items[l.product_id].name, sales_units=l.sales_units,
                                     sales_unit=items[l.product_id].sales_unit, unit_price=items[l.product_id].unit_price,
                                     line_total=round(l.sales_units * items[l.product_id].unit_price, 2),
                                     delivery_date=items[l.product_id].available_date)
                       for l in order.lines],
                total_net=total,
                total_with_tax=round(sum(l.sales_units * items[l.product_id].unit_price_with_tax for l in order.lines), 2),
                delivery_date=max(items[l.product_id].available_date for l in order.lines),
                confirmed_at=now, approved_by=approved_by, checks=checks)
            signed = sign_document(confirmation.model_dump(mode="json"), self.signer)
            s.add(OrderRecord(order_id=order_id, offer_id=offer.offer_id, mandate_id=mandate.mandate_id,
                              buyer_org=mandate.buyer_org, status="CONFIRMED", total_net=total,
                              confirmed_at=now.isoformat(timespec="seconds"),
                              delivery_date=confirmation.delivery_date.isoformat(), approval_signature=approval_sig,
                              confirmation_json=json.dumps(signed), buyer_reference=order.buyer_reference))
            s.commit()
        return Result(201, signed)

    # ------------------------------------------------------------ mandate versions

    def _note_version(self, s, mandate: Mandate, raw: dict, now: datetime) -> tuple[bool, str]:
        """Accept the newest version seen for this mandate id (recording it); refuse anything older or conflicting."""
        record = s.get(MandateRecord, mandate.mandate_id)
        if record is not None and record.buyer_org != mandate.buyer_org:
            return False, f"Mandate {mandate.mandate_id} belongs to another buyer organisation"
        if record is not None and mandate.version < record.version:
            return False, (f"Mandate version {mandate.version} has been replaced by version {record.version}; "
                           "the agent must use the current one")
        if record is not None and mandate.version == record.version and json.loads(record.mandate_json) != raw:
            return False, f"A different mandate {mandate.mandate_id} version {mandate.version} is already registered"
        if record is None:
            s.add(MandateRecord(mandate_id=mandate.mandate_id, version=mandate.version, buyer_org=mandate.buyer_org,
                                recorded_at=now.isoformat(timespec="seconds"), mandate_json=json.dumps(raw)))
        elif mandate.version > record.version:
            record.version, record.mandate_json = mandate.version, json.dumps(raw)
            record.recorded_at = now.isoformat(timespec="seconds")
        return True, f"Mandate version {mandate.version} is the current one"

    def publish_mandate(self, raw: dict) -> Result:
        """A buyer organisation announces a new mandate version. From now on older versions are refused."""
        try:
            mandate = Mandate.model_validate(raw)
        except ValidationError as e:
            return Result(422, {"error": f"malformed mandate: {e.errors()[0]['msg']}"})
        org_key = self.registry.public_key(mandate.signer_key_id, org=mandate.buyer_org)
        if not (org_key and verify_document(raw, org_key)):
            return Result(403, {"error": f"the mandate is not signed by {mandate.buyer_org}'s registered key"})
        with self.sessions() as s:
            ok, message = self._note_version(s, mandate, raw, self.clock())
            if not ok:
                return Result(409, {"error": message})
            s.commit()
        return Result(200, {"mandate_id": mandate.mandate_id, "version": mandate.version, "status": "current",
                            "message": message})

    def current_mandate_version(self, mandate_id: str) -> int | None:
        with self.sessions() as s:
            record = s.get(MandateRecord, mandate_id)
            return record.version if record else None

    def _check_authority(self, order: OrderRequest, mandate: Mandate, total: float, week: float, check) -> str | None:
        """Within its limits the agent orders alone; beyond them a signed approval of exactly these lines is needed."""
        limits = mandate.limits
        over = []
        if total > limits.per_order_auto_eur:
            over.append(f"{total:,.2f} EUR is above the {limits.per_order_auto_eur:,.2f} EUR the agent may order alone")
        if week + total > limits.per_week_auto_eur:
            over.append(f"this week's orders would reach {week + total:,.2f} EUR, above the agent's "
                        f"{limits.per_week_auto_eur:,.2f} EUR weekly limit")
        if not over:
            check("authority", True, "Within what the agent may order alone")
            return None
        if order.approval is None:
            check("authority", False, "; ".join(over).capitalize() + ", and no approval was attached")
            return None
        try:
            approval = Approval.model_validate(order.approval)
        except ValidationError:
            check("authority", False, "The attached approval is malformed")
            return None
        problems = []
        if not (mandate.approver_public_key and verify_document(order.approval, mandate.approver_public_key)):
            problems.append("the approval is not signed by the approver the mandate names")
        if approval.decision != "APPROVED" or approval.mandate_id != mandate.mandate_id:
            problems.append("the approval does not approve an order under this mandate")
        if {(l.product_id, l.sales_units) for l in approval.lines} != {(l.product_id, l.sales_units) for l in order.lines}:
            problems.append("the approved lines differ from the ordered lines")
        if total > approval.total_net + 0.005:
            problems.append(f"the total {total:,.2f} EUR is above the approved {approval.total_net:,.2f} EUR")
        who = approval.approver_name or approval.approver
        check("authority", not problems,
              "; ".join(problems).capitalize() if problems else
              f"Above the agent's own limits; approved by {who} ({approval.decided_at:%d %b %H:%M}), signature verified")
        return who if not problems else None

    def _week_spend(self, s, mandate_id: str, now: datetime) -> float:
        monday = (now - timedelta(days=now.weekday())).date().isoformat()
        total = s.scalar(select(func.coalesce(func.sum(OrderRecord.total_net), 0.0)).where(
            OrderRecord.mandate_id == mandate_id, OrderRecord.status != "CANCELLED",
            OrderRecord.confirmed_at >= monday))
        return round(float(total), 2)

    # ------------------------------------------------------------ pausing agents (Valio Aimo's operators)

    def agents(self) -> list[dict]:
        """Every agent Valio Aimo has seen a mandate for, with its orders and whether it is paused."""
        with self.sessions() as s:
            paused = {p.agent_id: p for p in s.scalars(select(PausedAgent))}
            out = []
            for m in s.scalars(select(MandateRecord).order_by(MandateRecord.buyer_org)):
                agent_id = json.loads(m.mandate_json).get("agent_id", "")
                orders = s.execute(select(func.count(), func.max(OrderRecord.confirmed_at)).where(
                    OrderRecord.mandate_id == m.mandate_id)).one()
                p = paused.get(agent_id)
                out.append({"agent_id": agent_id, "buyer_org": m.buyer_org, "mandate_id": m.mandate_id,
                            "mandate_version": m.version, "orders": orders[0], "last_order": orders[1],
                            "paused": p is not None, "paused_at": p.paused_at if p else None,
                            "paused_by": p.paused_by if p else None, "note": p.note if p else ""})
            return out

    def pause(self, agent_id: str, by: str, note: str = "") -> Result:
        with self.sessions() as s:
            known = next((a for a in self.agents() if a["agent_id"] == agent_id), None)
            if known is None:
                return Result(404, {"error": f"no agent {agent_id} has a mandate here"})
            if s.get(PausedAgent, agent_id) is None:
                s.add(PausedAgent(agent_id=agent_id, buyer_org=known["buyer_org"],
                                  paused_at=self.clock().isoformat(timespec="seconds"), paused_by=by[:80],
                                  note=note.strip()[:300]))
                s.commit()
        return Result(200, {"agent_id": agent_id, "status": "paused"})

    def resume(self, agent_id: str) -> Result:
        with self.sessions() as s:
            p = s.get(PausedAgent, agent_id)
            if p is not None:
                s.delete(p)
                s.commit()
        return Result(200, {"agent_id": agent_id, "status": "active"})

    # ------------------------------------------------------------ order status

    def get_order(self, order_id: str) -> dict | None:
        with self.sessions() as s:
            record = s.get(OrderRecord, order_id)
            if record is None:
                return None
            return {"order_id": record.order_id, "status": record.status,
                    "confirmation": json.loads(record.confirmation_json)}


def _is_past(moment: datetime, now: datetime) -> bool:
    # The backend's cut-off times carry an offset; compare naive ones as Helsinki wall-clock time.
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=config.HELSINKI)
    return moment <= now
