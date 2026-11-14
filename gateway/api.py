"""HTTP interface of Valio Aimo's agent gateway. Interactive docs at /docs, spec at /openapi.json.

Run:  .venv/Scripts/python -m uvicorn gateway.api:create_app --factory --port 8100
"""
from __future__ import annotations

import hmac
from html import escape

from fastapi import Cookie, FastAPI, Form, Header, HTTPException, Path, Query
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

import settings as config

from gateway.backend import ValioClient
from gateway.keys import gateway_signer, registry
from gateway.service import Gateway
from gateway.store import make_sessionmaker
from gateway import catalog
from protocol.wire import OfferRequest, OrderConfirmation, OrderRefused, PlaceOrder, ProductDetail, SearchResult

DESCRIPTION = """
The interface through which an external purchasing agent works with Valio Aimo.

1. **Search** the catalog (static product facts: claims, allergens, labels, delivery reliability).
2. **Request an offer**: live prices and availability, fixed and signed by Valio Aimo, valid for 15 minutes.
3. **Place an order** against the offer. The order is signed by the agent and carries the buyer
   organisation's signed mandate. Above the agent's own limits it must also carry a person's signed approval.
   Valio Aimo checks all of this itself and answers with a signed confirmation, or refuses with the reasons.

Mandates change: a buyer publishes each new signed version (`PUT /v1/mandates`), and Valio Aimo refuses orders
under any older version.

Verify Valio Aimo's signatures with the key at `/.well-known/jwks.json` (Ed25519 over canonical JSON).
"""


def create_app(gateway: Gateway | None = None) -> FastAPI:
    gw = gateway or Gateway(ValioClient(), make_sessionmaker(), gateway_signer(), registry())
    api = FastAPI(title="Valio Aimo agent gateway", version="1.0", description=DESCRIPTION)
    api.state.gateway = gw

    @api.get("/", include_in_schema=False)
    def root():
        return RedirectResponse("/docs")

    @api.get("/.well-known/jwks.json", tags=["trust"], summary="Valio Aimo's public signing key")
    def jwks():
        return gw.jwks()

    @api.get("/v1/products", tags=["catalog"], response_model=SearchResult, summary="Search the catalog")
    def search(q: str = Query(..., min_length=2, description="Words the product name must contain, e.g. 'milk'"),
               category: str | None = Query(None, description="Category code prefix, e.g. 10400"),
               exclude: list[str] = Query([], description="Leave out names containing these words"),
               claims: list[str] = Query([], description="Required claims, e.g. FREE_FROM_LACTOSE"),
               allergens: list[str] = Query([], description="Excluded allergens (GS1 codes), e.g. AN")):
        return gw.search(q.lower().split(), category, exclude, claims, allergens)

    @api.get("/v1/products/{product_id}", tags=["catalog"], response_model=ProductDetail,
             summary="One product's catalog facts: allergens, claims, reliability, description")
    def product(product_id: str = Path(..., pattern=r"^[0-9A-Za-z_-]{1,20}$")):
        p = catalog.products_by_id([product_id]).get(product_id)
        if p is None:
            raise HTTPException(404, f"no product {product_id}")
        return catalog.to_detail(p)

    @api.post("/v1/offers", tags=["offers"], status_code=201, summary="Request a signed offer")
    def offer(req: OfferRequest):
        try:
            return gw.offer(req)
        except Exception as e:                       # the backend is down, rate-limited or answered oddly
            raise HTTPException(502, f"price and availability backend unavailable: {type(e).__name__}") from e

    @api.post("/v1/orders", tags=["orders"], status_code=201, summary="Place an order against an offer",
              responses={201: {"model": OrderConfirmation}, 403: {"model": OrderRefused},
                         409: {"model": OrderRefused}, 422: {"model": OrderRefused}})
    def place_order(body: PlaceOrder):
        result = gw.place_order(body)
        return JSONResponse(result.body, status_code=result.status)

    @api.put("/v1/mandates", tags=["mandates"], summary="Publish a new version of a buyer's signed mandate")
    def publish_mandate(mandate: dict):
        """After this, orders signed under any older version of the mandate are refused."""
        result = gw.publish_mandate(mandate)
        return JSONResponse(result.body, status_code=result.status)

    @api.get("/v1/mandates/{mandate_id}", tags=["mandates"], summary="The mandate version Valio Aimo holds as current")
    def mandate_version(mandate_id: str):
        version = gw.current_mandate_version(mandate_id)
        if version is None:
            raise HTTPException(404, "no version of this mandate has been seen")
        return {"mandate_id": mandate_id, "version": version}

    @api.get("/v1/orders/{order_id}", tags=["orders"], summary="Order status and its signed confirmation")
    def get_order(order_id: str):
        found = gw.get_order(order_id)
        if found is None:
            raise HTTPException(404, "no such order")
        return found

    # ------------------------------------------------------------ Valio Aimo's operators

    def operator_ok(token: str) -> None:
        if not config.OPERATOR_TOKEN:
            raise HTTPException(404, "the operator page is off (set OPERATOR_TOKEN)")
        if not hmac.compare_digest(token.encode(), config.OPERATOR_TOKEN.encode()):
            raise HTTPException(401, "operator token required")

    @api.get("/v1/agents", tags=["operators"], summary="Agents with a mandate here, and whether each is paused")
    def list_agents(x_operator_token: str = Header(default="")):
        operator_ok(x_operator_token)
        return gw.agents()

    @api.post("/v1/agents/pause", tags=["operators"], summary="Pause an agent: its orders are refused")
    def pause_agent(agent_id: str = Form(...), note: str = Form(""), x_operator_token: str = Header(default="")):
        operator_ok(x_operator_token)
        result = gw.pause(agent_id, "Valio Aimo operator", note)
        return JSONResponse(result.body, status_code=result.status)

    @api.post("/v1/agents/resume", tags=["operators"], summary="Resume a paused agent")
    def resume_agent(agent_id: str = Form(...), x_operator_token: str = Header(default="")):
        operator_ok(x_operator_token)
        return gw.resume(agent_id).body

    @api.get("/operator", include_in_schema=False)
    def operator_page(token: str = "", operator: str = Cookie(default="")):
        if token:                                   # first visit with ?token=: keep it in a cookie, drop it from the URL
            operator_ok(token)
            response = RedirectResponse("/operator", status_code=303)
            response.set_cookie("operator", token, httponly=True, samesite="strict")
            return response
        operator_ok(operator)
        return HTMLResponse(_operator_html(gw.agents()))

    @api.post("/operator/pause", include_in_schema=False)
    def operator_pause(agent_id: str = Form(...), note: str = Form(""), operator: str = Cookie(default="")):
        operator_ok(operator)
        gw.pause(agent_id, "Valio Aimo operator", note)
        return RedirectResponse("/operator", status_code=303)

    @api.post("/operator/resume", include_in_schema=False)
    def operator_resume(agent_id: str = Form(...), operator: str = Cookie(default="")):
        operator_ok(operator)
        gw.resume(agent_id)
        return RedirectResponse("/operator", status_code=303)

    return api


