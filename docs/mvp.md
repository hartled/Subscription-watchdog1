# Subscription Watchdog — MVP

**Promise:** Drop in a bank/card CSV (or paste statement text) and in 30 seconds see every recurring charge, what it costs per month/year, which ones went up in price or look forgotten, and exactly how to cancel — without ever giving us your bank login.

## In scope
- CSV upload with auto-detected layouts (date/description/amount; debit/credit split; negative = spend or positive = spend) and pasted statement text.
- Merchant-name normalization (strip processor prefixes, store IDs, dates; alias map for popular services).
- Recurring detection: weekly / biweekly / monthly / quarterly / yearly cadence with tolerance; amount tolerance.
- Flags: **price hike** (latest charge > earlier typical), **possibly forgotten** (long-running ≥6 charges, small ≤$10/mo, annual, or recently increased).
- Monthly + yearly totals, per-subscription normalized cost.
- Cancel-guide library (~25 services) with steps and official links; generic fallback (incl. card-issuer dispute / app-store routes).
- Reminders: next expected renewal, "upcoming in 14 days" list, user-set reminder dates, downloadable `.ics`.
- Session-scoped data (random cookie ID), one-click delete-my-data, privacy page, disclaimer.

## Non-goals (MVP)
- No bank login/aggregator (Plaid etc.), no storing credentials — ever.
- No accounts/passwords, no email/SMS/push sending, no concierge cancellation, no bill negotiation.
- No PDF statement parsing (paste text instead), no budgeting/credit score, no mobile app, no payments.

## Differentiation
1. **Privacy-first:** file-in, analysis, delete — no bank credentials, minimal retention, no third-party trackers.
2. **Automatic detection without bank linking** (manual trackers can't find what you forgot).
3. **Price-hike alerts** front and center.
4. **Free self-serve cancel guides** vs. paid concierge.
5. Calendar-native reminders (.ics) instead of yet another notification app.

## Success metric for MVP
A user uploads a real export and the top recurring charges are detected correctly with ≤1 false positive per 10 results.
