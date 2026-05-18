(() => {
  const TOKEN_KEY = "research_token";
  const BRANCH_PREFILL_KEY = "branch_prefill";  // sessionStorage handoff to viewAsk
  const COLLAPSE_KEY = "threads_collapsed";    // sessionStorage tree-collapse state
  const $ = (id) => document.getElementById(id);
  const gate = $("gate"), appEl = $("app"), view = $("view");
  let token = localStorage.getItem(TOKEN_KEY) || "";

  const esc = (s) => (s == null ? "" : String(s)).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
  const fmtDate = (iso) => { if (!iso) return ""; try { return new Date(iso).toLocaleString(); } catch { return iso; } };

  function showGate() {
    gate.hidden = false; appEl.hidden = true;
    $("token-input").value = "";
    setTimeout(() => $("token-input").focus(), 50);
  }
  function showApp() {
    gate.hidden = true; appEl.hidden = false;
    if (!location.hash) location.hash = "#/ask";
    else route();
  }

  $("token-save").addEventListener("click", () => {
    const t = $("token-input").value.trim();
    if (!t) return;
    token = t; localStorage.setItem(TOKEN_KEY, t);
    showApp();
  });
  $("token-input").addEventListener("keydown", (e) => { if (e.key === "Enter") $("token-save").click(); });
  $("logout").addEventListener("click", () => { localStorage.removeItem(TOKEN_KEY); token = ""; showGate(); });

  async function api(path, opts = {}) {
    const headers = Object.assign({}, opts.headers || {}, { "Authorization": "Bearer " + token });
    if (opts.body && !headers["Content-Type"]) headers["Content-Type"] = "application/json";
    const res = await fetch(path, { ...opts, headers });
    if (res.status === 401 || res.status === 403) {
      localStorage.removeItem(TOKEN_KEY); token = ""; showGate();
      throw new Error("unauthorized");
    }
    if (!res.ok) {
      let msg = `${res.status}`;
      try { const t = await res.text(); msg += ` ${t.slice(0, 200)}`; } catch {}
      throw new Error(msg);
    }
    const ct = res.headers.get("content-type") || "";
    return ct.includes("application/json") ? res.json() : res.text();
  }

  function toast(msg, kind = "") {
    const t = document.createElement("div");
    t.className = "toast " + kind;
    t.textContent = msg;
    document.body.appendChild(t);
    setTimeout(() => t.remove(), 4000);
  }

  function setLoading() { view.innerHTML = `<div class="meta"><span class="spinner"></span>loading…</div>`; }

  function _parseReportPayload(answer) {
    if (!answer) return null;
    try {
      const j = JSON.parse(answer);
      if (j && j.shape === "report") return j;
    } catch (_) {}
    return null;
  }

  function _citationCard(c, i) {
    const n = c.n ?? (i + 1);
    const titleOrQuery = c.title || c.query || "";
    const link = c.url
      ? `<a href="${esc(c.url)}" target="_blank" rel="noopener">${esc(titleOrQuery)}</a>`
      : esc(titleOrQuery);
    const threadLink = c.thread_id ? ` · <a href="#/threads/${encodeURIComponent(c.thread_id)}">thread</a>` : "";
    const scoreLabel = c.score != null ? ` · ${c.score.toFixed(3)}` : "";
    return `
      <div class="card" style="margin-top:6px">
        <div class="meta">[${n}] ${link}${threadLink}${scoreLabel}</div>
        ${c.snippet ? `<pre>${esc(c.snippet)}</pre>` : ""}
      </div>`;
  }

  function renderReport(report) {
    const toc = (report.toc || []).map((h, i) => `<li><a href="#sec-${i}" data-toc-target="sec-${i}">${esc(h)}</a></li>`).join("");
    const sections = (report.sections || []).map((s, i) => {
      const cites = (s.citations || []).map(_citationCard).join("");
      return `
        <div class="card" style="margin-top:10px">
          <h3 id="sec-${i}" style="margin:0 0 6px">${esc(s.heading || "")}</h3>
          <div class="report">${marked.parse(s.body || "")}</div>
          ${cites ? `<details style="margin-top:8px"><summary class="meta">Citations (${s.citations.length})</summary>${cites}</details>` : ""}
        </div>`;
    }).join("");
    const termLabel = {
      empty_gaps: "report complete (no remaining gaps)",
      iteration_cap: "stopped at iteration cap — partial report",
      token_cap: "stopped at token cap — partial report",
    }[report.termination] || (report.termination ? String(report.termination) : "");
    return `
      <div class="card" style="margin-top:12px">
        <div class="meta">Table of contents</div>
        <ol style="margin:6px 0 0 18px">${toc}</ol>
      </div>
      ${sections}
      ${termLabel ? `<div class="meta" style="margin-top:10px">${esc(termLabel)}</div>` : ""}`;
  }

  function renderAskAnswer(syn) {
    if (!syn) return "";
    if (syn.report) return renderReport(syn.report);
    const reportFromAnswer = _parseReportPayload(syn.answer);
    if (reportFromAnswer) return renderReport(reportFromAnswer);
    if (!syn.answer) return "";
    const cites = (syn.citations || []).map(_citationCard).join("");
    return `
      <div class="report" style="margin-top:12px">${marked.parse(syn.answer)}</div>
      ${cites ? `<details style="margin-top:8px"><summary class="meta">Citations (${syn.citations.length})</summary>${cites}</details>` : ""}`;
  }

  async function pollAskRun(runId, { intervalMs = 2000, maxMs = 240000, onStatus = null } = {}) {
    const start = Date.now();
    while (Date.now() - start < maxMs) {
      const r = await api(`/ask_runs/${encodeURIComponent(runId)}`);
      if (r.status === "complete" || r.status === "failed" || r.status === "expired") return r;
      if (typeof onStatus === "function") {
        try { onStatus(r); } catch (_) {}
      }
      await new Promise((res) => setTimeout(res, intervalMs));
    }
    throw new Error("ask polling timed out");
  }

  async function submitAsk({ thread_id = null, q, mode, depth = "standard",
                             parent_thread_id = null, parent_turn_id = null, parent_quote = null,
                             triggerEl, statusEl, answerEl, badgeEl }) {
    // Branched requests always POST /ask (the webhook forces a new thread); regular
    // continuations POST /threads/{id}/ask. Never both at once — the validator rejects.
    const isBranch = !!(parent_thread_id && parent_turn_id);
    const path = (isBranch || !thread_id)
      ? "/ask"
      : `/threads/${encodeURIComponent(thread_id)}/ask`;
    if (triggerEl) triggerEl.disabled = true;
    statusEl.innerHTML = `<span class="spinner"></span>queuing…`;
    if (badgeEl) badgeEl.textContent = "";
    if (answerEl) answerEl.innerHTML = "";
    const pollMs = depth === "deep" ? 600000 : 240000;
    try {
      let r;
      try {
        const body = { question: q, mode, depth };
        if (isBranch) {
          body.parent_thread_id = parent_thread_id;
          body.parent_turn_id = parent_turn_id;
          if (parent_quote) body.parent_quote = parent_quote;
        }
        r = await api(path, { method: "POST", body: JSON.stringify(body) });
      } catch (e) {
        const msg = e.message || "";
        if (/deep_daily_cap/i.test(msg)) {
          statusEl.textContent = "daily deep-run cap reached for this bearer";
          toast("deep daily cap reached", "warn");
        } else {
          toast(msg, "err"); statusEl.textContent = "";
        }
        return null;
      }
      const initialLabel = r.status === "queued" ? "queued" : "ingesting…";
      statusEl.innerHTML = `<span class="spinner"></span>via ${esc(r.route)} · ${esc(r.depth || depth)} · ${initialLabel}`;
      try {
        const result = await pollAskRun(r.run_id, {
          maxMs: pollMs,
          onStatus(s) {
            if (s.status === "queued") {
              const pos = (s.queue_position ?? 0) + 1;
              const total = s.queue_total ?? 1;
              statusEl.innerHTML = `<span class="spinner"></span>queued · position ${pos} of ${total} · waiting for slot`;
            } else if (s.status === "running") {
              statusEl.innerHTML = `<span class="spinner"></span>via ${esc(s.route)} · ${esc(s.depth || depth)} · synthesizing…`;
            }
          },
        });
        if (result.status === "failed") {
          const msg = (result.errors && result.errors[0] && result.errors[0].message) || "ask failed";
          statusEl.textContent = `failed: ${msg}`;
          return null;
        }
        const n = (result.ingested || []).length;
        if (badgeEl) badgeEl.textContent = n > 0 ? `+${n} fetched` : "no new docs";
        statusEl.textContent = `via ${result.route} · ${r.depth || depth}`;
        if (answerEl) answerEl.innerHTML = renderAskAnswer(result.synthesis);
        return Object.assign({}, r, { result });
      } catch (e) { toast(e.message, "err"); statusEl.textContent = ""; return null; }
    } finally {
      if (triggerEl) triggerEl.disabled = false;
    }
  }

  async function viewAsk() {
    // Branch handoff: a Branch / "Ask about this" tap stashed parent context here.
    let prefill = null;
    try {
      const raw = sessionStorage.getItem(BRANCH_PREFILL_KEY);
      if (raw) {
        prefill = JSON.parse(raw);
        sessionStorage.removeItem(BRANCH_PREFILL_KEY);  // one-shot
      }
    } catch (_) { prefill = null; }
    const initialQ = prefill && prefill.seed ? prefill.seed : "";
    const branchBanner = prefill ? `
      <div class="parent-breadcrumb" style="margin-bottom:8px">
        Branching from <a href="#/threads/${encodeURIComponent(prefill.parent_thread_id)}">parent thread</a>
        ${prefill.parent_quote ? "· selection-level" : "· turn-level"}
      </div>
      ${prefill.parent_quote ? `<div class="parent-quote">${esc(prefill.parent_quote)}</div>` : ""}` : "";

    view.innerHTML = `
      <div class="card">
        <h3>Ask${prefill ? " — new branch" : ""}</h3>
        ${branchBanner}
        <div class="meta">Fetches sources, ingests them, then synthesizes a cited answer.</div>
        <textarea id="ask-q" placeholder="e.g. compare LightRAG and GraphRAG for evaluation robustness">${esc(initialQ)}</textarea>
        <div style="display:flex;align-items:center;gap:14px;flex-wrap:wrap;margin-top:8px">
          <span class="meta" style="font-weight:600">Depth:</span>
          <label class="meta" style="display:inline-flex;align-items:center;gap:4px"><input type="radio" name="ask-depth" value="standard" checked> standard — default, ~one search round</label>
          <label class="meta" style="display:inline-flex;align-items:center;gap:4px"><input type="radio" name="ask-depth" value="deep"> deep — minutes, sectioned report, higher cost</label>
        </div>
        <div style="display:flex;align-items:center;gap:14px;flex-wrap:wrap;margin-top:6px">
          <span class="meta" style="font-weight:600">Mode:</span>
          <label class="meta" style="display:inline-flex;align-items:center;gap:4px"><input type="radio" name="ask-mode" value="auto" checked> auto</label>
          <label class="meta" style="display:inline-flex;align-items:center;gap:4px"><input type="radio" name="ask-mode" value="cloud"> cloud</label>
          <label class="meta" style="display:inline-flex;align-items:center;gap:4px"><input type="radio" name="ask-mode" value="local"> my subscription</label>
        </div>
        <div class="row" style="margin-top:10px;gap:8px;align-items:center">
          <button id="ask-go">Ask</button>
          <span id="ask-status" class="meta"></span>
          <span id="ask-badge" class="meta"></span>
        </div>
      </div>
      <div id="ask-answer"></div>`;
    $("ask-go").addEventListener("click", async () => {
      const q = $("ask-q").value.trim(); if (!q) return;
      const mode = (document.querySelector('input[name="ask-mode"]:checked') || {}).value || "auto";
      const depth = (document.querySelector('input[name="ask-depth"]:checked') || {}).value || "standard";
      const branchParams = prefill ? {
        parent_thread_id: prefill.parent_thread_id,
        parent_turn_id: prefill.parent_turn_id,
        parent_quote: prefill.parent_quote || null,
      } : {};
      const r = await submitAsk({
        q, mode, depth, ...branchParams,
        triggerEl: $("ask-go"),
        statusEl: $("ask-status"),
        answerEl: $("ask-answer"),
        badgeEl: $("ask-badge"),
      });
      if (r && r.thread_id) {
        // Land the user inside the (possibly newly-branched) conversation.
        location.hash = `#/threads/${encodeURIComponent(r.thread_id)}`;
      }
    });
  }

  async function viewReports() {
    setLoading();
    try {
      const list = await api("/reports");
      if (!list.length) { view.innerHTML = `<p class="meta">No reports yet. Ask something.</p>`; return; }
      view.innerHTML = list.map((f) => `
        <a class="card" style="display:block" href="#/reports/${encodeURIComponent(f.file)}">
          <h3>${esc(f.file.replace(/\.md$/, ""))}</h3>
          <div class="meta">${esc(fmtDate(f.mtime))} · ${Math.round(f.size / 1024)} KB</div>
        </a>`).join("");
    } catch (e) { toast(e.message, "err"); view.innerHTML = ""; }
  }

  async function viewReportDetail(file) {
    setLoading();
    try {
      const md = await api("/reports/" + encodeURIComponent(file));
      view.innerHTML = `<a href="#/reports" class="meta">&larr; all reports</a>
        <div class="report" style="margin-top:10px">${marked.parse(md)}</div>`;
    } catch (e) { toast(e.message, "err"); view.innerHTML = ""; }
  }

  function hitCard(h) {
    return `
      <div class="card">
        <h3>${esc(h.query || "(no query)")}</h3>
        <div class="meta">
          score ${(h.score ?? 0).toFixed(3)}
          ${h.thread_id ? ` · <a href="#/threads/${encodeURIComponent(h.thread_id)}">thread</a>` : ""}
        </div>
        <pre>${esc(h.text)}</pre>
      </div>`;
  }

  async function viewSearch() {
    view.innerHTML = `
      <div class="card">
        <h3>Semantic search</h3>
        <div class="meta">Find excerpts across all reports (hybrid BM25 + vector + cross-encoder rerank).</div>
        <div class="row" style="margin-top:10px">
          <input id="s-q" placeholder="search term" style="flex:1">
          <button id="s-go">Go</button>
        </div>
      </div>
      <div id="s-hits"></div>`;
    const go = async () => {
      const q = $("s-q").value.trim(); if (!q) return;
      $("s-hits").innerHTML = `<div class="meta"><span class="spinner"></span></div>`;
      try {
        const hits = await api("/search2?q=" + encodeURIComponent(q) + "&k=10");
        if (!hits.length) { $("s-hits").innerHTML = `<p class="meta">No matches.</p>`; return; }
        $("s-hits").innerHTML = hits.map(hitCard).join("");
      } catch (e) { toast(e.message, "err"); $("s-hits").innerHTML = ""; }
    };
    $("s-go").addEventListener("click", go);
    $("s-q").addEventListener("keydown", (e) => { if (e.key === "Enter") go(); });
  }

  function _getCollapsed() {
    try { return new Set(JSON.parse(sessionStorage.getItem(COLLAPSE_KEY) || "[]")); }
    catch { return new Set(); }
  }
  function _setCollapsed(s) {
    sessionStorage.setItem(COLLAPSE_KEY, JSON.stringify([...s]));
  }

  function _buildThreadTree(list) {
    // Group children by parent_thread_id; identify roots (parent_thread_id null
    // OR parent thread not present in `list`, which can happen near the 50-item
    // limit — treat orphans as roots so they remain visible).
    const byId = {};
    list.forEach((t) => { byId[t.id] = t; });
    const childrenOf = {};
    const roots = [];
    list.forEach((t) => {
      const pid = t.parent_thread_id;
      if (pid && byId[pid]) {
        (childrenOf[pid] = childrenOf[pid] || []).push(t);
      } else {
        roots.push(t);
      }
    });
    return { roots, childrenOf };
  }

  function _renderThreadNode(t, depth, childrenOf, collapsed) {
    const kids = childrenOf[t.id] || [];
    const hasKids = kids.length > 0;
    const isCollapsed = collapsed.has(t.id);
    const toggle = hasKids
      ? `<button class="tree-toggle" data-toggle="${esc(t.id)}" title="${isCollapsed ? "expand" : "collapse"}">${isCollapsed ? "▶" : "▼"}</button>`
      : `<span class="tree-toggle placeholder">·</span>`;
    const indent = depth > 0 ? `style="padding-left:${depth * 18}px"` : "";
    const quoteTag = t.has_quote ? '<span class="has-quote-tag">selection</span>' : "";
    const node = `
      <div class="tree-row" ${indent}>
        ${toggle}
        <a class="card" style="flex:1;display:block;margin-bottom:8px" href="#/threads/${encodeURIComponent(t.id)}">
          <h3>${esc(t.title || "(untitled)")} ${quoteTag}</h3>
          <div class="meta">${esc(fmtDate(t.updated_at))} · ${t.turn_count} turn${t.turn_count === 1 ? "" : "s"}${hasKids ? ` · ${kids.length} branch${kids.length === 1 ? "" : "es"}` : ""}</div>
        </a>
      </div>`;
    const kidHtml = (hasKids && !isCollapsed)
      ? kids.map((k) => _renderThreadNode(k, depth + 1, childrenOf, collapsed)).join("")
      : "";
    return node + kidHtml;
  }

  async function viewThreads() {
    setLoading();
    try {
      const list = await api("/threads?limit=50");
      if (!list.length) { view.innerHTML = `<p class="meta">No conversations yet. Start one from the <a href="#/ask">Ask</a> tab.</p>`; return; }

      const { roots, childrenOf } = _buildThreadTree(list);
      const collapsed = _getCollapsed();
      view.innerHTML = roots.map((r) => _renderThreadNode(r, 0, childrenOf, collapsed)).join("");

      // Wire collapse/expand toggles. Each click flips the entry in sessionStorage
      // and re-renders just the threads tree.
      view.querySelectorAll("[data-toggle]").forEach((btn) => {
        btn.addEventListener("click", (ev) => {
          ev.preventDefault();
          ev.stopPropagation();
          const id = btn.dataset.toggle;
          const s = _getCollapsed();
          if (s.has(id)) s.delete(id); else s.add(id);
          _setCollapsed(s);
          viewThreads();  // re-render with new collapse state
        });
      });
    } catch (e) { toast(e.message, "err"); view.innerHTML = ""; }
  }

  function turnCard(turn) {
    const route = turn.route ? `<span class="meta">via ${esc(turn.route)}</span>` : "";
    const depthBadge = turn.depth && turn.depth !== "standard"
      ? `<span class="meta">· ${esc(turn.depth)}</span>` : "";
    const docCount = (turn.ingested_doc_ids || []).length;
    const ingest = docCount ? `<span class="meta">· +${docCount} fetched</span>` : "";
    const isReport = turn.payload_shape === "report";
    const reportObj = isReport ? _parseReportPayload(turn.answer_md) : null;

    // Answer body wrapped in [data-answer-body] so the selection listener can
    // scope itself to *just* the answer text (not question, not metadata).
    let body = "";
    if (reportObj) {
      body = `<div data-answer-body>${renderReport(reportObj)}</div>`;
    } else if (turn.answer_md) {
      body = `<div data-answer-body class="report" style="margin-top:8px">${marked.parse(turn.answer_md)}</div>`;
    } else {
      body = `<div class="meta" style="margin-top:8px">(no answer recorded)</div>`;
    }

    let citeBlock = "";
    if (!reportObj) {
      const cites = (turn.citations || []).length;
      if (cites) {
        citeBlock = `<details style="margin-top:8px"><summary class="meta">Citations (${cites})</summary>${
          (turn.citations || []).map(_citationCard).join("")
        }</details>`;
      }
    }

    // Branching is enabled when the user can identify a turn to fork from.
    // Disabled (button hidden) when window.THREAD_BRANCHING_ENABLED===false
    // (e.g. set by a future runtime config endpoint).
    const branchBtn = window.THREAD_BRANCHING_ENABLED === false
      ? ""
      : `<button class="branch-btn" data-act="branch" data-turn-id="${esc(turn.id || "")}" title="Fork a new thread from this turn">Branch</button>`;

    const anchorId = turn.id ? `id="turn-${esc(turn.id)}"` : "";
    return `
      <div class="card" data-turn="${esc(turn.id || "")}" ${anchorId}>
        <div class="row" style="justify-content:space-between;align-items:flex-start;gap:8px">
          <div class="meta" style="flex:1">${esc(fmtDate(turn.created_at))} ${route} ${depthBadge} ${ingest}</div>
          ${branchBtn}
        </div>
        <div style="margin-top:6px"><strong>Q:</strong> ${esc(turn.question || "")}</div>
        ${body}
        ${citeBlock}
      </div>`;
  }

  function _stashBranchAndGo({ parent_thread_id, parent_turn_id, parent_quote, seed }) {
    sessionStorage.setItem(BRANCH_PREFILL_KEY, JSON.stringify({
      parent_thread_id, parent_turn_id,
      parent_quote: parent_quote || null,
      seed: seed || "",
    }));
    location.hash = "#/ask";
  }

  async function viewThreadDetail(tid, focusTurnId = null) {
    setLoading();
    try {
      const t = await api("/threads/" + encodeURIComponent(tid));
      const turnsHtml = (t.turns || []).map(turnCard).join("");
      const parents = t.parents || [];
      const parent = parents[0] || null;  // v1 enforces ≤ 1
      // Breadcrumb passes the target turn id via query string; the route handler
      // for /threads/:id scrolls the matching turn into view after render.
      const parentBlock = parent ? `
        <div style="margin:6px 0 10px">
          <a class="parent-breadcrumb" href="#/threads/${encodeURIComponent(parent.thread_id)}?focus=${encodeURIComponent(parent.turn_id)}">
            &larr; branched from parent thread
          </a>
          ${parent.quote ? `<div class="parent-quote" id="parent-quote">${esc(parent.quote)}</div>` : ""}
        </div>` : "";

      view.innerHTML = `
        <div class="row" style="justify-content:space-between;align-items:flex-start">
          <a href="#/threads" class="meta">&larr; all conversations</a>
          <button id="c-delete" title="delete conversation">✕</button>
        </div>
        <h3 style="margin:8px 0 4px">${esc(t.title || "(untitled)")}</h3>
        <div class="meta" style="margin-bottom:6px">${esc(fmtDate(t.updated_at))}</div>
        ${parentBlock}
        <div id="thread-turns">${turnsHtml || `<p class="meta">No turns yet.</p>`}</div>
        <div id="thread-children"></div>
        <div class="card" style="margin-top:18px">
          <h3 style="margin:0 0 6px">Continue</h3>
          <textarea id="c-q" placeholder="follow-up question — fetches more sources if needed"></textarea>
          <div style="display:flex;align-items:center;gap:14px;flex-wrap:wrap;margin-top:8px">
            <span class="meta" style="font-weight:600">Depth:</span>
            <label class="meta" style="display:inline-flex;align-items:center;gap:4px"><input type="radio" name="c-depth" value="standard" checked> standard</label>
            <label class="meta" style="display:inline-flex;align-items:center;gap:4px"><input type="radio" name="c-depth" value="deep"> deep</label>
          </div>
          <div style="display:flex;align-items:center;gap:14px;flex-wrap:wrap;margin-top:6px">
            <span class="meta" style="font-weight:600">Mode:</span>
            <label class="meta" style="display:inline-flex;align-items:center;gap:4px"><input type="radio" name="c-mode" value="auto" checked> auto</label>
            <label class="meta" style="display:inline-flex;align-items:center;gap:4px"><input type="radio" name="c-mode" value="cloud"> cloud</label>
            <label class="meta" style="display:inline-flex;align-items:center;gap:4px"><input type="radio" name="c-mode" value="local"> my subscription</label>
          </div>
          <div class="row" style="margin-top:10px;gap:8px;align-items:center">
            <button id="c-go">Continue</button>
            <span id="c-status" class="meta"></span>
            <span id="c-badge" class="meta"></span>
          </div>
          <div id="c-answer"></div>
        </div>`;
      $("c-delete").onclick = async () => {
        if (!confirm("Delete this conversation? Turns are removed; ingested docs stay in the corpus.")) return;
        try { await api("/threads/" + encodeURIComponent(tid), { method: "DELETE" }); location.hash = "#/threads"; }
        catch (e) { toast(e.message, "err"); }
      };
      $("c-go").onclick = async () => {
        const q = $("c-q").value.trim(); if (!q) return;
        const mode = (document.querySelector('input[name="c-mode"]:checked') || {}).value || "auto";
        const depth = (document.querySelector('input[name="c-depth"]:checked') || {}).value || "standard";
        const r = await submitAsk({
          thread_id: tid, q, mode, depth,
          triggerEl: $("c-go"),
          statusEl: $("c-status"),
          answerEl: $("c-answer"),
          badgeEl: $("c-badge"),
        });
        if (r) {
          $("c-q").value = "";
          setTimeout(() => viewThreadDetail(tid), 600);
        }
      };

      // Wire per-turn Branch buttons + selection-anchored "Ask about this".
      _wireTurnBranching(tid, t);

      // Load + render immediate children at the bottom (independent fetch so a
      // failed children call doesn't block thread render).
      _loadAndRenderChildren(tid);

      // Breadcrumb focus: scroll the requested turn into view after render.
      if (focusTurnId) {
        const target = document.getElementById(`turn-${focusTurnId}`);
        if (target) {
          setTimeout(() => target.scrollIntoView({ behavior: "smooth", block: "start" }), 100);
        }
      }
    } catch (e) { toast(e.message, "err"); view.innerHTML = ""; }
  }

  function _wireTurnBranching(tid, thread) {
    // Map turn id -> question, for the "Follow up on: ..." seed text.
    const byId = {};
    (thread.turns || []).forEach((t) => { if (t.id) byId[t.id] = t; });

    // Turn-level Branch button: seeds composer with "Follow up on: <parent question>".
    view.querySelectorAll('button.branch-btn[data-act="branch"]').forEach((btn) => {
      btn.onclick = (ev) => {
        ev.stopPropagation();
        const turnId = btn.dataset.turnId;
        const t = byId[turnId];
        const parentQ = (t && t.question) || "";
        _stashBranchAndGo({
          parent_thread_id: tid,
          parent_turn_id: turnId,
          seed: parentQ ? `Follow up on: ${parentQ}` : "",
        });
      };
    });

    // Selection-level branching: floating "Ask about this" button anchored to selection.
    _installSelectionListener(tid);
  }

  let _floatingAskBtn = null;
  function _clearFloatingAsk() {
    if (_floatingAskBtn) { _floatingAskBtn.remove(); _floatingAskBtn = null; }
  }

  function _installSelectionListener(tid) {
    // Use a single listener on the view container; check selection scope on each event.
    // On iOS Safari this co-exists with the native context menu (the floating button
    // sits above the answer area and disappears when the selection clears).
    const handler = () => {
      const sel = window.getSelection();
      if (!sel || sel.isCollapsed) { _clearFloatingAsk(); return; }
      const range = sel.rangeCount ? sel.getRangeAt(0) : null;
      if (!range) { _clearFloatingAsk(); return; }
      // Selection must start *and* end inside a single turn's answer body.
      const startAnchor = range.startContainer.parentElement
        ? range.startContainer.parentElement.closest("[data-answer-body]") : null;
      const endAnchor = range.endContainer.parentElement
        ? range.endContainer.parentElement.closest("[data-answer-body]") : null;
      if (!startAnchor || startAnchor !== endAnchor) { _clearFloatingAsk(); return; }
      const turnCardEl = startAnchor.closest("[data-turn]");
      if (!turnCardEl) { _clearFloatingAsk(); return; }
      const turnId = turnCardEl.dataset.turn;
      const quote = sel.toString().trim();
      if (!quote) { _clearFloatingAsk(); return; }

      const rect = range.getBoundingClientRect();
      _clearFloatingAsk();
      const btn = document.createElement("button");
      btn.className = "selection-ask-btn";
      btn.textContent = "Ask about this";
      // Position above the selection in document coords (account for page scroll).
      btn.style.top = `${Math.max(8, rect.top + window.scrollY - 38)}px`;
      btn.style.left = `${Math.max(8, rect.left + window.scrollX)}px`;
      btn.onmousedown = (e) => e.preventDefault();  // don't clear the selection on click-down
      btn.onclick = (e) => {
        e.preventDefault();
        const truncatedSeed = quote.length > 200 ? quote.slice(0, 200).trimEnd() + "…" : quote;
        _stashBranchAndGo({
          parent_thread_id: tid,
          parent_turn_id: turnId,
          parent_quote: quote,
          seed: `Going deeper on: ${truncatedSeed}`,
        });
      };
      document.body.appendChild(btn);
      _floatingAskBtn = btn;
    };
    document.addEventListener("selectionchange", handler);
    // Clear on hashchange + tear listener down so we don't leak across routes.
    const teardown = () => {
      document.removeEventListener("selectionchange", handler);
      _clearFloatingAsk();
      window.removeEventListener("hashchange", teardown);
    };
    window.addEventListener("hashchange", teardown);
  }

  async function _loadAndRenderChildren(tid) {
    const slot = $("thread-children");
    if (!slot) return;
    try {
      const kids = await api(`/threads/${encodeURIComponent(tid)}/children`);
      if (!kids || !kids.length) { slot.innerHTML = ""; return; }
      slot.innerHTML = `
        <div class="card" style="margin-top:14px">
          <h3 style="margin:0 0 6px">Branched threads (${kids.length})</h3>
          <div class="meta" style="margin-bottom:6px">Forks that branched off from this conversation.</div>
          ${kids.map((c) => `
            <a class="card" style="display:block;margin-top:6px" href="#/threads/${encodeURIComponent(c.id)}">
              <div>${esc(c.title || c.first_turn_question || "(untitled)")} ${c.has_quote ? '<span class="has-quote-tag">selection</span>' : ""}</div>
              <div class="meta">${esc(fmtDate(c.created_at))}</div>
            </a>`).join("")}
        </div>`;
    } catch (e) {
      // Non-fatal — just leave the slot empty.
    }
  }

  // -------------------- Learn (course layer) -------------------- //
  const PROG_KEY = (cid) => `course_progress_${cid}`;
  const getProgress = (cid) => {
    try { return new Set(JSON.parse(localStorage.getItem(PROG_KEY(cid)) || "[]")); }
    catch { return new Set(); }
  };
  const setProgress = (cid, s) =>
    localStorage.setItem(PROG_KEY(cid), JSON.stringify([...s]));

  let _pollTimer = null;
  const stopPoll = () => { if (_pollTimer) { clearTimeout(_pollTimer); _pollTimer = null; } };

  async function viewCourses() {
    stopPoll();
    view.innerHTML = `
      <div class="card">
        <h3>New course</h3>
        <div class="meta">Generates a draft course from your corpus (3-5 lessons, Bloom-tagged). You can edit + regenerate after.</div>
        <textarea id="co-q" placeholder="e.g. evaluation methods for retrieval-augmented generation"></textarea>
        <div style="margin-top:10px" class="row"><button id="co-go">Generate</button><span id="co-status" class="meta"></span></div>
      </div>
      <div id="co-list"><div class="meta"><span class="spinner"></span>loading…</div></div>`;
    const loadList = async () => {
      try {
        const list = await api("/courses");
        const pending = list.some((c) => c.status === "pending" || c.status === "generating");
        if (!list.length) {
          $("co-list").innerHTML = `<p class="meta">No courses yet.</p>`;
          return;
        }
        $("co-list").innerHTML = list.map((c) => `
          <a class="card" style="display:block" href="#/learn/${encodeURIComponent(c.id)}">
            <h3>${esc(c.title || "(untitled)")}</h3>
            <div class="meta">${esc(c.status)} · ${esc(fmtDate(c.updated_at))}</div>
            <div style="margin-top:6px">${esc(c.objective || "")}</div>
          </a>`).join("");
        if (pending && location.hash === "#/learn") {
          _pollTimer = setTimeout(loadList, 5000);
        }
      } catch (e) { toast(e.message, "err"); }
    };
    $("co-go").addEventListener("click", async () => {
      const q = $("co-q").value.trim(); if (!q) return;
      $("co-status").innerHTML = '<span class="spinner"></span>queuing…';
      try {
        const r = await api("/courses", { method: "POST", body: JSON.stringify({ query_seed: q }) });
        $("co-status").textContent = `queued. ${r.course_id}`;
        $("co-q").value = "";
        await loadList();
      } catch (e) { toast(e.message, "err"); $("co-status").textContent = ""; }
    });
    await loadList();
  }

  function bloomBadge(level) {
    if (!level) return "";
    return `<span class="meta" style="display:inline-block;padding:1px 8px;border:1px solid #333;border-radius:10px;font-size:11px;margin-left:8px">${esc(level)}</span>`;
  }

  function lessonCard(c, lesson, i, total, completed) {
    const isDone = completed.has(lesson.id);
    const srcTag = lesson.source && lesson.source !== "generated"
      ? `<span class="meta">· ${esc(lesson.source)}${lesson.edited_at ? " " + esc(fmtDate(lesson.edited_at)) : ""}</span>` : "";
    return `
      <div class="card" data-lesson="${esc(lesson.id)}">
        <div class="row" style="justify-content:space-between;align-items:flex-start">
          <div style="flex:1">
            <div class="meta">Lesson ${i + 1} of ${total}${bloomBadge(lesson.bloom_level)} ${srcTag}</div>
            <h3 data-field="title" style="margin:4px 0">${esc(lesson.title || "")}</h3>
            <div class="meta" data-field="objective">${esc(lesson.objective || "")}</div>
          </div>
          <div class="row" style="gap:4px">
            <button data-act="up" ${i === 0 ? "disabled" : ""} title="move up">↑</button>
            <button data-act="down" ${i === total - 1 ? "disabled" : ""} title="move down">↓</button>
            <button data-act="edit" title="edit">✎</button>
            <button data-act="delete" title="delete">✕</button>
          </div>
        </div>
        <div class="report" data-field="body" style="margin-top:10px">${marked.parse(lesson.body_md || "")}</div>
        <div class="row" style="margin-top:10px;gap:8px;flex-wrap:wrap">
          <button data-act="done">${isDone ? "✓ completed" : "mark complete"}</button>
          <button data-act="ask">Ask follow-up</button>
          <button data-act="regen">Regenerate with feedback</button>
        </div>
        <div data-slot="panel"></div>
        <div data-slot="followups" style="margin-top:8px"></div>
      </div>`;
  }

  function citationList(cites) {
    if (!cites || !cites.length) return "";
    return `<details style="margin-top:8px"><summary class="meta">Citations (${cites.length})</summary>` +
      cites.map((c) => `
        <div class="card" style="margin-top:6px">
          <div class="meta">[${c.n ?? ""}] ${esc(c.query || "")}${c.thread_id ? ` · <a href="#/threads/${encodeURIComponent(c.thread_id)}">thread</a>` : ""}${c.score != null ? ` · ${c.score.toFixed(3)}` : ""}</div>
          <pre>${esc(c.snippet || "")}</pre>
        </div>`).join("") + `</details>`;
  }

  function followUpCard(f) {
    const created = f.created_at ? ` · ${esc(fmtDate(f.created_at))}` : "";
    return `
      <div class="card" data-fu data-q="${esc(f.question || "")}">
        <div class="meta">Q: ${esc(f.question)}${created}</div>
        <div data-fu-answer style="margin-top:6px" class="report">${marked.parse(f.answer_md || "")}</div>
        <div data-fu-citations>${citationList(f.citations)}</div>
        <div class="row" style="margin-top:6px;gap:6px;align-items:center">
          <button data-act="research" title="Fetch fresh sources & re-answer">Research this</button>
          <span data-fu-status class="meta"></span>
          <span data-fu-badge class="meta"></span>
        </div>
        <div data-slot="research-panel"></div>
      </div>`;
  }

  async function pollResearch(runId, { intervalMs = 2000, maxMs = 240000 } = {}) {
    const start = Date.now();
    while (Date.now() - start < maxMs) {
      const r = await api(`/research/${encodeURIComponent(runId)}`);
      if (r.status === "complete" || r.status === "failed") return r;
      await new Promise((res) => setTimeout(res, intervalMs));
    }
    throw new Error("research polling timed out");
  }

  function wireFollowUp(card, lUrl) {
    const q = card.dataset.q;
    const status = card.querySelector("[data-fu-status]");
    const badge = card.querySelector("[data-fu-badge]");
    const panel = card.querySelector('[data-slot="research-panel"]');
    card.querySelector('[data-act="research"]').onclick = () => {
      panel.innerHTML = `
        <div class="card" style="margin-top:6px">
          <div class="meta">Optional: paste URLs (one per line). Empty = web search.</div>
          <textarea data-research-urls rows="3" placeholder="https://..."></textarea>
          <div class="row" style="margin-top:6px;gap:6px">
            <button data-act="research-go">Fire</button>
            <button data-act="research-cancel">Cancel</button>
          </div>
        </div>`;
      panel.querySelector('[data-act="research-cancel"]').onclick = () => { panel.innerHTML = ""; };
      panel.querySelector('[data-act="research-go"]').onclick = async () => {
        const raw = panel.querySelector("[data-research-urls]").value.trim();
        const urls = raw ? raw.split(/\s+/).filter(Boolean) : [];
        panel.innerHTML = "";
        badge.textContent = "";
        status.innerHTML = '<span class="spinner"></span>firing routine…';
        try {
          const fire = await api(`${lUrl}/research_fire`, {
            method: "POST",
            body: JSON.stringify({ question: q, urls, max_fetches: 5 }),
          });
          status.innerHTML = '<span class="spinner"></span>routine running…';
          const result = await pollResearch(fire.run_id);
          if (result.status === "failed") {
            const msg = (result.errors && result.errors[0] && result.errors[0].message) || "routine failed";
            status.textContent = `failed: ${msg}`;
            return;
          }
          const n = (result.ingested || []).length;
          badge.textContent = n > 0 ? `+${n} newly fetched` : "no new docs found";
          status.innerHTML = '<span class="spinner"></span>re-answering…';
          const r = await api(`${lUrl}/ask`, { method: "POST", body: JSON.stringify({ question: q }) });
          status.textContent = "";
          card.querySelector("[data-fu-answer]").innerHTML = marked.parse(r.answer_md || "");
          card.querySelector("[data-fu-citations]").innerHTML = citationList(r.citations);
        } catch (e) {
          status.textContent = "";
          toast(e.message, "err");
        }
      };
    };
  }

  async function viewCourseDetail(cid) {
    stopPoll();
    setLoading();

    const render = async () => {
      let c;
      try { c = await api(`/courses/${encodeURIComponent(cid)}`); }
      catch (e) { toast(e.message, "err"); view.innerHTML = ""; return; }

      if (c.status !== "draft" && c.status !== "failed") {
        view.innerHTML = `
          <a href="#/learn" class="meta">&larr; all courses</a>
          <div class="card" style="margin-top:10px">
            <h3>${esc(c.title || "(untitled)")}</h3>
            <div class="meta"><span class="spinner"></span>${esc(c.status)}…</div>
            <div style="margin-top:8px">${esc(c.objective || "")}</div>
          </div>`;
        _pollTimer = setTimeout(render, 5000);
        return;
      }

      const completed = getProgress(cid);
      const lessonsHtml = (c.lessons || []).map((l, i) =>
        lessonCard(c, l, i, c.lessons.length, completed)).join("");

      view.innerHTML = `
        <a href="#/learn" class="meta">&larr; all courses</a>
        <div class="card" style="margin-top:10px">
          <div class="row" style="justify-content:space-between;align-items:flex-start">
            <div style="flex:1">
              <h3 data-field="title" style="margin:0">${esc(c.title || "(untitled)")}</h3>
              <div class="meta" data-field="objective" style="margin-top:6px">${esc(c.objective || "")}</div>
              <div class="meta" style="margin-top:4px">${esc(c.status)}${c.model ? " · " + esc(c.model) : ""} · ${esc(fmtDate(c.updated_at))}</div>
            </div>
            <div class="row" style="gap:4px">
              <button data-course-act="edit" title="edit">✎</button>
              <button data-course-act="delete" title="delete course">✕</button>
            </div>
          </div>
          ${c.status === "failed" && c.error ? `<pre style="color:#c66">${esc(c.error)}</pre>` : ""}
        </div>
        <div id="co-lessons">${lessonsHtml}</div>
        <div class="card">
          <h3>Add lesson</h3>
          <input id="add-title" placeholder="title" style="width:100%;margin-bottom:6px">
          <input id="add-obj" placeholder="objective" style="width:100%;margin-bottom:6px">
          <div class="row"><button id="add-go">Add</button><span id="add-status" class="meta"></span></div>
        </div>`;

      wireCourseHeader(c);
      (c.lessons || []).forEach((l, i) => wireLesson(c, l, i));
      wireAddLesson(c);
    };

    // -- header (course) --
    function wireCourseHeader(c) {
      const root = view.querySelector(".card");
      root.querySelector('[data-course-act="delete"]').onclick = async () => {
        if (!confirm("Delete this entire course?")) return;
        try { await api(`/courses/${encodeURIComponent(c.id)}`, { method: "DELETE" }); location.hash = "#/learn"; }
        catch (e) { toast(e.message, "err"); }
      };
      root.querySelector('[data-course-act="edit"]').onclick = () => {
        const tEl = root.querySelector('[data-field="title"]');
        const oEl = root.querySelector('[data-field="objective"]');
        const tVal = c.title || "", oVal = c.objective || "";
        tEl.outerHTML = `<input data-field="title" value="${esc(tVal)}" style="width:100%;font-size:18px;font-weight:600">`;
        oEl.outerHTML = `<textarea data-field="objective" style="width:100%">${esc(oVal)}</textarea>
          <div class="row" style="margin-top:6px;gap:6px"><button data-course-act="save">Save</button><button data-course-act="cancel">Cancel</button></div>`;
        root.querySelector('[data-course-act="save"]').onclick = async () => {
          const title = root.querySelector('input[data-field="title"]').value.trim();
          const objective = root.querySelector('textarea[data-field="objective"]').value.trim();
          try {
            await api(`/courses/${encodeURIComponent(c.id)}`, {
              method: "PATCH", body: JSON.stringify({ title, objective }),
            });
            await render();
          } catch (e) { toast(e.message, "err"); }
        };
        root.querySelector('[data-course-act="cancel"]').onclick = render;
      };
    }

    // -- per-lesson wiring --
    function wireLesson(c, lesson, i) {
      const el = view.querySelector(`[data-lesson="${CSS.escape(lesson.id)}"]`);
      if (!el) return;
      const lUrl = `/courses/${encodeURIComponent(c.id)}/lessons/${encodeURIComponent(lesson.id)}`;

      el.querySelector('[data-act="up"]').onclick = async () => {
        if (i === 0) return;
        const order = c.lessons.map((x) => x.id);
        [order[i - 1], order[i]] = [order[i], order[i - 1]];
        try {
          await api(`/courses/${encodeURIComponent(c.id)}/lessons/reorder`, {
            method: "PATCH", body: JSON.stringify({ order }),
          });
          await render();
        } catch (e) { toast(e.message, "err"); }
      };
      el.querySelector('[data-act="down"]').onclick = async () => {
        if (i === c.lessons.length - 1) return;
        const order = c.lessons.map((x) => x.id);
        [order[i + 1], order[i]] = [order[i], order[i + 1]];
        try {
          await api(`/courses/${encodeURIComponent(c.id)}/lessons/reorder`, {
            method: "PATCH", body: JSON.stringify({ order }),
          });
          await render();
        } catch (e) { toast(e.message, "err"); }
      };
      el.querySelector('[data-act="delete"]').onclick = async () => {
        if (!confirm(`Delete lesson "${lesson.title}"?`)) return;
        try { await api(lUrl, { method: "DELETE" }); await render(); }
        catch (e) { toast(e.message, "err"); }
      };
      el.querySelector('[data-act="done"]').onclick = () => {
        const s = getProgress(c.id);
        s.has(lesson.id) ? s.delete(lesson.id) : s.add(lesson.id);
        setProgress(c.id, s);
        render();
      };
      el.querySelector('[data-act="edit"]').onclick = () => {
        el.querySelector('[data-field="title"]').outerHTML =
          `<input data-field="title" value="${esc(lesson.title || "")}" style="width:100%;font-size:16px;font-weight:600;margin:4px 0">`;
        el.querySelector('[data-field="objective"]').outerHTML =
          `<input data-field="objective" value="${esc(lesson.objective || "")}" style="width:100%">`;
        el.querySelector('[data-field="body"]').outerHTML =
          `<textarea data-field="body" rows="14" style="width:100%;margin-top:10px">${esc(lesson.body_md || "")}</textarea>`;
        const panel = el.querySelector('[data-slot="panel"]');
        panel.innerHTML = `<div class="row" style="gap:6px;margin-top:8px">
          <button data-act="save">Save</button><button data-act="cancel">Cancel</button></div>`;
        panel.querySelector('[data-act="save"]').onclick = async () => {
          const title = el.querySelector('input[data-field="title"]').value.trim();
          const objective = el.querySelector('input[data-field="objective"]').value.trim();
          const body_md = el.querySelector('textarea[data-field="body"]').value;
          try {
            await api(lUrl, { method: "PATCH", body: JSON.stringify({ title, objective, body_md }) });
            await render();
          } catch (e) { toast(e.message, "err"); }
        };
        panel.querySelector('[data-act="cancel"]').onclick = render;
      };
      el.querySelector('[data-act="regen"]').onclick = () => {
        const panel = el.querySelector('[data-slot="panel"]');
        panel.innerHTML = `
          <div class="card">
            <div class="meta">What should the rewrite emphasize or fix?</div>
            <textarea data-regen-fb placeholder="e.g. too shallow on evaluation tradeoffs, add more on concrete metrics"></textarea>
            <div class="row" style="margin-top:6px;gap:6px"><button data-act="go-regen">Regenerate</button><button data-act="cancel-regen">Cancel</button><span data-regen-status class="meta"></span></div>
          </div>`;
        panel.querySelector('[data-act="cancel-regen"]').onclick = () => { panel.innerHTML = ""; };
        panel.querySelector('[data-act="go-regen"]').onclick = async () => {
          const fb = panel.querySelector("[data-regen-fb]").value.trim(); if (!fb) return;
          panel.querySelector("[data-regen-status]").innerHTML = '<span class="spinner"></span>rewriting…';
          try {
            await api(`${lUrl}/regenerate`, { method: "POST", body: JSON.stringify({ feedback: fb }) });
            await render();
          } catch (e) { toast(e.message, "err"); panel.querySelector("[data-regen-status]").textContent = ""; }
        };
      };
      el.querySelector('[data-act="ask"]').onclick = async () => {
        const panel = el.querySelector('[data-slot="panel"]');
        const fuSlot = el.querySelector('[data-slot="followups"]');
        panel.innerHTML = `
          <div class="card">
            <div class="meta">Ask a follow-up about this lesson.</div>
            <textarea data-ask-q placeholder="clarify or dig deeper"></textarea>
            <div class="row" style="margin-top:6px;gap:6px"><button data-act="go-ask">Ask</button><button data-act="cancel-ask">Cancel</button><span data-ask-status class="meta"></span></div>
          </div>`;
        panel.querySelector('[data-act="cancel-ask"]').onclick = () => { panel.innerHTML = ""; };
        panel.querySelector('[data-act="go-ask"]').onclick = async () => {
          const q = panel.querySelector("[data-ask-q]").value.trim(); if (!q) return;
          panel.querySelector("[data-ask-status]").innerHTML = '<span class="spinner"></span>thinking…';
          try {
            const r = await api(`${lUrl}/ask`, { method: "POST", body: JSON.stringify({ question: q }) });
            panel.innerHTML = "";
            fuSlot.insertAdjacentHTML("afterbegin", followUpCard(r));
            wireFollowUp(fuSlot.querySelector("[data-fu]"), lUrl);
          } catch (e) { toast(e.message, "err"); panel.querySelector("[data-ask-status]").textContent = ""; }
        };
        // lazy load existing follow-ups on first ask click
        if (!fuSlot.dataset.loaded) {
          try {
            const list = await api(`${lUrl}/follow_ups`);
            fuSlot.dataset.loaded = "1";
            fuSlot.innerHTML = list.map(followUpCard).join("");
            fuSlot.querySelectorAll("[data-fu]").forEach((card) => wireFollowUp(card, lUrl));
          } catch {}
        }
      };
    }

    // -- add-lesson form --
    function wireAddLesson(c) {
      $("add-go").onclick = async () => {
        const title = $("add-title").value.trim();
        const objective = $("add-obj").value.trim();
        if (!title || !objective) { $("add-status").textContent = "title + objective required"; return; }
        $("add-status").innerHTML = '<span class="spinner"></span>adding…';
        try {
          await api(`/courses/${encodeURIComponent(c.id)}/lessons`, {
            method: "POST", body: JSON.stringify({ title, objective, body_md: "" }),
          });
          await render();
        } catch (e) { toast(e.message, "err"); $("add-status").textContent = ""; }
      };
    }

    await render();
  }

  const routes = [
    [/^#\/ask$/, viewAsk],
    [/^#\/reports$/, viewReports],
    [/^#\/reports\/(.+)$/, (m) => viewReportDetail(decodeURIComponent(m[1]))],
    [/^#\/search$/, viewSearch],
    [/^#\/threads$/, viewThreads],
    [/^#\/threads\/([^?]+)(?:\?(.*))?$/, (m) => {
      const tid = decodeURIComponent(m[1]);
      const qs = new URLSearchParams(m[2] || "");
      const focus = qs.get("focus");
      return viewThreadDetail(tid, focus ? decodeURIComponent(focus) : null);
    }],
    [/^#\/learn$/, viewCourses],
    [/^#\/learn\/(.+)$/, (m) => viewCourseDetail(decodeURIComponent(m[1]))],
  ];
  function route() {
    const hash = location.hash || "#/ask";
    document.querySelectorAll("nav a").forEach((a) => {
      const href = a.getAttribute("href");
      a.classList.toggle("active", hash === href || hash.startsWith(href + "/"));
    });
    for (const [re, fn] of routes) {
      const m = hash.match(re);
      if (m) { fn(m); return; }
    }
    location.hash = "#/ask";
  }
  window.addEventListener("hashchange", () => { stopPoll(); route(); });
  document.addEventListener("click", (e) => {
    const a = e.target.closest("[data-toc-target]");
    if (!a) return;
    e.preventDefault();
    const el = document.getElementById(a.getAttribute("data-toc-target"));
    if (el) el.scrollIntoView({ behavior: "smooth", block: "start" });
  });

  if (token) showApp(); else showGate();
})();
