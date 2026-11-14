"""One request from the command line, through Valio Aimo's gateway (which must be running).

    python -m app.cli "I need 1,000 litres of lactose-free milk by Monday."
    python -m app.cli --buyer school --no-llm "..."
"""
import argparse
import sys

from app import agent, keys
from app.audit import verify_chain
from app.db import SessionLocal, init_db
from app.gateway_client import GatewayClient
from app.llm import get_llm
from app.mandates import DEMO_MANDATES

DEFAULT_REQUEST = ("I need 1,000 litres of lactose-free milk by Monday. "
                   "Find an option that meets our requirements based on price and availability.")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Trusted Trade buyer agent")
    ap.add_argument("request", nargs="?", default=DEFAULT_REQUEST)
    ap.add_argument("--buyer", default="hotel", choices=sorted(DEMO_MANDATES))
    ap.add_argument("--no-llm", action="store_true", help="rule-based intent parser only")
    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    init_db()
    mandate = keys.load_mandates()[args.buyer]
    with SessionLocal() as session:
        result = agent.run(args.request, mandate, session, GatewayClient(), llm=None if args.no_llm else get_llm())
        chain = verify_chain(session)
    print(f"=== {result.status} (trace {result.trace_id}) ===")
    print(result.receipt)
    print(f"Audit chain: {'verified' if chain.ok else 'BROKEN'}, {chain.events_checked} events.")
    return 0 if result.status in ("ORDERED", "NEEDS_HUMAN") else 1


if __name__ == "__main__":
    raise SystemExit(main())
