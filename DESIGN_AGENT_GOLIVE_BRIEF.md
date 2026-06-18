# Design-agent brief: Go-Live wizard UI fixes

A ready-to-paste prompt for a **separate design agent** that will implement the
`/setup` (Go-Live) wizard fixes, plus the backend→template data contract the
template can rely on. The backend changes (new template vars) are owned by a
code agent; this brief documents the contract so the design agent codes against it.

---

## Ready-to-paste prompt

> You are implementing UI fixes to FirstBack's Go-Live wizard. Files in scope:
> `templates/setup.html` and `static/setup.css` (plus `templates/app_shell.html`
> for nav icons). **Do not touch any `.py` file.**
>
> **Must-obey rules (non-negotiable):**
> - **Tokens, never literals** — every color/space/radius/size references a CSS
>   custom property; if you need a value, add a token, don't inline a hex or px.
> - **No inline styles** — no `style="…"` attributes; all styling in `setup.css`.
> - **Static-first / works without JS** — every control must be complete, legible,
>   and operable with zero JS. JS is enhancement only.
> - **WCAG AA contrast** — body text ≥ 4.5:1, large/meaningful UI ≥ 3:1, against
>   its *actual* background. Verify computed values; don't eyeball.
> - Read and obey `/Users/jonathanmorris/apps/design_hub/UI/PRINCIPLES.md`.
> - Keep the audit at **0/0**:
>   `python3 ~/apps/design_hub/UI/skills/ui-audit/scripts/audit.py templates/setup.html static/setup.css`
>
> **The fixes:**
>
> **(A) `.code-cancel` contrast** (`setup.css:68`) — change `color:var(--ink-faint)`
> → `var(--ink-soft)`. (`--ink-faint` fails AA on the card background.)
>
> **(B) `.step-num` contrast** (`setup.css:26–29`) — change the badge text color
> `var(--ink-faint)` → `var(--ink-soft)`, and **remove** any `opacity:.72` that
> lands on the number text so the digit meets contrast. (Note: `.step.is-todo`
> sets `opacity:.72` on the whole step at `setup.css:24`; the step-num digit must
> still read at AA — restructure so the number itself isn't dimmed below the
> threshold, or lift the badge out of the dimmed scope.)
>
> **(C) Stepper semantics** (`setup.html:39–54`) — make the stepper an ordered
> list: wrap the steps in `<ol class="stepper">` with each step an `<li>`
> (keep the `<section class="step …">` inside, or promote to `<li>`). Put
> `aria-current="step"` on the current step. Give each step an
> `aria-label="Step N of M: <title>"`. The `step-num` badge is decorative once the
> aria-label carries the number — mark it `aria-hidden="true"`.
>
> **(D) Decorative SVGs `aria-hidden`** — the banner icons at `setup.html:18` and
> `setup.html:29`, and the nav icons in `app_shell.html`, get
> `aria-hidden="true"` (they're decorative; the adjacent text carries meaning).
>
> **(E) Carrier `<select onchange>`** (`setup.html:152–161`) — **always render**
> the "Show my code" submit button (move it **out of** `<noscript>`), so the form
> works without JS. Keep `onchange="this.form.submit()"` as a progressive
> enhancement on top.
>
> **(F) Delete dead CSS** — after grep-confirming they're unused in `templates/`,
> delete `.choice*` (`setup.css:46–53`) and `.step-hint` (`setup.css:34`).
> (Already grep-confirmed unused at the time of writing — re-confirm before
> deleting.)
>
> **(G) Banner honesty** — the wizard must not imply completeness it doesn't have.
> Two new template vars are provided (see data contract): `is_live` (server live)
> and `live_verified` (`is_live` AND a test call was texted back).
> - Show the green **"You're live"** state **only** when `live_verified`.
> - When `is_live && !live_verified`, show **"Setup complete — make a test call to
>   confirm forwarding works"** with a test-call affordance (e.g. tap-to-call the
>   tenant's own number / clear instruction to place the call).
> - The "N of N steps done" line must **not** imply completeness when `!is_live`.
> - Steps come pre-computed with `sms_configured` factored in — **don't hardcode
>   green**; render each step's status from `steps[].status` / `steps[].done`.
>
> **Done means:** the audit command above prints **0/0**, the page is fully usable
> with JS disabled, and contrast is verified. Show the audit output as proof.

---

## Backend → template data contract

The design agent codes the template against these variables. `is_live` is being
**narrowed** to mean *server-live only*, and `live_verified` is **new**.

| Var | Type | Meaning |
|---|---|---|
| `is_live` | bool | **Server live only** — `compliance.launch_blockers` is empty (number bound, A2P approved, forwarding confirmed). Does **not** mean a test call has been confirmed. |
| `live_verified` | bool | **NEW.** `is_live` **AND** a test call was texted back (forwarding proven end-to-end). Gate the green "You're live" banner on this. |
| `steps` | list | Ordered setup steps. Each: `.key`, `.title`, `.status` (`done`/`current`/`ready`/`todo`), `.done` (bool), `.open` (bool). Already factors `sms_configured` — don't re-derive. |
| `done_count` | int | Count of completed steps. **Already excludes `sms_configured`-blocked steps** — don't add to it. |
| `blockers` | list[str] | Plain-English list of what's left before go-live (empty == live). |
| `last_call` | obj/None | `.from_number`, `.engaged` (bool — whether the caller was texted back). |
| `carriers` | dict | `key → {label, …}` for the carrier `<select>`. |
| `fwd` | obj | The resolved forwarding code: `.activate`, `.cancel`, `.note`, `.label`. |
| `sms_configured` | bool | Whether Twilio is configured on the server. Already factored into `steps`/`done_count`; surface honest "not configured yet" copy where relevant. |

**Notes for the design agent:**
- `is_live` now means **server-live only**; use `live_verified` for the green
  "You're live" claim.
- `done_count` and `steps` **already exclude** `sms_configured`-blocked steps —
  treat them as the source of truth; never hardcode a step green.
