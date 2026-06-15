# Site Truth Audit — RingBack

**Goal:** every button, link, and claim on the site is TRUE — we only say we connect with
who we actually connect with. Run 2026-06-15 by a 3-teammate parallel audit (integrations /
buttons+links / copy). Severity: **P0** = false claim a buyer reads as fact · **P1** = misleading
or broken · **P2** = soften/polish.

## Headline result
The **integration claims are mostly honest**: Twilio SMS, Google Calendar, Google Contacts,
SMTP email, and the AI brain are all real, *gated* modules whose UI says "simulated until set up" /
"Coming soon" when credentials are missing. The Outlook/Apple/Yahoo calendar tiles are correctly
disabled "Coming soon" with no backing route. The untruths are concentrated in **third-party brand
logos, fabricated testimonials/stats, a "free" vs paid-pricing contradiction, and a few dead/
mislabeled controls.**

---

## P0 — false claims (fix now / decide now)

| ID | Where | Problem | Fix |
|----|-------|---------|-----|
| I1 | `onboarding.html:149` (LIVE homepage `/`) | "works with **Jobber** · **Housecall Pro**" — no integration code exists for either (they're competitors). | Remove both spans; keep Google Calendar + "Your existing number". ✅ fixing |
| C1 | `customers.html:23` | Fabricated testimonial: "Dana W. — Whitfield Heating & Air", 5★, "**0 missed** emergency calls". No such customer. | DECISION (see check-in) |
| C2 | `customers.html:29` | Fabricated testimonial: "Priya A. — Anand Plumbing", 5★, "**4 sec** average reply". | DECISION |
| C3 | `customers.html:17` | "Marcus B. — Heritage House Painting" + "Paid for itself the first week" / "**+9 estimates** month one". Business is real; the person/quote/stats are invented. | DECISION |
| C10 | `onboarding.html:60`, `marketing_base.html:59,72,103`, `pricing.html:25,41`, `product.html:16`, `auth.html:60` | "**Sign up for free** / Start free" everywhere, but pricing offers only paid plans ($99/$199/$399) and there's no billing system → "free" is undefined. | DECISION |

## P1 — misleading or broken

| ID | Where | Problem | Fix |
|----|-------|---------|-----|
| B1 | `auth.html:62` | "Terms" → `href="#"` though `/terms` exists (200). | Point to `/terms`. ✅ fixing |
| B2 | `auth.html:62` | "Privacy Policy" → `href="#"` though `/privacy` exists (200). | Point to `/privacy`. ✅ fixing |
| B3 | `auth.html:54` | "Forgot password?" → `href="#"`, no reset route exists. | Remove dead link now; password-reset → SETUP_NEEDED. ✅ fixing |
| B4 | `onboarding.html:153` | "Chat with us" button is inert (no handler anywhere). | Make it a real `/contact` link. ✅ fixing |
| C5–C8 | `company.html:25-27`; "4s" in `onboarding.html:117,139`, `customers.html:30`, `landing.html:38` | Invented stat tiles ("**4 sec** avg text-back", "**1 day** to live", "24/7") + a hard "4-second" reply time presented as measured fact. | DECISION (soften to "in seconds" / capabilities) |
| C11 | `pricing.html:64`, `product.html:38-45`, `onboarding.html` Call toggle | AI voice callback marketed as **included on Pro/Crew**, but the voice service is deactivated in prod → falls back to text. | DECISION (mark beta vs re-enable) |
| C12/C13 | `pricing.html:10,63,67`, `help.html:31-32`, `company.html:19`, `marketing_base.html:70` | "Cancel anytime / no cancellation fees / no per-call fees **Ever**" — no billing system to back these. | DECISION (pricing-model intent; drop absolutes) |
| C14 | `webinars.html:15,32-34` | "● Live · **Thursday, June 19** · 12 PM ET" + "Watch on demand" cards → `/signup`; no events or recordings exist. | DECISION (remove fake events / build real) |
| B5–B8 | `webinars.html:23,32,33,34` | "Save your spot" / "Watch on demand" CTAs all dump to `/signup` (mislabeled). | Tied to C14 decision |
| C4 | `customers.html:2-3,9` | 5★ on every story + "How real crews booked more work" implies a rated customer base (only 1 real business). | Tied to testimonials decision |
| C16 | `guides.html:12,14`, `help.html:13`, `product.html:96` | "**Most contractors** finish this in a sitting / are live within a day" implies a customer population. | Reword to capability ("you can get live in a day") |

## P2 — soften / polish

| ID | Where | Problem | Fix |
|----|-------|---------|-----|
| B9 | `product.html:17` | "See the live demo" → `/simulator`, which is auth-gated (302→/login). | Relabel "Sign up to try the demo" or build a public demo |
| C9 | `blog.html:14` | "Study after study… jump dramatically" — vague appeal to unnamed studies. | Cite a source or mark as opinion |
| C15 | `company.html:11`, `marketing_base.html:115` | "built by people who run a real crew" — unverifiable founder claim. | Soften to "built for" if not literally true |
| I2 | `landing.html:99-103` | "works with Jobber / Housecall Pro / **Angi**" — same false logos. | Page is DEAD/unrouted; recommend deleting landing.html (roadmap already flags it) |
| I3 | `ui_kit.html:189` | "syncs with your calendar" demo string. | Internal `/ui-kit` showcase only; no action |

---

## Verified HONEST (no change) — the core "do we connect with who we say" answer
- **Twilio SMS** (`messaging.py`) — `settings.html` says "Texts are simulated until Twilio is set up." ✅
- **Google Calendar** (`google_cal.py`) — status is dynamically Connected / Coming soon; connect link only when configured. ✅
- **Google Contacts** (`google_contacts.py`) — `callers.html` connect button disabled + "Coming soon" until configured. ✅
- **SMTP email** (`mail.py`) — "Email is simulated until SMTP is set up." ✅
- **Outlook / Apple iCloud / Yahoo** (`settings.html:74`) — disabled "Coming soon" tiles, no backing route, no JS handler. ✅
- **AI brain** (`ai.py`) — `simulator.html` honestly reports Claude (live) / MiniMax (live) / Built-in demo responder. ✅

> Teammate agent IDs (continue via SendMessage): integrations `a8a0ec36b7774ac32` · buttons/links
> `a63b52144be032b8d` · copy `a00af387c9fe57c6e`.
