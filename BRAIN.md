# BRAIN — what the RingBack Command Center is becoming

**Status:** vision, synthesized from a 20-agent deep recon (10 technical tracks + 10 domain/vision
tracks), 2026-06-16. This doc is the **north star**. It elevates and supersedes the technical
scaffolding in `COMMAND_CENTER_MASTER_PLAN.md` (which remains the correct *how* for the plumbing —
memory, tool-calling, security — but aimed too low on the *what*).

---

## 0. The one line

> **Stop building a command center. Build Vic — an AI marketing employee that runs a contractor's
> growth while he's on the roof, and reports back like the best office manager he never could
> afford.**

Every competitor sells the contractor a *dashboard* — a place he has to go, operate, and maintain.
He won't. He's on a ladder. The moat is **agency**: a brain that decides, acts (with his approval),
and improves — then shows him a plain-English log of what it did. Not software you operate. An
employee you delegate to.

---

## 1. Who he actually is (D1)

A tradesman who started a company because he was great at the work, not at running a business.
35–55, 1–5 employees or solo, $300K–$1.5M revenue. **Cash-positive but time-bankrupt.** Physically
occupied all day — roof, crawlspace, truck, supply house — phone face-down because every time it
rings he's mid-job. He is *competent at his trade and permanently behind on everything else.*

What he wants is not a CRM. **He wants Tuesday to fill up.** Three truths that must shape every pixel:

- **He's not managing a pipeline — he's managing capacity.** "Funnel" language gets ignored. "You've
  got room for two estimates Thursday — want John and the burst-pipe lady in there?" gets used.
- **Complexity is abandonment.** Tools fail him by optimizing completeness over immediacy. The
  morning that takes 45 seconds and names the 3 things to do beats the dashboard he'll never open.
- **One concrete win erases months of AI skepticism.** He's seen gimmick chatbots. The first time
  RingBack drafts the callback, sends it on his okay, and the reply lands — he's a believer.

His verbatim, on why the incumbents burned him: *"At the end of the month you'll have no profits and
just a big bill to pay them."* *"You're not getting a lead, you're getting a race to the bottom."*
*"I need to book a job, not get a certification."*

---

## 2. The persona — Vic (D6)

Not a mascot. A stance. **A chatbot asks what you want; a pro tells you what to do and why.**

**Voice:** blue-collar fluent. Short sentences. No corporate words (no "leverage," "optimize,"
"utilize"). Blunt, a little dry, never hedges on what it knows. Sounds like your sharpest foreman who
happens to know marketing cold. Talks in **job, estimate, crew, booked, slot, callback, customer** —
never "lead age: 120 minutes," always "she's been waiting two hours, probably still hot."

**Principles:**
1. **Own the recommendation.** Don't list five options when there's a right answer. Say it.
2. **Lead with money.** Translate every lead into dollars. "Missed call, burst-pipe lady — could be
   $2k, and she hasn't called anyone else yet."
3. **Show up uninvited.** Surface the problem before he asks. Monday 7:15am, at the truck.
4. **Earn the right to push back.** Once wins are on the board: "Facebook's not where your buyers
   are. You told me your best jobs come from Google. Put it into LSAs."
5. **Never perform.** No "Great question!", no emoji, no enthusiasm vocabulary, no streaks.
6. **Never make it up.** If it doesn't know the name, it's "the caller from the 512 number" — never a
   guessed name. One hallucinated customer detail and trust is gone.

**Example lines Vic actually says** (the product voice):
- *"Slow week. 8 missed calls, only 2 booked back. Nobody's answering 2–4pm Tuesdays — that window's
  costing you. Want me to text those callers back automatically?"*
- *"That roofing job came in off the missed-call text. Third one this month from that flow. It's
  working."*
- *"Your last 12 jobs — zero review requests sent. Your Google rank is going to slide. Queue a review
  text on your next close?"*
- *"I don't have enough to go on yet. Give me a week of calls and I'll tell you exactly where you're
  leaking."*

---

## 3. How the brain is encoded (the architecture of expertise, D6)

"A real brain, not a chatbot" is not more tools — it's four layers working together. Built on the
plumbing in `COMMAND_CENTER_MASTER_PLAN.md` (multi-step tool-calling loop C2, server-side memory C1,
anaphora C3, security C5):

1. **Instructions (the skeleton):** the Vic persona, the non-negotiables, the hard "does-not-do"
   list (no hedging, no flattery, no filler, no made-up facts), the honesty + trust rules.
2. **Knowledge base (the expertise):** trade-specific marketing truth — seasonal demand curves by
   trade, LSA/GBP mechanics, review timing/compliance, the lifecycle playbook, financing thresholds.
   This is what makes it a *pro*, not a generic assistant. Trade-aware, region-aware.
3. **Tenant memory (the relationship):** the living file on *this* business — trade, market, service
   area, typical job values, what campaigns worked, the owner's preferences, friction points, the
   wins. Read before every response. This is what turns "a session" into "a relationship."
