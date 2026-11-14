"""HTTP interface of Valio Aimo's agent gateway. Interactive docs at /docs, spec at /openapi.json.

Run:  .venv/Scripts/python -m uvicorn gateway.api:create_app --factory --port 8100
"""
from __future__ import annotations

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse, RedirectResponse

from gateway.backend import ValioClient
from gateway.keys import gateway_signer, registry
from gateway.service import Gateway
from gateway.store import make_sessionmaker
from protocol.wire import OfferRequest, OrderConfirmation, OrderRefused, PlaceOrder, SearchResult

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

    return api
