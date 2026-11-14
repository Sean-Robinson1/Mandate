"""Valio Aimo's gateway as MCP tools, so any MCP-capable agent (Claude Desktop, Claude Code, ...) can use it.

The tools call the same HTTP interface as every other agent; nothing here bypasses its checks. An agent can
search and get signed offers freely. Placing an order needs what the HTTP interface needs: an order signed with
the agent's key and the buyer organisation's signed mandate, so an MCP client cannot order without authority.

    .venv/Scripts/python -m gateway.mcp_server            (stdio; the gateway must be running on GATEWAY_URL)

Claude Desktop / Claude Code config:
    {"mcpServers": {"valio-aimo": {"command": "<repo>/.venv/Scripts/python", "args": ["-m", "gateway.mcp_server"],
                                   "cwd": "<repo>"}}}
"""
from __future__ import annotations

import httpx
from mcp.server.mcpserver import MCPServer

import settings as config

server = MCPServer(
    name="valio-aimo",
    title="Valio Aimo agent gateway",
    instructions=(
        "Valio Aimo is a Finnish food-service wholesaler. Use search_products to find products, then request_offer "
        "for live prices and availability (a signed offer valid for 15 minutes). Prices are EUR before VAT per "
        "sales unit; comparison_price is per litre or kilogram. place_order needs an order signed by the buyer's "
        "agent key and the buyer's signed mandate; without them Valio Aimo refuses and explains why."
    ),
)


def _http() -> httpx.Client:
    return httpx.Client(base_url=config.GATEWAY_URL, timeout=30)


def _answer(r: httpx.Response) -> dict:
    try:
        body = r.json()
    except ValueError:
        body = {"detail": r.text[:500]}
    return body if r.is_success else {"http_status": r.status_code, **(body if isinstance(body, dict) else {"body": body})}


@server.tool()
def search_products(query: str, claims: list[str] | None = None, exclude_allergens: list[str] | None = None,
                    category: str | None = None) -> dict:
    """Search Valio Aimo's catalog.

    query: words the product name must contain, e.g. "milk" or "oat drink".
    claims: required claims, e.g. ["FREE_FROM_LACTOSE"], ["VEGAN"], ["FREE_FROM_GLUTEN"].
    exclude_allergens: GS1 allergen codes to exclude, e.g. ["AN", "AP"] for tree nuts and peanuts.
    category: optional category code prefix, e.g. "10400" for milk.
    Returns matching products with their claims, allergens, labels and delivery reliability.
    """
    params = {"q": query, "claims": claims or [], "allergens": exclude_allergens or []}
    if category:
        params["category"] = category
    with _http() as http:
        return _answer(http.get("/v1/products", params=params))


@server.tool()
def request_offer(product_ids: list[str], delivery_by: str, buyer_org: str | None = None) -> dict:
    """Get a signed offer: fixed prices and availability for up to 200 products, delivered by an ISO date.

    The offer is signed by Valio Aimo (verify with get_signing_key) and valid for 15 minutes.
    """
    with _http() as http:
        return _answer(http.post("/v1/offers", json={"products": product_ids, "delivery_by": delivery_by,
                                                     "buyer_org": buyer_org}))


@server.tool()
def place_order(signed_order: dict, signed_mandate: dict) -> dict:
    """Place an order against an offer.

    signed_order: {"offer_id", "mandate_id", "lines": [{"product_id", "sales_units"}], "approval"?} signed by the
    agent key named in the mandate. signed_mandate: the buyer organisation's signed mandate.
    Returns Valio Aimo's signed confirmation, or the refusal with every check it made.
    """
    with _http() as http:
        return _answer(http.post("/v1/orders", json={"order": signed_order, "mandate": signed_mandate}))


@server.tool()
def get_order(order_id: str) -> dict:
    """Status of an order and its signed confirmation."""
    with _http() as http:
        return _answer(http.get(f"/v1/orders/{order_id}"))


@server.tool()
def get_signing_key() -> dict:
    """Valio Aimo's public Ed25519 key (JWKS), for verifying offers and confirmations."""
    with _http() as http:
        return _answer(http.get("/.well-known/jwks.json"))


if __name__ == "__main__":
    server.run()