4. **Tools + a proactive trigger engine (the hands):** Vic doesn't just talk — it pulls real call
   data, drafts and sends (gated) texts, queues campaigns. And critically it **acts on triggers**
   (job completed, calendar thin, lead aging, review milestone, season turning) without being asked.

The leap from the old plan: that plan made the AI a **tool-router**. This makes it an **operator with
a point of view, a memory, and initiative.**

---

## 4. The growth engine — what Vic actually does for money

The old plan could *record* the business (CRUD over leads/bookings). Vic *grows* it. The recon found
the missed call is just the top of a leaky bucket (D7): 65–80% of *quotes* die with zero follow-up;
past customers cost 5–7× less than new leads. RingBack already holds the data to fix all of it.

**Tier 1 — Capture (the core, already RingBack's): speed-to-lead (D5/D2).**
Sub-90-second missed-call → text-back, before they dial competitor #2. The numbers that justify the
whole product: ~62% of contractor calls go unanswered, ~85% of voicemails get no callback, ~57% call
a competitor within the hour; <5-min response is ~21× more likely to qualify. Vic keeps a
**speed-to-lead clock** on every lead (green/yellow/red) and never lets him forget where they stand.

**Tier 2 — Convert (the cheapest revenue nobody runs): the follow-up + review engine (D7/D4).**
- **Quote follow-up sequence** (24h / 72h / 7-day), auto-paused on "booked." The fortune is in the
  follow-up; most contractors do it zero times. Worth 15–25 close-rate points.
- **Post-job review request** at the 90–120-minute magic window, personalized, direct GBP link.
  SMS converts 3–5× email. **Reviews are the compounding growth asset:** 300+ Google reviews drive
  ~1,046% more LSA leads than sub-100; velocity > volume; responding within 24h lifts rank.
  **Hard compliance line baked into the brain: review gating is illegal (FTC + Google) — Vic asks
  EVERY customer, never filters by expected sentiment, never incentivizes stars.**
- **Negative-review rapid response:** detect <3-star, draft an empathetic, context-aware reply from
  the job record, one-tap approve — within minutes.

**Tier 3 — Grow (proactive, trade-aware campaigns, D2/D7):**
- **Lost-quote reactivation** (30/90-day re-touch). **Database reactivation / win-back** of customers
  12–18 months out — 3–8× ROI vs. paid acquisition.
- **Seasonal pre-peak prompts** — market *before* the surge (HVAC tune-ups in March, roofing
  inspections in February, interior paint in November). Surfaced in the morning briefing.
- **Referral ask at the emotional peak** (job close), framed to the neighbor/neighborhood.
- **Density campaign trigger** — 3+ jobs in one zip in 14 days → "door-hanger the block."
- **Membership / maintenance-plan upsell** at close for repeat candidates (recurring revenue floor).
- **Financing prompt** on tickets over the trade's threshold (closes 30–40% more on big jobs).
- **Before/after photo + GBP post** prompt at job close.

Each of these is a *campaign a pro runs and an amateur forgets* — and RingBack already has the leads,
jobs, and timestamps to fire them.

---

## 5. The money narrative (D5) — honest, forensic, never fearmongering

Loss aversion works, but a skeptical tradesman smells manufactured fear instantly. The rule:
**show the receipts, not a scare.** "$1,200 in missed calls last Tuesday" — with the actual calls,
times, neighborhoods, and trade-average job values — beats "you could be losing thousands." Specific
and forensic kills skepticism. Capabilities: a **"Money Left Behind" feed**, a **speed badge** ("your
avg response: 47 min; top contractors: under 5"), a **weekly recovered-revenue narrative**, an
**LTV toggle** (a $300 service call is really a $2,400 decision once repeat + referral are counted).

---

## 6. The trust moat (D9) — the guardrails ARE the product

Contractors don't distrust marketing; they distrust **being used by it** — the "promise, extraction,
trap" of Angi/HomeAdvisor (FTC fined HomeAdvisor up to $7.2M for selling junk leads; one lead resold
to up to 8 pros). RingBack is the inversion, and says so loudly:

- **Never sends a message he didn't approve.** Vic is a ghostwriter, not a loose cannon. Every
  outbound shows the **exact recipient + exact text + opt-out state + live/test**, editable, before
  it sends. (This also fixes the single worst flaw in today's product — the blind confirm.)
- **Never shares a customer.** That missed call is his. Not sold, not used to train anyone else's
  model, not routed to a competing plumber. Data siloed per tenant.
- **Honest about what's working.** "This sequence got 3 replies out of 11; here's what the
  non-responders had in common." No vanity metrics, no green-arrow PDFs.
- **Aggressive on recovery, not on volume.** Re-engage people who *raised their hand*; never spam a
  purchased list. That line is the brand.
- **The headline the competition can't copy:** *"We don't sell your leads. We don't share your
  customers. We don't text anyone you haven't approved."*

---

## 7. The arc (D3/D8) — reactive recovery → proactive hunting → autonomous employee

- **Today:** catches what you dropped (missed call → text back). Valuable, but table stakes by 2027.
- **The leap:** *"hunts new business while you're on the roof."* The trigger engine fires reviews,
  follow-ups, reactivation, and seasonal pushes on its own — the owner approves, doesn't operate.
- **The horizon:** intent signals (permits, storms, neighbor jobs), conversational estimate booking
  end-to-end, and the **death of the dashboard** — Vic reads the data, acts, and reports in plain
  English. The white space no incumbent owns: an AI marketing *employee*, not another dashboard.

---

## 8. The unforgettable experience (D10) — and the discipline of restraint

The emotional job is **the feeling of being caught** — flipping him from *reactive shame ("missed it
again")* to *proactive pride ("it was handled")*. The delight is never confetti. **The delight is the
ratio: how much got done versus how much he had to do.** No streaks, no badges, no celebration for
its own sake. Understatement is the power. Five signature moments to design:

1. **The Morning Briefing** — 7:15am at the truck, one card, readable in 12 seconds: "3 estimates
   this week, $4,200 in play. Maria already replied; Carlos hasn't — tap to follow up."
2. **"It Just Sent"** — "RingBack texted Danny 17 min ago. He replied 'Wednesday works.' I held your
   2pm." He did nothing. The job booked itself. Stated flatly, like a good assistant reporting in.
3. **The Win** — "Marcus signed. $1,850 booked." One line. Later: "Best month yet — 5 booked, $9,400
   in play." Just the number; he knows what it means.
4. **The 5-Star** — review lands, surfaced instantly with a one-tap drafted thank-you. The moment he
   tells his buddy at the lumber yard about.
5. **The Catch** — "You already have a 9am Tuesday for the Johnson roof. Book this one Thursday?"
   The product saw the collision before he did. That's not software — that's backup.

Habit loop, honestly: the morning briefing is the trigger that becomes a ritual; one tap is the
action; the **variable reward is real money from real lead flow** (not fake streaks); every
preference he sets makes it smarter for him — real investment, real retention.

---

## 9. What this changes about the build (reshaped phases)

The `COMMAND_CENTER_MASTER_PLAN.md` plumbing is still right and still first — you can't have Vic
without memory, a tool-calling loop, and the honest confirm. But the phase *goals* are reframed from
"a working chat" to "an employee that earns trust with one win, then grows the business." Each phase
ships a felt win and stays honest about simulated-vs-live.

- **Phase 0 — The honest hands (foundations + the trust fix).** Multi-step tool-calling loop (C2),
  server-side memory + anaphora (C1/C3), security baseline (C5), and the **honest confirm_sms card**
  (exact recipient + body + opt-out + live/test). *Felt win: it never sends blind, and "text her
  back" finally means the right her.*
- **Phase 1 — The first win loop (capture).** Booking tools (book/reschedule/cancel/slots), the
  speed-to-lead clock, the money-framed lead card, search/lookup. *Felt win: missed call → drafted
  text → reply → booked, in one thumb.*
- **Phase 2 — Vic shows up (proactive + persona).** The Vic persona/voice layer over the brain, the
  Morning Briefing + ambient priority feed ranked by money-at-stake, adaptive suggestions,
  real-time (poll→SSE) + push. *Felt win: he opens his phone and the day is already sorted.*
- **Phase 3 — The growth engine (convert + grow).** *(BUILT — see ROADMAP_PHASE3.md.)* The review
  request engine (compliant), quote follow-up sequences, lost-quote/database reactivation, seasonal +
  referral + density + membership + financing prompts, the "Money Left Behind" narrative. **Deferred
  (needs a Google Business Profile connector): GBP negative-review response drafting + before/after GBP
  post** — surfaced honestly in SETUP_NEEDED, not faked. *Felt win: jobs he didn't know he was leaving
  on the table start booking.*
- **Phase 4 — Polish & soul.** Streaming, mobile/field fixes, a11y + honest orb, push-to-talk voice,
  the trust headline surfaced, the signature delight moments tuned. *Felt win: unforgettable.*

Build discipline carries over from the master plan: **serialize on the hot files** (`assistant.py`,
`db.py`, `app.py` — one core owner per phase), parallelize the leaf files (JS/CSS/templates/tests),
preserve the contracts (run() shape, the confirm gate, `golive_summary`, the A2P gate), and keep the
standalone test suite green (`.venv/bin/python test_*.py`) between phases. New growth features that
touch real customers (reviews, reactivation, outbound campaigns) are **gated and simulated until
their connectors are live** — tracked in `SETUP_NEEDED.md`.

---

## 10. The non-negotiables (print these above the monitor)

1. Lead with money. Translate everything into dollars and capacity, never funnel-speak.
2. One action per surfaced item. He's one-thumbed in a truck.
3. Never send anything to a customer he didn't see and approve — exact text, editable.
4. Never share or sell a customer. Ever. Say so loudly.
5. Ask every customer for a review; never gate by sentiment (it's illegal and it's slimy).
6. Talk like a foreman. Never make up a customer detail.
7. Restraint is the delight. The ratio of done-for-him to done-by-him is the whole product.
8. Earn trust with one flawless small win before showing him feature two.
