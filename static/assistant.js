/* =========================================================================
   Command Center -- the Jarvis core (WebGL orb) + the chat controller.
   Self-contained, no dependencies. Degrades to a static CSS orb when WebGL
   is unavailable or motion is reduced, and the page stays fully usable.
   ========================================================================= */
(function () {
  "use strict";
  var shell = document.getElementById("command");
  if (!shell) return;

  var REDUCED = false;
  try { REDUCED = matchMedia("(prefers-reduced-motion: reduce)").matches; } catch (e) {}
  // Touch/field devices: never auto-grab focus (it pops the keyboard over the briefing the
  // owner is trying to read between jobs). Focus is restored after a turn on desktop only.
  var TOUCH = false;
  try { TOUCH = matchMedia("(pointer: coarse)").matches; } catch (e) {}

  /* The orb's hue is the app's own --accent token, so one file matches every brand. */
  function brandColor() {
    var fallback = [0.06, 0.72, 0.5];
    try {
      var raw = getComputedStyle(shell).getPropertyValue("--accent").trim();
      var m = raw.match(/^#([0-9a-f]{6})$/i);
      if (m) {
        var n = parseInt(m[1], 16);
        return [(n >> 16 & 255) / 255, (n >> 8 & 255) / 255, (n & 255) / 255];
      }
      m = raw.match(/rgba?\(([^)]+)\)/);
      if (m) {
        var p = m[1].split(",").map(function (x) { return parseFloat(x); });
        return [p[0] / 255, p[1] / 255, p[2] / 255];
      }
    } catch (e) {}
    return fallback;
  }

  /* ----------------------------------------------------------------------
     1. THE ORB  -- a fragment-shader energy sphere that reacts to state.
        state: 0 idle (slow breath), 1 thinking (fast churn), 2 responding
        (a brief settle as a reply lands). "Responding" is honest: there is
        no audio -- the orb never pretends to speak. It is gated off (static
        glow) for reduced-motion, Save-Data, and a low/unplugged battery.
     ---------------------------------------------------------------------- */
  var Orb = (function () {
    var canvas = document.getElementById("orb");
    var gl = null, prog = null, buf = null, raf = 0;
    var uTime, uRes, uState, uPulse, uCol;
    var state = 0, pulse = 0, start = performance.now(), running = false;
    var col = brandColor();   // [r,g,b] 0..1 from the app's --accent token

    var VERT = "attribute vec2 p;void main(){gl_Position=vec4(p,0.,1.);}";
    var FRAG = [
      "precision highp float;",
      "uniform vec2 u_res;uniform float u_time;uniform float u_state;uniform float u_pulse;uniform vec3 u_col;",
      "float hash(vec3 q){return fract(sin(dot(q,vec3(27.17,61.7,113.))) *43758.5453);}",
      "float noise(vec3 x){vec3 i=floor(x),f=fract(x);f=f*f*(3.-2.*f);",
      "  float n=mix(mix(mix(hash(i+vec3(0,0,0)),hash(i+vec3(1,0,0)),f.x),",
      "    mix(hash(i+vec3(0,1,0)),hash(i+vec3(1,1,0)),f.x),f.y),",
      "    mix(mix(hash(i+vec3(0,0,1)),hash(i+vec3(1,0,1)),f.x),",
      "    mix(hash(i+vec3(0,1,1)),hash(i+vec3(1,1,1)),f.x),f.y),f.z);return n;}",
      "float fbm(vec3 x){float v=0.,a=.5;for(int i=0;i<5;i++){v+=a*noise(x);x*=2.03;a*=.5;}return v;}",
      // ridged noise -> thin bright filaments, the electric arcs of a plasma ball
      "float arc(vec3 p){return pow(1.0-abs(fbm(p)*2.0-1.0),3.5);}",
      "void main(){",
      "  vec2 uv=(gl_FragCoord.xy-.5*u_res)/min(u_res.x,u_res.y);",
      "  float d=length(uv);",
      "  float ang=atan(uv.y,uv.x);",
      "  float act=clamp(u_state*0.7+u_pulse,0.0,1.0);",   // 0 idle .. 1 active (on send)
      "  float bright=mix(0.5,1.18,act);",                 // dim at rest, flares on send
      "  float t=u_time*(0.25+0.7*act);",                  // and moves faster when active
      // warped radial noise -> jagged, branching lightning bolts from the core
      "  vec3 p=vec3(ang*1.7, d*3.4-t*1.1, t*0.5);",
      "  p+=fbm(p)*0.9;",                                  // domain warp = wandering + forks
      "  float n=fbm(p*1.8);",
      "  float bolt=0.016/(abs(n-0.5)+0.016);",            // thin glowing filaments at n=0.5
      "  vec3 p2=vec3(ang*3.1, d*6.5+t*0.8, t*0.7);",
      "  p2+=fbm(p2)*0.7;",
      "  float n2=fbm(p2);",
      "  bolt+=(0.010/(abs(n2-0.5)+0.013))*0.6;",          // finer secondary branches
      "  bolt*=smoothstep(0.64,0.03,d);",                  // emanate from core, fade outward
      "  float core=smoothstep(0.13,0.0,d);",              // white-hot core flash
      "  float halo=smoothstep(0.5,0.0,d)*0.22;",          // faint inner glow
      "  float energy=(bolt*0.6+core*1.7+halo)*bright;",
      "  vec3 hot=mix(u_col,vec3(1.0),0.92);",
      "  vec3 col=u_col*energy + hot*(core*1.5+pow(max(bolt-0.5,0.0),1.4)*0.7)*bright;",
      // gaps between bolts stay translucent, so the page (and any text) reads through
      "  float alpha=clamp(bolt*0.7+core+halo*0.6,0.0,1.0)*mix(0.55,1.0,act);",
      "  col=col/(col+vec3(0.9));",                        // gentle tonemap
      "  col=pow(col,vec3(.95));",
      "  gl_FragColor=vec4(col,alpha);",
      "}"
    ].join("\n");

    function compile(type, src) {
      var s = gl.createShader(type); gl.shaderSource(s, src); gl.compileShader(s);
      if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) { return null; }
      return s;
    }
    function init() {
      if (!canvas) return false;
      try { gl = canvas.getContext("webgl", { alpha: true, premultipliedAlpha: false, antialias: true }); }
      catch (e) { gl = null; }
      if (!gl) return false;
      var vs = compile(gl.VERTEX_SHADER, VERT), fs = compile(gl.FRAGMENT_SHADER, FRAG);
      if (!vs || !fs) return false;
      prog = gl.createProgram(); gl.attachShader(prog, vs); gl.attachShader(prog, fs);
      gl.linkProgram(prog);
      if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) return false;
      gl.useProgram(prog);
      buf = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, buf);
      gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW);
      var loc = gl.getAttribLocation(prog, "p");
      gl.enableVertexAttribArray(loc); gl.vertexAttribPointer(loc, 2, gl.FLOAT, false, 0, 0);
      gl.enable(gl.BLEND); gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);
      uTime = gl.getUniformLocation(prog, "u_time");
      uRes = gl.getUniformLocation(prog, "u_res");
      uState = gl.getUniformLocation(prog, "u_state");
      uPulse = gl.getUniformLocation(prog, "u_pulse");
      uCol = gl.getUniformLocation(prog, "u_col");
      resize();
      return true;
    }
    function resize() {
      if (!gl) return;
      var dpr = Math.min(window.devicePixelRatio || 1, 2);
      var w = canvas.clientWidth || 360, h = canvas.clientHeight || 360;
      canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr);
      gl.viewport(0, 0, canvas.width, canvas.height);
    }
    function frame() {
      if (!running) return;
      var time = (performance.now() - start) / 1000;
      pulse += ((state === 2 ? 1 : 0) - pulse) * 0.08;       // ease toward target
      var sval = state === 0 ? 0.0 : (state === 1 ? 1.0 : 0.5);
      gl.uniform1f(uTime, time);
      gl.uniform2f(uRes, canvas.width, canvas.height);
      gl.uniform1f(uState, sval);
      gl.uniform1f(uPulse, Math.max(pulse, 0));
      gl.uniform3f(uCol, col[0], col[1], col[2]);
      gl.drawArrays(gl.TRIANGLES, 0, 3);
      raf = requestAnimationFrame(frame);
    }
    function teardown() {
      running = false; cancelAnimationFrame(raf); shell.classList.add("no-webgl");
    }
    /* Save-Data is an explicit "don't burn my data/battery" signal -> static glow. */
    function saveData() {
      try { return !!(navigator.connection && navigator.connection.saveData); }
      catch (e) { return false; }
    }
    /* Drop to the static orb when the battery is low and unplugged (and keep watching, so a
       charge that drains mid-session also retires the WebGL loop). Honest power citizenship. */
    function watchBattery() {
      if (!navigator.getBattery) return;
      navigator.getBattery().then(function (b) {
        function check() { if (b.level <= 0.2 && !b.charging) teardown(); }
        check();
        b.addEventListener("levelchange", check);
        b.addEventListener("chargingchange", check);
      }).catch(function () {});
    }
    return {
      mount: function () {
        if (REDUCED || saveData() || !init()) { shell.classList.add("no-webgl"); return; }
        running = true; frame();
        watchBattery();
        window.addEventListener("resize", resize);
        document.addEventListener("visibilitychange", function () {
          if (document.hidden) { running = false; cancelAnimationFrame(raf); }
          else if (gl && !shell.classList.contains("no-webgl")) {
            running = true; start = performance.now(); frame();
          }
        });
      },
      set: function (s) { state = s; }
    };
  })();

  /* ----------------------------------------------------------------------
     2. THE CHAT CONTROLLER
     ---------------------------------------------------------------------- */
  var transcript = document.getElementById("transcript");
  var form = document.getElementById("commandBar");
  var input = document.getElementById("commandInput");
  var send = document.getElementById("commandSend");
  var chipsEl = document.getElementById("chips");
  var csrf = (document.getElementById("csrfToken") || {}).value || "";
  // A per-page-load key so the back-and-forth groups into one saved conversation.
  var convoKey = (Date.now().toString(36) + Math.random().toString(36).slice(2, 8));
  // A durable key kept in localStorage: it survives reloads, so "text her back" still
  // resolves to who you were just looking at after a refresh. Falls back to the page key.
  var browserKey = (function () {
    try {
      var k = localStorage.getItem("rb_convo_key");
      if (!k) {
        k = Date.now().toString(36) + Math.random().toString(36).slice(2, 10);
        localStorage.setItem("rb_convo_key", k);
      }
      return k;
    } catch (e) { return convoKey; }
  })();
  var history = [];
  var busy = false;

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }
  function scrollDown() { transcript.scrollTop = transcript.scrollHeight; }

  function addTurn(role, text) {
    var turn = el("div", "turn " + role);
    var av = el("div", "turn-avatar", role === "user" ? "Y" : "V");   // V = Vic, the AI employee
    var body = el("div", "turn-body");
    var bub = el("div", "bubble");
    bub.textContent = text;
    body.appendChild(bub);
    turn.appendChild(av); turn.appendChild(body);
    transcript.appendChild(turn);
    scrollDown();
    return body;
  }

  function addThinking() {
    var turn = el("div", "turn agent");
    turn.dataset.thinking = "1";
    turn.appendChild(el("div", "turn-avatar", "V"));
    var body = el("div", "turn-body");
    var bub = el("div", "bubble thinking");
    bub.setAttribute("aria-hidden", "true");      // the dots are decorative
    bub.appendChild(el("i")); bub.appendChild(el("i")); bub.appendChild(el("i"));
    // Announced ONCE by the transcript's aria-live (no nested live region / double-speak).
    body.appendChild(el("span", "sr-only", "Working on it"));
    body.appendChild(bub); turn.appendChild(body);
    transcript.appendChild(turn); scrollDown();
    return turn;
  }

  /* ---- card renderers ---- */
  function btn(label, cls, onClick) {
    var b = el("button", "a-btn " + (cls || ""), label);
    b.type = "button"; b.addEventListener("click", onClick); return b;
  }
  function renderCard(card, host) {
    var c = el("div", "a-card");
    if (card.type === "stat") {
      if (card.title) c.appendChild(el("div", "a-card-title", card.title));
      var grid = el("div", "stat-grid");
      (card.groups || []).forEach(function (g) {
        var cell = el("div", "stat-cell");
        cell.appendChild(el("div", "stat-v", String(g.value)));
        cell.appendChild(el("div", "stat-k", g.label));
        if (g.sub) cell.appendChild(el("div", "stat-s", g.sub));
        grid.appendChild(cell);
      });
      c.appendChild(grid);
    } else if (card.type === "list" || card.type === "plays") {
      if (card.title) c.appendChild(el("div", "a-card-title", card.title));
      var ul = el("ul", "a-list");
      (card.items || []).forEach(function (it) {
        var li = el("li");
        li.appendChild(el("span", "li-t", it.title || it.label || ""));
        if (it.sub || it.blurb) li.appendChild(el("span", "li-s", it.sub || it.blurb));
        if (it.recommended) li.appendChild(el("span", "li-rec",
          it.recommended.replace("_", " ")));
        ul.appendChild(li);
      });
      c.appendChild(ul);
    } else if (card.type === "draft") {
      if (card.title) c.appendChild(el("div", "a-card-title", card.title));
      c.appendChild(el("div", "draft-body", card.body || ""));
      var meta = el("div", "draft-meta");
      meta.appendChild(el("span", "draft-note", card.note || ""));
      c.appendChild(meta);
      var acts = el("div", "a-actions");
      acts.appendChild(btn("Publish it", "primary", function () {
        runAction({ tool: "publish_post", args: { post_id: card.post_id } });
      }));
      var open = el("a", "a-btn ghost", "Open in Queue"); open.href = "/queue";
      acts.appendChild(open);
      c.appendChild(acts);
    } else if (card.type === "link") {
      c.classList.add("a-link");
      if (card.title) c.appendChild(el("div", "a-card-title", card.title));
      if (card.note) c.appendChild(el("p", "al-note", card.note));
      var a = el("a", "a-btn primary", card.label || "Open"); a.href = card.href || "#";
      c.appendChild(a);
    } else if (card.type === "note") {
      c.classList.add("a-note"); if (card.tone) c.classList.add(card.tone);
      c.textContent = card.body || "";
    } else if (card.type === "golive") {
      c.classList.add("a-golive");
      /* header: "Getting you live" + an honest status pill. The pill never says
         "You're live" unless the backend reported status === "live" (verified). */
      var head = el("div", "ag-head");
      head.appendChild(el("div", "a-card-title", "Getting you live"));
      var PILL = { not_live: ["pill-neutral", "Not live yet"],
                   setup_complete: ["pill-warning", "Make a test call"],
                   live: ["pill-booked", "You're live"] };
      var pm = PILL[card.status] || PILL.not_live;
      var pill = el("span", "pill " + pm[0]);
      pill.appendChild(el("span", "pill-dot"));
      pill.appendChild(document.createTextNode(pm[1]));
      head.appendChild(pill);
      c.appendChild(head);

      /* mini-stepper: state shown by shape (dot fill) + a visually-hidden word, never
         color alone. The active node carries aria-current; glyph dots are aria-hidden. */
      var STATE_WORD = { done: "done", current: "in progress", ready: "ready", todo: "to do" };
      var steps = card.steps || [];
      var nav = el("ol", "ag-steps");
      nav.setAttribute("aria-label", (card.done || 0) + " of " + (card.total || steps.length) + " steps complete");
      steps.forEach(function (s) {
        var li = el("li", "ag-step is-" + (s.state || "todo"));
        if (s.state === "current") li.setAttribute("aria-current", "step");
        var dot = el("span", "ag-dot"); dot.setAttribute("aria-hidden", "true");
        li.appendChild(dot);
        li.appendChild(el("span", "ag-step-title", s.title || ""));
        li.appendChild(el("span", "sr-only", " — " + (STATE_WORD[s.state] || "to do")));
        nav.appendChild(li);
      });
      c.appendChild(nav);
      c.appendChild(el("div", "ag-count", (card.done || 0) + " of " + (card.total || steps.length)));

      /* blocker line — omitted entirely when there's nothing blocking (no fake reassurance). */
      if (card.blocker) {
        var bl = el("div", "ag-blocker");
        bl.appendChild(el("span", "ag-blocker-k", "Next: "));
        bl.appendChild(el("span", "ag-blocker-v", card.blocker));
        c.appendChild(bl);
      }

      var gacts = el("div", "a-actions");
      var ga = el("a", "a-btn primary", card.label || "Open Go Live");
      ga.href = card.href || "/setup";
      gacts.appendChild(ga);
      c.appendChild(gacts);
    } else if (card.type === "briefing") {
      /* The morning briefing rendered in-chat ("what should I focus on?"). Uses the same
         appendBriefing as the server-rendered hero block, so the two never drift. */
      appendBriefing(c, card);
    } else {
      c.classList.add("a-note"); c.textContent = card.body || "";
    }
    host.appendChild(c);
  }

  /* The briefing body (headline + sub + tap-action rows), shared by the in-chat card and
     the server/poll-rendered hero block so the two never drift. Each row is a one-tap
     action; an sr-only status word keeps tone from being conveyed by color alone. */
  function appendBriefing(c, card) {
    if (card.headline) c.appendChild(el("p", "briefing-headline", card.headline));
    if (card.sub) c.appendChild(el("p", "briefing-sub", card.sub));
    var bl = el("ul", "briefing-list");
    (card.items || []).forEach(function (it) {
      var li = el("li", "briefing-item is-" + (it.tone || "new"));
      var row = el("button", "briefing-row"); row.type = "button";
      if (it.label) row.appendChild(el("span", "sr-only", it.label + ": "));
      var dot = el("span", "briefing-dot"); dot.setAttribute("aria-hidden", "true");
      row.appendChild(dot);
      var tx = el("span", "briefing-text");
      tx.appendChild(el("span", "briefing-t", it.title || ""));
      if (it.sub) tx.appendChild(el("span", "briefing-s", it.sub));
      row.appendChild(tx);
      var go = el("span", "briefing-go"); go.setAttribute("aria-hidden", "true");
      go.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m9 18 6-6-6-6"/></svg>';
      row.appendChild(go);
      if (it.action) {                         // one tap runs the action through the chat
        row.dataset.action = it.action;
        row.addEventListener("click", function () { submit(row.dataset.action); });
      } else {
        row.disabled = true;
      }
      li.appendChild(row);
      bl.appendChild(li);
    });
    c.appendChild(bl);
  }
  function buildBriefingNode(card) {
    if (!card || !(card.items || []).length) return null;
    var region = el("div", "briefing");
    region.setAttribute("role", "region");
    region.setAttribute("aria-label", "Your briefing");
    appendBriefing(region, card);
    return region;
  }

  function renderCards(cards, body) {
    if (!cards || !cards.length) return;
    var wrap = el("div", "cards");
    cards.forEach(function (card) { renderCard(card, wrap); });
    body.appendChild(wrap); scrollDown();
  }

  /* ---- confirm flow (gated actions) ----
     When the server sends a preview (a text to a real customer), show exactly WHO it
     goes to, the verbatim body (editable -- SMS can't be recalled, so we edit before we
     send, never undo after), and an honest live/test/opt-out badge. No blind sends. */
  var CONFIRM_MODE = {
    live:       ["pill-booked",  "Will send for real"],
    simulated:  ["pill-neutral", "Test mode — not sent for real yet"],
    blocked:    ["pill-warning", "Held — carrier registration not approved yet"],
    suppressed: ["pill-warning", "This contact opted out — nothing will send"],
    skipped:    ["pill-warning", "No number on file — nothing will send"]
  };
  function renderConfirm(pending, body) {
    var c = el("div", "a-card confirm-card");
    c.appendChild(el("div", "cc-q", pending.summary || "Run this action?"));
    var preview = pending.preview, bodyField = null, suppressed = false;
    if (preview) {
      suppressed = (preview.mode === "suppressed" || preview.mode === "skipped");
      var pv = el("div", "cc-preview");
      var who = el("div", "cc-to");
      who.appendChild(el("span", "cc-to-k", "To"));
      var nm = (preview.recipient_name || "").trim();
      who.appendChild(el("span", "cc-to-v",
        (nm ? nm + " · " : "") + (preview.recipient_phone || "unknown number")));
      pv.appendChild(who);
      bodyField = el("textarea", "cc-body");
      bodyField.value = preview.body || "";
      bodyField.rows = 3;
      bodyField.setAttribute("aria-label", "Message to send — edit before sending");
      pv.appendChild(bodyField);
      var m = CONFIRM_MODE[preview.mode] || CONFIRM_MODE.simulated;
      var badge = el("span", "pill " + m[0] + " cc-mode");
      badge.appendChild(el("span", "pill-dot"));
      badge.appendChild(document.createTextNode(m[1]));
      pv.appendChild(badge);
      c.appendChild(pv);
    }
    var acts = el("div", "cc-actions");
    var label = preview ? (suppressed ? "Can't send" : "Send") : "Confirm";
    var confirmBtn = btn(label, "primary", function () {
      if (bodyField) pending.args.message = bodyField.value;   // honor any edit
      c.parentNode && c.parentNode.removeChild(c);             // retire the prompt
      runAction(pending);
    });
    if (suppressed) confirmBtn.disabled = true;                // opted out -> can't send
    acts.appendChild(confirmBtn);
    acts.appendChild(btn("Cancel", "ghost", function () {
      acts.innerHTML = ""; acts.appendChild(el("span", "draft-note", "Cancelled."));
    }));
    c.appendChild(acts);
    var wrap = el("div", "cards"); wrap.appendChild(c); body.appendChild(wrap); scrollDown();
    try { confirmBtn.focus(); } catch (e) {}
  }

  /* ---- proactive teaching offer (Vic offers to remember a recurring gap) ---- */
  function renderCoach(coach, body) {
    var c = el("div", "a-card confirm-card");
    c.appendChild(el("div", "cc-q", coach.prompt || "Want me to remember that?"));
    var acts = el("div", "cc-actions");
    acts.appendChild(btn("Yes, learn that", "primary", function () {
      acts.innerHTML = "";
      post("/assistant/learn", { pattern: coach.pattern, action: coach.action || "route",
        value: coach.value || "" })
        .then(function () {
          c.appendChild(el("div", "a-note ok", "Done. I will take you straight there from now on."));
        })
        .catch(function () { c.appendChild(el("div", "a-note warn", "Could not save that just now.")); });
    }));
    acts.appendChild(btn("Not now", "ghost", function () {
      acts.innerHTML = ""; acts.appendChild(el("span", "draft-note", "No problem."));
    }));
    c.appendChild(acts);
    var wrap = el("div", "cards"); wrap.appendChild(c); body.appendChild(wrap); scrollDown();
  }

  /* Run a confirmed/gated action and append the result as a fresh agent turn. */
  function runAction(pending) {
    if (busy) return;
    busy = true; send.disabled = true; Orb.set(1);
    var thinking = addThinking();
    post("/assistant/confirm", { tool: pending.tool, args: JSON.stringify(pending.args || {}) })
      .then(function (res) {
        thinking.parentNode && thinking.parentNode.removeChild(thinking);
        var body = addTurn("agent", res.reply || "Done.");
        history.push({ role: "assistant", content: res.reply || "" });
        renderCards(res.cards, body);
        respond();
      })
      .catch(function () {
        thinking.parentNode && thinking.parentNode.removeChild(thinking);
        addTurn("agent", "Something went wrong running that. Try again.");
        Orb.set(0);
      })
      .finally(function () { busy = false; send.disabled = false; refocus(); });
  }

  function post(url, data) {
    var p = new URLSearchParams();
    p.set("_csrf", csrf);
    p.set("convo_key", convoKey);
    p.set("browser_key", browserKey);
    Object.keys(data).forEach(function (k) { p.set(k, data[k]); });
    return fetch(url, {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: p.toString()
    }).then(function (r) { if (!r.ok) { var e = new Error("http " + r.status); e.status = r.status; throw e; } return r.json(); });
  }

  /* The orb's brief "responding" settle as a reply lands -- honest: no audio is played. */
  function respond() { Orb.set(2); setTimeout(function () { Orb.set(0); }, 1800); }
  function refocus() { if (!TOUCH) input.focus(); }

  function submit(text) {
    text = (text || "").trim();
    if (!text || busy) return;
    busy = true; send.disabled = true;
    shell.classList.add("chatting");
    addTurn("user", text);
    history.push({ role: "user", content: text });
    input.value = "";
    var thinking = addThinking();
    Orb.set(1);
    if (canStream()) streamSubmit(text, thinking);
    else postSubmit(text, thinking);
  }

  /* Reset the turn state and (on desktop) return focus to the bar. */
  function endTurn() { busy = false; send.disabled = false; refocus(); }

  /* Apply a completed result to a turn body: authoritative reply text, then cards/confirm/
     coach. Shared by the streaming and non-streaming paths so the two never drift. */
  function applyResult(res, body) {
    var bub = body.querySelector(".bubble");
    if (bub) bub.textContent = res.reply || bub.textContent || "";
    history.push({ role: "assistant", content: res.reply || "" });
    renderCards(res.cards, body);
    if (res.pending_action) renderConfirm(res.pending_action, body);
    if (res.coach) renderCoach(res.coach, body);
    respond();
  }

  /* Non-streaming fallback: one JSON POST, render the whole reply at once. Used when the
     browser can't stream, when reduced-motion is set (instant answer is the a11y win), and
     as the recovery path if a stream fails before any text arrives. */
  function postSubmit(text, thinking) {
    post("/assistant", { message: text, history: JSON.stringify(history.slice(-12)) })
      .then(function (res) {
        thinking.parentNode && thinking.parentNode.removeChild(thinking);
        applyResult(res, addTurn("agent", ""));
      })
      .catch(function (err) {
        thinking.parentNode && thinking.parentNode.removeChild(thinking);
        var msg = (err && err.status === 403)
          ? "Your session expired — refresh the page to keep going."
          : "I could not reach the server just now. Try again in a moment.";
        addTurn("agent", msg);
        Orb.set(0);
      })
      .finally(endTurn);
  }

  /* Streaming path: read the SSE body, append text deltas live, finalize on the done frame.
     The done frame carries the same shape /assistant returns, so cards/confirm/coach reuse
     the exact renderers and the confirm gate is identical. */
  function canStream() {
    return !REDUCED && window.fetch && window.ReadableStream && window.TextDecoder;
  }
  function streamSubmit(text, thinking) {
    var p = new URLSearchParams();
    p.set("_csrf", csrf); p.set("convo_key", convoKey); p.set("browser_key", browserKey);
    p.set("message", text); p.set("history", JSON.stringify(history.slice(-12)));
    var body = null, started = false, acc = "", done = false;
    function ensureBody() {
      if (body) return;
      thinking.parentNode && thinking.parentNode.removeChild(thinking);
      body = addTurn("agent", "");
    }
    function onFrame(f) {
      if (f.t === "delta") {
        started = true; ensureBody(); acc += f.v;
        var bub = body.querySelector(".bubble"); if (bub) bub.textContent = acc;
        scrollDown();
      } else if (f.t === "done") {
        done = true; ensureBody(); applyResult(f.result || {}, body);
      }
    }
    fetch("/assistant/stream", {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/x-www-form-urlencoded" }, body: p.toString()
    }).then(function (resp) {
      if (!resp.ok || !resp.body) throw new Error("no stream");
      var reader = resp.body.getReader(), dec = new TextDecoder(), buf = "";
      function pump() {
        return reader.read().then(function (r) {
          if (r.done) return;
          buf += dec.decode(r.value, { stream: true });
          var parts = buf.split("\n\n"); buf = parts.pop();
          parts.forEach(function (chunk) {
            var line = chunk.replace(/^data: /, "").trim();
            if (line) { try { onFrame(JSON.parse(line)); } catch (e) {} }
          });
          return pump();
        });
      }
      return pump();
    }).then(function () {
      if (!done && !started) postSubmit(text, thinking);   // empty stream -> JSON fallback
      else endTurn();
    }).catch(function () {
      if (!started) { postSubmit(text, thinking); return; }  // failed early -> JSON fallback
      addTurn("agent", "The connection dropped mid-reply. Try that again.");
      Orb.set(0); endTurn();
    });
  }

  /* ---- suggestion chips (rebuilt on each feed refresh) ---- */
  function rebuildChips(list) {
    chipsEl.innerHTML = "";
    (list || []).forEach(function (s) {
      var chip = el("button", "chip", s); chip.type = "button";
      chip.addEventListener("click", function () { submit(s); });
      chipsEl.appendChild(chip);
    });
  }
  (function () {
    var list = [];
    try { list = JSON.parse(shell.getAttribute("data-suggestions") || "[]"); } catch (e) {}
    rebuildChips(list);
  })();

  /* ---- server-rendered briefing: wire the initial items as one-tap actions ---- */
  (function () {
    var rows = document.querySelectorAll("#briefingSlot .briefing-row[data-action]");
    Array.prototype.forEach.call(rows, function (row) {
      row.addEventListener("click", function () { submit(row.getAttribute("data-action")); });
    });
  })();

  /* ---- real-time feed poll: refresh the briefing + chips in place, never touching the
     transcript -- so a just-missed call surfaces without a reload that would wipe the chat.
     Skips while busy or backgrounded; refreshes on tab focus. (SSE + web push are the
     documented next step -- see SETUP_NEEDED.) ---- */
  (function () {
    var lastSig = shell.getAttribute("data-feed-sig") || "";
    var slot = document.getElementById("briefingSlot");
    function applyFeed(res) {
      if (!res || !res.sig || res.sig === lastSig) return;
      lastSig = res.sig;
      if (slot) {
        slot.innerHTML = "";
        var node = buildBriefingNode(res.briefing);
        if (node) slot.appendChild(node);
      }
      rebuildChips(res.suggestions);
    }
    function poll() {
      if (busy || document.hidden) return;
      fetch("/api/feed", { credentials: "same-origin" })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (res) { if (res) applyFeed(res); })
        .catch(function () {});
    }
    setInterval(poll, 25000);
    document.addEventListener("visibilitychange", function () { if (!document.hidden) poll(); });
  })();

  form.addEventListener("submit", function (e) { e.preventDefault(); submit(input.value); });

  /* ---- Offline banner: an honest "you're offline" strip so a failed send isn't a mystery
     in the field (spotty signal in a crawlspace / truck). Polite + dismissable by reconnect. */
  (function () {
    var dock = document.querySelector(".command-dock");
    if (!dock) return;
    var bar = el("div", "offline-banner", "You're offline — RingBack will catch up when you reconnect.");
    bar.setAttribute("role", "status");
    bar.hidden = true;
    dock.insertBefore(bar, dock.firstChild);
    function sync() { bar.hidden = navigator.onLine !== false; }
    window.addEventListener("online", sync);
    window.addEventListener("offline", sync);
    sync();
  })();

  /* ---- Push-to-talk voice: Web Speech API only, no new infra. Tap to start/stop; the
     transcript fills the command bar so the owner can read + edit before sending -- it never
     auto-sends (no blind send). The mic stays hidden when the browser can't do speech. ---- */
  (function () {
    var mic = document.getElementById("commandMic");
    if (!mic) return;
    var SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SR) { mic.hidden = true; return; }
    mic.hidden = false;
    var rec = new SR();
    rec.lang = "en-US"; rec.interimResults = true; rec.maxAlternatives = 1;
    var listening = false, finalText = "";
    rec.onstart = function () {
      listening = true; mic.classList.add("is-listening");
      mic.setAttribute("aria-pressed", "true");
    };
    rec.onend = function () {
      listening = false; mic.classList.remove("is-listening");
      mic.setAttribute("aria-pressed", "false");
    };
    rec.onerror = rec.onend;
    rec.onresult = function (e) {
      var interim = "", fin = finalText;
      for (var i = e.resultIndex; i < e.results.length; i++) {
        var t = e.results[i][0].transcript;
        if (e.results[i].isFinal) fin += t; else interim += t;
      }
      finalText = fin;
      input.value = (fin + interim).replace(/\s+/g, " ").trim();
    };
    mic.addEventListener("click", function () {
      if (listening) { try { rec.stop(); } catch (e) {} return; }
      finalText = input.value ? input.value.trim() + " " : "";
      try { rec.start(); } catch (e) {}
    });
    window.addEventListener("pagehide", function () { if (listening) { try { rec.stop(); } catch (e) {} } });
  })();

  Orb.mount();
  if (!TOUCH) input.focus();
})();
