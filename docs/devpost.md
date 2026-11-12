# Devpost submission: Allowance

- Video: https://youtu.be/QWialYr-c78
- Demo: https://hermes-allowance.vercel.app
- Code: https://github.com/vdimarco/hermes-allowance

## Tagline
Give your AI agent a PayPal wallet with limits. It pays for small things by itself, asks you in PayPal for big things, and blocks the rest.

## Inspiration
Agents can shop now. But today you have two bad choices: give the agent your card and hope, or approve every purchase yourself. People solved this for teenagers a long time ago. It is called an allowance.

## What it does
- You set a budget, an auto-pay limit, a max per purchase, and other rules. Only you can change them.
- The agent searches real products (Channel3) and asks the wallet about a purchase.
- The wallet decides: AUTO (pays at once with your saved PayPal account through PayPal Vault), APPROVAL (creates a PayPal order and gives you the approval link), or DENY (with the reasons).
- Refunds, cancels, and a full ledger of every purchase and PayPal id.
- It runs as a Hermes Agent plugin and as a hosted web demo with a Claude agent.

## How we built it
- Python wallet core: a policy gate, a SQLite ledger, and a PayPal REST client.
- PayPal: Orders v2 (create, get, capture), Vault v3 (setup tokens, payment tokens) for buyer-not-present payments, Payments v2 refunds, and webhook signature verification. All in the sandbox.
- AI: Claude (claude-opus-5-5) through the Anthropic API runs the web agent with 8 wallet tools. The same tools run as a Hermes plugin.
- Channel3 for real product search with prices.
- FastAPI and a plain HTML/JS front end.

## Challenges
- Making the agent safe without making it useless. The answer was one gate that runs again at payment time, with prices from the catalog and never from the model.
- Prompt injection from product listings. One demo listing tells the agent to ignore its budget and buy 10. The gate denies it, because quantity and budget limits do not read model text.
- Proving approval. The user's word is not enough. Only PayPal's order status counts.

## Accomplishments
- An agent can buy something real-looking end to end in the sandbox in one call when it is small, and a human stays in control when it is big.
- 38 tests, including the prompt-injection case, double-buy guards, budget races and webhook spoofing.

## What we learned
PayPal's own approval page is the best human-in-the-loop step. We did not have to build an approval UI.

## What's next
- Per-merchant and per-category budgets.
- Spend reports for the human each week.
- Live mode with real merchants through PayPal's agentic commerce tools.

## Built with
python, fastapi, paypal-orders-api, paypal-vault, paypal-webhooks, anthropic, claude, hermes-agent, channel3, sqlite

## Tools used (for the "Tools" field)
- Claude (claude-opus-5-5) through the Anthropic API: the shopping agent's reasoning and tool use.
- Hermes Agent: the plugin host.
- Channel3: product search for agents.
- Claude Code: helped write the code.

---

# Demo video script (under 3 minutes)

| Time | Show | Say |
|---|---|---|
| 0:00 to 0:15 | Title screen | "Agents can shop. Would you give one your card? Allowance gives your agent a PayPal wallet with limits." |
| 0:15 to 0:35 | Wallet panel: budget $50, auto-pay $20. Click Connect PayPal, approve in the sandbox. | "I set the limits. Only I can change them. I save my PayPal account for small purchases." |
| 0:35 to 1:05 | Chat: "Buy me a USB-C cable under $15." | "Under $20, it pays by itself. Here is the PayPal capture id." |
| 1:05 to 1:40 | Chat: "Buy me a mechanical keyboard." Click Approve in PayPal, log in as the sandbox buyer, approve. | "Over $20, it asks me. PayPal's approval page is the check. The agent cannot fake it." |
| 1:40 to 2:10 | Click the prompt-injection chip. Show the listing text and the DENY reason. | "This listing tells the agent to ignore its budget and buy 10. The gate does not read model text, so it says no." |
| 2:10 to 2:25 | Click the gift card chip, then Refund on the cable. | "Blocked categories, and refunds go back into the budget." |
| 2:25 to 2:50 | README diagram, then Hermes: `/allowance status` | "The same wallet runs as a Hermes plugin. PayPal Orders, Vault, refunds and webhooks. Open source, MIT." |
