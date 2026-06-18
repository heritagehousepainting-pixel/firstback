# Phase 5 — Pre-Build Synthesis ("best-in-class differentiators")
**Date:** 2026-06-18 · Synthesized by Opus from 2 parallel Sonnet planners (PREBUILD-1 scope/sequencing, PREBUILD-2 honesty/consent/risk), against verified code + the F06/F07/F08/F10/F11/F13-FINAL plans. Base: `staging` @ ~bfd6ceb. **Phase 5 is too big for one loop — it ships as ordered sub-phases.** This doc locks the breakdown, what exists, and the non-negotiable honesty/consent gates. Each sub-phase gets its own build-loop spec when we build it.

## Per-feature state (verified)
| Feature | State | Key anchors / delta |
|---|---|---|
| **SF-6 server-bound confirm token** | **MISSING** | `/assistant/confirm` (app.py:~864) echoes client-supplied tool+args — "you approve exactly what you saw" is NOT enforced server-side. Need a `pending_confirms` table + issue/redeem (idempotent, content-bound). The other two SF-6 items (quiet-hours backstop, START re-subscribe) are already built (Phase 1). |
| **F11 Vic proactive** | **PARTIAL** | briefing compute (`_compose_briefing`, `briefing_signature`, adaptive suggestions) solid; the whole proactive PUSH path (7AM one-tap SMS, stall nudge, briefing-enhanced alerts, pre-issued tokens) missing. |
| **F07 screening graduation** | **PARTIAL** | scoring engine (triage.py:70-193) + `set_screen_mode` complete; missing: `screening_promoted_at`, false-positive counter, graduation ticker job, "this was real" rescue tap, sensitivity slider, velocity burst. |
| **F13 growth engine** | **PARTIAL** | `plays()`/`scan()` exist but `growth_on` is still the old boolean; missing `growth_mode` (off/tray/auto), `status='held'`, release API, `growth_touch_log`, morning tray SMS, reply parser. |
| **F10 voice** | **PARTIAL (not deployed)** | `voice_service.py` (185 lines) + consent gate + quiet-hours built; `render.yaml` voice service commented out, `FIRSTBACK_VOICE_URL` unset, **streaming absent (barge-in is a no-op, dead air exists), no real call ever placed**. |
| **F06 cold follow-up** | **PARTIAL** | detection loop + generic template built; missing contextual Sonnet copy, Touch 2, robocaller exclusion. |
| **F08 contacts** | **PARTIAL** | OAuth + parsers + ingest + manual sync built; missing nightly auto re-sync (~20-line tick_once add) + bulk-accept UI; needs operator OAuth creds. |
| **SF-10 Crew multi-tenant** | **MISSING** | no org/team schema; design-only for now (do early so billing/auth aren't retrofitted). |

## LOCKED SUB-PHASE ORDER (risk-ordered; SF-6 first as the unblocker, voice last)
- **5a — SF-6 confirm token** (S, ~1 day). The unblocker for F11 + F13. ~3 files: `pending_confirms` table + `_issue_token(business, tool, args)→token` + a redeem path that re-loads the EXACT stored content and is idempotent (one redeem). Spec'd in F11-FINAL §4.1. No owner-ops. **Build this first.**
- **5b — F11 Vic proactive** (M-L; depends on 5a): briefing-enhanced alert SMS, the 7AM ONE consolidated push (notification + one-tap, never auto-send), warm-lead stall nudge, referential-ambiguity guard.
- **5c — F07 screening graduation** (S-M; independent — can parallelize with 5b): monitor→enforce auto-graduation (precision-first), the "this was real" rescue (ship BEFORE the graduation cron), blocked counter, sensitivity slider, velocity burst.
- **5d — F13 growth tray** (M-L; depends on 5a): `growth_mode` off/tray/auto + `held` status + release API + morning tray SMS + frequency cap + audit log. (`auto` stays UI-locked until the streak gate — see honesty rules.)
- **5e — F06 cold-follow-up hardening** (S-M; independent): contextual Sonnet copy + Touch 2 + robocaller exclusion.
- **5f — F08 contacts nightly sync** (S; needs owner Google Contacts OAuth creds): the tick_once re-sync + bulk-accept UI.
- **5g — F10 voice** (L; LAST — ops deploy + the streaming quality gate): deploy the voice service, token streaming, premium-voice ear-test, pre-call guard, metering, AMD/voicemail, the 7-check gate. Highest variance.
- **5h — SF-10 Crew schema design** (design-only, do early): org→users→businesses→numbers, so Starter ships as the 1-seat case without a later rewrite. Also closes the Phase-4 P2 (per-tenant dispatcher ownership check).

## NON-NEGOTIABLE HONESTY / CONSENT GATES (bake into each sub-phase spec)
- **[P0] SF-6 — server-bound + idempotent.** A token binds to the EXACT content the owner saw; redeem re-loads stored content (never client-supplied), redeems ONCE (no replay/double-send), expires. "You approve exactly what you saw" must be true at the SERVER, not just the UI.
- **[P0] Consent stays ONE-TAP (F11/F13).** Every customer-facing write (Vic's proactive sends, growth marketing) requires Dave's tap on a server-bound token EVERY time. A "proactive push" is a notification + chip, NOT a send. The "you approve everything" promise (assistant.py:~637) is load-bearing brand architecture — never optimize the tap away.
- **[P0] F13 `growth_mode='auto'` stays UI-locked** (visible, not clickable) until the earned-trust streak gate (L2 7-day GO streak) ships. Auto-sending marketing to past customers without the tap = TCPA exposure. Tray/off only until then. Win-backs scoped to inbound-initiated customers only. Dollar framing ("$X on the table") must be REAL (actual past jobs), labeled an estimate when from trade defaults.
- **[P0] F10 voice — do NOT claim or activate until ALL 7 checks pass:** deployed → real end-to-end call → streaming <1.5s first word → barge-in works → voicemail no-babble → quiet-hours blocks → premium-voice ear-test. Haiku mandatory (Opus = ~3s dead air = disqualifying). Streaming ships BEFORE the quality gate (running the gate without streaming guarantees failure). Until all pass: pricing says "coming soon" (already true). Mediocre voice is WORSE than none — it's the only failure that damages Dave's reputation with homeowners and compounds.
- **[P1] F07 — precision-first graduation + reversible.** The "this was real" rescue path ships BEFORE the graduation cron. Never silence a real homeowner. Cross-tenant spam ledger can be poisoned by a mass-flagger → keep CROWD_MIN + an operator audit before the first `enforce` tenant.
- **[P1] F11 referential ambiguity** ("text her back" → which lead?): never guess the recipient — ask. The 7AM push is ONE consolidated notification per morning, not per-event.

## BIGGEST GAPS / HAZARDS to close in the sub-phase specs
- **F13 `status='held'` needs a `due_scheduled_messages` exclusion guard as an ATOMIC commit** (guard + insert + release API together) — inserting held rows without the query guard auto-fires them on the next ticker tick.
- **F10 streaming is the highest-variance build in Phase 5** — dead air + no-op barge-in exist today; treat as a spike + hard quality gate, not a normal slice.
- F07 rescue-before-graduation ordering; F08 needs operator OAuth creds (owner-ops) before nightly sync is real.

## Recommendation
Build **5a (SF-6 confirm token) next** — small, no owner-ops, fully spec'd, and it unblocks the two highest-leverage retention features (F11 + F13). Then run the standard loop per sub-phase. Keep voice (5g) last and behind its 7-check gate.
