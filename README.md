# Junta

An AI organizer for rotating savings groups (*juntas*, *panderos*, *tandas*), with contributions collected through PayPal invoices and the pot released through PayPal Payouts.

> Work in progress for the [PayPal AI Hackathon 2026](https://paypalaihackathon.devpost.com/). Submission deadline: Nov 12, 2026, 12:00 PM PST.
>
> **Prototype on the PayPal sandbox with test accounts only. No real money. A real launch would require a compliant setup.**

## Idea

- Each member pays their monthly contribution through a **PayPal invoice**: the member approves their own payment.
- The pot is paid to that month's member through **PayPal Payouts**, and only when every contribution is confirmed as paid. This rule is enforced by deterministic code, not by the AI.
- An **AI organizer** in the group chat understands messages like "I can't pay this month" and proposes a **fair turn swap**. A hand-written assignment algorithm computes the swap, and members approve it.

Junta makes the group's commitments visible. It does not guarantee that a member who has already received the pot will keep paying.

## Status

- [x] PayPal sandbox viability test: order → approval → capture → payout
- [x] Core: group, turns, contributions, pot release, history (16 tests)
- [x] Web app: organizer panel, member page, PayPal return flow; full round verified on sandbox
- [x] AI organizer in the chat (Groq free tier, `openai/gpt-oss-120b`; keyword-rules fallback without a key)
- [x] Fair turn-swap algorithm (Hungarian algorithm from scratch, checked against brute force)
- [x] Web interface: group chat, swap proposals with Accept/Reject, organizer panel (37 tests)
- [ ] Evaluation set for the organizer, README, demo video

## Local setup

1. Copy `.env.example` to `.env` and fill in your **sandbox** credentials from developer.paypal.com → Apps & Credentials.
2. Never commit `.env`. It is listed in `.gitignore`.
