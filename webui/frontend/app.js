const PHASES = [["connectivity", "Connectivity"], ["configuration", "Configuration"], ["testing", "Testing"]];
const PHASE_LABEL = Object.fromEntries(PHASES);
const SVG_NS = "http://www.w3.org/2000/svg";
const WIRE_COLORS = { UART_TX: "#f7d878", UART_RX: "#a0c385", VCC_5V: "#f28b82", GND: "#c9ced8" };

const comps = {};          // component id -> {panel, scroller, inner, pills, li, tab, wires, done, name, stuck}
let activeId = null, shownId = null, hasKey = false;
let tests = {}, wiring = {}, projectList = [], currentProject = null, replaying = false;

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}
function svg(tag, attrs, text) {
  const e = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
  if (text != null) e.textContent = text;
  return e;
}
function icon(name, cls) {
  const s = svg("svg", cls ? { class: cls } : {});
  s.append(svg("use", { href: `#i-${name}` }));
  return s;
}
function dots() {
  const d = el("span", "dots");
  for (const [key] of PHASES) { const x = el("span", "dot"); x.dataset.phase = key; d.append(x); }
  return d;
}
const clock = (ts) => new Date((ts ?? Date.now() / 1000) * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });

const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
ws.onmessage = (ev) => { const m = JSON.parse(ev.data); (handlers[m.type] || (() => {}))(m); };
ws.onclose = () => { const b = $("#banner"); b.textContent = "Disconnected from server. Reload the page."; b.classList.remove("hidden"); };
const send = (m) => ws.send(JSON.stringify(m));

// ---- component chat panels ----
function ensureComponent(id) {
  if (comps[id]) return comps[id];
  $("#panels .empty-state")?.remove();
  const panel = el("section", "chat-panel");
  const head = el("header", "chat-head");
  const pills = {}, pillsEl = el("div", "stage-pills");
  for (const [key, label] of PHASES) {
    const p = el("span", "pill pending"); p.append(el("span", "idle-dot"), label, el("span", "pill-state", "pending"));
    pills[key] = p; pillsEl.append(p);
  }
  head.append(el("span", "code", id), el("h2", "name", "New Sensor"), pillsEl);
  const scroller = el("div", "transcript"), inner = el("div", "transcript-inner");
  scroller.append(inner);
  panel.append(head, scroller);
  $("#panels").append(panel);

  const li = el("li"); li.append(icon("sensor"), el("span", "cname", "New Sensor"), dots());
  li.onclick = () => show(id);
  $("#inputs").append(li);

  const tab = el("button", "tab"); tab.append(el("span", "idx", id), el("span", "cname", "New Sensor"), dots());
  tab.onclick = () => show(id);
  $("#tabs").append(tab);
  const c = comps[id] = { panel, scroller, inner, pills, li, tab, wires: {}, done: false, name: "New Sensor", stuck: true };
  // Stay pinned to the bottom while the reader is there; cards (charts) grow after they mount.
  scroller.addEventListener("scroll", () => { c.stuck = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 80; });
  new ResizeObserver(() => { if (c.stuck) scroller.scrollTop = scroller.scrollHeight; }).observe(inner);
  return c;
}

function show(id) {
  shownId = id;
  for (const [cid, c] of Object.entries(comps)) {
    c.panel.classList.toggle("hidden", cid !== id);
    c.li.classList.toggle("shown", cid === id);
    c.tab.classList.toggle("on", cid === id);
  }
  $("#title-center").textContent = `${currentProject?.name ?? "Probe"} — ${id} ${comps[id].name}`;
  comps[id].stuck = true;
  updateComposer(); scrollDown(comps[id]);
}

function updateComposer() {
  const running = activeId && !comps[activeId].done;
  for (const b of $("#mode-seg").children) b.disabled = !!running;
  $("#mode-seg").title = running ? "Switch modes between components" : "";
  const live = running && shownId === activeId;
  $("#text").disabled = !live; $("#send").disabled = !live || !$("#text").value.trim();
  $("#text").placeholder = live ? "Message Probe…" : running ? "Switch to the active component to reply" : currentProject ? "Add a component to start" : "Create or open a project to start";
  $("#quick").classList.toggle("hidden", !live || !$("#quick").children.length);
  if (live) $("#text").focus();
}

// Quick replies: one-tap answers for the step Probe is waiting on.
function setQuick(labels) {
  $("#quick").replaceChildren(...labels.map((label) => {
    const b = el("button", "chip", label);
    b.onclick = () => { send({ type: "user_message", text: label.toLowerCase() === "completed" ? "completed" : label }); setQuick([]); };
    return b;
  }));
  updateComposer();
}

// ---- workspace: welcome page, project empty state, reset ----
function resetWorkspace() {
  for (const k of Object.keys(comps)) delete comps[k];
  tests = {}; wiring = {}; activeId = shownId = null;
  $("#panels").replaceChildren(); $("#tabs").replaceChildren(); $("#inputs").replaceChildren();
  setQuick([]);
}

