# FirstBack — Handoff for the next chat (2026-06-25)

## TL;DR
All net-new dev is DONE and **promoted to production** (`main` == `staging` == `89cce45`, live at
**ringback-gixe.onrender.com**, healthy). Everything new is **gated/inert** until the owner adds credentials.
The dev is well ahead of go-live — the remaining leverage is OWNER-SIDE (A2P, Stripe, voice deploy) + a
red-team punch-list. Owner gates every `staging`→`main` promotion (it's a clean fast-forward each time).

## Infra (Render workspace "My Workspace" `tea-d6rj2ccr85hc73eevi4g`)
- **ringback** (prod web) `srv-d8nfbh3tqb8s73d4jdgg` — branch `main`, auto-deploy, disk DB `/var/data/firstback.db`, url ringback-gixe.onrender.com
- **ringback-voice** `srv-d8o50umgvqtc73fu67j0` — DEPLOYED + healthy (`/twiml` 200), but web app's `FIRSTBACK_VOICE_URL` is UNSET → voice inert
- **firstback-tasks-cron** — 60s → `/tasks/run-due` (✅ working)
- **RingBackv2** (staging web), **jobmagnet** (sibling product, separate repo)
- Render MCP can WRITE env (`update_environment_variables`, merge) but **cannot read** values.

## What got built + promoted this session (all gated/inert)
Homepage "morning briefing" section · P2 **Jobber** FSM sync · P6 **Outlook** calendar · P1 **voice go-live
gaps** (dispatcher URL bug, httpx pin, toggle/copy honesty) · **Housecall Pro** FSM (2nd provider, push=no-op,
built against web-verified API) · **live inbound voice answering** · **pricing checkout wiring** (gated on
`billing.configured()`) + fixed a real bug (checkout/portal URLs used VOICE_PUBLIC_URL → now PUBLIC_BASE_URL).
Trackers/plans/audits in `product-review/` (BUILD-LOOP-*.md, plans/13-17, plan-audits/).

## Founder decisions (RESOLVED 2026-06-23, see [[firstback-founder-decisions]])
money-back guarantee = NO · hero "books the job"+Vic briefing = DONE · reputation bundle = GO but DEFERRED
until pricing live · soft-overage = HOLD · Jobber-vs-HCP = both built, owner picks which to connect.

## GO-LIVE — owner-side (the real remaining work; full steps in SETUP_NEEDED.md)
- **A2P 10DLC** (Heritage = **LLC with EIN** → Standard brand). Twilio creds ARE in Render. `FIRSTBACK_PUBLIC_URL`
  was MISCONFIGURED (Render had key `PUBLIC_BASE_URL` but code reads `FIRSTBACK_PUBLIC_URL`) — **FIXED** (set
  on ringback; render.yaml comment corrected). NEXT: owner creates a **Twilio Trust Hub Customer Profile** →
  pastes the `BU…` SID → assistant sets `TWILIO_TRUST_PRODUCT_SID` on ringback → owner runs `/setup` A2P step
  (submits Brand+Campaign) → carrier vetting (days) → `a2p_sync` auto-flips `a2p_status` to approved → A2P-gated
  features (widget, voicemail, recap, full text-back) go live + queued sends flush.
- **Stripe / pricing** — wired + dormant (`billing.configured()` False in prod). Owner: Stripe acct (test mode
  first), 6 price IDs ($99/$199/$399 monthly + annual −20%), set STRIPE_SECRET_KEY/WEBHOOK_SECRET/STRIPE_PRICE_*.
  Then assistant can verify checkout end-to-end with test keys. **BUT fix the pricing red-team items first** (below).
- **Voice** — service deployed; one env from live: set `FIRSTBACK_VOICE_URL=https://ringback-voice.onrender.com`
  on ringback + matching `FIRSTBACK_INTERNAL_SECRET` on both. Has its own gates (recording disclosure + attorney
  review). **Fix voice red-team items (B7–B10) before deploying.**

## Passwords (in progress)
Owner resetting BOTH apps' owner login to `Test1234` (email `heritagehousepainting@gmail.com`) via each
service's Render **Shell** with: `python -c "import db; from werkzeug.security import generate_password_hash as h; u=db.get_user_by_email('heritagehousepainting@gmail.com'); db.update_user_password(u['id'], h('Test1234')); print('OK', u['email'])"`
(FirstBack = `ringback` shell; JobMagnet = `jobmagnet` shell). No self-serve reset exists (forgot→/contact).

## RED-TEAM audit (2026-06-25) — DONE, awaiting owner pick on fixes
4-lane sonnet audit (`product-review/red-team/RED-TEAM-SYNTHESIS.md` + 01-04 lane reports). All test suites
green; findings latent. Verified clean: webhook sigs, token encryption, cross-tenant isolation, opt-out, A2P
approved-only gate, SQLi protection. Prioritized fix batches (NONE applied — owner gates each):
1. **Security hardening (LIVE):** ProxyFix for the X-Forwarded-For rate-limit bypass; logout→POST+CSRF; CSRF+rate-limit on /contact & /auth/forgot; remove hardcoded owner email default; TEMPLATES_AUTO_RELOAD=DEBUG.
2. **Pricing honesty (pre-Stripe):** Crew sells phantom features (team logins/multiple profiles — FTC risk); annual toggle cosmetic (all billed monthly); in-app "+50 for $12" link to nonexistent product; drop "No per-call fees. Ever."
3. **Billing robustness:** Stripe webhook race drops grant; no in-app cancel/upgrade; canceled subs keep grant.
4. **Voice pre-deploy:** outbound greeting missing "AI" disclosure (FCC); hardcoded "painted?" SMS for all trades; stream double-writes transcript turns; preflight probes wrong URL.
5. **Core correctness:** assistant rate-limiter double-count + wrong reset tz; rebook not transactional; "cancel all" → AI not opt-out.
6. **UX:** dead /command-center link; 404→/dashboard loop; "Send All" no confirm; missing reset/verify/export/delete.

## Standing rules
Owner gates every `staging`→`main` promotion (build on staging, push staging freely, fast-forward to main on
"promote"). Kernel files (`convos.py`,`llm.py`,`static/assistant.css`) — edit local copies, never run sync.py.
Honesty over spin. **Parallel background subagents in one workspace can git-corrupt the tree** — forbid `git`
in their prompts + have them write to the scratchpad (see [[parallel-agents-git-hazard]]).
