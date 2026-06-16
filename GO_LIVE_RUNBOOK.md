# RingBack Go-Live Runbook

How to ship the code now and flip tenants live the moment Twilio clears. Accurate
to the code as of this build. See also `MIGRATION_NOTES.md` (A2P sync can
downgrade) and `HANDOFF.md` (overall session state).

---

## A. Ready-to-deploy vs blocked-on-Twilio

Twilio **MESSAGING access is currently PENDING.** Deploying the **code** is **not**
blocked. What's blocked is a **tenant going live** — that needs the A2P campaign
`CMf9c3fa6e814b0d777b8809ba5b453177` to clear vetting and Twilio creds on the
server.

**Build-ready now (mock-testable, no live Twilio):**
- Profile save (the A2P intake: name + EIN + business address).
- A2P submit + the operator/installer email.
- Forwarding star-code generation (per-carrier conditional-forward codes).
- Screening / conversation engine (triage + AI brain).
- The `/simulator`.
- The honest `is_live` logic (delegates to `compliance.launch_blockers`; can't
  claim live until number bound, A2P approved, forwarding confirmed).

**Blocked-on-Twilio (need a live, approved account):**
- Buy / search numbers.
- A2P status auto-sync (`connections.a2p_sync` — reads Twilio `campaign_status`).
- Real SMS send.
- Real voice dial.

---

## B. Render ENV checklist (CORRECT names)

| Env var | Value / note |
|---|---|
| `RINGBACK_PUBLIC_URL` | `https://ringback-gixe.onrender.com` — **required**. (Code constant is `PUBLIC_BASE_URL`; the env var is `RINGBACK_PUBLIC_URL`.) |
| `RINGBACK_VOICE_URL` | Only if AI voice is used. |
| `RINGBACK_TASKS_SECRET` | Strong random. **Unset ⇒ `/tasks/run-due` always 403** (cron can't run). |
| `TWILIO_ACCOUNT_SID` | From Twilio. |
| `TWILIO_AUTH_TOKEN` | Paste in Render directly — **never** in the repo. |
| `TWILIO_FROM_NUMBER` | `+12677562454` |
| `RINGBACK_DB_PATH` | `/var/data/ringback.db` — must point at a **persistent disk** or the DB resets every deploy. |
| `RINGBACK_SECRET` | App session secret. |
| `RINGBACK_HTTPS` | `1` |
| `RINGBACK_OPERATOR_EMAILS` | Operator allowlist for the A2P record action (being added this build). |
| `ANTHROPIC_API_KEY` + `RINGBACK_PROVIDER=claude` | The real brain. |
| `RINGBACK_OWNER_PASSWORD` | Change it off the `ringback123` seed. |

> Note: `HANDOFF.md` previously listed the wrong name `PUBLIC_BASE_URL` for this
> env var — fixed. The **env var** is `RINGBACK_PUBLIC_URL`; the **code constant**
> is named `PUBLIC_BASE_URL`.

---

## C. Operator go-live runbook (per tenant, once Twilio approved)

1. **Set env + redeploy** (section B).
2. **Get the number** — buy one (buying auto-wires the webhooks to
   `RINGBACK_PUBLIC_URL`) or attach `+12677562454`.
3. **Wire webhooks:**
   - **VOICE webhook ON THE NUMBER** (POST) →
     `https://ringback-gixe.onrender.com/webhooks/twilio/voice/inbound`
   - **SMS webhook ON THE MESSAGING SERVICE** — *not* on the number — (POST) →
     `https://ringback-gixe.onrender.com/webhooks/twilio/sms/inbound`
4. **Operator records A2P SIDs** on the tenant's Go-Live page — brand
   `BNf134617225d3b091f768e7f1f7262985`, campaign
   `CMf9c3fa6e814b0d777b8809ba5b453177`, and the messaging-service SID. `a2p_sync`
   flips the tenant to `approved` when Twilio reports VERIFIED.
5. **Catcher mode** — dial the carrier conditional-forward code **on the
   contractor's own phone**:
   - Verizon: `*71+12677562454`
   - AT&T: `*92+12677562454`
   - T-Mobile / US Cellular: `**61*+12677562454#`
   - Universal (GSM): `**004*+12677562454#`
6. **End-to-end test call** — call the contractor's **real** number, let it ring
   out → expect an instant text-back from the 267 number. Reply → AI responds.
   Text `STOP` → opt-out.

---

## D. Deploy milestone (honest)

**Shipped ≠ tenants live.** Ship the code now; tenants flip live **automatically**
once the A2P campaign is vetted and the SIDs are recorded (or on the cron's next
`a2p_sync`).

**Recommended now:** add a Render cron that POSTs `/tasks/run-due` with header
`X-Tasks-Secret`. It's a safe no-op pre-Twilio and auto-flips tenants live after
approval. (Requires `RINGBACK_TASKS_SECRET` set, or the endpoint 403s.)
