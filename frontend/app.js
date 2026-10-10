/* Print Bridge frontend — vanilla JS, no build step.
 * Talks to the FastAPI backend at /api (same origin; backend serves this page at /).
 */
(function () {
  "use strict";

  /* ---------- tiny DOM helpers (all dynamic text goes through textContent) ---------- */
  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = text;
    return n;
  }
  function $(sel, root) { return (root || document).querySelector(sel); }
  function show(node) { node.hidden = false; }
  function hide(node) { node.hidden = true; }
  function setError(node, msg) {
    if (!msg) { node.textContent = ""; hide(node); return; }
    node.textContent = msg;   // textContent, never innerHTML
    show(node);
  }

  /* Only allow plain #rrggbb / #rgb colors into style attributes.
     Accepts API-style bare "RRGGBB" as well as "#RRGGBB". */
  function safeHex(hex, fallback) {
    fallback = fallback || "#9ca3af";
    if (typeof hex === "string") {
      var h = hex.trim();
      if (/^(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$/.test(h)) h = "#" + h;
      if (/^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$/.test(h)) return h;
    }
    return fallback;
  }

  /* ---------- plain-language status text ---------- */
  var STATUS_TEXT = {
    created: "Getting your model…",
    fetching: "Getting your model…",
    inspecting: "Checking the colors…",
    awaiting_confirm: "Waiting for you to check the colors",
    slicing: "Preparing the print…",
    uploading: "Sending to printer…",
    printing: "Printing",
    done: "Done ✓",
    failed: "Something went wrong",
    cancelled: "Cancelled"
  };
  var TERMINAL = ["done", "failed", "cancelled"];

  function formatDuration(sec) {
    if (sec === null || sec === undefined || isNaN(sec)) return null;
    sec = Math.max(0, Math.round(sec));
    var h = Math.floor(sec / 3600);
    var m = Math.round((sec % 3600) / 60);
    if (h > 0) return h + " h " + m + " min";
    if (m > 0) return m + " min";
    return sec + " s";
  }

  /* Rough everyday name for a hex color, so slots read like "Slot 1 needs red PLA". */
  var NAMED = [
    ["red", 220, 40, 40], ["orange", 240, 130, 30], ["yellow", 240, 210, 40],
    ["green", 40, 160, 70], ["blue", 40, 110, 220], ["purple", 140, 70, 200],
    ["pink", 245, 140, 180], ["brown", 130, 85, 50], ["black", 30, 30, 30],
    ["white", 245, 245, 245], ["gray", 140, 140, 140], ["natural", 235, 225, 200]
  ];
  function colorName(hex) {
    var m = /^#?([0-9a-fA-F]{6})/.exec(String(hex || ""));
    if (!m) return "";
    var r = parseInt(m[1].slice(0, 2), 16),
        g = parseInt(m[1].slice(2, 4), 16),
        b = parseInt(m[1].slice(4, 6), 16);
    var best = "gray", bestD = Infinity;
    NAMED.forEach(function (c) {
      var d = (r - c[1]) * (r - c[1]) + (g - c[2]) * (g - c[2]) + (b - c[3]) * (b - c[3]);
      if (d < bestD) { bestD = d; best = c[0]; }
    });
    return best;
  }
  function colorDistance(a, b) {
    var ma = /^#?([0-9a-fA-F]{6})/.exec(String(a || "")),
        mb = /^#?([0-9a-fA-F]{6})/.exec(String(b || ""));
    if (!ma || !mb) return Infinity;
    var d = 0;
    for (var i = 0; i < 3; i++) {
      var x = parseInt(ma[1].slice(i * 2, i * 2 + 2), 16) - parseInt(mb[1].slice(i * 2, i * 2 + 2), 16);
      d += x * x;
    }
    return d;
  }

  /* ---------- state ---------- */
  var state = {
    toolheads: null,     // array of {toolhead, color_hex, material, spool|null, source}, or null
    toolheadsUnavailable: false,
    jobs: [],
    jobsTimer: null,
    expandedLogs: {},    // jobId -> true (preserved across re-renders)
    modal: null          // { job, toolSel: {0: toolheadIdx}, plate }
  };

  /* ---------- fetch wrapper with friendly errors ---------- */
  async function readBody(resp) {
    var text = "";
    try { text = await resp.text(); } catch (e) { /* ignore */ }
    return text;
  }
  async function parseJsonBody(resp) {
    var text = await readBody(resp);
    try { return JSON.parse(text); } catch (e) { return null; }
  }
  function friendlyError(status, data, fallback) {
    if (data) {
      if (typeof data.error === "string" && data.error) return data.error;
      if (typeof data.message === "string" && data.message) return data.message;
      if (typeof data.detail === "string" && data.detail) return data.detail;
    }
    return fallback || "Something went wrong talking to the bridge. Please try again.";
  }
  async function apiJson(path, opts) {
    opts = opts || {};
    var resp;
    try {
      resp = await fetch(path, opts);
    } catch (e) {
      throw new Error("Couldn't reach the bridge. Is it running and on the same network?");
    }
    if (resp.status === 204) return { status: 204, data: null };
    var data = await parseJsonBody(resp);
    return { status: resp.status, ok: resp.ok, data: data };
  }

  /* ================= health ================= */
  var pill = $("#health-pill"), popover = $("#health-popover");
  var SERVICES = [["moonraker", "Printer"], ["spoolman", "Spoolman"], ["ntfy", "Alerts"]];

  async function refreshHealth() {
    var h = null;
    try {
      var r = await apiJson("/api/health");
      if (r.ok && r.data) h = r.data;
    } catch (e) { /* h stays null */ }
    renderHealth(h);
  }
  function renderHealth(h) {
    popover.textContent = "";  // rebuild safely
    if (!h || !h.ok) {
      pill.textContent = "Something's offline";
      pill.className = "pill bad";
      var p = el("p", "hint", "Couldn't reach the bridge's health check. The app may be down.");
      popover.appendChild(p);
      return;
    }
    var svcs = (h.services || {});
    var allOk = SERVICES.every(function (s) { return svcs[s[0]] && svcs[s[0]].ok; });
    pill.textContent = allOk ? "All good" : "Something's offline";
    pill.className = "pill " + (allOk ? "good" : "bad");
    SERVICES.forEach(function (s) {
      var key = s[0], label = s[1];
      var info = svcs[key] || {};
      var row = el("div", "svc");
      var name = el("div", "svc-name");
      var dot = el("span", "dot " + (info.ok ? "good" : "bad"));
      name.appendChild(dot);
      name.appendChild(el("span", null, label + (info.ok ? " — OK" : " — offline")));
      row.appendChild(name);
      var detail = info.detail || info.error || "";
      row.appendChild(el("div", "svc-detail", detail || (info.ok ? "Responding normally." : "No response.")));
      popover.appendChild(row);
    });
  }
  pill.addEventListener("click", function (e) {
    e.stopPropagation();
    popover.hidden = !popover.hidden;
  });
  document.addEventListener("click", function (e) {
    if (!popover.hidden && !popover.contains(e.target)) hide(popover);
  });

  /* ================= config (spoolman deep link) ================= */
  async function loadConfig() {
    try {
      var r = await apiJson("/api/config");
      if (r.ok && r.data && r.data.spoolman_url) {
        $("#manage-spools").href = r.data.spoolman_url;
      }
      // quality picker options come from the backend
      var sel = $("#quality-select");
      if (sel && r.ok && r.data && r.data.qualities) {
        sel.textContent = "";
        Object.keys(r.data.qualities).forEach(function (key) {
          var opt = document.createElement("option");
          opt.value = key;
          opt.textContent = r.data.qualities[key];
          if (key === "standard") opt.selected = true;
          sel.appendChild(opt);
        });
      }
    } catch (e) { /* leave the placeholder link */ }
  }

  /* ================= toolheads strip ================= */
  async function refreshSpools() {
    var strip = $("#spool-strip");
    try {
      var r = await apiJson("/api/toolheads");
      if (!r.ok || !r.data || !Array.isArray(r.data.toolheads)) throw new Error("unavailable");
      state.toolheads = r.data.toolheads;
      state.toolheadsUnavailable = false;
    } catch (e) {
      state.toolheads = null;
      state.toolheadsUnavailable = true;
    }
    renderSpools(strip);
  }
  function toolheadEntry(n) {
    // {toolhead, color_hex, material, spool|null, source} for T0..T3, or null
    if (!state.toolheads || n < 0 || n > 3) return null;
    for (var i = 0; i < state.toolheads.length; i++) {
      if (state.toolheads[i].toolhead === "T" + n) return state.toolheads[i];
    }
    return null;
  }
  function toolheadSpool(n) {
    var e = toolheadEntry(n);
    return e ? e.spool : null;
  }
  function toolheadIndexForSpool(id) {
    if (!state.toolheads || id === null || id === undefined) return null;
    for (var n = 0; n < 4; n++) {
      var s = toolheadSpool(n);
      if (s && String(s.id) === String(id)) return n;
    }
    return null;
  }
  function toolheadLabel(n) {
    var e = toolheadEntry(n);
    if (!e) return "T" + n;
    var bits = ["T" + n];
    var nm = colorName(e.color_hex);
    if (nm) bits.push(nm);
    if (e.material) bits.push(e.material);
    if (e.spool && e.spool.remaining_g !== null && e.spool.remaining_g !== undefined) {
      bits.push(Math.round(e.spool.remaining_g) + " g left");
    }
    return bits.join(" — ");
  }
  function renderSpools(strip) {
    strip.textContent = "";
    if (state.toolheadsUnavailable) {
      strip.appendChild(el("p", "hint", "Toolhead info unavailable — check the printer and Spoolman are running."));
      return;
    }
    for (var n = 0; n < 4; n++) {
      var e = toolheadEntry(n);
      var chip = el("div", "spool-chip" + (e && (e.color_hex || e.spool) ? "" : " empty"));
      chip.appendChild(el("div", "slot", "T" + n));
      if (e && (e.color_hex || e.spool)) {
        var dot = el("span", "color-dot");
        dot.style.backgroundColor = safeHex(e.color_hex);
        chip.appendChild(dot);
        chip.appendChild(el("div", "mat", e.material || ""));
        var s = e.spool;
        var grams = (s && s.remaining_g !== null && s.remaining_g !== undefined)
          ? Math.round(s.remaining_g) + " g left" : "grams unknown";
        chip.appendChild(el("div", "grams", grams));
        chip.title = (s && s.name) || "";
      } else {
        chip.appendChild(el("div", null, "empty"));
      }
      strip.appendChild(chip);
    }
  }

  /* ================= jobs list ================= */
  var jobsList = $("#jobs-list"), jobsEmpty = $("#jobs-empty");

  async function refreshJobs() {
    try {
      var r = await apiJson("/api/jobs");
      if (r.ok && r.data && Array.isArray(r.data.jobs)) {
        state.jobs = r.data.jobs;
      }
    } catch (e) {
      // Keep the last good list; polling will retry.
    }
    renderJobs();
    manageJobsPolling();
    refreshCamera();
  }
  function manageJobsPolling() {
    var active = state.jobs.some(function (j) { return TERMINAL.indexOf(j.status) < 0; });
    if (active && !state.jobsTimer) {
      state.jobsTimer = setInterval(refreshJobs, 5000);
    } else if (!active && state.jobsTimer) {
      clearInterval(state.jobsTimer);
      state.jobsTimer = null;
    }
  }

  /* ================= live camera ================= */
  var camTimer = null;
  function stopCamera() {
    if (camTimer) { clearInterval(camTimer); camTimer = null; }
    var img = $("#cam-img");
    if (img) {
      img.removeAttribute("src");
      img.onerror = img.onload = null;
      hide(img);
    }
    hide($("#cam-note"));
  }
  async function refreshCamera() {
    var card = $("#camera-card");
    if (!card) return;
    var printing = (state.jobs || []).some(function (j) {
      return j.status === "printing" || j.status === "uploading";
    });
    if (!printing) { hide(card); stopCamera(); return; }
    var img = $("#cam-img");
    if (!card.hidden && img.getAttribute("src")) return; // already streaming
    var r;
    try {
      r = await apiJson("/api/camera");
    } catch (e) {
      return; // try again on the next jobs poll
    }
    if (!r.ok || !r.data || !r.data.available) { hide(card); return; }
    show(card);
    var cam = r.data;
    $("#cam-open").href = cam.stream_url;
    var note = $("#cam-note");
    stopCamera();
    show(img);
    img.onerror = function () {
      img.onerror = null;
      // MJPEG didn't load (mixed content, camera busy...): poll snapshots.
      note.textContent = "Live stream didn't load — refreshing a snapshot every few seconds instead.";
      show(note);
      var tick = function () {
        img.src = cam.snapshot_url +
          (cam.snapshot_url.indexOf("?") >= 0 ? "&" : "?") + "t=" + Date.now();
      };
      tick();
      camTimer = setInterval(tick, 3000);
    };
    img.onload = function () { hide(note); };
    img.alt = "Live view of the printer (" + (cam.name || "camera") + ")";
    img.src = cam.stream_url;
  }

  function etaText(job) {
    // Remaining print time from the slicer's estimate and live progress.
    var total = job.estimates && job.estimates.time_s;
    if (!total || total <= 0) return null;
    var p = (job.progress === null || job.progress === undefined) ? 0 : job.progress;
    var remain = Math.max(0, total * (1 - p / 100));
    if (remain < 60) return "finishing up";
    var mins = Math.round(remain / 60);
    if (mins < 60) return "~" + mins + " min left";
    var h = Math.floor(mins / 60), m = mins % 60;
    return "~" + h + "h" + (m ? " " + m + "m" : "") + " left";
  }

  function jobStatusLine(job) {
    var s = job.status;
    if (s === "printing") {
      var pct = (job.progress === null || job.progress === undefined) ? null : Math.round(job.progress);
      var label = pct === null ? "Printing" : "Printing — " + pct + "%";
      if (job.printer_state && /paused/i.test(job.printer_state)) label = "Paused";
      else {
        var eta = etaText(job);
        if (eta) label += " · " + eta;
      }
      return label;
    }
    return STATUS_TEXT[s] || String(s);
  }

  function renderJobs() {
    jobsList.textContent = "";
    jobsEmpty.hidden = state.jobs.length > 0;
    state.jobs.forEach(function (job) { jobsList.appendChild(jobCard(job)); });
  }

  function jobCard(job) {
    var card = el("div", "job");
    card.dataset.jobId = job.id;

    var top = el("div", "job-top");
    if (job.thumbnail) {
      var img = el("img", "job-thumb");
      img.src = job.thumbnail;   // backend thumbnail endpoint
      img.alt = "";
      top.appendChild(img);
    }
    var meta = el("div");
    meta.appendChild(el("p", "job-title", job.title || "Print"));
    var st = el("p", "job-status");
    var strong = el("strong", null, jobStatusLine(job));
    st.appendChild(strong);
    meta.appendChild(st);
    var opts = [];
    if (job.scale && (job.scale.x !== 1 || job.scale.y !== 1 || job.scale.z !== 1)) {
      var pct = function (v) { return Math.round(v * 100) + "%"; };
      opts.push("scaled " + pct(job.scale.x) + " × " + pct(job.scale.y) + " × " + pct(job.scale.z));
    }
    if (job.copies && job.copies > 1) opts.push(job.copies + " copies");
    if (job.quality && job.quality !== "standard") opts.push(job.quality);
    if (job.timelapse) opts.push("timelapse on");
    if (opts.length) meta.appendChild(el("p", "job-estimate", opts.join(" · ")));
    if (job.status === "done") {
      if (job.final_snapshot) {
        var snap = el("img", "job-snapshot");
        snap.src = job.final_snapshot;
        snap.alt = "Finished print";
        snap.loading = "lazy";
        meta.appendChild(snap);
      }
      if (job.timelapse_video) {
        var tl = el("a", "job-timelapse", "▶ Timelapse video");
        tl.href = job.timelapse_video;
        tl.target = "_blank";
        tl.rel = "noopener";
        meta.appendChild(tl);
      }
    }
    if (job.estimates && (job.estimates.time_s !== null || job.estimates.filament_g !== null)) {
      var bits = [];
      var dur = formatDuration(job.estimates.time_s);
      if (dur) bits.push("about " + dur);
      if (job.estimates.filament_g !== null && job.estimates.filament_g !== undefined) {
        bits.push(Math.round(job.estimates.filament_g) + " g of filament");
      }
      if (bits.length) meta.appendChild(el("p", "job-estimate", bits.join(" · ")));
    }
    top.appendChild(meta);
    card.appendChild(top);

    if (job.status === "printing" && job.progress !== null && job.progress !== undefined) {
      var bar = el("div", "progress");
      var fill = el("div");
      fill.style.width = Math.max(0, Math.min(100, job.progress)) + "%";
      bar.appendChild(fill);
      card.appendChild(bar);
    }

    if (job.error) {
      card.appendChild(el("p", "job-error", "⚠ " + job.error));
    }

    var actions = el("div", "job-actions");
    addActions(actions, job);
    card.appendChild(actions);

    if (state.expandedLogs[job.id] && Array.isArray(job.log) && job.log.length) {
      var logText = job.log.map(function (l) {
        return (l.ts || "") + " [" + (l.level || "info") + "] " + (l.msg || "");
      }).join("\n");
      var copyBtn = el("button", "btn small", "Copy log");
      copyBtn.type = "button";
      copyBtn.addEventListener("click", function () {
        var full = (job.error ? "Error: " + job.error + "\n\n" : "") + logText;
        copyText(full, copyBtn);
      });
      card.appendChild(copyBtn);
      var pre = el("pre", "job-log");
      pre.textContent = logText;
      card.appendChild(pre);
    }
    return card;
  }

  function copyText(text, btn) {
    function done(ok) {
      var label = btn.textContent;
      btn.textContent = ok ? "Copied ✓" : "Copy failed";
      setTimeout(function () { btn.textContent = label; }, 1500);
    }
    // navigator.clipboard needs a secure context (often missing on plain
    // http:// LAN URLs), so fall back to the textarea trick.
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(function () { done(true); },
        function () { fallbackCopy(text, done); });
    } else {
      fallbackCopy(text, done);
    }
  }
  function fallbackCopy(text, done) {
    try {
      var ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      var ok = document.execCommand("copy");
      document.body.removeChild(ta);
      done(!!ok);
    } catch (e) {
      done(false);
    }
  }

  function btn(label, action, jobId, cls) {
    var b = el("button", "btn small" + (cls ? " " + cls : ""), label);
    b.type = "button";
    b.dataset.action = action;
    b.dataset.jobId = jobId;
    return b;
  }

  function addActions(actions, job) {
    var id = job.id;
    if (job.status === "awaiting_confirm") {
      actions.appendChild(btn("Check colors & print", "confirm", id, "primary"));
    }
    if (job.status === "printing") {
      var paused = job.printer_state && /paused/i.test(job.printer_state);
      actions.appendChild(btn(paused ? "Resume" : "Pause", paused ? "resume" : "pause", id));
      actions.appendChild(btn("Cancel", "cancel", id, "danger"));
    }
    if (job.status === "failed") {
      if (job.error_code === "needs_file") {
        actions.appendChild(btn("Attach the 3MF file", "attach", id, "primary"));
      } else if (job.source_url) {
        actions.appendChild(btn("Try again", "retry", id, "primary"));
      }
    }
    if (job.status === "done" || job.status === "failed" || job.status === "cancelled") {
      actions.appendChild(btn("Reprint", "reprint", id));
    }
    actions.appendChild(btn("Details", "details", id));
    actions.appendChild(btn("Remove", "remove", id, "danger"));
    // hidden file input for "Attach the 3MF file"
    var fi = document.createElement("input");
    fi.type = "file"; fi.accept = ".3mf"; fi.hidden = true;
    fi.dataset.jobId = id;
    fi.addEventListener("change", function () { attachFile(id, fi); });
    actions.appendChild(fi);
  }

  /* ---------- job actions (event delegation) ---------- */
  jobsList.addEventListener("click", async function (e) {
    var t = e.target.closest("[data-action]");
    if (!t) return;
    var action = t.dataset.action, id = t.dataset.jobId;
    t.disabled = true;
    try {
      if (action === "confirm") { await openConfirm(id); }
      else if (action === "pause" || action === "resume" || action === "cancel") {
        await jobControl(id, action);
      }
      else if (action === "retry") {
        var job = state.jobs.find(function (j) { return String(j.id) === String(id); });
        if (job && job.source_url && confirm("Try this print again from the same link?")) {
          await createJobFromUrl(job.source_url);
        }
      }
      else if (action === "attach") {
        var fi = jobsList.querySelector('input[type="file"][data-job-id="' + id + '"]');
        if (fi) fi.click();
      }
      else if (action === "reprint") {
        var rr = await apiJson("/api/jobs/" + encodeURIComponent(id) + "/reprint", { method: "POST" });
        if (rr.status === 201 || rr.ok) {
          refreshJobs();
          window.scrollTo({ top: $("#jobs-section").offsetTop - 60, behavior: "smooth" });
        } else {
          alert(friendlyError(rr.status, rr.data, "Couldn't start the reprint. Try again."));
        }
      }
      else if (action === "details") {
        state.expandedLogs[id] = !state.expandedLogs[id];
        renderJobs();
      }
      else if (action === "remove") {
        if (confirm("Remove this print from the list?")) await removeJob(id);
      }
    } finally {
      t.disabled = false;
    }
  });

  async function jobControl(id, action) {
    var r = await apiJson("/api/jobs/" + encodeURIComponent(id) + "/" + action, { method: "POST" });
    if (!r.ok) {
      alertFriendly(friendlyError(r.status, r.data, "Couldn't " + action + " the print right now."));
      return;
    }
    refreshJobs();
  }
  async function removeJob(id) {
    var r = await apiJson("/api/jobs/" + encodeURIComponent(id), { method: "DELETE" });
    if (!(r.status === 204 || r.ok)) {
      alertFriendly(friendlyError(r.status, r.data, "Couldn't remove that print."));
      return;
    }
    delete state.expandedLogs[id];
    refreshJobs();
  }
  function alertFriendly(msg) {
    // window.alert with a plain message (never raw JSON)
    window.alert(msg);
  }

  async function attachFile(id, input) {
    if (!input.files || !input.files[0]) return;
    var fd = new FormData();
    fd.append("file", input.files[0]);
    var r = await apiJson("/api/jobs/" + encodeURIComponent(id) + "/file", { method: "POST", body: fd });
    input.value = "";
    if (!r.ok) {
      alertFriendly(friendlyError(r.status, r.data, "Couldn't attach that file. Is it a 3MF?"));
      return;
    }
    refreshJobs();
  }

  /* ================= new print: URL ================= */
  $("#print-btn").addEventListener("click", async function () {
    var input = $("#url-input");
    var url = input.value.trim();
    var err = $("#url-error");
    if (!url) { setError(err, "Paste a link first — for example a Printables or MakerWorld model page."); return; }
    setError(err, null);
    await maybePickFile(url);
  });

  async function createJobFromUrl(url, fileId) {
    var err = $("#url-error");
    setError(err, null);
    var body = { source_url: url };
    if (fileId) body.file_id = fileId;
    var r = await apiJson("/api/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    });
    if (r.status === 201 || r.ok) {
      $("#url-input").value = "";
      refreshJobs();
      window.scrollTo({ top: $("#jobs-section").offsetTop - 60, behavior: "smooth" });
      return;
    }
    if (r.status === 422) {
      setError(err, "That link didn't look right. Paste the full model page URL and try again.");
    } else {
      setError(err, friendlyError(r.status, r.data, "Couldn't start that print. Please try again."));
    }
  }

  /* ---- file picker: when a Printables page has several files, let the
     user choose instead of silently taking the first one ---- */
  async function maybePickFile(url) {
    // non-Printables URLs keep the old direct path
    if (!/printables\.com\/model\/\d+/i.test(url)) {
      await createJobFromUrl(url);
      return;
    }
    var err = $("#url-error");
    var btn = $("#print-btn");
    btn.disabled = true;
    var oldText = btn.textContent;
    btn.textContent = "Checking files…";
    try {
      var r = await apiJson("/api/printables/files", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url: url })
      });
      if (!r.ok || !r.data || !Array.isArray(r.data.files) || !r.data.files.length) {
        await createJobFromUrl(url); // fall back: backend picks like before
        return;
      }
      if (r.data.files.length === 1) {
        await createJobFromUrl(url, r.data.files[0].id);
        return;
      }
      openFilePicker(r.data.title, r.data.files, url);
    } finally {
      btn.disabled = false;
      btn.textContent = oldText;
    }
  }

  function openFilePicker(title, files, url) {
    var backdrop = $("#file-backdrop");
    $("#file-title").textContent = title || "Choose a file";
    var list = $("#file-list");
    list.textContent = "";
    files.forEach(function (f) {
      var b = el("button", "btn file-option");
      b.type = "button";
      var kind = f.kind === "3mf" ? "3MF" : "STL";
      b.textContent = kind + " — " + (f.name || f.id);
      b.addEventListener("click", function () {
        closeFilePicker();
        createJobFromUrl(url, f.id);
      });
      list.appendChild(b);
    });
    show(backdrop);
    document.body.style.overflow = "hidden";
  }
  function closeFilePicker() {
    hide($("#file-backdrop"));
    document.body.style.overflow = "";
  }

  /* ================= new print: file upload ================= */
  $("#file-input").addEventListener("change", async function () {
    var input = $("#file-input");
    var err = $("#upload-error");
    setError(err, null);
    if (!input.files || !input.files[0]) return;
    $("#file-name").textContent = input.files[0].name;
    var fd = new FormData();
    fd.append("file", input.files[0]);
    var r = await apiJson("/api/jobs/upload", { method: "POST", body: fd });
    input.value = "";
    if (r.status === 201 || r.ok) {
      $("#file-name").textContent = "";
      refreshJobs();
      window.scrollTo({ top: $("#jobs-section").offsetTop - 60, behavior: "smooth" });
      return;
    }
    setError(err, friendlyError(r.status, r.data, "Couldn't upload that file. It needs to be a 3MF."));
  });

  /* ================= confirm modal ================= */
  var backdrop = $("#modal-backdrop");
  var modalJobTitle = $("#modal-job-title");
  var modalToolheads = $("#modal-toolheads");
  var modalSlots = $("#modal-slots");
  var modalError = $("#modal-error");
  var plateRow = $("#plate-row");
  var plateSelect = $("#plate-select");
  var confirmStart = $("#confirm-start");

  async function openConfirm(id) {
    setError(modalError, null);
    modalJobTitle.textContent = "Loading…";
    modalToolheads.textContent = "";
    modalSlots.textContent = "";
    hide(plateRow);
    show(backdrop);
    document.body.style.overflow = "hidden";

    var r = await apiJson("/api/jobs/" + encodeURIComponent(id));
    if (!r.ok || !r.data || !r.data.job) {
      setError(modalError, friendlyError(r.status, r.data, "Couldn't load the color plan. Close and try again."));
      modalJobTitle.textContent = "";
      return;
    }
    var job = r.data.job;
    if (state.toolheads === null && !state.toolheadsUnavailable) await refreshSpools();

    var plan = Array.isArray(job.plan) ? job.plan.slice() : [];
    plan.sort(function (a, b) { return (a.slot || 0) - (b.slot || 0); });

    // Initial toolhead per row: the Spoolman-location toolhead of the proposed
    // spool (== the slot's positional toolhead), else the positional default.
    // Deliberately NOT derived from printer-reported colors: those can be
    // stale/misordered and used to bias every row toward T0.
    var toolSel = {};
    plan.forEach(function (p, i) {
      var key = String(i);
      var th = (typeof p.spool_toolhead === "number") ? p.spool_toolhead : null;
      if (th === null) {
        var tm = /^T([0-3])$/.exec(p.toolhead || "");
        th = tm ? parseInt(tm[1], 10) : Math.min(i, 3);
      }
      toolSel[key] = th;
    });
    state.modal = { job: job, plan: plan, toolSel: toolSel, plate: job.plate || 0,
                    scale: { mode: "pct", x: "100", y: "100", z: "100", linked: true },
                    copies: 1 };
    renderModal();
    resetSizeUI(job);
  }

  function closeModal() {
    hide(backdrop);
    document.body.style.overflow = "";
    state.modal = null;
  }
  $("#confirm-cancel").addEventListener("click", closeModal);
  backdrop.addEventListener("click", function (e) { if (e.target === backdrop) closeModal(); });

  function renderModal() {
    var m = state.modal;
    if (!m) return;
    var job = m.job;
    modalJobTitle.textContent = job.title || "";
    modalToolheads.textContent = "";
    modalSlots.textContent = "";
    setError(modalError, null);

    // What's actually loaded on each toolhead (printer-reported).
    for (var n = 0; n < 4; n++) {
      var e = toolheadEntry(n);
      var loaded = e && (e.color_hex || e.spool);
      var chip = el("div", "spool-chip" + (loaded ? "" : " empty"));
      chip.appendChild(el("div", "slot", "T" + n));
      if (loaded) {
        var dot = el("span", "color-dot");
        dot.style.backgroundColor = safeHex(e.color_hex);
        chip.appendChild(dot);
        var nm = colorName(e.color_hex);
        chip.appendChild(el("div", "mat", ((nm ? nm + " " : "") + (e.material || "")).trim()));
      } else {
        chip.appendChild(el("div", null, "empty"));
      }
      modalToolheads.appendChild(chip);
    }
    if (state.toolheadsUnavailable) {
      modalToolheads.appendChild(el("p", "hint", "Couldn't read the toolheads — is the printer on?"));
    }

    if (!m.plan.length) {
      modalSlots.appendChild(el("p", "hint", "This model doesn't need multiple colors — every slot can stay as-is."));
    }

    m.plan.forEach(function (p, i) {
      var key = String(i);
      var thIdx = m.toolSel[key];
      var entry = toolheadEntry(thIdx);

      var row = el("div", "slot-row");

      var line = el("div", "slot-line");
      var sw = el("span", "swatch");
      sw.style.backgroundColor = safeHex(p.required_hex);
      line.appendChild(sw);
      var needName = colorName(p.required_hex);
      line.appendChild(el("span", "need",
        "Needs " + (needName ? needName + " " : "") + (p.required_material || "filament")));
      row.appendChild(line);

      // Toolhead picker: only loaded toolheads are selectable.
      var sel = document.createElement("select");
      sel.setAttribute("aria-label", "Which toolhead prints this color");
      for (var t = 0; t < 4; t++) {
        var te = toolheadEntry(t);
        var isLoaded = te && (te.color_hex || te.spool);
        var o = document.createElement("option");
        o.value = String(t);
        o.textContent = isLoaded ? "Print from " + toolheadLabel(t) : "T" + t + " — empty";
        o.disabled = !isLoaded;
        if (t === thIdx) o.selected = true;
        sel.appendChild(o);
      }
      if (sel.options[sel.selectedIndex] && sel.options[sel.selectedIndex].disabled) {
        for (var t2 = 0; t2 < 4; t2++) {
          if (!sel.options[t2].disabled) { sel.selectedIndex = t2; m.toolSel[key] = t2; break; }
        }
      }
      (function (k) {
        sel.addEventListener("change", function () {
          m.toolSel[k] = parseInt(sel.value, 10);
          renderModal();
        });
      })(key);
      row.appendChild(sel);

      // Match badge: required color vs what's on the chosen toolhead.
      var badgeText, badgeCls;
      var haveColor = entry && (entry.color_hex || (entry.spool && entry.spool.color_hex));
      if (!haveColor) {
        badgeText = "⚠ empty — pick a loaded toolhead"; badgeCls = "badge warn";
      } else if (!p.required_hex) {
        badgeText = "✓ prints as loaded"; badgeCls = "badge ok";
      } else {
        var d = colorDistance(p.required_hex, entry.color_hex || entry.spool.color_hex);
        if (d < 3000) { badgeText = "✓ looks good"; badgeCls = "badge ok"; }
        else { badgeText = "⚠ different color — check it"; badgeCls = "badge warn"; }
      }
      row.appendChild(el("p", badgeCls, badgeText));

      modalSlots.appendChild(row);
    });

    if (job.plates && job.plates > 1) {
      show(plateRow);
      plateSelect.textContent = "";
      for (var n = 0; n < job.plates; n++) {
        var o = document.createElement("option");
        o.value = String(n);
        o.textContent = "Part " + (n + 1) + " of " + job.plates;
        if (n === (m.plate || 0)) o.selected = true;
        plateSelect.appendChild(o);
      }
    } else {
      hide(plateRow);
    }

    // Final preview: model color -> chosen toolhead, side by side, so the
    // mapping can be double-checked before Start print is tapped.
    var pv = $("#modal-preview");
    pv.textContent = "";
    if (m.plan.length) {
      pv.appendChild(el("h3", "preview-title", "Preview — how it will print in your colors"));
      var renderWrap = el("div", "preview-render-wrap");
      var renderMsg = el("div", "preview-render-msg", "Rendering color preview\u2026");
      renderWrap.appendChild(renderMsg);
      var render = document.createElement("img");
      render.className = "preview-render";
      render.alt = "";
      render.style.display = "none";
      var cmap = m.plan.map(function (p, i) {
        // Color that will print = the chosen spool's color (Spoolman record),
        // falling back to the model's required color. Keyed by PHYSICAL
        // toolhead: the slicer rewrites G-code T-commands to the picks.
        var hx = (p.spool && p.spool.color_hex) || p.required_hex;
        hx = String(hx || "").replace("#", "").toUpperCase();
        if (!/^[0-9A-F]{6}$/.test(hx)) hx = "808080";
        return m.toolSel[String(i)] + ":" + hx;
      }).join(",");
      render.onload = function () { renderMsg.remove(); render.style.display = ""; };
      render.onerror = function () { renderWrap.remove(); };
      render.src = "/api/jobs/" + encodeURIComponent(job.id) + "/color-preview?colors=" + encodeURIComponent(cmap);
      renderWrap.appendChild(render);
      pv.appendChild(renderWrap);
      // Scale-aware grams: plan grams are for 100% x1; adjust live.
      var sf = scaleFactors();
      var gscale = (sf.x || 1) * (sf.y || 1) * (sf.z || 1) * (m.copies || 1);
      m.plan.forEach(function (p, i) {
        var thIdx = m.toolSel[String(i)];
        var entry = toolheadEntry(thIdx);
        // Spoolman spool color first (authoritative); printer color fallback.
        var spool = p.spool || (entry && entry.spool);
        var loadedHex = (spool && spool.color_hex) || (entry && entry.color_hex);
        var row = el("div", "preview-row");
        var need = el("div", "preview-side");
        var nsw = el("span", "swatch");
        nsw.style.backgroundColor = safeHex(p.required_hex);
        need.appendChild(nsw);
        var nn = colorName(p.required_hex);
        need.appendChild(el("span", null,
          "Needs " + (nn ? nn + " " : "") + (p.required_material || "filament")));
        if (typeof p.grams === "number" && p.grams > 0) {
          var gspan = el("span", "row-grams");
          gspan.setAttribute("data-base-grams", p.grams);
          gspan.textContent = " \u00B7 ~" + (p.grams * gscale).toFixed(1) + " g max";
          need.appendChild(gspan);
        }
        row.appendChild(need);
        row.appendChild(el("span", "preview-arrow", "\u2192"));
        var got = el("div", "preview-side");
        var gsw = el("span", "swatch");
        gsw.style.backgroundColor = safeHex(loadedHex);
        got.appendChild(gsw);
        var lbl = toolheadLabel(thIdx);
        if (entry && entry.mismatch) lbl += " \u26A0";
        got.appendChild(el("span", null, lbl));
        row.appendChild(got);
        var ok = !!loadedHex && (!p.required_hex || colorDistance(p.required_hex, loadedHex) < 3000);
        row.appendChild(el("span", "preview-verdict " + (ok ? "ok" : "warn"), ok ? "\u2713" : "\u26A0"));
        pv.appendChild(row);
        if (entry && entry.mismatch) {
          pv.appendChild(el("div", "preview-mismatch",
            "T" + thIdx + ": Spoolman and the printer disagree on the loaded color. " +
            "Check the spool or update its Spoolman location."));
        }
      });
    }
  }

  plateSelect.addEventListener("change", function () {
    if (state.modal) state.modal.plate = parseInt(plateSelect.value, 10) || 0;
  });

  /* ---- size (scale) + copies ---- */
  function updateGramsUI() {
    var m = state.modal;
    if (!m) return;
    var sf = scaleFactors();
    var gscale = (sf.x || 1) * (sf.y || 1) * (sf.z || 1) * (m.copies || 1);
    document.querySelectorAll("#modal-slots .row-grams").forEach(function (sp) {
      var base = parseFloat(sp.getAttribute("data-base-grams")) || 0;
      sp.textContent = " \u00B7 ~" + (base * gscale).toFixed(1) + " g max";
    });
  }

  function scaleFactors() {
    var m = state.modal;
    if (!m) return { x: 1, y: 1, z: 1 };
    var s = m.scale || { mode: "pct", x: "100", y: "100", z: "100" }, d = m.job.dimensions;
    if (s.mode === "mm" && d && d.x > 0 && d.y > 0 && d.z > 0) {
      return {
        x: (parseFloat(s.x) || 0) / d.x,
        y: (parseFloat(s.y) || 0) / d.y,
        z: (parseFloat(s.z) || 0) / d.z
      };
    }
    return {
      x: (parseFloat(s.x) || 100) / 100,
      y: (parseFloat(s.y) || 100) / 100,
      z: (parseFloat(s.z) || 100) / 100
    };
  }
  function scaledDims() {
    var m = state.modal;
    if (!m || !m.job.dimensions) return null;
    var f = scaleFactors(), d = m.job.dimensions;
    return { x: d.x * f.x, y: d.y * f.y, z: d.z * f.z };
  }
  function maxCopies() {
    var sd = scaledDims();
    if (!sd) return 25;
    var gap = 8, bed = 270;
    var cols = Math.max(1, Math.floor((bed + gap) / (sd.x + gap)));
    var rows = Math.max(1, Math.floor((bed + gap) / (sd.y + gap)));
    return Math.min(25, cols * rows);
  }
  function updateSizeUI() {
    var m = state.modal;
    if (!m) return;
    var d = m.job.dimensions;
    $("#size-dims").textContent = d ? ("model is " + d.x + " × " + d.y + " × " + d.z + " mm") : "";
    var sd = scaledDims(), hint = "";
    if (sd) {
      hint = "≈ " + Math.round(sd.x) + " × " + Math.round(sd.y) + " × " + Math.round(sd.z) + " mm";
      if (sd.x > 270 || sd.y > 270 || sd.z > 270) hint += " — too big for the 270 mm bed";
    }
    $("#scale-hint").textContent = hint;
    var mc = maxCopies(), ci = $("#copies-input");
    ci.max = mc;
    $("#copies-hint").textContent = mc < 25 ? ("up to " + mc + " fit on the bed") : "";
    if (m.copies > mc) { m.copies = mc; ci.value = mc; }
  }
  function readScaleInputs() {
    var m = state.modal;
    if (!m) return;
    var vals = {
      x: $("#scale-x").value, y: $("#scale-y").value, z: $("#scale-z").value
    };
    if (m.scale.linked) {
      // propagate the changed axis to the others; find which one changed
      var changed = ["x", "y", "z"].filter(function (ax) { return vals[ax] !== m.scale[ax]; });
      var src = changed.length ? changed[changed.length - 1] : "x";
      m.scale.x = m.scale.y = m.scale.z = vals[src];
      $("#scale-x").value = $("#scale-y").value = $("#scale-z").value = vals[src];
    } else {
      m.scale.x = vals.x; m.scale.y = vals.y; m.scale.z = vals.z;
    }
    updateSizeUI();
    updateGramsUI();
  }
  ["x", "y", "z"].forEach(function (ax) {
    $("#scale-" + ax).addEventListener("input", readScaleInputs);
  });
  $("#scale-link").addEventListener("click", function () {
    var m = state.modal;
    if (!m) return;
    m.scale.linked = !m.scale.linked;
    $("#scale-link").textContent = m.scale.linked ? "🔗" : "🔓";
    $("#scale-link").title = m.scale.linked ? "Keep proportions locked" : "Scale axes independently";
    if (m.scale.linked) readScaleInputs();
  });
  document.querySelectorAll("#scale-mode-seg .seg-btn").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var m = state.modal;
      if (!m) return;
      document.querySelectorAll("#scale-mode-seg .seg-btn").forEach(function (b) {
        b.classList.toggle("active", b === btn);
      });
      var prev = m.scale.mode;
      m.scale.mode = btn.getAttribute("data-mode");
      var d = m.job.dimensions;
      // convert current values between % and mm when dimensions are known
      if (d && prev !== m.scale.mode) {
        ["x", "y", "z"].forEach(function (ax) {
          var v = parseFloat(m.scale[ax]) || 0;
          m.scale[ax] = m.scale.mode === "mm"
            ? String(Math.round(v / 100 * d[ax] * 10) / 10)
            : String(Math.round(v / d[ax] * 1000) / 10);
        });
        $("#scale-x").value = m.scale.x;
        $("#scale-y").value = m.scale.y;
        $("#scale-z").value = m.scale.z;
      }
      updateSizeUI();
    });
  });
  $("#copies-input").addEventListener("input", function () {
    var m = state.modal;
    if (!m) return;
    var v = parseInt($("#copies-input").value, 10);
    m.copies = isNaN(v) ? 1 : Math.min(Math.max(v, 1), maxCopies());
    updateGramsUI();
    updateSizeUI();
  });
  function resetSizeUI(job) {
    var m = state.modal;
    m.scale = { mode: "pct", x: "100", y: "100", z: "100", linked: true };
    m.copies = 1;
    $("#scale-x").value = $("#scale-y").value = $("#scale-z").value = "100";
    $("#copies-input").value = "1";
    $("#scale-link").textContent = "🔗";
    var qs = $("#quality-select");
    if (qs) qs.value = "standard";
    var tl = $("#timelapse-check");
    if (tl) tl.checked = false;
    var ss = $("#supports-select");
    if (ss) ss.value = "model";
    document.querySelectorAll("#scale-mode-seg .seg-btn").forEach(function (b) {
      b.classList.toggle("active", b.getAttribute("data-mode") === "pct");
    });
    // mm mode needs measured dimensions; without them % is the only option
    $("#scale-mode-seg").style.display = job.dimensions ? "" : "none";
    updateSizeUI();
  }

  $("#file-cancel").addEventListener("click", closeFilePicker);
  $("#file-backdrop").addEventListener("click", function (e) {
    if (e.target === $("#file-backdrop")) closeFilePicker();
  });

  confirmStart.addEventListener("click", async function () {
    var m = state.modal;
    if (!m) return;
    confirmStart.disabled = true;
    setError(modalError, null);
    // slot_toolheads: {paletteSlot: physicalToolhead} -- the slicer rewrites
    // G-code T-commands so the picks actually take effect. The backend reads
    // the printer's live toolhead state (color/material per T0..T3) for the
    // filament profiles; Spoolman is no longer in the critical path.
    var slotToolheads = {};
    m.plan.forEach(function (p, i) {
      var slotKey = String(p.slot !== undefined && p.slot !== null ? p.slot : i);
      slotToolheads[slotKey] = m.toolSel[String(i)];
    });
    var r = await apiJson("/api/jobs/" + encodeURIComponent(m.job.id) + "/confirm", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        slot_spools: {},
        slot_toolheads: slotToolheads,
        plate: m.plate || 0,
        scale: scaleFactors(),
        copies: Math.min(Math.max(parseInt(m.copies, 10) || 1, 1), 25),
        quality: $("#quality-select") ? $("#quality-select").value : "standard",
        timelapse: !!($("#timelapse-check") && $("#timelapse-check").checked),
        supports: $("#supports-select") ? $("#supports-select").value : "model"
      })
    });
    confirmStart.disabled = false;
    if (r.status === 202 || r.ok) {
      closeModal();
      refreshSpools();
      refreshJobs();
      return;
    }
    if (r.status === 409) {
      var d = r.data || {};
      var msg = typeof d.error === "string" && d.error ? d.error
        : typeof d.message === "string" && d.message ? d.message
        : typeof d.detail === "string" && d.detail ? d.detail
        : (d.printer_state ? "The printer isn't ready: " + d.printer_state + "." : null);
      setError(modalError, msg || "The printer isn't ready to start this right now. Try again in a bit.");
      return;
    }
    setError(modalError, friendlyError(r.status, r.data, "Couldn't start the print. Please try again."));
  });

  /* ================= theme (dark by default) ================= */
  var themeToggle = $("#theme-toggle");
  function applyTheme(theme) {
    document.documentElement.setAttribute("data-theme", theme);
    try { localStorage.setItem("pb-theme", theme); } catch (e) { /* private mode */ }
    themeToggle.textContent = theme === "dark" ? "☀️" : "🌙";
    themeToggle.setAttribute("aria-label",
      theme === "dark" ? "Switch to light theme" : "Switch to dark theme");
  }
  (function initTheme() {
    var stored = null;
    try { stored = localStorage.getItem("pb-theme"); } catch (e) { /* ignore */ }
    applyTheme(stored === "light" ? "light" : "dark");
  })();
  themeToggle.addEventListener("click", function () {
    var cur = document.documentElement.getAttribute("data-theme") || "dark";
    applyTheme(cur === "dark" ? "light" : "dark");
  });

  /* ================= boot ================= */
  refreshHealth();
  setInterval(refreshHealth, 30000);
  loadConfig();
  refreshSpools();
  refreshJobs();
})();
