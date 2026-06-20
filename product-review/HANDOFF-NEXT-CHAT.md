# FirstBack — Handoff for the next chat

**Status as of 2026-06-20: the autonomous C–G build is COMPLETE and LIVE in production.**
This is NOT a "resume the build" handoff — the build shipped. It's an orientation + "what's left
needs the owner" handoff.

## What happened
The 12-lane product audit → 10 build-ready plans (`product-review/plans/01..10-*.md`) → a batched
autonomous `/goal` build of **FirstBack** (live missed-call text-back + AI-booking SaaS for
contractors, dogfooded on Heritage House Painting). Batches **C, D, E, F, G** were built, tested,
audited twice, shipped to `staging`, and — on the owner's explicit OK — **promoted to production**.

- Repo: `~/apps/firstback` · GitHub `heritagehousepainting-pixel/RingBack`.
- Production: **https://ringback-gixe.onrender.com** · Render service `srv-d8nfbh3tqb8s73d4jdgg`,
  auto-deploys from **`main`** · disk DB `/var/data/firstback.db`. **`main` now == commit `8163f56`**
  (was `92aacde` at session start). Render MCP needs a workspace selected (don't auto-select).
- 76/76 standalone tests green (`.venv/bin/python test_X.py`). Both goal-skill audit passes done.

## Read these FIRST (the durable record — don't re-derive)
- `product-review/AUTONOMOUS-BUILD-LOOP.md` — the per-batch tracker (everything that shipped).
- `SETUP_NEEDED.md` — the reconciled owner-action list (section "Autonomous build C–G — owner items to go live").
- `product-review/plan-audits/*.md` + `product-review/final-audit/{security,function,wording-ui}.md` — all audits.
- Memory: `firstback-cg-build-live`, `multi-agent-research-style` (the worktree-swarm gotcha),
  `jobmagnet-ringback-platform` (kernel facts), `no-spin-honest-assessments`, `friction-is-the-product`.

## FIRST do a READ-ONLY audit (change no code)
`git status` + `git log --oneline -6`; run the full suite; confirm production is healthy
(`curl https://ringback-gixe.onrender.com/health/ticker` → fresh; `/widget.js` → 200 means the C–G
code is live); confirm gates intact (pricing `/pricing` still "coming soon", voice off). THEN ask the
owner which remaining item to take — do NOT autonomously start new feature work without a pick.

## What's LEFT — all owner-gated (nothing to build solo without a decision/credential)
1. **4 founder decisions** (copy/policy deliberately NOT shipped): 30-day money-back guarantee badge;
   lead the hero with "it books the job" + the Vic briefing; bundle paid caller-reputation
   (`FIRSTBACK_REPUTATION_PROVIDER`, real cost); soft-overage billing vs hard cap.
2. **Turn on the shipped-but-OFF features** (all inert by default): Settings toggles for
   **voicemail→lead** and the **web-chat widget**; set `GOOGLE_PLACES_API_KEY` (review tracking);
   generate `/static/og-default.png` (1200×630) then re-add the `og:image`/`twitter:image` tags.
3. **NEEDS-OWNER features (not built):** deposit link at booking (Stripe Payment Link), GBP review
   dashboard (Google business-scope re-auth).
4. **Small deferred enrichments:** analytics milestones-timeline + `/api/roi_milestones` (07-3e);
   customer-book briefing-card hook (07-2e); briefing `deep_link` backend (C8B — frontend already
   works for `?lead_id`); annual-toggle checkout wiring (cosmetic until checkout exists).

## Live behavior changes the owner already accepted (don't "fix" as bugs)
Owner **quiet hours default ON 22:00–07:00** (non-urgent owner alerts held overnight; in-app feed +
8am digest still show them; urgent always sends — changeable in Settings); analytics page leads with
the dollar figure; `/customers` is the authed Customer Book (marketing → `/resources/customer-stories`);
monthly recap SMS day 28–31 when A2P-ready + ≥1 booking.

## Standing rules
- **The owner gates EVERY `staging`→`main` (production) promotion.** Never push `main` without an
  explicit OK. Build on `staging`, run the per-batch loop (plan-audit → build → tests green →
  audit → commit/push staging), promote only on "go."
- **Kernel files** (`convos.py`, `llm.py`, `static/assistant.css`) — edit firstback's LOCAL copies,
  never run `sync.py` (firstback's are intentionally ahead). Smart quotes break Jinja *expressions*.
- Honesty over spin; no overclaiming in copy; report tests/outcomes faithfully.

---
### Paste-ready prompt for the next chat
> You are picking up **FirstBack** (~/apps/firstback) after the autonomous C–G build shipped to
> production. Read `product-review/HANDOFF-NEXT-CHAT.md` first, then `SETUP_NEEDED.md` and
> `product-review/AUTONOMOUS-BUILD-LOOP.md`, plus the memory entry `firstback-cg-build-live`.
> Do a READ-ONLY audit (git status; full suite; curl https://ringback-gixe.onrender.com/health/ticker
> + /widget.js to confirm the C–G build is live; confirm pricing "coming soon" + voice off). Then ask
> me which remaining item to tackle — a founder decision, turning on a shipped-OFF feature, a
> NEEDS-OWNER feature, or a deferred enrichment. The owner gates every production (`main`) promotion;
> build on `staging`, never push `main` without my explicit OK.
