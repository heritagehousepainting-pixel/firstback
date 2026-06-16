// RingBack front-end glue. Vanilla JS — no build step.
// Chat bubbles are built to match templates/components/ui/chat_bubble.html.

const AGENT_SVG =
  '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M12 2l1.7 6.1L20 10l-6.3 1.9L12 18l-1.7-6.1L4 10l6.3-1.9z"/></svg>';

function fmtClock(iso) {
  let d = iso ? new Date(iso) : new Date();
  if (isNaN(d.getTime())) d = new Date();
  let h = d.getHours();
  const m = d.getMinutes();
  const ap = h >= 12 ? "PM" : "AM";
  h = h % 12 || 12;
  return h + ":" + String(m).padStart(2, "0") + " " + ap;
}

function clearEmpty(container) {
  const empty = container.querySelector(".empty");
  if (empty) empty.remove();
}

// Single fetch helper for every API call: throws on a non-2xx response and
// surfaces the server's JSON {error} message, so callers can try/catch, show a
// graceful message, and re-enable their controls in a finally block.
async function apiFetch(url, options) {
  const res = await fetch(url, options);
  if (!res.ok) {
    let msg = "Request failed (" + res.status + ")";
    try {
      const e = await res.json();
      if (e && e.error) msg = e.error;
    } catch (_) {}
    throw new Error(msg);
  }
  return res.json();
}

// Mirrors the chat_bubble macro: agent (right, dark, spark) vs customer (left, light, initial).
function addBubble(container, { text, who, time, initial }) {
  clearEmpty(container);
  const wrap = document.createElement("div");
  wrap.className = "msg msg-" + who;

  const av = document.createElement("span");
  av.className = "msg-avatar";
  if (who === "agent") av.innerHTML = AGENT_SVG;
  else av.textContent = (initial || "C").trim().charAt(0).toUpperCase() || "C";

  const stack = document.createElement("div");
  stack.className = "msg-stack";
  const bubble = document.createElement("div");
  bubble.className = "msg-bubble";
  bubble.textContent = text;
  stack.appendChild(bubble);
  if (time) {
    const t = document.createElement("span");
    t.className = "msg-time";
    t.textContent = time;
    stack.appendChild(t);
  }

  wrap.appendChild(av);
  wrap.appendChild(stack);
  container.appendChild(wrap);
  container.scrollTop = container.scrollHeight;
}

function addMeta(container, text) {
  clearEmpty(container);
  const el = document.createElement("div");
  el.className = "chat-meta";
  el.textContent = text;
  container.appendChild(el);
}

// ---------- Simulator (the demo) ----------
(function () {
  const trigger = document.getElementById("trigger");
  if (!trigger) return;
  const thread = document.getElementById("thread");
  const form = document.getElementById("reply-form");
  const input = document.getElementById("reply-input");
  const sendBtn = form.querySelector("button");
  const status = document.getElementById("sim-status");
  const CUSTOMER = "Homeowner";
  // A demo homeowner's number (distinct from the business's own RingBack number,
  // which is shown in the device header from the business profile).
  const DEMO_CALLER = "+1 (415) 555-0142";
  let leadId = null;

  function banner(kind, label, text) {
    const el = document.createElement("div");
    el.className = "sim-banner sim-banner-" + kind;
    const strong = document.createElement("strong");
    strong.textContent = label;
    el.appendChild(strong);
    if (text) el.appendChild(document.createTextNode(" " + text));
    status.appendChild(el);
  }

  // A screened outcome (spam / known caller): no thread, just a status card showing
  // what RingBack did and why — the "knows who to text" story, made visible.
  function screenedCard(data) {
    const spam = data.status === "screened_spam";
    const el = document.createElement("div");
    el.className = "sim-screened " + (spam ? "sim-screened-spam" : "sim-screened-known");
    const head = document.createElement("div");
    head.className = "sim-screened-head";
    head.textContent = (spam ? "🚫 Screened — looks like spam" : "👤 Known caller — left for you");
    el.appendChild(head);
    const sub = document.createElement("p");
    sub.className = "sim-screened-sub";
    sub.textContent = spam
      ? "No text sent. RingBack won’t cold-pitch a robocaller — that protects your number."
      : "No automated text. You’ve dealt with this caller before, so RingBack leaves them to you.";
    el.appendChild(sub);
    (data.reasons || []).forEach((r) => {
      const li = document.createElement("div");
      li.className = "sim-screened-reason";
      li.textContent = "• " + r;
      el.appendChild(li);
    });
    thread.appendChild(el);
  }

  async function runScenario(scenario, btn, callerLabel) {
    thread.innerHTML = "";
    status.innerHTML = "";
    leadId = null;
    input.disabled = true;
    sendBtn.disabled = true;
    addMeta(thread, "Missed call · " + callerLabel + " · just now");
    btn.disabled = true;
    try {
      const data = await apiFetch("/api/sim/incoming", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: CUSTOMER, phone: DEMO_CALLER, scenario }),
      });
      if (data.screened) {
        screenedCard(data);                 // spam / known: show the screen, no conversation
        return;
      }
      leadId = data.lead_id;                 // prospect: the normal text-back conversation
      addBubble(thread, { text: data.reply, who: "agent", time: fmtClock() });
      input.disabled = false;
      sendBtn.disabled = false;
      trigger.lastChild.textContent = "Restart demo";
      input.focus();
    } catch (err) {
      addMeta(thread, "Could not start the demo. " + err.message);
    } finally {
      btn.disabled = false;
    }
  }

  trigger.addEventListener("click", () => runScenario("prospect", trigger, "real homeowner"));
  const spamBtn = document.getElementById("trigger-spam");
  const knownBtn = document.getElementById("trigger-known");
  if (spamBtn) spamBtn.addEventListener("click", () => runScenario("spam", spamBtn, "unknown number"));
  if (knownBtn) knownBtn.addEventListener("click", () => runScenario("known", knownBtn, "a contact you know"));

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const text = input.value.trim();
    if (!text || !leadId) return;
    addBubble(thread, { text, who: "customer", time: fmtClock(), initial: CUSTOMER });
    input.value = "";
    input.disabled = true;
    sendBtn.disabled = true;
    try {
      const data = await apiFetch("/api/sim/reply", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ lead_id: leadId, body: text }),
      });
      if (data.urgent) banner("urgent", "Urgent job", "— the owner is notified immediately.");
      addBubble(thread, { text: data.reply, who: "agent", time: fmtClock() });
      if (data.booked) banner("booked", "Estimate booked", "— " + data.booked + ". See it on the Dashboard.");
    } catch (err) {
      addMeta(thread, "Message not delivered. " + err.message);
    } finally {
      input.disabled = false;
      sendBtn.disabled = false;
      input.focus();
    }
  });
})();

