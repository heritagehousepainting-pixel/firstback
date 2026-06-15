/* ============================================================================
   RingBack — motion.js  (Firecrawl-inspired)
   1) Scroll reveals: fade-up + stagger as elements enter the viewport.
   2) Nav mega-menu dropdowns: hover + click + keyboard, with a page scrim.
   Honors prefers-reduced-motion (no reveals, instant menus).
   Loaded on marketing pages AND the signed-in app shell.
   ============================================================================ */
(function () {
  "use strict";
  var RM = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  /* ---------------- Nav dropdowns ---------------- */
  function initDropdowns() {
    var dds = document.querySelectorAll(".ob-dd");
    if (!dds.length) return;
    var body = document.body;
    var scrim = document.querySelector(".ob-scrim");
    if (!scrim) {
      scrim = document.createElement("div");
      scrim.className = "ob-scrim";
      body.appendChild(scrim);
    }
    var openEl = null, hoverTimer = null;

    function open(dd) {
      if (openEl && openEl !== dd) close(openEl);
      openEl = dd;
      dd.classList.add("open");
      dd.querySelector(".ob-ddtrigger").setAttribute("aria-expanded", "true");
      body.classList.add("dd-open");
    }
    function close(dd) {
      dd.classList.remove("open");
      var t = dd.querySelector(".ob-ddtrigger");
      if (t) t.setAttribute("aria-expanded", "false");
      if (openEl === dd) openEl = null;
      if (!document.querySelector(".ob-dd.open")) body.classList.remove("dd-open");
    }

    dds.forEach(function (dd) {
      var trigger = dd.querySelector(".ob-ddtrigger");
      if (!trigger) return;
      // click toggles (and is the touch / keyboard path)
      trigger.addEventListener("click", function (e) {
        e.preventDefault();
        dd.classList.contains("open") ? close(dd) : open(dd);
      });
      // hover (desktop) — small delay so it doesn't flicker
      dd.addEventListener("mouseenter", function () {
        if (window.innerWidth <= 820) return;
        clearTimeout(hoverTimer); open(dd);
      });
      dd.addEventListener("mouseleave", function () {
        if (window.innerWidth <= 820) return;
        clearTimeout(hoverTimer);
        hoverTimer = setTimeout(function () { close(dd); }, 120);
      });
    });

    scrim.addEventListener("click", function () { if (openEl) close(openEl); });
    document.addEventListener("keydown", function (e) { if (e.key === "Escape" && openEl) close(openEl); });
    document.addEventListener("click", function (e) {
      if (openEl && !openEl.contains(e.target)) close(openEl);
    });
  }

  /* ---------------- Scroll reveals ---------------- */
  function initReveals() {
    if (RM || !("IntersectionObserver" in window)) return;
    var SEL = [
      ".mk-section-head", ".mk-card", ".mk-row", ".mk-step", ".mk-price",
      ".mk-stat", ".mk-story", ".mk-webinar", ".mk-trade", ".mk-faq details",
      ".page-body .card", ".page-body .stat-row", ".page-body .empty"
    ].join(",");
    var targets = Array.prototype.slice.call(document.querySelectorAll(SEL));
    if (!targets.length) return;
    var vh = window.innerHeight || document.documentElement.clientHeight;

    targets.forEach(function (el) {
      var r = el.getBoundingClientRect();
      // Already in (or near) view on load → leave visible, no flash.
      if (r.top < vh * 0.92) return;
      // stagger by position within the parent (grid children animate in sequence)
      var idx = 0, sib = el;
      while ((sib = sib.previousElementSibling)) { if (sib.matches && sib.matches(SEL)) idx++; }
      el.dataset.delay = Math.min(idx, 6) * 0.06;
      el.style.opacity = "0";
      el.style.transform = "translateY(18px)";
      el.style.willChange = "opacity, transform";
      el.classList.add("reveal-pending");
    });

    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) {
        if (!e.isIntersecting) return;
        var el = e.target, d = el.dataset.delay || 0;
        el.style.transition = "opacity .6s cubic-bezier(.16,.84,.44,1) " + d + "s, transform .6s cubic-bezier(.16,.84,.44,1) " + d + "s";
        el.style.opacity = "1";
        el.style.transform = "none";
        el.addEventListener("transitionend", function () { el.style.willChange = "auto"; }, { once: true });
        io.unobserve(el);
      });
    }, { threshold: 0.12, rootMargin: "0px 0px -7% 0px" });

    document.querySelectorAll(".reveal-pending").forEach(function (el) { io.observe(el); });
  }

  /* ---------------- Count-up on numbers ---------------- */
  function animateCount(el) {
    var raw = el.textContent.trim();
    var m = raw.match(/^(\D*)([\d,]+(?:\.\d+)?)(.*)$/);
    if (!m) return;
    var pre = m[1], numStr = m[2], suf = m[3];
    var hasComma = numStr.indexOf(",") > -1;
    var decimals = (numStr.split(".")[1] || "").length;
    var target = parseFloat(numStr.replace(/,/g, ""));
    if (!isFinite(target)) return;
    function fmt(v) {
      var s = decimals ? v.toFixed(decimals) : String(Math.round(v));
      if (hasComma) s = Number(s).toLocaleString("en-US", decimals ? { minimumFractionDigits: decimals } : {});
      return pre + s + suf;
    }
    var dur = 1100, start = null;
    el.textContent = fmt(0);
    function step(ts) {
      if (!start) start = ts;
      var p = Math.min((ts - start) / dur, 1);
      el.textContent = fmt(target * (1 - Math.pow(1 - p, 3)));
      if (p < 1) requestAnimationFrame(step);
      else el.textContent = pre + numStr + suf;   // restore exact original formatting
    }
    requestAnimationFrame(step);
  }
  function initCounters() {
    if (RM || !("IntersectionObserver" in window)) return;
    var els = document.querySelectorAll(".stat-value, .mk-stat .big, [data-count]");
    if (!els.length) return;
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) {
        if (!e.isIntersecting) return;
        animateCount(e.target);
        io.unobserve(e.target);
      });
    }, { threshold: 0.5 });
    els.forEach(function (el) { io.observe(el); });
  }

  /* ---------------- Sticky nav shadow on scroll ---------------- */
  function initStickyNav() {
    var nav = document.querySelector(".mk-nav");
    if (!nav) return;
    var onScroll = function () { nav.classList.toggle("scrolled", window.scrollY > 8); };
    onScroll();
    window.addEventListener("scroll", onScroll, { passive: true });
  }

  function init() { initDropdowns(); initReveals(); initCounters(); initStickyNav(); }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
