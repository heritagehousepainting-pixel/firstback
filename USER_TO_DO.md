# FirstBack — Your To-Do List

> **➡️ Work from `OWNER_TODO.md` instead — it's the single, complete, up-to-date owner
> to-do list. This file (local-dev framed, older) is kept for reference detail only.**

Things only **you** can do (set up accounts, keys, passwords). Each item has
step-by-step instructions. Work top to bottom; the first section matters today,
the last only when you're ready to put FirstBack online for real.

Last updated: 2026-06-14

---

## ✅ Do now (2 minutes)

### 1. Change your password
The app now requires a login. I seeded you a starter account:

- **Email:** `heritagehousepainting@gmail.com`
- **Password:** `firstback123`  ← change this

**Steps:**
1. Start the app and open http://127.0.0.1:8800/login
2. Sign in with the email and starter password above.
3. Go to **Settings** (left sidebar).
4. Scroll to the **Password** card at the bottom.
5. Enter the current password (`firstback123`) and a new one (8+ characters), then **Update password**.

> If you ever get locked out, tell me and I can reset it for you.

---

## 📅 When you want Google Calendar to work (~10 minutes)

The "Connect" button on **Settings → Connect your calendar** says **"Coming soon"**
until you give the app permission to talk to Google. This is a one-time setup in
Google's developer console. (Outlook / Apple / Yahoo stay "Coming soon" — they
don't offer a clean way to do this yet.)

**Steps:**
1. Go to **https://console.cloud.google.com** and sign in with the Google account
   whose calendar you want to use.
2. Top bar → **project dropdown → New Project** (name it e.g. "FirstBack"), then
   select it.
3. Left menu → **APIs & Services → Library** → search **"Google Calendar API"** →
   open it → **Enable**.
