/* FirstBack web-chat "Text us" widget (Plan 10-2).
 * One-line embed on any contractor site:
 *   <script src="https://firstback.app/widget.js?slug=abc123" defer></script>
 * Reads its own slug + the FirstBack origin from this <script> tag, injects a floating
 * "Text us" bubble, collects a phone (E.164-validated server-side) with a TCPA consent
 * line, and POSTs to /webhooks/widget/lead. Inert if the slug's widget isn't enabled.
 */
(function () {
  "use strict";
  var me = document.currentScript;
  if (!me) {
    var all = document.getElementsByTagName("script");
    me = all[all.length - 1];
  }
  if (!me || !me.src) return;
  var slug = (me.src.match(/[?&]slug=([^&#]+)/) || [])[1];
  if (!slug) return;
  slug = decodeURIComponent(slug);
  var origin = "";
  try { origin = new URL(me.src).origin; } catch (e) { return; }
  var ENDPOINT = origin + "/webhooks/widget/lead";
  var bizName = "us";

  // Best-effort config fetch (sets the business name; never blocks the bubble).
  var cs = document.createElement("script");
  cs.src = origin + "/api/widget/" + encodeURIComponent(slug) + "/config.js";
  cs.onload = function () {
    if (window.__fb && window.__fb.biz) { bizName = window.__fb.biz; relabel(); }
  };
  document.head.appendChild(cs);

  var css = [
    ".fb-w{position:fixed;right:18px;bottom:18px;z-index:2147483000;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif}",
    ".fb-w-btn{display:flex;align-items:center;gap:8px;border:0;cursor:pointer;background:#2563eb;color:#fff;font-weight:600;font-size:15px;padding:12px 18px;border-radius:999px;box-shadow:0 6px 20px rgba(0,0,0,.18)}",
    ".fb-w-panel{display:none;width:300px;max-width:86vw;background:#fff;color:#0f172a;border-radius:14px;box-shadow:0 12px 40px rgba(0,0,0,.22);padding:16px;position:absolute;right:0;bottom:58px}",
    ".fb-w.open .fb-w-panel{display:block}",
    ".fb-w-h{font-weight:700;font-size:16px;margin:0 0 4px}",
    ".fb-w-sub{font-size:13px;color:#475569;margin:0 0 12px}",
    ".fb-w input{width:100%;box-sizing:border-box;padding:10px 12px;border:1px solid #cbd5e1;border-radius:9px;font-size:14px;margin-bottom:8px}",
    ".fb-w-send{width:100%;border:0;cursor:pointer;background:#2563eb;color:#fff;font-weight:600;font-size:15px;padding:11px;border-radius:9px}",
    ".fb-w-send:disabled{opacity:.6;cursor:default}",
    ".fb-w-consent{font-size:11px;color:#64748b;margin:8px 0 0;line-height:1.4}",
    ".fb-w-msg{font-size:14px;color:#0f172a;text-align:center;padding:6px 0}",
    ".fb-w-x{position:absolute;top:10px;right:12px;border:0;background:none;font-size:18px;color:#94a3b8;cursor:pointer;line-height:1}"
  ].join("");
  var st = document.createElement("style");
  st.textContent = css;
  document.head.appendChild(st);

  var root = document.createElement("div");
  root.className = "fb-w";
  root.innerHTML =
    '<div class="fb-w-panel" role="dialog" aria-label="Text us">' +
      '<button class="fb-w-x" aria-label="Close">&times;</button>' +
      '<div class="fb-w-body">' +
        '<p class="fb-w-h">Text us</p>' +
        '<p class="fb-w-sub">Leave your number and we’ll text you right back.</p>' +
        '<input class="fb-w-name" type="text" placeholder="Your name (optional)" autocomplete="name">' +
        '<input class="fb-w-phone" type="tel" placeholder="Your phone number" autocomplete="tel" inputmode="tel">' +
        '<button class="fb-w-send" type="button">Send</button>' +
        '<p class="fb-w-consent">By submitting, you agree to receive texts from <span class="fb-w-biz">us</span>. Reply STOP to opt out.</p>' +
      '</div>' +
    '</div>' +
    '<button class="fb-w-btn" type="button" aria-label="Text us">' +
      '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 11.5a8.38 8.38 0 0 1-8.5 8.5 8.5 8.5 0 0 1-3.8-.9L3 21l1.9-5.7A8.38 8.38 0 0 1 4 11.5 8.5 8.5 0 0 1 12.5 3 8.38 8.38 0 0 1 21 11.5z"/></svg>' +
      "Text us</button>";
  document.body.appendChild(root);

  var panel = root.querySelector(".fb-w-panel");
  var nameEl = root.querySelector(".fb-w-name");
  var phoneEl = root.querySelector(".fb-w-phone");
  var sendEl = root.querySelector(".fb-w-send");
  var bodyEl = root.querySelector(".fb-w-body");

  function relabel() {
    var b = root.querySelector(".fb-w-biz");
    if (b && bizName) b.textContent = bizName;
  }
  function toggle(open) {
    root.classList.toggle("open", open);
    if (open) phoneEl.focus();
  }
  root.querySelector(".fb-w-btn").addEventListener("click", function () { toggle(!root.classList.contains("open")); });
  root.querySelector(".fb-w-x").addEventListener("click", function () { toggle(false); });

  sendEl.addEventListener("click", function () {
    var phone = (phoneEl.value || "").trim();
    if (phone.replace(/\D/g, "").length < 10) { phoneEl.focus(); return; }
    sendEl.disabled = true;
    sendEl.textContent = "Sending…";
    fetch(ENDPOINT, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ slug: slug, phone: phone, name: (nameEl.value || "").trim() })
    }).then(function (r) { return r.ok ? r.json() : Promise.reject(r); })
      .then(function () { bodyEl.innerHTML = '<p class="fb-w-msg">Got it — we’ll text you right back!</p>'; })
      .catch(function () {
        sendEl.disabled = false;
        sendEl.textContent = "Try again";
      });
  });
})();