// ---------- Dashboard conversation viewer ----------
(function () {
  const rows = document.querySelectorAll(".dt-row[data-id]");
  const convo = document.getElementById("convo");
  if (!rows.length || !convo) return;
  const notesEl = document.getElementById("lead-notes");

  function esc(s) {
    const d = document.createElement("div");
    d.textContent = s == null ? "" : String(s);
    return d.innerHTML;
  }
  function renderNotes(lead) {
    if (!notesEl) return;
    if (!(lead.summary || lead.address || lead.project_type)) {
      notesEl.innerHTML = "";
      return;
    }
    const stage = (lead.stage || "").toLowerCase();
    const pill =
      stage === "scheduled" ? "pill-booked" : stage === "warm" ? "pill-warning" : "pill-neutral";
    const label = stage ? stage.charAt(0).toUpperCase() + stage.slice(1) : "Lead";
    const row = (k, v) =>
      v ? `<div class="ln-row"><dt>${k}</dt><dd>${esc(v)}</dd></div>` : "";
    notesEl.innerHTML =
      `<div class="ln-head"><span>Lead notes</span><span class="pill ${pill}">${esc(label)}</span></div>` +
      `<dl class="ln-grid">${row("Name", lead.name)}${row("Address", lead.address)}${row("Project", lead.project_type)}</dl>` +
      (lead.summary ? `<p class="ln-summary">${esc(lead.summary)}</p>` : "");
  }

  const convoActions = document.getElementById("convo-actions");
  const flagSpamBtn = document.getElementById("convo-flag-spam");
  let openLeadId = null;

  if (flagSpamBtn) {
    flagSpamBtn.addEventListener("click", async () => {
      if (!openLeadId) return;
      if (!window.confirm("Mark this caller as spam? They won’t be texted again, and it helps screen this number for other businesses.")) return;
      flagSpamBtn.disabled = true;
      flagSpamBtn.textContent = "Marking…";
      try {
        await apiFetch("/api/leads/" + openLeadId + "/flag-spam", { method: "POST" });
        window.location.reload();
      } catch (err) {
        flagSpamBtn.disabled = false;
        flagSpamBtn.textContent = "Try again";
      }
    });
  }

  async function openLead(row) {
    rows.forEach((r) => {
      r.classList.remove("is-selected");
      r.setAttribute("aria-pressed", "false");
    });
    row.classList.add("is-selected");
    row.setAttribute("aria-pressed", "true");
    openLeadId = row.dataset.id;
    if (convoActions) convoActions.hidden = false;
    if (flagSpamBtn) { flagSpamBtn.disabled = false; flagSpamBtn.textContent = "Mark as spam"; }
    if (notesEl) notesEl.innerHTML = '<p class="ln-loading">Loading notes…</p>';
    convo.innerHTML = "";
    try {
      const data = await apiFetch(`/api/leads/${row.dataset.id}/messages`);
      const lead = data.lead || {};
      renderNotes(lead);
      if (!data.messages.length) {
        addMeta(convo, "No messages yet for " + (lead.name || "this lead"));
        return;
      }
      addMeta(convo, "Conversation with " + (lead.name || "lead"));
      data.messages.forEach((m) => {
        addBubble(convo, {
          text: m.body,
          who: m.direction === "out" ? "agent" : "customer",
          time: fmtClock(m.created_at),
          initial: lead.name || "C",
        });
      });
    } catch (err) {
      if (notesEl) notesEl.innerHTML = "";
      addMeta(convo, "Could not load this lead. " + err.message);
    }
  }

  rows.forEach((row) => {
    row.addEventListener("click", () => openLead(row));
    // Keyboard support: rows are role="button" tabindex="0" — Enter/Space activate.
    row.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " " || e.key === "Spacebar") {
        e.preventDefault();
        openLead(row);
      }
    });
  });
})();

