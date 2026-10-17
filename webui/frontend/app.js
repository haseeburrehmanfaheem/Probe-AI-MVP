const PHASES = [["connectivity", "Connectivity"], ["configuration", "Configuration"], ["testing", "Testing"]];
const PHASE_LABEL = Object.fromEntries(PHASES);
const SVG_NS = "http://www.w3.org/2000/svg";
const WIRE_COLORS = { UART_TX: "#f7d878", UART_RX: "#a0c385", VCC_5V: "#f28b82", VCC_3V3: "#f6ae6b", GND: "#c9ced8", RS485_A: "#7aa7ff", RS485_B: "#b39ddb" };

const comps = {};          // component id -> {panel, scroller, inner, pills, li, tab, wires, done, name, stuck}
let activeId = null, shownId = null, hasKey = false;
let tests = {}, lives = {}, wiring = {}, projectList = [], currentProject = null, replaying = false;

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
  $("#text").placeholder = live ? "Message Probe…" : running ? "Switch to the active component to reply"
    : currentProject ? (Object.keys(comps).length ? "Add the next sensor to continue" : "Add a component to start") : "Create or open a project to start";
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
  tests = {}; lives = {}; wiring = {}; activeId = shownId = null;
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
    // A yes/no question from Probe (e.g. "Shall I apply them?") gets one-tap answers.
    if (m.role === "probe" && !m.replay && /\?\s*(\(yes\s*\/\s*no\))?\s*$/i.test(m.text) &&
        /\b(shall i|should i|do you want|would you like|ok to|okay to|apply)\b/i.test(m.text)) setQuick(["Yes", "No"]);
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
  port_assigned(m) { append(m, el("div", "sys-msg", `Probe cube port ${m.port} (${m.interface.toUpperCase()}) assigned to ${m.model} · ${m.pins}`)); },
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
      const d = el("div"); d.append(el("dt", null, settingLabel(k)), el("dd", null, settingValue(k, v))); kv.append(d);
    }
    card.append(kv); cardRow(m, card);
    if (!m.replay) setQuick(["Looks good"]);
  },
  test_step(m) {
    const card = el("div", "card test"), title = el("div", "card-title");
    const chart = new LiveView({ seconds: m.seconds, units: m.units || {}, views: viewsOf(m) });
    const ctl = liveControls(m);
    title.append(el("span", null, "Live data"), el("span", "muted", prettyName(m.name)), ...ctl.nodes, chart.toggle);
    const status = el("div", "status"), result = el("ul", "checks");
    card.append(title, el("div", "instr", m.instruction), status, chart.el, result);
    tests[m.test_id] = { status, result, chart, ctl, seconds: m.seconds };
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
      t.ctl.setLive(!m.replay); t.chart.setLive(!m.replay);
      if (!m.replay) t.status.textContent = `Measuring for ${t.seconds} s…`;
    } else {
      t.status.textContent = `Measured ${m.samples} samples.`;
      t.ctl.setLive(false, true); t.chart.setLive(false);
    }
  },
  reading(m) { (m.view_id ? lives[m.view_id] : tests[m.test_id])?.chart.push(m.ts, m.values, m.replay); },
  // Live monitor after configuration: streams until Stop, the next board call or the end of the phase.
  live_view(m) {
    const card = el("div", "card test live"), title = el("div", "card-title");
    const win = m.live_window || 10;
    const chart = new LiveView({ seconds: win, window: win, units: m.units || {}, views: m.views });
    const pulse = el("span", `live-pill${m.replay ? " off" : ""}`, m.replay ? "Stopped" : "Live");
    const ctl = liveControls(m), stop = el("button", "btn ghost small", "Stop"); stop.type = "button";
    stop.onclick = () => { stop.disabled = true; send({ type: "live_stop", view_id: m.view_id }); };
    title.append(el("span", null, "Live monitor"), el("span", "muted", `${m.title} · ${m.port}`), pulse, ...ctl.nodes);
    if (!m.replay) title.append(stop);
    title.append(chart.toggle);
    const status = el("div", "status", m.replay ? "" : m.note || `Streaming live. ${m.live_hint || ""}`.trim());
    card.append(title, status, chart.el);
    lives[m.view_id] = { chart, status, pulse, ctl, stop };
    ctl.setLive(!m.replay); chart.live = !m.replay;
    cardRow(m, card);
  },
  live_status(m) {
    const l = lives[m.view_id]; if (!l) return;
    l.pulse.classList.add("off"); l.ctl.setLive(false, true); l.stop.remove(); l.chart.setLive(false);
    if (m.status === "moved") { l.pulse.textContent = "Continued below"; l.status.textContent = "The live stream continues in the card below."; }
    else {
      l.pulse.textContent = "Stopped";
      l.status.textContent = m.error ? `Stopped: ${m.error}` : `Stopped · ${m.samples} samples at ${m.rate_hz ?? 0} Hz.`;
    }
    if (m.last && !l.chart.pts.length) l.chart.push(m.ts, m.last, true);  // replay: show the last pose / values
  },
  ui_action_result(m) { toast(m.ok ? `${m.label}: done` : `${m.label} failed: ${m.error || "no reply"}`, !m.ok); },
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
    const txt = el("div", "next-text"); txt.append(el("span", null, "Let’s move onto the next sensor"));
    if (m.next_ports) {
      const free = m.next_ports.map((p) => `${p.interface.toUpperCase()} → ${p.port} (${p.pins})`);
      txt.append(el("span", "next-hint", free.length ? `Free probe cube ports: ${free.join(" · ")}` : "All probe cube lab ports are in use."));
    }
    next.append(txt, btn);
    append(m, next);
    if (!m.replay) { setQuick([]); setPhaseStatus(`${m.component_id} · all phases complete`, "good"); }
    updateComposer();
  },
};

