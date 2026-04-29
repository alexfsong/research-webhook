(() => {
  const TOKEN_KEY = "research_token";
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

  async function viewAsk() {
    view.innerHTML = `
      <div class="card">
        <h3>New research</h3>
        <div class="meta">Kicks off a deep research run. Reports drop into Reports in a few minutes.</div>
        <textarea id="ask-q" placeholder="e.g. compare LightRAG and GraphRAG for evaluation robustness"></textarea>
        <div style="margin-top:10px" class="row"><button id="ask-go">Ask</button><span id="ask-status" class="meta"></span></div>
      </div>
      <div id="ask-existing"></div>`;
    const go = async () => {
      const q = $("ask-q").value.trim(); if (!q) return;
      $("ask-status").innerHTML = '<span class="spinner"></span>queuing…';
      try {
        const r = await api("/research", { method: "POST", body: JSON.stringify({ query: q }) });
        $("ask-status").textContent = "queued. check Reports shortly.";
        $("ask-q").value = "";
        if (r.existing_matches && r.existing_matches.length) {
          $("ask-existing").innerHTML = `<h3 style="margin:20px 0 8px">Related prior work</h3>` +
            r.existing_matches.map((m) => `
              <div class="card">
                <div class="meta">${esc(m.query || "")}${m.score != null ? ` · score ${m.score.toFixed(3)}` : ""}</div>
                <pre>${esc(m.preview)}</pre>
              </div>`).join("");
        }
      } catch (e) { toast(e.message, "err"); $("ask-status").textContent = ""; }
    };
    $("ask-go").addEventListener("click", go);
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

  function renderSynthResult(r) {
    const cites = (r.citations || []).map((c, i) => {
      const n = c.n ?? (i + 1);
      return `
        <div class="card">
          <div class="meta">[${n}] ${esc(fmtDate(c.date || ""))} · ${esc(c.query || "")}${c.thread_id ? ` · <a href="#/threads/${encodeURIComponent(c.thread_id)}">thread</a>` : ""}${c.score != null ? ` · score ${c.score.toFixed(3)}` : ""}</div>
          <pre>${esc(c.snippet || "")}</pre>
        </div>`;
    }).join("");
    return `
      <div class="report" style="margin-top:12px">${marked.parse(r.answer || "")}</div>
      ${cites ? `<h3 style="margin:18px 0 8px">Citations</h3>${cites}` : ""}`;
  }

  async function viewSynthesize() {
    view.innerHTML = `
      <div class="card">
        <h3>Synthesize across reports</h3>
        <div class="meta">Top excerpts + Claude answer with citations.</div>
        <textarea id="sy-q" placeholder="what do my reports say about X?"></textarea>
        <div style="margin-top:10px" class="row">
          <label class="meta" style="display:flex;align-items:center;gap:4px"><input type="checkbox" id="sy-subq">sub-questions</label>
          <button id="sy-go">Ask</button>
          <span id="sy-status" class="meta"></span>
        </div>
      </div>
      <div id="sy-out"></div>`;
    $("sy-go").addEventListener("click", async () => {
      const q = $("sy-q").value.trim(); if (!q) return;
      const subq = $("sy-subq").checked;
      $("sy-status").innerHTML = '<span class="spinner"></span>synthesizing…';
      try {
        const r = await api("/synthesize2", {
          method: "POST",
          body: JSON.stringify({ question: q, k: 8, rerank: true, subq }),
        });
        $("sy-status").textContent = r.engine ? `(${r.model || ""} · ${r.engine})` : (r.model ? `(${r.model})` : "");
        $("sy-out").innerHTML = renderSynthResult(r);
      } catch (e) { toast(e.message, "err"); $("sy-status").textContent = ""; }
    });
  }

  async function viewThreads() {
    setLoading();
    try {
      const list = await api("/threads?limit=30");
      if (!list.length) { view.innerHTML = `<p class="meta">No threads yet.</p>`; return; }
      view.innerHTML = list.map((t) => `
        <a class="card" style="display:block" href="#/threads/${encodeURIComponent(t.thread_id)}">
          <h3>${esc(t.query || "(untitled)")}</h3>
          <div class="meta">${esc(fmtDate(t.created_at))}${t.has_report ? " · has report" : ""}</div>
        </a>`).join("");
    } catch (e) { toast(e.message, "err"); view.innerHTML = ""; }
  }

  async function viewThreadDetail(tid) {
    setLoading();
    try {
      const t = await api("/threads/" + encodeURIComponent(tid));
      const msgs = (t.messages || []).map((m) => `
        <div class="card"><div class="meta">${esc(m.role || "")}</div><pre>${esc(m.content || "")}</pre></div>`).join("");
      view.innerHTML = `
        <a href="#/threads" class="meta">&larr; all threads</a>
        <div class="meta" style="margin:6px 0 14px">${esc(tid)}</div>
        ${t.final_report
          ? `<div class="report">${marked.parse(t.final_report)}</div>`
          : `<p class="meta">No final report yet.</p>`}
        <h3 style="margin:22px 0 8px">Continue this thread</h3>
        <div class="card">
          <textarea id="c-q" placeholder="follow-up: dig deeper into X, or explore Y angle"></textarea>
          <div style="margin-top:10px" class="row"><button id="c-go">Continue</button><span id="c-status" class="meta"></span></div>
          <div class="meta" style="margin-top:8px">A new report will appear under Reports when done.</div>
        </div>
        <details><summary class="meta">Messages (${t.messages.length})</summary>${msgs}</details>`;
      $("c-go").addEventListener("click", async () => {
        const instr = $("c-q").value.trim(); if (!instr) return;
        $("c-status").innerHTML = '<span class="spinner"></span>queuing…';
        try {
          await api(`/threads/${encodeURIComponent(tid)}/continue`, {
            method: "POST", body: JSON.stringify({ instruction: instr }),
          });
          $("c-status").textContent = "queued.";
          $("c-q").value = "";
        } catch (e) { toast(e.message, "err"); $("c-status").textContent = ""; }
      });
    } catch (e) { toast(e.message, "err"); view.innerHTML = ""; }
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
    [/^#\/synthesize$/, viewSynthesize],
    [/^#\/threads$/, viewThreads],
    [/^#\/threads\/(.+)$/, (m) => viewThreadDetail(decodeURIComponent(m[1]))],
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

  if (token) showApp(); else showGate();
})();
