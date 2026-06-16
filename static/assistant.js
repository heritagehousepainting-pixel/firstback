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
        state: 0 idle (slow breath), 1 thinking (fast churn), 2 speaking.
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
    return {
      mount: function () {
        if (REDUCED || !init()) { shell.classList.add("no-webgl"); return; }
        running = true; frame();
        window.addEventListener("resize", resize);
        document.addEventListener("visibilitychange", function () {
          if (document.hidden) { running = false; cancelAnimationFrame(raf); }
          else if (gl) { running = true; start = performance.now(); frame(); }
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
    var av = el("div", "turn-avatar", role === "user" ? "You" : "M");
    if (role === "user") av.textContent = "Y";
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
    turn.appendChild(el("div", "turn-avatar", "M"));
    var body = el("div", "turn-body");
    var bub = el("div", "bubble thinking");
    bub.appendChild(el("i")); bub.appendChild(el("i")); bub.appendChild(el("i"));
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
    } else {
      c.classList.add("a-note"); c.textContent = card.body || "";
    }
    host.appendChild(c);
  }

  function renderCards(cards, body) {
    if (!cards || !cards.length) return;
    var wrap = el("div", "cards");
    cards.forEach(function (card) { renderCard(card, wrap); });
    body.appendChild(wrap); scrollDown();
  }

  /* ---- confirm flow (gated actions) ---- */
  function renderConfirm(pending, body) {
    var c = el("div", "a-card confirm-card");
    c.appendChild(el("div", "cc-q", pending.summary || "Run this action?"));
    var acts = el("div", "cc-actions");
    acts.appendChild(btn("Confirm", "primary", function () {
      c.parentNode && c.parentNode.removeChild(c);   // retire the prompt
      runAction(pending);
    }));
    acts.appendChild(btn("Cancel", "ghost", function () {
      acts.innerHTML = ""; acts.appendChild(el("span", "draft-note", "Cancelled."));
    }));
    c.appendChild(acts);
    var wrap = el("div", "cards"); wrap.appendChild(c); body.appendChild(wrap); scrollDown();
  }

  /* ---- proactive teaching offer (Mason offers to remember a recurring gap) ---- */
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
        speak();
      })
      .catch(function () {
        thinking.parentNode && thinking.parentNode.removeChild(thinking);
        addTurn("agent", "Something went wrong running that. Try again.");
        Orb.set(0);
      })
      .finally(function () { busy = false; send.disabled = false; input.focus(); });
  }

  function post(url, data) {
    var p = new URLSearchParams();
    p.set("_csrf", csrf);
    p.set("convo_key", convoKey);
    Object.keys(data).forEach(function (k) { p.set(k, data[k]); });
    return fetch(url, {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: p.toString()
    }).then(function (r) { return r.json(); });
  }

  function speak() { Orb.set(2); setTimeout(function () { Orb.set(0); }, 1800); }

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
    post("/assistant", { message: text, history: JSON.stringify(history.slice(-12)) })
      .then(function (res) {
        thinking.parentNode && thinking.parentNode.removeChild(thinking);
        var body = addTurn("agent", res.reply || "");
        history.push({ role: "assistant", content: res.reply || "" });
        renderCards(res.cards, body);
        if (res.pending_action) renderConfirm(res.pending_action, body);
        if (res.coach) renderCoach(res.coach, body);
        speak();
      })
      .catch(function () {
        thinking.parentNode && thinking.parentNode.removeChild(thinking);
        addTurn("agent", "I could not reach the server just now. Try again in a moment.");
        Orb.set(0);
      })
      .finally(function () { busy = false; send.disabled = false; input.focus(); });
  }

  /* ---- suggestion chips ---- */
  (function () {
    var raw = shell.getAttribute("data-suggestions");
    var list = [];
    try { list = JSON.parse(raw || "[]"); } catch (e) {}
    list.forEach(function (s) {
      var chip = el("button", "chip", s); chip.type = "button";
      chip.addEventListener("click", function () { submit(s); });
      chipsEl.appendChild(chip);
    });
  })();

  form.addEventListener("submit", function (e) { e.preventDefault(); submit(input.value); });

  Orb.mount();
  input.focus();
})();