// Sensor-declared views for a card; older saved projects only carry the motion chart spec.
function viewsOf(m) { return m.views || (m.chart?.kind === "motion" ? [{ kind: "motion", ...m.chart }] : null); }

// Buttons on a live card: the sim bench action (Simulate motion / tilt) and sensor UI actions (e.g. Zero yaw).
function liveControls(m) {
  const nodes = [], buttons = [];
  let live = false;
  const sim = m.sim_action || (m.chart?.kind === "motion" ? { label: "Simulate motion", busy_label: "Moving…", seconds: 2.5 } : null);
  if (sim && m.sim && !m.replay) {
    const btn = el("button", "btn ghost small sim-motion", sim.label); btn.type = "button";
    btn.onclick = () => {
      send({ type: "sim_motion", seconds: sim.seconds }); btn.disabled = true; btn.textContent = sim.busy_label || "Running…";
      setTimeout(() => { btn.textContent = sim.label; btn.disabled = !live; }, sim.seconds * 1000);
    };
    nodes.push(btn); buttons.push(btn);
  }
  if (!m.replay) for (const [action, label] of Object.entries(m.ui_actions || {})) {
    const btn = el("button", "btn ghost small", label); btn.type = "button";
    btn.onclick = () => send({ type: "ui_action", action });
    nodes.push(btn); buttons.push(btn);
  }
  // final: the card is finished (test done, stream stopped), so its buttons are removed rather than greyed out
  const setLive = (on, final = false) => { live = on; for (const b of buttons) { b.disabled = !on; if (final) b.remove(); } };
  setLive(false);
  return { nodes, setLive };
}

const SETTING_LABELS = { baud: "Baud rate", data_bits: "Data bits", parity: "Parity", stop_bits: "Stop bits", rate_hz: "Output rate",
  rate_ms: "Stream period", mount: "Mounting", port: "Port" };
function settingLabel(k) { return SETTING_LABELS[k] || prettyName(k); }
function settingValue(k, v) {
  if (k === "parity") return { n: "None", e: "Even", o: "Odd" }[String(v).toLowerCase()] || String(v);
  if (k.endsWith("_hz")) return `${v} Hz`;
  if (k.endsWith("_ms")) return `${v} ms`;
  if (k === "mount") return prettyName(String(v));
  return String(v);
}
function prettyName(s) { const t = String(s).replace(/_/g, " "); return t.charAt(0).toUpperCase() + t.slice(1); }

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
  box(16, m.sensor, "Sensor"); box(384, "Probe Cube", m.port ? `port ${m.port}` : m.board?.split(" (")[0] ?? "", true);
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
      g.append(svg("text", { x: 392, y: y + 4, class: "schem-pin" }, w.mcu_function ? `${w.cube_port} · ${w.mcu_function}` : w.cube_port));
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