def _operator_html(agents: list[dict]) -> str:
    rows = []
    for a in agents:
        aid = escape(a["agent_id"], quote=True)
        if a["paused"]:
            state = f'<span class="chip bad">Paused</span><span class="sub">since {escape(a["paused_at"][:16].replace("T", " "))}'                     f'{" · " + escape(a["note"]) if a["note"] else ""}</span>'
            action = f'<form method="post" action="/operator/resume"><input type="hidden" name="agent_id" value="{aid}">'                      '<button class="btn secondary">Resume</button></form>'
        else:
            state = '<span class="chip ok">Active</span>'
            action = f'<form method="post" action="/operator/pause" class="pause"><input type="hidden" name="agent_id" value="{aid}">'                      f'<label class="sr">Reason for pausing {aid}</label><input name="note" placeholder="Reason (optional)" maxlength="300">'                      '<button class="btn danger">Pause agent</button></form>'
        last = escape(a["last_order"][:16].replace("T", " ")) if a["last_order"] else "none yet"
        rows.append(f'<tr><td><strong>{escape(a["buyer_org"])}</strong><span class="sub mono">{aid}</span></td>'
                    f'<td>{a["orders"]}<span class="sub">last {last}</span></td><td>v{a["mandate_version"]}</td>'
                    f'<td>{state}</td><td class="act">{action}</td></tr>')
    body = "".join(rows) or '<tr><td colspan="5">No agent has sent a mandate yet.</td></tr>'
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Valio Aimo · Agents</title><style>
:root{{--ink:#14213d;--muted:#5b6478;--line:#e3e7ef;--bg:#f4f6fa;--card:#fff;--bad:#b42318;--ok:#067647}}
body{{margin:0;font:15px/1.45 system-ui,sans-serif;color:var(--ink);background:var(--bg)}}
main{{max-width:1040px;margin:0 auto;padding:32px 16px}} h1{{margin:4px 0 6px}} .eyebrow,.lead,.sub{{color:var(--muted)}}
.eyebrow{{font-size:13px}} .lead{{margin:0 0 20px}} .card{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:8px 20px;overflow-x:auto}}
table{{width:100%;border-collapse:collapse}} th,td{{text-align:left;padding:12px 8px;border-top:1px solid var(--line);vertical-align:middle}}
th{{font-size:13px;color:var(--muted);font-weight:600;border-top:0}} .sub{{display:block;font-size:12.5px}} .mono{{font-family:ui-monospace,monospace}}
.chip{{display:inline-block;padding:2px 10px;border-radius:99px;font-size:13px;font-weight:600}} .chip.ok{{background:#e7f6ec;color:var(--ok)}} .chip.bad{{background:#fdecea;color:var(--bad)}}
.act form{{display:flex;gap:8px;justify-content:flex-end;margin:0}} input{{font:inherit;padding:7px 10px;border:1px solid var(--line);border-radius:8px;min-width:0}}
.btn{{font:inherit;font-weight:600;padding:7px 14px;border-radius:8px;border:1px solid transparent;cursor:pointer;white-space:nowrap}}
.btn.danger{{background:var(--bad);color:#fff}} .btn.secondary{{background:#fff;border-color:var(--line);color:var(--ink)}}
.sr{{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0)}}
</style></head><body><main>
<div class="eyebrow">Valio Aimo · agent gateway operations</div><h1>Purchasing agents</h1>
<p class="lead">Every customer agent that orders through the gateway. Pausing one takes effect on its next order: Valio Aimo refuses it with the reason, whatever its mandate allows.</p>
<div class="card"><table><thead><tr><th>Customer and agent</th><th>Orders</th><th>Mandate</th><th>Status</th><th><span class="sr">Action</span></th></tr></thead>
<tbody>{body}</tbody></table></div></main></body></html>"""