function ago(ts) {
  const s = Date.now() / 1000 - ts;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  if (s < 7 * 86400) return `${Math.floor(s / 86400)}d ago`;
  return new Date(ts * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}
const plural = (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`;

function renderWelcome() {
  const w = el("div", "welcome");
  const mark = el("div", "wordmark"); mark.append(icon("logo"), el("h2", null, "Probe"));
  const cols = el("div", "cols"), start = el("div"), recent = el("div");
  const newBtn = el("button", "link-row"); newBtn.append(icon("plus"), "New project…"); newBtn.onclick = startNewProject;
  start.append(el("h3", null, "Start"), newBtn);
  recent.append(el("h3", null, "Recent"));
  if (!projectList.length) recent.append(el("div", "none", "No projects yet. Create one to bring up your first sensor."));
  for (const p of projectList.slice(0, 6)) {
    const r = el("button", "link-row proj"); r.append(icon(p.pinned ? "pin-on" : "folder"), p.name, el("span", "sub", ago(p.updated)));
    r.onclick = () => send({ type: "open_project", id: p.id }); recent.append(r);
  }
  cols.append(start, recent);
  w.append(mark, el("p", "tagline", "Bring up sensors on the probe cube: connectivity, configuration and testing, one component at a time."), el("div", "rule"), cols);
  $("#panels").replaceChildren(w);
}

function renderProjectEmpty() {
  const e = el("div", "empty-state"), ring = el("div", "ring"); ring.append(icon("sensor"));
  const btn = el("button", "btn primary lg"); btn.append(icon("plus"), "Add a component");
  btn.onclick = () => send({ type: "add_component" });
  e.append(ring, el("h2", null, "No components yet"), el("p", null, "Add a sensor and Probe will walk you through wiring, configuring and testing it."), btn);
  $("#panels").replaceChildren(e);
}

function renderProjects() {
  const pinned = projectList.filter((p) => p.pinned), history = projectList.filter((p) => !p.pinned);
  const item = (p) => {
    const li = el("li", `proj-item${p.id === currentProject?.id ? " current" : ""}`);
    const done = p.components.filter((c) => c.done).length;
    const meta = el("div", "pmeta"), sub = el("span", "psub");
    sub.textContent = `${plural(p.components.length, "sensor")}${p.components.length ? ` · ${done} done` : ""} · ${ago(p.updated)}`;
    meta.append(el("span", "pname", p.name), sub);
    const acts = el("div", "acts");
    const pin = el("button", `icon-btn${p.pinned ? " pinned" : ""}`); pin.title = p.pinned ? "Unpin" : "Pin"; pin.append(icon(p.pinned ? "pin-on" : "pin"));
    pin.onclick = (e) => { e.stopPropagation(); send({ type: "pin_project", id: p.id, pinned: !p.pinned }); };
    const del = el("button", "icon-btn del"); del.title = "Delete project"; del.append(icon("trash"));
    del.onclick = (e) => { e.stopPropagation(); if (confirm(`Delete “${p.name}”? Its saved history will be removed.`)) send({ type: "delete_project", id: p.id }); };
    acts.append(pin, del);
    li.append(icon(p.pinned ? "pin-on" : "folder"), meta, acts);
    li.title = `${p.name}\nCreated ${new Date(p.created * 1000).toLocaleString()}`;
    li.onclick = () => send({ type: "open_project", id: p.id });
    return li;
  };
  $("#pinned").replaceChildren(...pinned.map(item)); $("#history").replaceChildren(...history.map(item));
  $("#pinned-count").textContent = pinned.length || ""; $("#history-count").textContent = history.length || "";
  $("#pinned-empty").classList.toggle("hidden", pinned.length > 0); $("#history-empty").classList.toggle("hidden", history.length > 0);
  if (!currentProject) renderWelcome();
}

function startNewProject() {
  showView("explorer"); setCollapsed(false);
  $("#new-project").classList.add("hidden"); $("#new-project-form").classList.remove("hidden");
  const input = $("#project-name"); input.value = ""; input.focus();
}
function endNewProject() { $("#new-project").classList.remove("hidden"); $("#new-project-form").classList.add("hidden"); }

function toast(text, err = true) {
  const t = el("div", `toast${err ? " err" : ""}`, text); $("#toasts").append(t);
  setTimeout(() => t.remove(), 4500);
}

function setPhaseStatus(text, cls) { const s = $("#sb-phase"); s.textContent = text; s.className = `sb-item ${cls || ""}`; }

// ---- transcript rows ----
function avatar(role) {
  const a = el("span", "avatar");
  if (role === "probe") a.append(icon("logo")); else a.textContent = "You";
  return a;
}
function append(m, node) {
  const c = comps[m.component_id], typing = $(".msg.typing-row", c.inner);
  typing ? c.inner.insertBefore(node, typing) : c.inner.append(node);
  scrollDown(c);
  return node;
}
// Cards sit on Probe's side of the conversation, aligned with its bubbles.
function cardRow(m, card) {
  const row = el("div", "msg probe attach"), wrap = el("div", "bubble-wrap");
  row.append(el("span", "avatar spacer"), wrap); wrap.append(card);
  append(m, row);
  return card;
}
function lastRow(c) { return [...c.inner.children].filter((n) => !n.classList.contains("typing-row")).pop(); }
function scrollDown(c) { if (c && c.stuck) c.scroller.scrollTop = c.scroller.scrollHeight; }

// ---- event handlers ----
const handlers = {
  hello(m) {
    const transport = m.transport === "sim" ? "Simulated board" : `Serial`;
    for (const e of $$(".board-name")) e.textContent = m.board;
    $("#board-transport").textContent = transport;
    $("#sb-transport").textContent = m.transport === "sim" ? "◇ sim" : "◆ serial";
    $("#sim-toggle").classList.toggle("hidden", m.transport !== "sim");
  },
  mode(m) {
    const online = m.mode === "online", badge = $("#mode-badge");
    badge.className = `badge ${m.mode}`;
    badge.textContent = online ? `Online · ${m.model}` : "Offline · scripted";
    $("#board-llm").textContent = online ? `OpenAI ${m.model}` : "Offline (scripted, no LLM)";
    const sb = $("#sb-mode"); sb.textContent = online ? `● Online` : "● Offline mode"; sb.className = `sb-item ${online ? "good" : "warn"}`;
    for (const b of $("#mode-seg").children) b.classList.toggle("on", b.dataset.mode === m.mode);
    hasKey = m.has_key;
    $("#key-form").classList.add("hidden"); $("#mode-error").textContent = ""; $("#api-key").value = "";
  },
  mode_error(m) { $("#key-form").classList.remove("hidden"); $("#mode-error").textContent = m.text; },
  projects(m) {
    projectList = m.projects;
    if (currentProject) currentProject = projectList.find((p) => p.id === currentProject.id) || currentProject;
    renderProjects();
  },
  project_opened(m) {
    currentProject = m.project; replaying = true; resetWorkspace();
    $("#pane-project").classList.remove("hidden"); $("#project-title").textContent = m.project.name;
    $("#title-center").textContent = m.project.name; setPhaseStatus("");
    renderProjects();
  },
  replay_done(m) {
    replaying = false;
    for (const id of m.interrupted) {
      const c = comps[id]; if (!c) continue;
      c.done = true;
      for (const [key, p] of Object.entries(c.pills)) if (p.classList.contains("active")) {
        setPill(c, key, "interrupted");
        for (const d of $$(`.dot[data-phase="${key}"]`, c.li).concat($$(`.dot[data-phase="${key}"]`, c.tab))) d.className = "dot interrupted";
      }
      c.inner.append(el("div", "sys-msg warn", "This component was interrupted before it finished. Add a new component to bring it up again."));
    }
    const ids = Object.keys(comps);
    if (!ids.length) renderProjectEmpty(); else show(ids[ids.length - 1]);
    updateComposer();
  },
  project_closed() {
    currentProject = null; resetWorkspace(); $("#pane-project").classList.add("hidden");
    $("#title-center").textContent = "Probe"; setPhaseStatus(""); renderProjects(); updateComposer();
  },
  component(m) {
    const c = ensureComponent(m.component_id);
    c.name = m.name;
    $(".name", c.panel).textContent = m.name;
    $(".cname", c.li).textContent = m.name; $(".cname", c.tab).textContent = m.name;
    if (!m.replay) activeId = m.component_id;
    show(m.component_id);
  },
  phase(m) {
    const c = comps[m.component_id];
    setPill(c, m.phase, m.status === "active" ? "active" : "done");
    for (const d of $$(`.dot[data-phase="${m.phase}"]`, c.li).concat($$(`.dot[data-phase="${m.phase}"]`, c.tab))) d.className = `dot ${m.status}`;
    if (m.status === "active") {
      const div = el("div", "stage-divider"); div.append(el("span", null, PHASE_LABEL[m.phase])); append(m, div);
      if (!m.replay) setPhaseStatus(`${m.component_id} · ${PHASE_LABEL[m.phase]} in progress`, "warn");
    }
    if (!m.replay) setQuick([]);
  },
  message(m) {
    const row = el("div", `msg ${m.role}`), wrap = el("div", "bubble-wrap");
    wrap.append(el("div", "bubble", m.text), el("span", "ts", clock(m.ts)));
    row.append(avatar(m.role), wrap);
    append(m, row);
    if (m.role === "user" && !m.replay) setQuick([]);
  },
  thinking(m) {
    const c = comps[m.component_id];
    $(".msg.typing-row", c.inner)?.remove();
    if (m.on) {
      const row = el("div", "msg probe typing-row"), b = el("div", "bubble typing");
      b.append(el("span"), el("span"), el("span"));
      row.append(avatar("probe"), b); c.inner.append(row); scrollDown(c);
    }
  },
  error(m) {
    if (m.component_id) append(m, el("div", "sys-msg err", m.text));
    else toast(m.text);
  },
  schematic(m) { cardRow(m, renderSchematic(m, comps[m.component_id])); },
  wiring_step(m) {
    const c = comps[m.component_id], card = el("div", "card wiring");
    const sw = el("span", "swatch"); sw.style.background = WIRE_COLORS[c.wires[m.pin]?.signal] || "#8d97a8";
    const line = el("div", "wire-line"); line.append(sw, el("span", null, m.instruction));
    const actions = el("div", "actions");
    if (!m.replay) {
      const btn = el("button", "btn primary small", "Mark done");
      btn.onclick = () => { btn.disabled = true; send({ type: "user_message", text: "completed" }); setQuick([]); };
      actions.append(btn);
      setQuick(["Completed"]);
    }
    line.append(actions); card.append(line);
    wiring[`${m.component_id}:${m.pin}`] = actions;
    c.wires[m.pin]?.row.classList.add("next");
    cardRow(m, card);
  },
  wiring_confirmed(m) {
    const a = wiring[`${m.component_id}:${m.pin}`];
    if (a) { const ok = el("span", "ok"); ok.append("✓ Done"); a.replaceChildren(ok); }
    const w = comps[m.component_id].wires[m.pin];
    if (w) { w.group.classList.add("done"); w.row.classList.remove("next"); w.row.classList.add("done"); $(".state", w.row).textContent = "✓ Done"; }
  },
  board_io(m) {
    const c = comps[m.component_id];
    let block = $(".io", lastRow(c) || el("div"));
    if (!block) {
      block = el("details", "io");
      const sum = el("summary");
      sum.append(icon("chev", "chev"), el("span", "io-title", "Board console"), el("span", "io-count"), el("span", "io-last"));
      block.append(sum, el("div", "io-lines")); block.dataset.n = 0; block.dataset.errs = 0;
      cardRow(m, block);
    }
    const err = (m.error ?? /^ERR/.test(m.line)) && m.direction !== "tx";
    const arrow = { tx: "→", rx: "←", sensor: "⇠" }[m.direction] || "·";
    const text = `${arrow} ${m.line}`;
    $(".io-lines", block).append(el("div", `${m.direction}${err ? " err" : ""}`, text));
    block.dataset.n = +block.dataset.n + 1; block.dataset.errs = +block.dataset.errs + (err ? 1 : 0);
    $(".io-count", block).textContent = `${block.dataset.n} lines` + (+block.dataset.errs ? ` · ${block.dataset.errs} error${+block.dataset.errs > 1 ? "s" : ""}` : "");
    const last = $(".io-last", block); last.textContent = text; last.className = `io-last ${m.direction}${err ? " err" : ""}`;
    scrollDown(c);
  },
  config(m) {
    const card = el("div", "card config"), kv = el("dl", "kv");
    card.append(el("div", "card-title", "Configuration"));
    for (const [k, v] of Object.entries(m.settings)) {
      const d = el("div"); d.append(el("dt", null, k.replace(/_/g, " ")), el("dd", null, String(v))); kv.append(d);
    }
    card.append(kv); cardRow(m, card);
    if (!m.replay) setQuick(["Looks good"]);
  },
  test_step(m) {
    const card = el("div", "card test"), title = el("div", "card-title");
    const chart = new LiveChart(m.seconds, m.units || {}, m.chart);
    title.append(el("span", null, "Live data"), el("span", "muted", m.name), chart.toggle);
    if (m.chart?.kind === "motion" && m.sim && !m.replay) {
      const btn = el("button", "btn ghost small sim-motion", "Simulate motion"); btn.type = "button"; btn.disabled = true;
      btn.onclick = () => {
        send({ type: "sim_motion", seconds: 2.5 }); btn.disabled = true; btn.textContent = "Moving…";
        setTimeout(() => { btn.textContent = "Simulate motion"; btn.disabled = !tests[m.test_id].live; }, 2500);
      };
      title.insertBefore(btn, chart.toggle);
      chart.simBtn = btn;
    }
    const status = el("div", "status"), result = el("ul", "checks");
    card.append(title, el("div", "instr", m.instruction), status, chart.el, result);
    tests[m.test_id] = { status, result, chart, seconds: m.seconds };
    cardRow(m, card);
    if (m.replay) { tests[m.test_id].sampling = true; return; }
    let left = Math.round(m.lead);
    const tick = () => {
      if (tests[m.test_id].sampling) return;
      status.textContent = left > 0 ? `Starting in ${left}…` : "Starting…";
      if (left-- > 0) setTimeout(tick, 1000);
    };
    tick();
  },
  test_status(m) {
    const t = tests[m.test_id]; if (!t) return;
    if (m.status === "sampling") {
      t.sampling = true; t.chart.start(m.ts);
      t.live = !m.replay; if (t.chart.simBtn) t.chart.simBtn.disabled = !t.live;
      if (!m.replay) t.status.textContent = `Measuring for ${t.seconds} s…`;
    } else {
      t.status.textContent = `Measured ${m.samples} samples.`;
      t.live = false; if (t.chart.simBtn) { t.chart.simBtn.disabled = true; t.chart.simBtn.textContent = "Simulate motion"; }
    }
  },
  reading(m) { tests[m.test_id]?.chart.push(m.ts, m.values, m.replay); },
  test_result(m) {
    const t = tests[m.test_id]; if (!t) return;
    const li = el("li", m.passed ? "done" : "fail"); li.append(el("span", "ck"), el("b", null, m.passed ? "Passed" : "Failed"), el("span", "muted", m.summary));
    t.result.replaceChildren(li);
    if (!m.passed && !m.replay) setQuick(["Retry"]);
  },
  done(m) {
    const c = comps[m.component_id]; c.done = true;
    const next = el("div", "next"), btn = el("button", "btn primary"); btn.append(icon("plus"), "Add next sensor");
    btn.onclick = () => send({ type: "add_component" });
    next.append(el("span", null, "Let’s move onto the next sensor"), btn);
    append(m, next);
    if (!m.replay) { setQuick([]); setPhaseStatus(`${m.component_id} · all phases complete`, "good"); }
    updateComposer();
  },
};

function setPill(c, key, state) {
  const p = c.pills[key];
  p.className = `pill ${state}`;
  const mark = state === "done" ? el("span", "pill-ico", "✓") : state === "active" ? el("span", "spin-dot") : state === "interrupted" ? el("span", "pill-ico", "!") : el("span", "idle-dot");
  p.replaceChildren(mark, PHASE_LABEL[key], el("span", "pill-state", { done: "complete", active: "in progress", interrupted: "interrupted" }[state] || "pending"));
}

function renderSchematic(m, c) {
  const rows = m.connections, rowH = 40, top = 64, h = top + rows.length * rowH + 8;
  const s = svg("svg", { viewBox: `0 0 560 ${h}`, class: "schem", role: "img", "aria-label": "Wiring diagram" });
  const box = (x, label, sub, cube) => {
    s.append(svg("rect", { x, y: 14, width: 160, height: h - 22, rx: 10, class: `schem-box${cube ? " cube" : ""}` }));
    const t = svg("text", { x: x + 80, y: 36, "text-anchor": "middle", class: "schem-h" }, label);
    if (label.length > 18) { t.setAttribute("textLength", 144); t.setAttribute("lengthAdjust", "spacingAndGlyphs"); }
    s.append(t);
    s.append(svg("text", { x: x + 80, y: 51, "text-anchor": "middle", class: "schem-sub" }, sub));
  };
  box(16, m.sensor, "Sensor"); box(384, "Probe Cube", m.board?.split(" (")[0] ?? "", true);
  const card = el("div", "card schematic"), list = el("ol", "wire-list");
  const title = el("div", "card-title"); title.append(el("span", null, "Wiring schematic"), el("span", "muted", `${m.sensor} → Probe Cube`));
  rows.forEach((w, i) => {
    const y = top + 12 + i * rowH, color = WIRE_COLORS[w.signal] || "#8d97a8";
    const g = svg("g", { class: "wire" });
    g.append(svg("text", { x: 168, y: y + 4, "text-anchor": "end", class: "schem-pin" }, w.sensor_pin));
    g.append(svg("circle", { cx: 176, cy: y, r: 4, fill: color }));
    if (w.cube_port) {
      g.append(svg("path", { d: `M176 ${y} C 260 ${y}, 300 ${y}, 384 ${y}`, stroke: color, "stroke-width": 2.5, fill: "none", class: "wire-path" }));
      g.append(svg("circle", { cx: 384, cy: y, r: 4, fill: color }));
      g.append(svg("text", { x: 392, y: y + 4, class: "schem-pin" }, `${w.cube_port} · ${w.mcu_function}`));
      const ok = svg("g", { class: "wire-ok" });
      ok.append(svg("circle", { cx: 280, cy: y, r: 8 }), svg("path", { d: `M276 ${y} l3 3 l5 -6`, fill: "none" }));
      g.append(ok);
    }
    s.append(g);
    const li = el("li"), sw = el("span", "swatch"); sw.style.background = color;
    const label = el("span"); label.append("Sensor ", el("b", null, w.sensor_pin), " → Probe Cube ", el("b", null, w.cube_port ?? "—"));
    li.append(sw, label, el("span", "state muted", "pending"));
    list.append(li);
    if (c) c.wires[w.sensor_pin] = { group: g, row: li, signal: w.signal };
  });
  card.append(title, s, list);
  return card;
}

// ---- live chart: one small multiple per field (own y-scale, shared time axis) ----
class LiveChart {
  constructor(seconds, units, spec = null) {
    this.seconds = seconds; this.units = units; this.spec = spec; this.t0 = null; this.pts = []; this.fields = [];
    this.hover = null; this.view = "chart"; this.width = 560; this.raf = null;
    this.el = el("figure", "viz");
    this.rows = el("div", "viz-rows"); this.table = el("div", "viz-table hidden"); this.tip = el("div", "viz-tip hidden");
    this.el.append(this.rows, this.table, this.tip);
    this.toggle = el("div", "seg small");
    for (const v of ["chart", "table"]) {
      const b = el("button", v === "chart" ? "on" : "", v === "chart" ? "Chart" : "Table"); b.type = "button";
      b.onclick = () => { this.view = v; for (const x of this.toggle.children) x.classList.toggle("on", x === b); this.render(); };
      this.toggle.append(b);
    }
    new ResizeObserver((es) => { const w = Math.floor(es[0].contentRect.width); if (w && w !== this.width) { this.width = w; this.render(); } }).observe(this.el);
    this.render();
  }
  start(ts) { if (this.t0 === null && Number.isFinite(ts)) this.t0 = ts; }
  // ts is the server's event time. Without one (older server or saved project) use the
  // arrival time when live, or spread replayed samples evenly over the test duration.
  push(ts, values, replay = false) {
    if (!Number.isFinite(ts)) ts = replay ? null : Date.now() / 1000;
    if (ts !== null && this.t0 === null) this.t0 = ts;
    this.pts.push({ t: ts === null ? null : Math.max(0, ts - this.t0), v: values });
    if (ts === null) this.pts.forEach((p, i, a) => { p.t = a.length > 1 ? (i / (a.length - 1)) * this.seconds : 0; });
    for (const k of Object.keys(values)) if (!this.fields.includes(k)) this.fields.push(k);
    if (!this.raf) this.raf = requestAnimationFrame(() => { this.raf = null; this.render(); });
  }
  nearest(t) {
    let best = 0;
    for (let i = 1; i < this.pts.length; i++) if (Math.abs(this.pts[i].t - t) < Math.abs(this.pts[best].t - t)) best = i;
    return best;
  }
  render() {
    this.rows.classList.toggle("hidden", this.view !== "chart");
    this.table.classList.toggle("hidden", this.view !== "table");
    if (this.view === "table") return this.renderTable();
    this.tip.classList.add("hidden");
    if (!this.pts.length) { this.rows.replaceChildren(el("div", "viz-wait", "Waiting for samples…")); return; }
    if (this.spec?.kind === "motion") return this.renderMotion();
    const W = this.width, PAD = { l: 52, r: 96, t: 10, b: 6 }, PH = 84, XB = 22;
    const pw = W - PAD.l - PAD.r, span = Math.max(this.seconds, this.pts[this.pts.length - 1].t);
    const xt = niceTicks(0, span, 6);
    const x = (t) => PAD.l + (t / span) * pw;
    const hov = this.hover !== null ? this.pts[Math.min(this.hover, this.pts.length - 1)] : null;
    const rows = this.fields.map((f, idx) => {
      const series = this.pts.filter((p) => f in p.v);
      const yt = niceTicks(Math.min(...series.map((p) => p.v[f])), Math.max(...series.map((p) => p.v[f])), 4);
      const lo = yt.ticks[0], hi = yt.ticks[yt.ticks.length - 1];
      const lastRow = idx === this.fields.length - 1, H = PAD.t + PH + PAD.b + (lastRow ? XB : 0);
      const y = (v) => PAD.t + (1 - (v - lo) / (hi - lo)) * PH;
      const s = svg("svg", { width: W, height: H, class: "viz-svg", role: "img", tabindex: 0, "aria-label": `${f} over time` });
      for (const v of yt.ticks) {
        s.append(svg("line", { x1: PAD.l, x2: PAD.l + pw, y1: y(v), y2: y(v), class: v === lo ? "viz-axisline" : "viz-grid" }));
        s.append(svg("text", { x: PAD.l - 8, y: y(v) + 4, "text-anchor": "end", class: "viz-tick" }, v.toFixed(yt.decimals)));
      }
      if (lastRow) for (const t of xt.ticks.filter((t) => t <= span + 1e-9)) {
        s.append(svg("text", { x: x(t), y: H - 6, "text-anchor": "middle", class: "viz-tick" }, `${t.toFixed(xt.decimals)} s`));
      }
      // Line only: the y-axis is fitted to the data (not zero-based), so an area fill would exaggerate the change.
      const d = series.map((p, i) => `${i ? "L" : "M"}${x(p.t).toFixed(1)} ${y(p.v[f]).toFixed(1)}`).join(" ");
      const base = PAD.t + PH;
      s.append(svg("path", { d, class: "viz-line" }));
      const last = series[series.length - 1];
      s.append(svg("circle", { cx: x(last.t), cy: y(last.v[f]), r: 6.5, class: "viz-ring" }), svg("circle", { cx: x(last.t), cy: y(last.v[f]), r: 4.5, class: "viz-dot" }));
      s.append(svg("text", { x: x(last.t) + 12, y: y(last.v[f]) + 4, class: "viz-endlabel" }, `${fmt(last.v[f])} ${this.units[f] || ""}`));
      if (hov && f in hov.v) {
        s.append(svg("line", { x1: x(hov.t), x2: x(hov.t), y1: PAD.t, y2: base, class: "viz-cross" }));
        s.append(svg("circle", { cx: x(hov.t), cy: y(hov.v[f]), r: 6.5, class: "viz-ring" }), svg("circle", { cx: x(hov.t), cy: y(hov.v[f]), r: 4.5, class: "viz-dot" }));
      }
      const move = (cx) => { const r = s.getBoundingClientRect(); this.hover = this.nearest(((cx - r.left - PAD.l) / pw) * span); this.render(); };
      s.onpointermove = (e) => move(e.clientX);
      s.onpointerleave = () => { this.hover = null; this.render(); };
      s.onkeydown = (e) => {
        const n = this.pts.length - 1, h = this.hover ?? n;
        if (e.key === "ArrowLeft") this.hover = Math.max(0, h - 1);
        else if (e.key === "ArrowRight") this.hover = Math.min(n, h + 1);
        else if (e.key === "Escape") this.hover = null; else return;
        e.preventDefault(); this.render(); this.rows.querySelectorAll("svg")[idx]?.focus();
      };
      const row = el("div", "viz-row"), head = el("div", "viz-head");
      head.append(el("span", "viz-name", f), el("span", "viz-unit", this.units[f] || ""));
      row.append(head, s);
      return row;
    });
    this.rows.replaceChildren(...rows);
    if (hov) {
      this.tip.replaceChildren(el("div", "k", `${hov.t.toFixed(1)} s`), ...this.fields.filter((f) => f in hov.v).map((f) => {
        const v = el("div", "v"); v.append(el("span", "key"), `${f} `, el("b", null, fmt(hov.v[f])), el("span", "u", ` ${this.units[f] || ""}`)); return v;
      }));
      this.tip.style.left = `${Math.min(W - 160, Math.max(0, x(hov.t) + 12))}px`; this.tip.classList.remove("hidden");
    }
  }
  // Motion sensor: presence level on a fixed 0-1 axis, with bands where the motion flag is set.
  renderMotion() {
    const { level, flag } = this.spec, pts = this.pts.filter((p) => level in p.v);
    const W = this.width, PAD = { l: 40, r: 110, t: 12, b: 26 }, PH = 132, H = PAD.t + PH + PAD.b;
    const pw = W - PAD.l - PAD.r, span = Math.max(this.seconds, pts.length ? pts[pts.length - 1].t : 0);
    const x = (t) => PAD.l + (t / span) * pw, y = (v) => PAD.t + (1 - Math.min(1, Math.max(0, v))) * PH;
    const s = svg("svg", { width: W, height: H, class: "viz-svg", role: "img", tabindex: 0, "aria-label": "Presence level over time, motion periods shaded" });
    // motion bands first so the line draws over them
    let start = null;
    pts.forEach((p, i) => {
      const on = +p.v[flag] === 1, next = pts[i + 1];
      if (on && start === null) start = p.t;
      if (start !== null && (!on || !next)) {
        const end = on ? p.t : p.t;
        s.append(svg("rect", { x: x(start), y: PAD.t, width: Math.max(2, x(end) - x(start)), height: PH, class: "viz-band" }));
        start = null;
      }
    });
    for (const v of [0, 0.5, 1]) {
      s.append(svg("line", { x1: PAD.l, x2: PAD.l + pw, y1: y(v), y2: y(v), class: v === 0 ? "viz-axisline" : "viz-grid" }));
      s.append(svg("text", { x: PAD.l - 8, y: y(v) + 4, "text-anchor": "end", class: "viz-tick" }, v.toFixed(1)));
    }
    const xt = niceTicks(0, span, 6);
    for (const t of xt.ticks.filter((t) => t <= span + 1e-9)) s.append(svg("text", { x: x(t), y: H - 7, "text-anchor": "middle", class: "viz-tick" }, `${t.toFixed(xt.decimals)} s`));
    const d = pts.map((p, i) => `${i ? "L" : "M"}${x(p.t).toFixed(1)} ${y(p.v[level]).toFixed(1)}`).join(" ");
    s.append(svg("path", { d: `${d} L${x(pts[pts.length - 1].t).toFixed(1)} ${y(0)} L${x(pts[0].t).toFixed(1)} ${y(0)} Z`, class: "viz-area" }));
    s.append(svg("path", { d, class: "viz-line" }));
    const last = pts[pts.length - 1], moving = +last.v[flag] === 1;
    s.append(svg("circle", { cx: x(last.t), cy: y(last.v[level]), r: 6.5, class: "viz-ring" }), svg("circle", { cx: x(last.t), cy: y(last.v[level]), r: 4.5, class: "viz-dot" }));
    s.append(svg("text", { x: x(last.t) + 12, y: y(last.v[level]) + 4, class: "viz-endlabel" }, `${moving ? "Motion" : "Clear"} · ${last.v[level].toFixed(2)}`));
    const hov = this.hover !== null ? this.pts[Math.min(this.hover, this.pts.length - 1)] : null;
    if (hov && level in hov.v) {
      s.append(svg("line", { x1: x(hov.t), x2: x(hov.t), y1: PAD.t, y2: PAD.t + PH, class: "viz-cross" }));
      s.append(svg("circle", { cx: x(hov.t), cy: y(hov.v[level]), r: 6.5, class: "viz-ring" }), svg("circle", { cx: x(hov.t), cy: y(hov.v[level]), r: 4.5, class: "viz-dot" }));
    }
    s.onpointermove = (e) => { const r = s.getBoundingClientRect(); this.hover = this.nearest(((e.clientX - r.left - PAD.l) / pw) * span); this.render(); };
    s.onpointerleave = () => { this.hover = null; this.render(); };
    s.onkeydown = (e) => {
      const n = this.pts.length - 1, h = this.hover ?? n;
      if (e.key === "ArrowLeft") this.hover = Math.max(0, h - 1);
      else if (e.key === "ArrowRight") this.hover = Math.min(n, h + 1);
      else if (e.key === "Escape") this.hover = null; else return;
      e.preventDefault(); this.render(); this.rows.querySelector("svg")?.focus();
    };
    const head = el("div", "viz-head"), legend = el("span", "viz-legend");
    legend.append(el("span", "key line"), "Presence level", el("span", "key band"), "Motion detected");
    head.append(el("span", "viz-name", "Presence"), legend);
    const row = el("div", "viz-row"); row.append(head, s);
    this.rows.replaceChildren(row);
    if (hov && level in hov.v) {
      const v = el("div", "v"); v.append(el("span", "key"), "Level ", el("b", null, hov.v[level].toFixed(2)));
      this.tip.replaceChildren(el("div", "k", `${hov.t.toFixed(1)} s · ${+hov.v[flag] === 1 ? "Motion" : "Clear"}`), v);
      this.tip.style.left = `${Math.min(W - 160, Math.max(0, x(hov.t) + 12))}px`; this.tip.classList.remove("hidden");
    }
  }
  renderTable() {
    if (this.spec?.kind === "motion") {
      const { level, flag } = this.spec, t = el("table", "events"), head = el("tr");
      head.append(el("th", null, "Time"), el("th", null, "Presence level"), el("th", null, "Motion"));
      t.append(head);
      for (const p of this.pts.slice(-200)) {
        const tr = el("tr"); tr.append(el("td", null, `${p.t.toFixed(1)} s`), el("td", null, fmt(p.v[level])), el("td", null, +p.v[flag] === 1 ? "Yes" : "No")); t.append(tr);
      }
      return this.table.replaceChildren(t);
    }
    const t = el("table", "events"), head = el("tr");
    head.append(el("th", null, "Time"), ...this.fields.map((f) => el("th", null, `${f}${this.units[f] ? ` (${this.units[f]})` : ""}`)));
    t.append(head);
    for (const p of this.pts.slice(-200)) {
      const tr = el("tr"); tr.append(el("td", null, `${p.t.toFixed(1)} s`), ...this.fields.map((f) => el("td", null, f in p.v ? fmt(p.v[f]) : "—"))); t.append(tr);
    }
    if (!this.pts.length) { const tr = el("tr"), td = el("td", "muted", "No samples yet"); td.colSpan = this.fields.length + 1; tr.append(td); t.append(tr); }
    this.table.replaceChildren(t);
  }
}
// "Nice" axis ticks: steps of 1, 2, 2.5 or 5 x 10^n covering [lo, hi] with about `count` ticks.
function niceTicks(lo, hi, count) {
  if (hi - lo < 1e-9) { const pad = Math.max(Math.abs(hi) * 0.01, 0.1); lo -= pad; hi += pad; }
  const raw = (hi - lo) / Math.max(1, count - 1), mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((st) => st >= raw);
  let a = Math.floor(lo / step + 1e-9) * step, b = Math.ceil(hi / step - 1e-9) * step;
  if (Math.round((b - a) / step) < 2) b += step;
  const ticks = [];
  for (let v = a; v <= b + step / 2; v += step) ticks.push(+v.toFixed(10));
  const decimals = Math.max(0, -Math.floor(Math.log10(step) + 1e-9)) + (String(step / mag).includes(".") ? 1 : 0);
  return { ticks, decimals };
}
function fmt(v) { const a = Math.abs(v); return a >= 100 ? v.toFixed(0) : a >= 10 ? v.toFixed(1) : v.toFixed(2); }

// ---- sidebar: views, collapse, tree ----
const workbench = $("#workbench");
function setCollapsed(on) { workbench.classList.toggle("collapsed", on); localStorage.setItem("probe.sidebar", on ? "collapsed" : "open"); }
function showView(view) {
  for (const b of $$(".act")) b.classList.toggle("on", b.dataset.view === view);
  for (const v of $$(".view")) v.classList.toggle("hidden", v.dataset.view !== view);
}
for (const b of $$(".act")) b.onclick = () => {
  if (b.classList.contains("on")) return setCollapsed(!workbench.classList.contains("collapsed"));  // VS Code: click active view to toggle
  showView(b.dataset.view); setCollapsed(false);
};
$("#collapse").onclick = () => setCollapsed(true);
document.addEventListener("keydown", (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "b") { e.preventDefault(); setCollapsed(!workbench.classList.contains("collapsed")); }
});
setCollapsed(localStorage.getItem("probe.sidebar") === "collapsed");
for (const row of $$(".tree-group > .tree-row")) row.onclick = () => row.parentElement.classList.toggle("open");
for (const head of $$(".pane > .pane-head")) head.onclick = () => head.parentElement.classList.toggle("open");

// ---- controls ----
for (const b of $("#mode-seg").children) b.onclick = () => {
  if (b.classList.contains("on")) return;
  if (b.dataset.mode === "online" && !hasKey) { $("#key-form").classList.remove("hidden"); $("#api-key").focus(); return; }
  send({ type: "set_mode", mode: b.dataset.mode });
};
$("#key-form").onsubmit = (e) => {
  e.preventDefault();
  $("#mode-error").textContent = "Checking key…";
  send({ type: "set_mode", mode: "online", api_key: $("#api-key").value });
};
$("#key-cancel").onclick = () => { $("#key-form").classList.add("hidden"); $("#mode-error").textContent = ""; };
$("#add").onclick = () => send({ type: "add_component" });
$("#new-project").onclick = startNewProject;
$("#new-project-form").onsubmit = (e) => { e.preventDefault(); send({ type: "new_project", name: $("#project-name").value }); endNewProject(); };
$("#project-name").onkeydown = (e) => { if (e.key === "Escape") endNewProject(); };
$("#project-name").onblur = () => setTimeout(endNewProject, 150);
$("#sim-fault").onchange = (e) => send({ type: "sim_fault", on: e.target.checked });
$("#text").oninput = () => { $("#send").disabled = $("#text").disabled || !$("#text").value.trim(); };
$("#composer").onsubmit = (e) => {
  e.preventDefault();
  const text = $("#text").value.trim();
  if (!text) return;
  send({ type: "user_message", text }); $("#text").value = ""; $("#send").disabled = true;
};