4. Left menu → **APIs & Services → OAuth consent screen**:
   - Choose **External**, click Create.
   - Fill in app name ("FirstBack"), your email where required, **Save and continue**.
   - On the **Test users** step, click **Add users** and add your own Gmail
     address. (This lets you use it right away without Google's full review.)
   - Save through to the end.
5. Left menu → **APIs & Services → Credentials → Create credentials → OAuth client ID**:
   - Application type: **Web application**.
   - Under **Authorized redirect URIs**, click **Add URI** and paste **exactly**:
     ```
     http://127.0.0.1:8800/api/calendar/google/callback
     ```
   - Click **Create**. Google shows you a **Client ID** and a **Client secret** —
     keep this box open.
6. Open the file `firstback/.env` in a text editor and add these two lines
   (paste your real values):
   ```
   GOOGLE_CLIENT_ID=paste-your-client-id-here
   GOOGLE_CLIENT_SECRET=paste-your-client-secret-here
   ```
7. **Restart the app.**
8. Go to **Settings → Connect your calendar** → the Google card now shows a real
   **Connect** button. Click it, approve access, and you're synced:
   - The AI won't offer a time you already have a calendar event for.
   - Every estimate it books is added to your Google Calendar.

> Note: when you later host FirstBack on a real web address (not `127.0.0.1`),
> come back to step 5 and add that address's `/api/calendar/google/callback` to
> the Authorized redirect URIs, and update `GOOGLE_REDIRECT_URI` in `.env`.

---

## 📱 When you want real texting (Twilio)

> **The easy path: the in-app Go Live wizard (`/setup`).** A contractor no longer needs
> the Twilio console or a server shell. The **Go Live** page in the app walks them through
> their number (buy a local one or attach an existing one — it auto-wires the webhooks),
> carrier A2P registration (submit + live status), and call forwarding (the exact star code
> for their carrier, tap-to-dial). It's honest: it never shows "live" until the number is
> bound, A2P is **approved**, and forwarding is set. The server-side Twilio **credentials**
> below (`TWILIO_ACCOUNT_SID/AUTH_TOKEN`, `FIRSTBACK_PUBLIC_URL`) are still set once by the
> operator in Render env; everything else is self-serve. The steps below are the manual
> reference / what the wizard does under the hood.

FirstBack's reminders and owner-alerts work in **simulated** mode until Twilio is
connected: the message shows up on the lead's conversation, but no real text is sent.
To make those texts actually reach a phone, connect a Twilio number.

**Steps (the outbound part — sending texts):**
1. Sign up at **https://www.twilio.com** and, in the Twilio Console, **buy a
   phone number** with SMS enabled.
2. From the Console dashboard copy your **Account SID** and **Auth Token**.
3. Open `firstback/.env` and add these three lines (your real values; the number
   in `+1...` E.164 form):
   ```
   TWILIO_ACCOUNT_SID=paste-your-account-sid
   TWILIO_AUTH_TOKEN=paste-your-auth-token
   TWILIO_FROM_NUMBER=+15551234567
   ```
4. **Restart the app.** Reminders and alerts now send for real instead of being
   simulated. (Leave any of the three blank and it safely stays simulated.)

**Steps (the inbound + callback part — now built):**

5. FirstBack has to be reachable on the internet for Twilio to call it. For testing,
   run `ngrok http 8800` to get a temporary `https://…` address; for real, host it
   (see "Run a real web server" below). Put that address in `.env`:
   ```
   FIRSTBACK_PUBLIC_URL=https://your-public-address
   ```
6. In the Twilio Console, open your number and set its webhooks (both **POST**):
   - **Voice → A call comes in:** `https://your-public-address/webhooks/twilio/voice/inbound`
   - **Messaging → A message comes in:** `https://your-public-address/webhooks/twilio/sms/inbound`
   Now a real missed call triggers the instant text-back, and customer replies are
   answered by the AI. (FirstBack only accepts requests Twilio actually signed.)
7. **Ringing your cell first (optional).** By default an incoming call goes straight
   to the text-back. If you'd rather it ring your phone first and only text back when
   you don't pick up, tell me your cell number and I'll set it (`forward_to`).
8. **A2P 10DLC registration (required for US texting).** In the Console →
   **Messaging → Regulatory Compliance → A2P 10DLC**: if your business has an **EIN**,
   register a Standard (or Low-Volume Standard) brand; if you're a true sole
   proprietor with **no EIN**, use the **Sole Proprietor** brand. Approval takes
   hours to ~2 weeks; until it's approved, carriers filter your texts.
9. **Avoid "Spam Likely" on callbacks (recommended).** Set up a **Business Profile
   (Trust Hub)** + **STIR/SHAKEN** and enroll your number in **Voice Integrity** so
   your outbound calls are trusted.

**The AI voice callback (optional — "reply CALL and the AI phones them"):**
This runs as a **second service** (`voice_service.py`). Install the extra packages
(`pip install -r requirements.txt`), host it publicly over https, and add:
```
FIRSTBACK_VOICE_URL=https://your-voice-service-address
```
Without it, a customer who texts "call me" simply keeps chatting by text — nothing
breaks.

> **Before real customers — legal.** Have a TCPA attorney review your consent flow.
> FirstBack is built to stay on the safe side (the text-back is informational, the AI
> voice call only happens after the customer asks, every call opens with an AI +
> recording disclosure, and STOP / "stop texting me" / quiet hours are all honored),
> but get sign-off before you go live.

---

## 📧 When you want alerts by email (SMTP)

FirstBack can alert you the moment a lead comes in or an estimate books. By
default these alerts show up **in-app on your dashboard** (under "Recent
alerts"), and you choose which events alert you under **Settings → Owner
alerts**. To also get them by **text**, set up Twilio (above) and add your cell.
To get them by **email**, connect an email account here:

**Steps (Gmail example):**
1. In your Google account, create an **App Password** (Google Account → Security
   → 2-Step Verification → App passwords). Copy the 16-character password.
2. Open `firstback/.env` and add:
   ```
   SMTP_HOST=smtp.gmail.com
   SMTP_PORT=587
   SMTP_USER=your-gmail@gmail.com
   SMTP_PASS=the-16-char-app-password
   SMTP_FROM=your-gmail@gmail.com
   ```
3. **Restart the app.** Your alert email defaults to your login email, so email
   alerts start working immediately. Adjust it in **Settings → Owner alerts**.

> Until SMTP is set, email alerts are simply skipped — nothing breaks, and you
> still see every alert in-app.

---

## 🔔 Reminders & follow-ups (already on — nothing required)

FirstBack now texts a reminder before each booked estimate and sends **one** gentle
nudge to a warm lead that goes quiet. It runs automatically:
- **On by default.** Turn either off, or change the reminder lead time, under
  **Settings → Reminders & follow-ups**.
- **Simulated until Twilio.** Like alerts, the reminder posts to the lead's
  conversation so you can see it; it becomes a real text once you connect Twilio
  (above). The dashboard's "Scheduled estimates" shows each reminder's state
  (set / sent).
- **Quiet hours.** Texts only go out 8am–9pm (business-local) by default; anything
  due overnight waits until morning. Tune with `QUIET_START` / `QUIET_END` in `.env`.

> You don't need to do anything here — it's running. **Production note:** the
> scheduler runs inside the app process, so on a host that may restart, also set
> `FIRSTBACK_TASKS_SECRET` in `.env` and have cron `POST /tasks/run-due` every
> minute with that secret in an `X-Tasks-Secret` header (belt and suspenders).

---

## ☁️ Put FirstBack online (Render)

This puts FirstBack on the internet so you (and later real customers) can reach it.
About **$7/month**, ~20 minutes. (Vercel/Netlify won't work for this app — it needs
an always-on server, a saved database, and a background timer; Render gives all
three.)

**1. Put the code on GitHub** (Render deploys from a Git repo).
   - Make a free account at **https://github.com** and create a **new private repo**
     named e.g. `firstback` (don't add any files to it).
   - In a terminal in the `firstback` folder, push the code (ask me and I'll do the
     first commit, then give you the exact `git push` line for your repo).
   - Your secrets are safe: `.env` and the database are gitignored, so they never
     leave your computer.

**2. Create the Render service.**
   - Sign up at **https://render.com** → **New + → Blueprint** → connect GitHub and
     pick the `firstback` repo. Render reads `render.yaml` and sets up the web
     service, a persistent disk for the database, and the security keys for you.
     Click **Apply**.

**3. Add your AI brain key** (in the service → **Environment**):
   - Claude: `FIRSTBACK_PROVIDER=claude` and `ANTHROPIC_API_KEY=your-key`, or
   - MiniMax: `FIRSTBACK_PROVIDER=minimax` and `MINIMAX_API_KEY=your-key`.
   - Also set `FIRSTBACK_TZ` to your timezone (e.g. `America/New_York`).
   - (Skip it and the built-in demo responder runs — fine for a first look.) Save;
     Render redeploys automatically.

**4. Open your URL** (Render gives you something like
   `https://firstback.onrender.com`).

**5. Log in and lock it down.** It's a fresh database (no demo leads). Log in with
   the starter account (`heritagehousepainting@gmail.com` / `firstback123`) and
   **change the password** in Settings right away. (Or set `FIRSTBACK_OWNER_PASSWORD`
   in Render's Environment before the first load.)

You're now live in simulation. Real texts and calls are the **Twilio** steps above.

---

## 🚀 Before you put FirstBack online (production checklist)

These don't matter while you're testing on your own computer, but **do** matter
before real customers/contractors use it.

### A. Set a secret key for logins
Right now logins are signed with a built-in dev key. Set your own so sessions are
secure.
1. Generate a random value. In a terminal: `python3 -c "import secrets; print(secrets.token_hex(32))"`
2. Add it to `firstback/.env`:
   ```
   FIRSTBACK_SECRET=the-long-random-value-you-just-generated
   ```
3. Once you're serving over **HTTPS**, also add `FIRSTBACK_HTTPS=1` so the login
   cookie is only ever sent over a secure connection. (Leave it off for local
   http testing, or the browser won't store the cookie and you can't stay logged in.)

### A2. Encrypt stored Google tokens at rest (recommended)
When a contractor connects Google, FirstBack stores a **refresh token** for their
account in the database. Set a token-encryption key so those are encrypted on disk
instead of saved in plain text.
1. Generate a random value: `python3 -c "import secrets; print(secrets.token_hex(32))"`
2. Add it to `firstback/.env` (use a **different** value than `FIRSTBACK_SECRET`):
   ```
   FIRSTBACK_TOKEN_KEY=the-long-random-value-you-just-generated
   ```
3. Restart. From now on every newly stored/refreshed Google token is encrypted.
   - **Leave it unset for local testing** and nothing breaks — tokens are just
     stored as-is (the app reads both forms).
   - **Already-connected businesses keep working** without reconnecting: their old
     plain-text token still reads, and the next automatic refresh re-saves it
     encrypted. To encrypt everyone immediately instead of waiting for the next
     refresh, have each connected business click **Disconnect → Connect** once
     (there's no code that rewrites the live database for you, by design).
   - **Keep this key safe and don't change it.** If you rotate it, tokens encrypted
     with the old key become unreadable and those businesses simply reconnect once.

### B. Keep debug mode OFF
It's already off by default — just **never** set `FIRSTBACK_DEBUG=1` on the live
server (it would expose a remote code console).

### B2. Set your timezone (so dates never drift)
By default the app uses the server's timezone. If you host it somewhere in a
different zone than your business, set yours so the calendar and booked dates
stay correct:
1. Add to `firstback/.env` (use your IANA zone name):
   ```
   FIRSTBACK_TZ=America/New_York
   ```
2. Restart. (Leave it unset to just use the server's local time.)

### C. Run a real web server (not the built-in dev one)
The Flask dev server is for testing only. On your host, run something like
Gunicorn and put it behind HTTPS. (Tell me your host — Render, Railway, a VPS,
etc. — and I'll give you the exact commands.)

### D. Google for the public
The test-user setup in the Google section is fine for you. To let *anyone*
connect their Google Calendar, Google requires an app-verification review
(submitted from the OAuth consent screen). Do this only when you go public.

### E. Protect your secrets file
When you put this code in Git, add a `.gitignore` containing `.env` so your keys
never get committed. (Tell me and I'll set this up.)

### F. (Optional) Switch the AI to Claude for launch
Today the AI brain is **MiniMax** (key already in `.env`). For the public launch
you mentioned Claude:
1. Add to `.env`:
   ```
   FIRSTBACK_PROVIDER=claude
   ANTHROPIC_API_KEY=your-anthropic-key
   ```
2. Restart. (If the key is missing it safely falls back, so nothing breaks.)

### G. (Optional) Clear the demo data
The database has ~24 test leads from building/demoing. Before going live with
your real account you may want them gone — tell me and I'll clear them (or leave
them; they're only visible to the Heritage account).

---

## Notes / housekeeping
- Backup files named `firstback.db.bak-*` were created before risky changes. Once
  you're happy everything works, they're safe to delete.
- Deleting `firstback.db` resets everything (re-seeds Heritage + the starter login).
  Don't do this casually — it wipes leads and appointments.
