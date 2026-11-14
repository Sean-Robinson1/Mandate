# 3-minute demo script

The story: a kitchen's purchasing agent orders from Valio Aimo on its own, but only inside limits the buyer has
signed, and Valio Aimo checks those limits again itself. People step in only where the rules say they must.

## Setup (before the judges arrive)

1. Stop the services, then reset the demo data (empty databases, version 1 mandates, fresh stock):
   `.venv/Scripts/python -m scripts.reset`
2. Start both sides: `.venv/Scripts/python -m scripts.dev` (console on :8000, Valio Aimo gateway on :8100).
3. Open http://127.0.0.1:8000 in a wide browser window. You are Aino, head chef at the hotel.
4. Optional: open a second tab on http://127.0.0.1:8100/docs to show Valio Aimo's own API.
5. Dry run once: press **Next day** until a Valio Aimo item drops below its reorder level and the agent orders it,
   then reset again so the live run is fresh. Note which item it was.

Hotel limits in mandate version 1: the agent orders alone up to €2,500 per order (€8,000 per week); a person
approves up to €10,000; anything above is refused.

## Beats

| Time | Click | Say |
|---|---|---|
| 0:00–0:30 | Stay on **Supplies**. Point at **To order** (grouped by supplier; the Valio Aimo group says "Ordered by the purchasing agent"), **Arriving**, **Use first**, then **Stock by storage area**. | "This is the kitchen's real job: what we have, what runs out, what to order, and from whom; the agent is just one helper on the Valio Aimo group." |
| 0:30–1:00 | Press **Next day**. The Valio Aimo line changes to "Agent is ordering"; click it. | "A day of stock was used, milk dropped below its reorder level, and the agent reordered it by itself because the order is inside the limits our purchasing manager signed." |
| 1:00–1:30 | On the request page, show the verdict ("Ordered") and scroll to **How it happened**: the agent's steps on one side, Valio Aimo's on the other. Open the offer step. | "Every message between the two sides is here: Valio Aimo signed the offer with fixed prices for 15 minutes, the agent signed the order, and Valio Aimo checked our mandate again before confirming." |
| 1:30–2:05 | Back on Supplies, ask the agent panel for a large one-off order (for example "1,000 litres of lactose-free milk by Monday", which costs more than €2,500). Show "Needs Mikko's approval" with the reasons. Switch person to **Mikko** in the sidebar and press **Approve and order**. | "Over its limit the agent may not order alone, so it asks the purchasing manager named in the mandate; Mikko's approval is signed with his own key, and Valio Aimo verifies that signature too." |
| 2:05–2:40 | As Mikko, open **Purchasing rules**. Point at the suggestion (or press **Change rules**), raise the order limit, **Review the difference**, sign. Show "Version 2 signed and sent to Valio Aimo". | "Changing the rules is a signed new version, not a setting: Valio Aimo now holds version 2 and refuses anything signed under version 1." |
| 2:40–3:00 | Point at **Audit log intact** in the sidebar, open **Audit log** ("Chain verified"). | "Everything that happened, by the agent, by people, and by Valio Aimo, is in an append-only, hash-chained log, so any change to history shows up immediately." |

If time is short, skip the rules beat; the approval and the audit log carry the trust story.

## Likely judge questions

**What stops the AI from ordering something it shouldn't?**
The language model only reads the request. Every yes, ask, or no comes from fixed, tested code reading the signed
mandate, and Valio Aimo runs its own checks on the same mandate before it accepts an order.

**Why would Valio Aimo trust an outside agent at all?**
It doesn't have to. Each order carries the buyer's signed mandate, the agent's signature and, above the agent's
limits, the approver's signature. Valio Aimo checks all of them and the hard limits, and refuses old mandate versions.

**What is real and what is simulated?**
Prices and availability come from the challenge's Valio Aimo API. Stock use per day, deliveries and the two
kitchens are simulated; everything assumed is listed in `ASSUMPTIONS.md`.