// ---------- Dashboard: cancel a booked estimate ----------
(function () {
  const buttons = document.querySelectorAll(".appt-cancel[data-id]");
  if (!buttons.length) return;
  buttons.forEach((btn) => {
    btn.addEventListener("click", async () => {
      const when = btn.dataset.when || "this estimate";
      if (!window.confirm("Cancel " + when + "? The slot reopens and the customer is texted.")) return;
      btn.disabled = true;
      btn.textContent = "Cancelling…";
      try {
        await apiFetch("/api/appointments/" + btn.dataset.id + "/cancel", { method: "POST" });
        window.location.reload(); // refresh stats, the estimates table, and the calendar
      } catch (err) {
        btn.disabled = false;
        btn.textContent = "Try again";
      }
    });
  });
})();

// ---------- Marketing / front-door mobile nav (hamburger) ----------
(function () {
  const nav = document.querySelector(".ob-nav");
  const burger = nav && nav.querySelector(".ob-burger");
  if (!nav || !burger) return;
  function setOpen(open) {
    nav.classList.toggle("is-open", open);
    burger.setAttribute("aria-expanded", open ? "true" : "false");
    burger.setAttribute("aria-label", open ? "Close menu" : "Menu");
  }
  burger.addEventListener("click", (e) => {
    e.stopPropagation();
    setOpen(!nav.classList.contains("is-open"));
  });
  // Close when tapping outside the nav or pressing Escape.
  document.addEventListener("click", (e) => {
    if (nav.classList.contains("is-open") && !nav.contains(e.target)) setOpen(false);
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") setOpen(false);
  });
})();

// ---------- Scroll reveal (marketing landing only) ----------
(function () {
  const els = document.querySelectorAll(".reveal");
  if (!els.length) return;
  if (!("IntersectionObserver" in window)) {
    els.forEach((e) => e.classList.add("in"));
    return;
  }
  const io = new IntersectionObserver(
    (entries) => {
      entries.forEach((e) => {
        if (e.isIntersecting) {
          e.target.classList.add("in");
          io.unobserve(e.target);
        }
      });
    },
    { threshold: 0.12 }
  );
  els.forEach((e) => io.observe(e));
})();

// ---------- Settings: disconnect Google Calendar ----------
// (Connecting Google is a real OAuth redirect via the "Connect" link; the other
// providers are "Coming soon". Only disconnect needs JS.)
(function () {
  const btn = document.getElementById("google-disconnect");
  if (!btn) return;
  btn.addEventListener("click", async () => {
    btn.disabled = true;
    try {
      await apiFetch("/api/calendar/google/disconnect", { method: "POST" });
      window.location.href = "/settings";
    } catch (_) {
      btn.disabled = false;
      btn.textContent = "Try again";
    }
  });
})();