// ---- live views: declared per sensor (sensor.json "views"), one row per view over a shared time axis ----
// kinds: timeseries (fields in one plot), motion (level + flag bands), attitude (live 3D plane), stat (latest value).
// Without views every field gets its own small chart.
const SERIES = ["#3987e5", "#d95926", "#199e70"];  // categorical slots 1-3 (dark-surface steps), assigned in field order
const MAX_PTS = 3000;
class LiveView {
  constructor({ seconds = 10, units = {}, views = null, window = null } = {}) {
    this.seconds = seconds; this.units = units; this.spec = views?.length ? views : null; this.window = window;
    this.t0 = null; this.pts = []; this.fields = []; this.hoverT = null; this.hoverView = null;
    this.live = false;  // while data streams in, the plane always shows the latest sample (hover only scrubs charts)
    this.view = "chart"; this.width = 560; this.raf = null; this.attitudes = new Map();
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
    if (this.pts.length > MAX_PTS) this.pts.splice(0, this.pts.length - MAX_PTS);
    if (ts === null) this.pts.forEach((p, i, a) => { p.t = a.length > 1 ? (i / (a.length - 1)) * this.seconds : 0; });
    for (const k of Object.keys(values)) if (!this.fields.includes(k)) this.fields.push(k);
    if (!this.raf) this.raf = requestAnimationFrame(() => { this.raf = null; this.render(); });
  }
  views() { return this.spec || this.fields.map((f) => ({ kind: "timeseries", title: f, fields: [f] })); }
  // x domain: the whole test, or the last `window` seconds for an open-ended live monitor
  domain() {
    const last = this.pts.length ? this.pts[this.pts.length - 1].t : 0;
    if (this.window) { const hi = Math.max(this.window, last); return [hi - this.window, hi]; }
    return [0, Math.max(this.seconds, last)];
  }
  nearest(t) {
    let best = 0;
    for (let i = 1; i < this.pts.length; i++) if (Math.abs(this.pts[i].t - t) < Math.abs(this.pts[best].t - t)) best = i;
    return best;
  }
  // Hover is kept as a time (not an index), so it stays on the same moment while live data keeps arriving.
  hovered() { return this.hoverT !== null && this.pts.length ? this.pts[this.nearest(this.hoverT)] : null; }
  setLive(on) { this.live = on; this.render(); }
  bindHover(s, idx, PAD, pw) {
    const [d0, d1] = this.domain();
    const move = (cx) => { const r = s.getBoundingClientRect(); this.hoverView = idx; this.hoverT = d0 + ((cx - r.left - PAD.l) / pw) * (d1 - d0); this.render(); };
    s.onpointermove = (e) => move(e.clientX);
    s.onpointerleave = () => { this.hoverT = null; this.render(); };
    s.onkeydown = (e) => {
      const n = this.pts.length - 1, h = this.hoverT === null ? n : this.nearest(this.hoverT);
      if (e.key === "ArrowLeft") this.hoverT = this.pts[Math.max(0, h - 1)].t;
      else if (e.key === "ArrowRight") this.hoverT = this.pts[Math.min(n, h + 1)].t;
      else if (e.key === "Escape") this.hoverT = null; else return;
      this.hoverView = idx; e.preventDefault(); this.render(); this.rows.querySelector(`[data-view="${idx}"] svg`)?.focus();
    };
  }
  render() {
    this.rows.classList.toggle("hidden", this.view !== "chart");
    this.table.classList.toggle("hidden", this.view !== "table");
    if (this.view === "table") return this.renderTable();
    this.tip.classList.add("hidden");
    if (!this.pts.length) { this.rows.replaceChildren(el("div", "viz-wait", "Waiting for samples…")); return; }
    const views = this.views(), lastSeries = views.map((v) => v.kind).lastIndexOf("timeseries");
    const rows = views.map((v, idx) => {
      const row = v.kind === "attitude" ? this.renderAttitude(v, idx) : v.kind === "motion" ? this.renderMotion(v, idx)
        : v.kind === "stat" ? this.renderStat(v) : this.renderSeries(v, idx, idx === lastSeries);
      if (row) row.dataset.view = idx;
      return row;
    }).filter(Boolean);
    this.rows.replaceChildren(...rows);
    this.renderTip(views);
  }
  renderTip(views) {
    const hov = this.hovered(), v = views[this.hoverView];
    if (!hov || !v || v.kind === "attitude" || v.kind === "stat") return;
    const [d0, d1] = this.domain(), x = 52 + ((hov.t - d0) / (d1 - d0)) * (this.width - 52 - 96);
    if (v.kind === "motion") {
      const line = el("div", "v"); line.append(el("span", "key"), "Level ", el("b", null, fmt(hov.v[v.level] ?? 0)));
      this.tip.replaceChildren(el("div", "k", `${hov.t.toFixed(1)} s · ${+hov.v[v.flag] === 1 ? "Motion" : "Clear"}`), line);
    } else {
      const fields = v.fields.filter((f) => f in hov.v);
      this.tip.replaceChildren(el("div", "k", `${hov.t.toFixed(1)} s`), ...fields.map((f, i) => {
        const line = el("div", "v"), key = el("span", "key");
        if (fields.length > 1) key.style.background = SERIES[i % SERIES.length];
        line.append(key, `${f} `, el("b", null, fmt(hov.v[f])), el("span", "u", ` ${this.units[f] || ""}`)); return line;
      }));
    }
    this.tip.style.left = `${Math.min(this.width - 160, Math.max(0, x + 12))}px`; this.tip.classList.remove("hidden");
  }
  // One plot per view. Several fields share the view's y-scale (same unit) and get fixed series colors.
  renderSeries(v, idx, xLabels) {
    const fields = v.fields.filter((f) => this.fields.includes(f));
    if (!fields.length) return null;
    const multi = fields.length > 1, W = this.width, PAD = { l: 52, r: 96, t: 10, b: 6 }, PH = multi ? 112 : 84, XB = 22;
    const [d0, d1] = this.domain(), span = d1 - d0, pw = W - PAD.l - PAD.r;
    const pts = this.pts.filter((p) => p.t >= d0 - 1e-9);
    let lo = Infinity, hi = -Infinity;
    for (const p of pts) for (const f of fields) if (f in p.v) { lo = Math.min(lo, p.v[f]); hi = Math.max(hi, p.v[f]); }
    if (!Number.isFinite(lo)) return null;
    if (v.min_span && hi - lo < v.min_span) { const mid = (hi + lo) / 2; lo = mid - v.min_span / 2; hi = mid + v.min_span / 2; }
    const yt = niceTicks(lo, hi, 4), y0 = yt.ticks[0], y1 = yt.ticks[yt.ticks.length - 1];
    const H = PAD.t + PH + PAD.b + (xLabels ? XB : 0), base = PAD.t + PH;
    const x = (t) => PAD.l + ((t - d0) / span) * pw, y = (val) => PAD.t + (1 - (val - y0) / (y1 - y0)) * PH;
    const s = svg("svg", { width: W, height: H, class: "viz-svg", role: "img", tabindex: 0, "aria-label": `${v.title || fields.join(", ")} over time` });
    for (const val of yt.ticks) {
      s.append(svg("line", { x1: PAD.l, x2: PAD.l + pw, y1: y(val), y2: y(val), class: val === y0 ? "viz-axisline" : "viz-grid" }));
      s.append(svg("text", { x: PAD.l - 8, y: y(val) + 4, "text-anchor": "end", class: "viz-tick" }, val.toFixed(yt.decimals)));
    }
    if (xLabels) {
      const xt = niceTicks(d0, d1, 6);
      for (const t of xt.ticks.filter((t) => t >= d0 - 1e-9 && t <= d1 + 1e-9)) s.append(svg("text", { x: x(t), y: H - 6, "text-anchor": "middle", class: "viz-tick" }, `${t.toFixed(xt.decimals)} s`));
    }
    // Lines only: the y-axis is fitted to the data (not zero-based), so an area fill would exaggerate the change.
    const ends = [];
    fields.forEach((f, i) => {
      const series = pts.filter((p) => f in p.v);
      if (!series.length) return;
      const color = multi ? SERIES[i % SERIES.length] : null;
      const path = svg("path", { d: series.map((p, j) => `${j ? "L" : "M"}${x(p.t).toFixed(1)} ${y(p.v[f]).toFixed(1)}`).join(" "), class: "viz-line" });
      if (color) path.style.stroke = color;
      s.append(path);
      const last = series[series.length - 1];
      ends.push({ f, color, cx: x(last.t), cy: y(last.v[f]), val: last.v[f] });
    });
    // end dots + direct labels; labels are nudged apart so close series stay readable
    const labels = [...ends].sort((a, b) => a.cy - b.cy);
    labels.forEach((e, i) => { e.ly = Math.max(e.cy, i ? labels[i - 1].ly + 13 : -Infinity); });
    for (let i = labels.length - 1, lim = base; i >= 0; i--) { labels[i].ly = Math.min(labels[i].ly, lim); lim = labels[i].ly - 13; }
    for (const e of ends) {
      const dot = svg("circle", { cx: e.cx, cy: e.cy, r: 4.5, class: "viz-dot" }); if (e.color) dot.style.fill = e.color;
      s.append(svg("circle", { cx: e.cx, cy: e.cy, r: 6.5, class: "viz-ring" }), dot);
      s.append(svg("text", { x: e.cx + 12, y: e.ly + 4, class: "viz-endlabel" }, multi ? `${e.f} ${fmt(e.val)}` : `${fmt(e.val)} ${this.units[e.f] || ""}`));
    }
    const hov = this.hovered();
    if (hov && hov.t >= d0) {
      s.append(svg("line", { x1: x(hov.t), x2: x(hov.t), y1: PAD.t, y2: base, class: "viz-cross" }));
      fields.forEach((f, i) => {
        if (!(f in hov.v)) return;
        const dot = svg("circle", { cx: x(hov.t), cy: y(hov.v[f]), r: 4.5, class: "viz-dot" }); if (multi) dot.style.fill = SERIES[i % SERIES.length];
        s.append(svg("circle", { cx: x(hov.t), cy: y(hov.v[f]), r: 6.5, class: "viz-ring" }), dot);
      });
    }
    this.bindHover(s, idx, PAD, pw);
    const row = el("div", "viz-row"), head = el("div", "viz-head");
    head.append(el("span", "viz-name", v.title || fields[0]), el("span", "viz-unit", v.unit ?? this.units[fields[0]] ?? ""));
    if (multi) {
      const legend = el("span", "viz-legend");
      fields.forEach((f, i) => { const k = el("span", "key line"); k.style.background = SERIES[i % SERIES.length]; legend.append(k, f); });
      head.append(legend);
    }
    row.append(head, s);
    return row;
  }
  // Motion sensor: presence level on a fixed 0-1 axis, with bands where the motion flag is set.
  renderMotion(v, idx) {
    const { level, flag } = v, [d0, d1] = this.domain(), pts = this.pts.filter((p) => level in p.v && p.t >= d0 - 1e-9);
    if (!pts.length) return null;
    const W = this.width, PAD = { l: 40, r: 110, t: 12, b: 26 }, PH = 132, H = PAD.t + PH + PAD.b;
    const pw = W - PAD.l - PAD.r, span = d1 - d0;
    const x = (t) => PAD.l + ((t - d0) / span) * pw, y = (val) => PAD.t + (1 - Math.min(1, Math.max(0, val))) * PH;
    const s = svg("svg", { width: W, height: H, class: "viz-svg", role: "img", tabindex: 0, "aria-label": "Presence level over time, motion periods shaded" });
    // motion bands first so the line draws over them
    let start = null;
    pts.forEach((p, i) => {
      const on = +p.v[flag] === 1, next = pts[i + 1];
      if (on && start === null) start = p.t;
      if (start !== null && (!on || !next)) {
        s.append(svg("rect", { x: x(start), y: PAD.t, width: Math.max(2, x(p.t) - x(start)), height: PH, class: "viz-band" }));
        start = null;
      }
    });
    for (const val of [0, 0.5, 1]) {
      s.append(svg("line", { x1: PAD.l, x2: PAD.l + pw, y1: y(val), y2: y(val), class: val === 0 ? "viz-axisline" : "viz-grid" }));
      s.append(svg("text", { x: PAD.l - 8, y: y(val) + 4, "text-anchor": "end", class: "viz-tick" }, val.toFixed(1)));
    }
    const xt = niceTicks(d0, d1, 6);
    for (const t of xt.ticks.filter((t) => t >= d0 - 1e-9 && t <= d1 + 1e-9)) s.append(svg("text", { x: x(t), y: H - 7, "text-anchor": "middle", class: "viz-tick" }, `${t.toFixed(xt.decimals)} s`));
    const d = pts.map((p, i) => `${i ? "L" : "M"}${x(p.t).toFixed(1)} ${y(p.v[level]).toFixed(1)}`).join(" ");
    s.append(svg("path", { d: `${d} L${x(pts[pts.length - 1].t).toFixed(1)} ${y(0)} L${x(pts[0].t).toFixed(1)} ${y(0)} Z`, class: "viz-area" }));
    s.append(svg("path", { d, class: "viz-line" }));
    const last = pts[pts.length - 1], moving = +last.v[flag] === 1;
    s.append(svg("circle", { cx: x(last.t), cy: y(last.v[level]), r: 6.5, class: "viz-ring" }), svg("circle", { cx: x(last.t), cy: y(last.v[level]), r: 4.5, class: "viz-dot" }));
    s.append(svg("text", { x: x(last.t) + 12, y: y(last.v[level]) + 4, class: "viz-endlabel" }, `${moving ? "Motion" : "Clear"} · ${last.v[level].toFixed(2)}`));
    const hov = this.hovered();
    if (hov && level in hov.v && hov.t >= d0) {
      s.append(svg("line", { x1: x(hov.t), x2: x(hov.t), y1: PAD.t, y2: PAD.t + PH, class: "viz-cross" }));
      s.append(svg("circle", { cx: x(hov.t), cy: y(hov.v[level]), r: 6.5, class: "viz-ring" }), svg("circle", { cx: x(hov.t), cy: y(hov.v[level]), r: 4.5, class: "viz-dot" }));
    }
    this.bindHover(s, idx, PAD, pw);
    const head = el("div", "viz-head"), legend = el("span", "viz-legend");
    legend.append(el("span", "key line"), "Presence level", el("span", "key band"), "Motion detected");
    head.append(el("span", "viz-name", v.title || "Presence"), legend);
    const row = el("div", "viz-row"); row.append(head, s);
    return row;
  }
  // Live 3D model of the sensor's attitude. Kept across renders (it animates on its own); follows the hovered sample.
  renderAttitude(v, idx) {
    let a = this.attitudes.get(idx);
    if (!a) { a = new AttitudeView(v, this.units); this.attitudes.set(idx, a); }
    const hov = this.live ? null : this.hovered(), p = hov || this.pts[this.pts.length - 1];
    a.resize(this.width);
    a.set(p.v, !!hov, hov ? hov.t : null);
    return a.row;
  }
  renderStat(v) {
    const [d0] = this.domain(), pts = this.pts.filter((p) => p.t >= d0 - 1e-9);
    const row = el("div", "viz-row viz-stat"), head = el("div", "viz-head"), body = el("div", "stat-body");
    head.append(el("span", "viz-name", v.title || v.fields.join(", ")));
    for (const f of v.fields) {
      const vals = pts.filter((p) => f in p.v).map((p) => p.v[f]);
      if (!vals.length) continue;
      const tile = el("div", "stat-tile"), big = el("div", "stat-value");
      big.append(fmt(vals[vals.length - 1]), el("span", "stat-unit", ` ${this.units[f] || ""}`));
      tile.append(big, el("div", "stat-sub", `min ${fmt(Math.min(...vals))} · max ${fmt(Math.max(...vals))}`));
      body.append(tile);
    }
    if (!body.children.length) return null;
    row.append(head, body);
    return row;
  }
  renderTable() {
    const motion = this.spec?.length === 1 && this.spec[0].kind === "motion" ? this.spec[0] : null;
    if (motion) {
      const { level, flag } = motion, t = el("table", "events"), head = el("tr");
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

// ---- attitude view: a low-poly plane drawn on a canvas, rotated by the sensor's roll/pitch/yaw ----
// Sensor/body frame: X forward (nose), Y left, Z up. R = Rz(yaw)·Ry(pitch)·Rx(roll), angles in degrees.
const PLANE = (() => {
  const N = [1.5, 0, 0], T = [-1.5, 0, 0.06];
  const A = [[0.9, 0, 0.16], [0.9, 0.16, 0], [0.9, 0, -0.14], [0.9, -0.16, 0]];
  const B = [[-0.8, 0, 0.12], [-0.8, 0.1, 0.02], [-0.8, 0, -0.08], [-0.8, -0.1, 0.02]];
  const C = { body: [217, 221, 230], nose: [247, 216, 120], port: [242, 139, 130], star: [160, 195, 133], tail: [201, 206, 216], fin: [150, 160, 178] };
  const quad = (q, c) => [[q[0], q[1], q[2], c], [q[0], q[2], q[3], c]];
  const mirror = (pts) => pts.map(([x, y, z]) => [x, -y, z]);
  const faces = [];
  for (let i = 0; i < 4; i++) {
    const j = (i + 1) % 4;
    faces.push([N, A[i], A[j], C.nose], ...quad([A[i], A[j], B[j], B[i]], C.body), [T, B[j], B[i], C.body]);
  }
  const wing = [[0.45, 0.14, 0], [0.05, 1.55, 0.04], [-0.2, 1.55, 0.04], [-0.35, 0.14, 0]];
  const stab = [[-1.05, 0.08, 0.06], [-1.3, 0.6, 0.07], [-1.45, 0.6, 0.07], [-1.42, 0.08, 0.06]];
  faces.push(...quad(wing, C.port), ...quad(mirror(wing), C.star), ...quad(stab, C.tail), ...quad(mirror(stab), C.tail));
  faces.push(...quad([[-1.0, 0, 0.1], [-1.38, 0, 0.62], [-1.5, 0, 0.62], [-1.48, 0, 0.08]], C.fin));
  return faces;
})();
let ATT_CAM_AZ = -35;  // chase camera azimuth (deg): 0 = straight behind, -35 = 3/4 rear view from the left
const wrap180 = (a) => ((a + 180) % 360 + 360) % 360 - 180;
class AttitudeView {
  constructor(spec, units) {
    this.spec = spec; this.units = units; this.target = [0, 0, 0]; this.cur = null; this.raw = [0, 0, 0];
    this.level = [0, 0]; this.W = 560; this.H = 240; this.anim = null; this.last = 0;
    this.row = el("div", "viz-row attitude");
    const head = el("div", "viz-head"), legend = el("span", "viz-legend");
    for (const [cls, label] of [["port", "Left wing"], ["star", "Right wing"], ["nose", "Nose"]]) legend.append(el("span", `key sw ${cls}`), label);
    head.append(el("span", "viz-name", spec.title || "Orientation"), legend);
    this.canvas = el("canvas", "att-canvas"); this.canvas.setAttribute("role", "img");
    this.read = el("div", "att-read");
    this.vals = ["Roll", "Pitch", "Yaw"].map((name) => { const b = el("b"); const d = el("div", "att-val"); d.append(el("span", "muted", name), b); this.read.append(d); return b; });
    this.levelBtn = el("button", "btn ghost small", "Level here"); this.levelBtn.type = "button";
    this.levelBtn.title = "Treat the current roll/pitch as level in this view (display only)";
    this.levelBtn.onclick = () => {
      this.level = this.level[0] || this.level[1] ? [0, 0] : [this.raw[0], this.raw[1]];
      this.levelBtn.textContent = this.level[0] || this.level[1] ? "Reset level" : "Level here"; this.kick();
    };
    this.at = el("span", "muted att-at");
    this.read.append(this.at, this.levelBtn);
    this.row.append(head, this.canvas, this.read);
  }
  resize(w) {
    if (w === this.W && this.canvas.width) return;
    this.W = w; const dpr = window.devicePixelRatio || 1;
    this.canvas.width = Math.round(w * dpr); this.canvas.height = Math.round(this.H * dpr);
    this.canvas.style.width = `${w}px`; this.canvas.style.height = `${this.H}px`;
    this.kick();
  }
  set(values, immediate = false, at = null) {
    const { roll, pitch, yaw } = this.spec;
    this.at.textContent = at === null ? "" : `at ${at.toFixed(1)} s`;
    this.raw = [values[roll] ?? 0, values[pitch] ?? 0, values[yaw] ?? 0];
    this.target = [this.raw[0] - this.level[0], this.raw[1] - this.level[1], this.raw[2]];
    if (!this.cur || immediate) this.cur = [...this.target];
    this.vals.forEach((b, i) => { b.textContent = `${this.target[i].toFixed(1)}°`; });
    this.canvas.setAttribute("aria-label", `Orientation: roll ${this.target[0].toFixed(1)}°, pitch ${this.target[1].toFixed(1)}°, yaw ${this.target[2].toFixed(1)}°`);
    this.kick();
  }
  kick() { if (!this.anim) { this.last = performance.now(); this.anim = requestAnimationFrame((t) => this.frame(t)); } }
  frame(now) {
    this.anim = null;
    const k = 1 - Math.exp(-Math.max(0, now - this.last) / 70); this.last = now;  // ease toward the latest sample (~70 ms)
    let moving = false;
    this.cur = this.cur.map((c, i) => { const d = wrap180(this.target[i] - c); if (Math.abs(d) > 0.05) moving = true; return c + d * k; });
    this.draw();
    if (moving) this.anim = requestAnimationFrame((t) => this.frame(t));
  }
  draw() {
    const ctx = this.canvas.getContext("2d"), dpr = window.devicePixelRatio || 1, W = this.W, H = this.H;
    if (!this.cur) return;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0); ctx.clearRect(0, 0, W, H);
    const rad = Math.PI / 180, [r, p, y] = this.cur.map((a) => a * rad);
    const cr = Math.cos(r), sr = Math.sin(r), cp = Math.cos(p), sp = Math.sin(p), cy = Math.cos(y), sy = Math.sin(y);
    const R = [[cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr],
               [sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr],
               [-sp, cp * sr, cp * cr]];
    const rot = (v) => R.map((row) => row[0] * v[0] + row[1] * v[1] + row[2] * v[2]);
    // chase camera: behind the model, a little above, swung ATT_CAM_AZ degrees around; screen right = -Y, up = Z, depth = X
    const az = ATT_CAM_AZ * rad, el_ = 20 * rad, D = 5.2, f = Math.min(W, H * 2.2) * 0.62;
    const cam = (v) => {
      const xr = v[0] * Math.cos(az) - v[1] * Math.sin(az), yr = v[0] * Math.sin(az) + v[1] * Math.cos(az);
      // camera above, tilted down by el_: farther points sit higher on screen, higher points are nearer
      const right = -yr, up = v[2] * Math.cos(el_) + xr * Math.sin(el_), depth = xr * Math.cos(el_) - v[2] * Math.sin(el_);
      const z = depth + D;
      return [W / 2 + (f * right) / z, H * 0.52 - (f * up) / z, z];
    };
    // ground grid (fixed in the world) and the plane's shadow on it
    const G = -0.95;
    ctx.lineWidth = 1; ctx.strokeStyle = "#ffffff14";
    for (let i = -2; i <= 2; i += 0.5) {
      for (const [a, b] of [[[i, -2, G], [i, 2, G]], [[-2, i, G], [2, i, G]]]) {
        const pa = cam(a), pb = cam(b); ctx.beginPath(); ctx.moveTo(pa[0], pa[1]); ctx.lineTo(pb[0], pb[1]); ctx.stroke();
      }
    }
    const fwd = [cam([2.3, 0, G]), cam([2.0, 0.12, G]), cam([2.0, -0.12, G])];  // world X (yaw 0) marker
    ctx.fillStyle = "#ffffff30"; ctx.beginPath(); fwd.forEach(([sx, sy2], i) => (i ? ctx.lineTo(sx, sy2) : ctx.moveTo(sx, sy2))); ctx.fill();
    const faces = PLANE.map(([a, b, c, color]) => {
      const w = [rot(a), rot(b), rot(c)];
      const n = cross(sub3(w[1], w[0]), sub3(w[2], w[0]));
      return { w, s: w.map(cam), color, n };
    });
    ctx.fillStyle = "#00000059";
    for (const fc of faces) {
      const sh = fc.w.map(([x, yy]) => cam([x, yy, G]));
      ctx.beginPath(); sh.forEach(([sx, sy2], i) => (i ? ctx.lineTo(sx, sy2) : ctx.moveTo(sx, sy2))); ctx.closePath(); ctx.fill();
    }
    const L = norm3([0.35, -0.5, 0.8]);
    faces.sort((a, b) => (b.s[0][2] + b.s[1][2] + b.s[2][2]) - (a.s[0][2] + a.s[1][2] + a.s[2][2]));  // far to near
    for (const fc of faces) {
      const nn = norm3(fc.n), shade = 0.42 + 0.58 * Math.abs(nn[0] * L[0] + nn[1] * L[1] + nn[2] * L[2]);
      ctx.fillStyle = `rgb(${fc.color.map((c) => Math.round(c * shade)).join(",")})`;
      ctx.beginPath(); fc.s.forEach(([sx, sy2], i) => (i ? ctx.lineTo(sx, sy2) : ctx.moveTo(sx, sy2))); ctx.closePath(); ctx.fill();
      ctx.strokeStyle = ctx.fillStyle; ctx.lineWidth = 0.6; ctx.stroke();  // hide hairline seams between faces
    }
  }
}
function sub3(a, b) { return [a[0] - b[0], a[1] - b[1], a[2] - b[2]]; }
function cross(a, b) { return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]; }
function norm3(v) { const l = Math.hypot(...v) || 1; return v.map((x) => x / l); }
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
function fmt(v) { const a = Math.abs(v); return a >= 10000 ? v.toFixed(0) : a >= 10 ? v.toFixed(1) : v.toFixed(2); }

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
