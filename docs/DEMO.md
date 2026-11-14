# 5-minute demo script

The story: one day at Bistro Lumo. The kitchen dashboard shows what to order and what is about to be wasted; the
purchasing agent orders from Valio Aimo on its own, but only inside limits the owner signed, and Valio Aimo checks
those limits again itself. People step in only where the rules say they must.

Two people present: one drives, one talks. Practise the person switches; that is where time goes.

## Setup (before the judges arrive)

1. Stop the services and reset the demo data (empty databases, version 1 rules, fresh stock):
   `.venv/Scripts/python -m scripts.reset`
2. Start both sides: `.venv/Scripts/python -m scripts.dev`. It prints three links: the console (:8000), Valio
   Aimo's gateway (:8100) and Valio Aimo's operator page (`/operator?token=…`).
3. Browser tab 1: http://127.0.0.1:8000. In the sidebar, sign in as **Elina Mäkinen · Head chef** (Bistro Lumo).
4. Browser tab 2: the operator link from step 2 (Valio Aimo's screen for pausing an agent).
5. Dry run the whole script once, then reset and restart again so the live run starts fresh. Note the real
   amounts the agent quotes, so you are not surprised on stage.
6. If the AI is slow or offline, set `LLM_PROVIDER=none` in `.env` and restart: the rule-based reader handles every
   request in this script.

Bistro Lumo's rules (version 1): the agent orders alone up to **€600 per order** and €2,500 a week; Jussi Heikkinen
(owner) approves up to €3,000 per order; above that nothing is ordered, even with approval.

What a fresh start shows: the morning count found cream and grated cheese low, and yesterday's delivery brought
too much salmon and lettuce, so both have a waste alert.

## Beats

| Time | Click | Say |
|---|---|---|
| 0:00–0:30 | Tab 1, **Supplies**, still as Elina. Open the **Search the catalog yourself** tab and search `lactose free milk`. The first result is a chocolate milk drink. | "Restaurants bin 4 to 10% of the food they buy, and ordering eats hours. Today a buyer searches a webshop and gets this. An AI could do better, but why would anyone let one spend money?" |
| 0:30–1:15 | Point at the agent's suggestion card (cream and grated cheese run low), press **Order all**. Then point at the waste alert below it: 24 kg of salmon passes its date on Tuesday, about €341 in the bin. Note it says "Jussi decides menu prices". | "The morning count found two items low; the agent orders cream by itself. Grated cheese is set to 'ask first', so it waits for Jussi. And here the kitchen is about to throw away €341 of salmon. The numbers come from code: stock, use-by dates, the week's usual sales." |
| 1:15–1:45 | Switch to **Ask the agent**. Type `We need 150 litres of cream by Tuesday` and **Send to the agent**. Open the request: it waits for Jussi, with the reasons. | "Over €600 the agent may not order alone. It still did the work: found the product, checked allergens, got a price Valio Aimo signed, and it tells Jussi exactly why it is asking." |
| 1:45–2:30 | Switch person to **Jussi Heikkinen · Owner**. Open **Approvals**: the cream order shows the price against what we usually pay, why the agent asked, when it arrives, and which dishes use it. **Approve and order**. Back on **Supplies**, press **Run the special** on the salmon alert. | "Jussi decides in one screen, and his approval is signed with his own key; Valio Aimo checks that signature too. Then the waste alert: pan-fried salmon at €21.60 instead of €27 for four days uses up the salmon, and every portion still earns money." |
| 2:30–3:15 | Open **Try to cheat**, press **Run the cheating agent**. Seven tricks: an edited price, a reused offer, raised limits, the agent approving its own order, and more. Each one is refused with Valio Aimo's reason. | "Now assume the agent is broken or hacked. Valio Aimo doesn't trust it: every trick fails, and the reason comes from Valio Aimo's side, not ours." |
| 3:15–3:50 | Tab 2, Valio Aimo's operator page: **Pause agent** on Bistro Lumo with a reason. Tab 1: ask the agent for `20 litres of milk by Tuesday`. The request says it couldn't order: Valio Aimo paused this agent. Tab 2: **Resume**. | "And Valio Aimo has the off switch. Whatever our rules allow, Valio Aimo can stop an agent instantly, and the kitchen sees why in plain words." |
| 3:50–4:30 | Tab 1, **Supplies**: press **Next day**. Service plays, deliveries come in, the salmon special sells. Open **Food cost** ("Food cost and waste"). | "A day passes. The waste page shows what the kitchen throws away against the 4% a well-run kitchen aims for, what it costs a week, and where it comes from: quiet days, salmon, one cancelled group." |
| 4:30–5:00 | Point at **Audit log intact** in the sidebar, open **Audit log** ("Chain verified"). | "Any agent can order from Valio Aimo, but only within rules a person signed, and every step, by the agent, by people and by Valio Aimo, is in a log that shows any change to history." |

## If time is short

- Skip running **Try to cheat** live: run it in the dry run and show the finished results.
- Skip **Next day**: open **Food cost** straight away; it already has two weeks of history.
- Skip the pause beat last; it is the cheapest to describe in one sentence.

## Optional beat: changing the rules (+40 s)

As Jussi, open **Purchasing rules**, press **Change rules**, raise the agent's own limit, **Review the
difference**, sign. "Changing the rules is a signed new version, not a setting: Valio Aimo now holds version 2 and
refuses anything signed under version 1." Doing this before **Try to cheat** also lets its "old rules" trick run
(otherwise that one is skipped and says why).

## Likely judge questions

**What stops the AI from ordering something it shouldn't?**
The language model only reads the request. Every order, ask or stop comes from fixed, tested code reading the
signed rules, and Valio Aimo runs its own checks on the same rules before it accepts an order.

**What happens when the agent can't order?**
If a person could allow it (a higher price, a late delivery, more than usual), it asks the approver and says why.
If nobody could (an excluded allergen, above the per-order maximum, nothing arrives in time, a passed cut-off), it
says it couldn't order, explains why and suggests the next step.

**Why would Valio Aimo trust an outside agent at all?**
It doesn't have to. Each order carries the buyer's signed rules, the agent's signature and, above the agent's
limits, the approver's signature. Valio Aimo checks all of them, refuses old rule versions, and can pause any agent.

**Does the special really cut waste?**
The suggestion is worked out from stock, use-by dates and the usual week. In the simulation a dish on special sells
2.5 times as often, which is our assumption (there is no price-response data); see `ASSUMPTIONS.md`.

**What is real and what is simulated?**
Products, prices and availability come from the challenge's Valio Aimo API. The kitchens, their sales, stock use,
waste and other suppliers' deliveries are simulated; everything assumed is listed in `ASSUMPTIONS.md`.

**Which AI does it use?**
Whichever `.env` names: Claude, OpenAI/Codex or Gemini, or none at all. The safety does not depend on the model.