// ---------- Settings: in-house calendar ----------
(function () {
  const root = document.getElementById("calendar");
  if (!root) return;
  const grid = document.getElementById("cal-grid");
  const monthLbl = document.getElementById("cal-month");
  const detail = document.getElementById("cal-detail");
  let data = null;
  let current = null; // "YYYY-MM" or null = this month
  let selected = null; // ISO date

  function fmtPhone(raw) {
    let d = String(raw || "").replace(/\D/g, "");
    if (d.length === 11 && d[0] === "1") d = d.slice(1);
    return d.length === 10 ? `(${d.slice(0, 3)}) ${d.slice(3, 6)}-${d.slice(6)}` : (raw || "");
  }
  function chipTime(label) {
    const i = label.indexOf("·");
    return (i >= 0 ? label.slice(i + 1) : label).trim();
  }
  function fmtLongDate(iso) {
    const [y, m, dd] = iso.split("-").map(Number);
    return new Date(y, m - 1, dd).toLocaleDateString(undefined, {
      weekday: "long", month: "long", day: "numeric",
    });
  }
  function findDay(iso) {
    for (const w of data.weeks) for (const d of w) if (d.date === iso) return d;
    return null;
  }

  async function load(month) {
    try {
      data = await apiFetch(root.dataset.endpoint + (month ? "?month=" + month : ""));
      current = `${data.year}-${String(data.month).padStart(2, "0")}`;
      render();
    } catch (err) {
      grid.innerHTML = "";
      monthLbl.textContent = "Calendar unavailable";
      detail.textContent = "Could not load the calendar. Please try again.";
    }
  }

  function render() {
    monthLbl.textContent = data.label;
    grid.innerHTML = "";
    data.weeks.forEach((w) => w.forEach((d) => grid.appendChild(cell(d))));
    renderDetail();
  }

  function cell(day) {
    const el = document.createElement("button");
    el.type = "button";
    el.className = "cal-day"
      + (day.inMonth ? "" : " not-month")
      + (day.today ? " is-today" : "")
      + (day.past ? " is-past" : "")
      + (day.busy ? " is-busy" : "")
      + (day.estimates.length ? " has-est" : "")
      + (selected === day.date ? " is-selected" : "");
    let html = `<span class="cal-date">${day.day}</span>`;
    if (day.busy) html += `<span class="cal-flag">Busy</span>`;
    day.estimates.slice(0, 2).forEach((e) => {
      html += `<span class="cal-chip">${chipTime(e.label)}</span>`;
    });
    if (day.estimates.length > 2) html += `<span class="cal-more">+${day.estimates.length - 2} more</span>`;
    el.innerHTML = html;
    // Screen-reader label: full date + its state, since the visible cell is terse.
    const est = day.estimates.length;
    const state = day.busy ? "marked busy"
      : est ? est + (est === 1 ? " estimate booked" : " estimates booked")
      : day.past ? "past" : "open";
    el.setAttribute("aria-label", `${fmtLongDate(day.date)}, ${state}`);
    if (selected === day.date) el.setAttribute("aria-current", "date");
    if (day.past) el.setAttribute("aria-disabled", "true");
    el.addEventListener("click", () => { selected = day.date; render(); });
    return el;
  }

  function emptyDetail() {
    return `<div class="cal-detail-empty">`
      + `<span class="cal-detail-ico"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="18" rx="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/></svg></span>`
      + `<p>Select a day to see its estimates or block it as busy.</p></div>`;
  }

  function renderDetail() {
    if (!selected) { detail.innerHTML = emptyDetail(); return; }
    const d = findDay(selected);
    if (!d) { detail.innerHTML = emptyDetail(); return; }
    let h = `<div class="cal-detail-head"><h5>${fmtLongDate(d.date)}</h5>`;
    if (!d.past) {
      h += `<button type="button" class="btn btn-sm ${d.busy ? "btn-secondary" : "btn-ghost"} cal-block">${d.busy ? "Unblock day" : "Block day"}</button>`;
    }
    h += `</div>`;
    if (d.busy) h += `<p class="cal-detail-note">Marked busy — the AI won’t offer this day.</p>`;
    if (d.estimates.length) {
      h += `<ul class="cal-est-list">` + d.estimates.map((e) =>
        `<li><span class="cal-est-when">${e.label}</span><span class="cal-est-who">${e.name || "Lead"} · ${fmtPhone(e.phone)}</span></li>`
      ).join("") + `</ul>`;
    } else if (!d.busy) {
      h += `<p class="cal-detail-note">No estimates booked.${d.past ? "" : " This day is open for the AI to fill."}</p>`;
    }
    detail.innerHTML = h;
    const blk = detail.querySelector(".cal-block");
    if (blk) blk.addEventListener("click", async () => {
      blk.disabled = true;
      try {
        await apiFetch("/api/calendar/busy", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ date: d.date, busy: !d.busy }),
        });
        await load(current);  // re-renders the grid + detail (replaces this button)
      } catch (err) {
        blk.disabled = false;
        blk.textContent = "Try again";
      }
    });
  }

  root.querySelector(".cal-prev").addEventListener("click", () => load(data.prev));
  root.querySelector(".cal-next").addEventListener("click", () => load(data.next));
  root.querySelector(".cal-today").addEventListener("click", () => { selected = null; load(null); });

  load(null);
})();

