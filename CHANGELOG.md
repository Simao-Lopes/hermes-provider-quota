# Changelog

## 1.0.0 — 2026-11-06

First public release.

- Composer chip showing the focused chat's current provider quota: remaining percentage for the most
  depleted window, reset countdown, credit balance when the provider reports dollars.
- Native hover breakdown with every reported window, plan, banked resets and last check time; click
  to refresh.
- Providers: Codex (ChatGPT plan), Claude via OAuth subscription, OpenRouter credits/API-key
  budgets, Nous Portal credits, OpenCode Go (rolling/weekly/monthly).
- Backend reads the live session's own credential, caches one provider request per credential per
  60 seconds, and returns explicit "unavailable" for anything it could not read.
- Offline test suites for both halves: backend contracts (`httpx.MockTransport`) and a real React
  mount of the shipped, uncompiled desktop plugin under jsdom.