// ---------- ROI / analytics page ----------
(function () {
  const root = document.getElementById("roi");
  if (!root) return;
  const tilesEl = document.getElementById("roi-tiles");
  const chartEl = document.getElementById("roi-chart");
  const buttons = root.querySelectorAll(".roi-r");
  const money = (n) => "$" + Number(n).toLocaleString();

  function tile(value, label, sub, tone) {
    return '<div class="stat-tile"><div class="stat-value">' + value + "</div>"
      + '<div class="stat-label">' + label + "</div>"
      + (sub ? '<div class="stat-sub ' + (tone || "") + '">' + sub + "</div>" : "")
      + "</div>";
  }
  function renderTiles(d) {
    const t = d.totals, hasRev = t.revenue != null;
    tilesEl.innerHTML =
      tile(t.leads, "Leads captured") +
      tile(t.booked, "Estimates booked", t.leads ? t.conversion + "% conversion" : null, t.booked ? "good" : "") +
      tile(t.conversion + "%", "Conversion rate") +
      tile(hasRev ? money(t.revenue) : "—", "Est. revenue recovered",
           hasRev ? "at " + money(d.avg_job_value) + "/job" : "Set avg job value in Settings",
           hasRev ? "good" : "");
  }
  function renderChart(series) {
    const days = (series || []).filter(Boolean);
    const total = days.reduce((s, d) => s + d.leads + d.booked, 0);
    if (!total) {
      chartEl.innerHTML = '<p class="roi-empty">No activity in this range yet. Fire a demo call to see it here.</p>';
      return;
    }
    const max = Math.max(1, ...days.map((d) => Math.max(d.leads, d.booked)));
    const W = 720, H = 200, pad = 24, n = days.length;
    const slot = (W - pad * 2) / n;
    const bw = Math.max(2, Math.min(16, slot * 0.34));
    const y = (v) => H - pad - (v / max) * (H - pad * 2);
    let rects = "";
    days.forEach((d, i) => {
      const cx = pad + slot * i + slot / 2;
      rects += '<rect class="roi-bar-leads" x="' + (cx - bw - 1).toFixed(1) + '" y="' + y(d.leads).toFixed(1)
        + '" width="' + bw.toFixed(1) + '" height="' + (H - pad - y(d.leads)).toFixed(1) + '" rx="2"><title>'
        + d.date + ": " + d.leads + " leads</title></rect>";
      rects += '<rect class="roi-bar-booked" x="' + (cx + 1).toFixed(1) + '" y="' + y(d.booked).toFixed(1)
        + '" width="' + bw.toFixed(1) + '" height="' + (H - pad - y(d.booked)).toFixed(1) + '" rx="2"><title>'
        + d.date + ": " + d.booked + " booked</title></rect>";
    });
    chartEl.innerHTML =
      '<svg viewBox="0 0 ' + W + " " + H + '" class="roi-svg" preserveAspectRatio="none" role="img" '
      + 'aria-label="Leads and booked estimates per day">'
      + '<line class="roi-axis" x1="' + pad + '" y1="' + (H - pad) + '" x2="' + (W - pad) + '" y2="' + (H - pad) + '"/>'
      + rects + "</svg>"
      + '<div class="roi-xaxis"><span>' + days[0].date + "</span><span>" + days[days.length - 1].date + "</span></div>";
  }
  async function load(range) {
    try {
      const d = await apiFetch(root.dataset.endpoint + "?range=" + range);
      renderTiles(d);
      renderChart(d.series);
    } catch (err) {
      chartEl.innerHTML = '<p class="roi-empty">Could not load analytics. ' + err.message + "</p>";
    }
  }
  buttons.forEach((b) =>
    b.addEventListener("click", () => {
      buttons.forEach((x) => { x.classList.remove("is-active"); x.setAttribute("aria-pressed", "false"); });
      b.classList.add("is-active");
      b.setAttribute("aria-pressed", "true");
      load(b.dataset.range);
    })
  );
  load("30d");
})();

// ---------- Callers page: review inbox (QuickBooks-style) + screened directory ----------
(function () {
  const root = document.getElementById("callers");
  if (!root) return;

  function esc(s) {
    const d = document.createElement("div");
    d.textContent = s == null ? "" : String(s);
    return d.innerHTML;
  }
  function fmtPhone(raw) {
    let d = String(raw || "").replace(/\D/g, "");
    if (d.length === 11 && d[0] === "1") d = d.slice(1);
    return d.length === 10 ? `(${d.slice(0, 3)}) ${d.slice(3, 6)}-${d.slice(6)}` : (raw || "");
  }
  function fmtDate(iso) {
    const d = iso ? new Date(iso) : null;
    if (!d || isNaN(d.getTime())) return "";
    return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  }
  const CAT_LABEL = { personal: "Personal", vendor: "Vendor", blocked: "Blocked", customer: "Client" };
  const CAT_PILL = { personal: "pill-neutral", vendor: "pill-warning", blocked: "pill-urgent", customer: "pill-booked" };
  const CAT_OPTS = [["customer", "Client"], ["vendor", "Vendor"], ["personal", "Personal"], ["blocked", "Blocked"]];

  // ===== Review inbox (To review / Sorted / Dismissed) =====
  const tabs = [...root.querySelectorAll(".cl-tab")];
  const inboxEl = document.getElementById("cl-inbox");
  const searchEl = document.getElementById("cl-search");
  const bulkBar = document.getElementById("cl-bulk");
  const bulkCount = document.getElementById("cl-bulk-count");
  const bulkActions = document.getElementById("cl-bulk-actions");
  let tab = "pending";
  let items = [];
  const selected = new Set();

  function setCounts(counts) {
    tabs.forEach((t) => {
      const badge = t.querySelector(".cl-tab-count");
      if (badge) badge.textContent = (counts && counts[t.dataset.tab]) || 0;
    });
  }
  function filtered() {
    const q = (searchEl.value || "").trim().toLowerCase();
    if (!q) return items;
    const digits = q.replace(/\D/g, "");
    return items.filter((s) =>
      (s.name || "").toLowerCase().includes(q) ||
      (digits && (s.number || "").includes(digits)) ||
      (s.reason || "").toLowerCase().includes(q));
  }
  function renderInbox() {
    const rows = filtered();
    if (!rows.length) {
      inboxEl.innerHTML = '<p class="cl-empty">' + (tab === "pending"
        ? "Nothing to review. RingBack flags a caller here whenever they start to look like a client, a vendor, or spam."
        : tab === "accepted" ? "Nothing sorted yet." : "Nothing dismissed.") + "</p>";
      syncBulk();
      return;
    }
    const head = tab === "pending"
      ? '<th class="dt-check"><input type="checkbox" id="cl-all" aria-label="Select all"></th><th>Caller</th><th>Why</th><th>Suggested</th><th></th>'
      : '<th class="dt-check"><input type="checkbox" id="cl-all" aria-label="Select all"></th><th>Caller</th><th>'
        + (tab === "accepted" ? "Sorted as" : "Was") + "</th><th>When</th><th></th>";
    const body = rows.map((s) => {
      const checkAttr = selected.has(s.id) ? " checked" : "";
      const check = `<td class="dt-check"><input type="checkbox" class="cl-pick" data-id="${s.id}"${checkAttr}></td>`;
      const who = `<td class="dt-strong">${esc(s.name || fmtPhone(s.number))}<div class="cl-sub">${esc(fmtPhone(s.number))}</div></td>`;
      if (tab === "pending") {
        const opts = CAT_OPTS.map(([v, l]) => `<option value="${v}"${v === s.suggested_category ? " selected" : ""}>${l}</option>`).join("");
        return `<tr data-id="${s.id}">${check}${who}<td class="dt-muted">${esc(s.reason || "")}</td>`
          + `<td><select class="field-control cl-cat" aria-label="Category">${opts}</select></td>`
          + `<td class="dt-actions"><button type="button" class="btn btn-primary btn-sm cl-accept">Accept</button> `
          + `<button type="button" class="btn btn-ghost btn-sm cl-dismiss">Dismiss</button></td></tr>`;
      }
      const tone = CAT_PILL[s.suggested_category] || "pill-neutral";
      return `<tr data-id="${s.id}">${check}${who}`
        + `<td><span class="pill ${tone}">${esc(CAT_LABEL[s.suggested_category] || s.suggested_category)}</span></td>`
        + `<td class="dt-muted">${esc(fmtDate(s.updated_at || s.created_at))}</td>`
        + `<td class="dt-actions"><button type="button" class="btn btn-ghost btn-sm cl-reopen">Undo</button></td></tr>`;
    }).join("");
    inboxEl.innerHTML = `<div class="dt-wrap"><table class="dt"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
    wireRows();
    syncBulk();
  }
  function wireRows() {
    const all = document.getElementById("cl-all");
    if (all) all.addEventListener("change", () => {
      filtered().forEach((s) => (all.checked ? selected.add(s.id) : selected.delete(s.id)));
      renderInbox();
    });
    inboxEl.querySelectorAll(".cl-pick").forEach((cb) =>
      cb.addEventListener("change", () => {
        const id = Number(cb.dataset.id);
        cb.checked ? selected.add(id) : selected.delete(id);
        syncBulk();
      }));
    inboxEl.querySelectorAll("tr[data-id]").forEach((tr) => {
      const id = Number(tr.dataset.id);
      const acc = tr.querySelector(".cl-accept");
      if (acc) acc.addEventListener("click", () =>
        act("/api/suggestions/" + id + "/accept", { category: tr.querySelector(".cl-cat").value }, tr, id));
      const dis = tr.querySelector(".cl-dismiss");
      if (dis) dis.addEventListener("click", () => act("/api/suggestions/" + id + "/dismiss", null, tr, id));
      const reo = tr.querySelector(".cl-reopen");
      if (reo) reo.addEventListener("click", () => act("/api/suggestions/" + id + "/reopen", null, tr, id));
    });
  }
  function syncBulk() {
    if (!bulkBar) return;
    const n = selected.size;
    bulkBar.hidden = n === 0;
    if (!n) return;
    bulkCount.textContent = n + " selected";
    bulkActions.innerHTML = tab === "pending"
      ? '<button type="button" class="btn btn-primary btn-sm" data-bulk="accept">Accept selected</button>'
        + '<button type="button" class="btn btn-ghost btn-sm" data-bulk="dismiss">Dismiss selected</button>'
      : '<button type="button" class="btn btn-ghost btn-sm" data-bulk="reopen">Undo selected</button>';
    bulkActions.querySelectorAll("[data-bulk]").forEach((b) =>
      b.addEventListener("click", () => bulk(b.dataset.bulk)));
  }
  async function act(url, body, tr, id) {
    if (tr) tr.querySelectorAll("button").forEach((b) => (b.disabled = true));
    try {
      await apiFetch(url, body
        ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }
        : { method: "POST" });
      selected.delete(id);
      await loadTab();
      loadDirectory();  // an accept/undo may have changed the directory
    } catch (e) {
      if (tr) tr.querySelectorAll("button").forEach((b) => (b.disabled = false));
    }
  }
  async function bulk(action) {
    const ids = [...selected];
    if (!ids.length) return;
    try {
      await apiFetch("/api/suggestions/bulk", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ids, action }),
      });
      selected.clear();
      await loadTab();
      loadDirectory();
    } catch (e) { window.alert(e.message); }
  }
  async function loadTab() {
    try {
      const d = await apiFetch("/api/suggestions?status=" + tab);
      items = d.suggestions || [];
      setCounts(d.counts);
      renderInbox();
    } catch (e) {
      inboxEl.innerHTML = '<p class="cl-empty">Could not load callers.</p>';
    }
  }
  tabs.forEach((t) => t.addEventListener("click", () => {
    tabs.forEach((x) => { x.classList.remove("is-active"); x.setAttribute("aria-selected", "false"); });
    t.classList.add("is-active"); t.setAttribute("aria-selected", "true");
    tab = t.dataset.tab; selected.clear();
    loadTab();
  }));
  if (searchEl) searchEl.addEventListener("input", renderInbox);

  // ===== Screened directory (the manual rules) =====
  const listEl = document.getElementById("screen-list");
  const custEl = document.getElementById("screen-customers");
  const numEl = document.getElementById("screen-number");
  const catEl = document.getElementById("screen-category");
  const nameEl = document.getElementById("screen-name");
  const addBtn = document.getElementById("screen-add");

  function renderDirectory(d) {
    const managed = (d && d.managed) || [];
    if (!managed.length) {
      listEl.innerHTML = '<p class="screen-empty">No numbers screened yet. Add the owner’s '
        + 'personal contacts, your suppliers, or a known spammer so RingBack never texts them.</p>';
    } else {
      listEl.innerHTML = managed.map((c) =>
        `<div class="screen-row"><div class="screen-row-id">`
        + `<span class="screen-row-name">${esc(c.name || fmtPhone(c.number))}</span>`
        + `<span class="screen-row-num">${esc(fmtPhone(c.number))}</span></div>`
        + `<span class="pill ${CAT_PILL[c.category] || "pill-neutral"}">${esc(CAT_LABEL[c.category] || c.category)}</span>`
        + `<button type="button" class="btn btn-ghost btn-sm screen-remove" data-number="${esc(c.number)}" `
        + `aria-label="Remove ${esc(fmtPhone(c.number))}">Remove</button></div>`
      ).join("");
      listEl.querySelectorAll(".screen-remove").forEach((b) =>
        b.addEventListener("click", () => removeContact(b.dataset.number, b)));
    }
    const n = (d && d.customers) || 0;
    custEl.textContent = n
      ? `RingBack also recognizes ${n} past client${n === 1 ? "" : "s"} automatically, so their calls always get answered.`
      : "";
  }
  async function loadDirectory() {
    try { renderDirectory(await apiFetch("/api/contacts")); }
    catch (e) { listEl.innerHTML = '<p class="screen-empty">Could not load your screened list.</p>'; }
  }
  async function addContact() {
    const number = (numEl.value || "").trim();
    if (!number) { numEl.focus(); return; }
    addBtn.disabled = true;
    try {
      await apiFetch("/api/contacts", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ number, category: catEl.value, name: (nameEl.value || "").trim() }),
      });
      numEl.value = ""; nameEl.value = "";
      await loadDirectory();
      numEl.focus();
    } catch (e) { window.alert(e.message); } finally { addBtn.disabled = false; }
  }
  async function removeContact(number, btn) {
    if (btn) btn.disabled = true;
    try {
      await apiFetch("/api/contacts/delete", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ number }),
      });
      await loadDirectory();
    } catch (e) { if (btn) { btn.disabled = false; btn.textContent = "Try again"; } }
  }
  addBtn.addEventListener("click", addContact);
  [numEl, nameEl].forEach((el) =>
    el.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); addContact(); }
    }));

  // ===== Contact import: file upload + Google Contacts =====
  const importForm = document.getElementById("cl-import-form");
  const fileEl = document.getElementById("cl-file");
  const fileNameEl = document.getElementById("cl-file-name");
  const importBtn = document.getElementById("cl-import-btn");
  const resultEl = document.getElementById("cl-import-result");

  function showResult(msg, isError) {
    if (!resultEl) return;
    resultEl.textContent = msg;
    resultEl.classList.toggle("is-error", !!isError);
  }
  function importSummary(d) {
    const n = d.suggested || 0;
    let lead;
    if (n) {
      const bits = [];
      if (d.customers) bits.push(d.customers + " past client" + (d.customers === 1 ? "" : "s"));
      if (d.vendors) bits.push(d.vendors + (d.vendors === 1 ? " company" : " companies"));
      lead = "Added " + n + " to review" + (bits.length ? " (" + bits.join(", ") + ")" : "") + ".";
    } else {
      lead = "No new clients or companies to review.";
    }
    const tail = [];
    if (d.unclassified) tail.push(d.unclassified + " left as normal prospects");
    if (d.skipped) tail.push(d.skipped + " already sorted");
    const c = d.contacts || 0;
    return "Imported " + c + " contact" + (c === 1 ? "" : "s") + ". " + lead
      + (tail.length ? " " + tail.join("; ") + "." : "");
  }
  function selectPendingTab() {
    if (tab === "pending") return;
    const pendingTab = tabs.find((t) => t.dataset.tab === "pending");
    if (!pendingTab) return;
    tabs.forEach((x) => { x.classList.remove("is-active"); x.setAttribute("aria-selected", "false"); });
    pendingTab.classList.add("is-active"); pendingTab.setAttribute("aria-selected", "true");
    tab = "pending"; selected.clear();
  }

  if (fileEl) fileEl.addEventListener("change", () => {
    const f = fileEl.files && fileEl.files[0];
    fileNameEl.textContent = f ? f.name : "No file chosen";
  });
  if (importForm) importForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const file = fileEl.files && fileEl.files[0];
    if (!file) { fileEl.click(); return; }
    importBtn.disabled = true;
    showResult("Importing…", false);
    try {
      const fd = new FormData();
      fd.append("file", file);
      const d = await apiFetch("/api/contacts/import", { method: "POST", body: fd });
      showResult(importSummary(d), false);
      fileEl.value = ""; fileNameEl.textContent = "No file chosen";
      selectPendingTab();
      await loadTab();
      loadDirectory();
    } catch (err) {
      showResult(err.message, true);
    } finally {
      importBtn.disabled = false;
    }
  });

  const gcSync = document.getElementById("cl-gc-sync");
  const gcDisconnect = document.getElementById("cl-gc-disconnect");
  async function runGoogleSync() {
    if (gcSync) gcSync.disabled = true;
    showResult("Syncing from Google…", false);
    try {
      const d = await apiFetch("/api/contacts/google/sync", { method: "POST" });
      showResult(importSummary(d), false);
      selectPendingTab();
      await loadTab();
      loadDirectory();
    } catch (err) {
      showResult(err.message, true);
    } finally {
      if (gcSync) gcSync.disabled = false;
    }
  }
  if (gcSync) gcSync.addEventListener("click", runGoogleSync);
  if (gcDisconnect) gcDisconnect.addEventListener("click", async () => {
    gcDisconnect.disabled = true;
    try {
      await apiFetch("/api/contacts/google/disconnect", { method: "POST" });
      window.location = "/callers";
    } catch (err) { gcDisconnect.disabled = false; showResult(err.message, true); }
  });

  // On return from the Google OAuth flow: auto-run a first sync (or surface an
  // error), then strip the query so a refresh doesn't repeat it.
  (function handleGoogleReturn() {
    const p = new URLSearchParams(window.location.search);
    if (p.get("gcsync")) {
      history.replaceState(null, "", "/callers");
      runGoogleSync();
    } else if (p.get("gcerror")) {
      history.replaceState(null, "", "/callers");
      const m = {
        unconfigured: "Google Contacts isn’t set up yet.",
        denied: "Google sign-in was canceled.",
        state: "That Google sign-in expired. Please try again.",
        exchange: "Could not connect to Google. Please try again.",
      };
      showResult(m[p.get("gcerror")] || "Google Contacts connection failed.", true);
    }
  })();

  loadTab();
  loadDirectory();
})();

// ---------- Dashboard: re-engage a screened caller ----------
(function () {
  const buttons = document.querySelectorAll(".screen-engage[data-id]");
  if (!buttons.length) return;
  buttons.forEach((btn) => {
    btn.addEventListener("click", async () => {
      const num = btn.dataset.num || "this caller";
      if (!window.confirm("Text " + num + " back? They’ll be treated as a normal lead from now on.")) return;
      btn.disabled = true;
      btn.textContent = "Texting…";
      try {
        await apiFetch("/api/calls/" + btn.dataset.id + "/engage", { method: "POST" });
        window.location.reload();
      } catch (err) {
        btn.disabled = false;
        btn.textContent = "Try again";
      }
    });
  });
})();

// ---------- Dashboard: confirm a caller is spam (blocks + feeds the cross-tenant ledger) ----------
(function () {
  const buttons = document.querySelectorAll(".screen-spam[data-id]");
  if (!buttons.length) return;
  buttons.forEach((btn) => {
    btn.addEventListener("click", async () => {
      const num = btn.dataset.num || "this caller";
      if (!window.confirm("Mark " + num + " as spam? They won’t be texted again, and it helps screen this number for other businesses.")) return;
      btn.disabled = true;
      btn.textContent = "Marking…";
      try {
        await apiFetch("/api/calls/" + btn.dataset.id + "/flag-spam", { method: "POST" });
        window.location.reload();
      } catch (err) {
        btn.disabled = false;
        btn.textContent = "Try again";
      }
    });
  });
})();
