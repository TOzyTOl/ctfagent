/* CTFAGENT front end: no framework, no build step.
   Jobs: keep the task rail live, render the conversation and the event log as
   they stream, and keep chat on the page (a chat that reloads the document on
   every turn is not a chat). */

(function () {
  "use strict";

  // ---------------------------------------------------------------- helpers

  function relTime(iso) {
    if (!iso) return "";
    // SQLite writes "YYYY-MM-DD HH:MM:SS" in UTC; make that explicit.
    var text = String(iso).replace(" ", "T");
    if (!/[Zz]|[+-]\d\d:?\d\d$/.test(text)) text += "Z";
    var then = Date.parse(text);
    if (isNaN(then)) return "";
    var s = Math.max(0, Math.round((Date.now() - then) / 1000));
    if (s < 5) return "刚刚";
    if (s < 60) return s + "秒前";
    if (s < 3600) return Math.floor(s / 60) + "分前";
    if (s < 86400) return Math.floor(s / 3600) + "小时前";
    return Math.floor(s / 86400) + "天前";
  }

  function refreshRelTimes() {
    document.querySelectorAll("[data-ts]").forEach(function (el) {
      var text = relTime(el.getAttribute("data-ts"));
      if (text) el.textContent = text;
    });
  }

  // "How long has this been going" is the number an operator watches, so it
  // has to move on its own. The server renders the first value (the page is
  // correct without JS); this only keeps it current. Character-for-character
  // the same format as `_fmt_duration` in server.py — a mismatch would flicker
  // once a second as the two write over each other.
  function fmtDuration(seconds) {
    var s = Math.max(0, Math.round(seconds));
    if (s < 60) return s + "秒";
    if (s < 3600) return Math.floor(s / 60) + "分" + pad2(s % 60) + "秒";
    return Math.floor(s / 3600) + "小时" + pad2(Math.floor((s % 3600) / 60)) + "分";
  }

  function pad2(n) { return (n < 10 ? "0" : "") + n; }

  function utcMs(value) {
    if (!value) return NaN;
    var text = String(value).replace(" ", "T");
    if (!/[Zz]|[+-]\d\d:?\d\d$/.test(text)) text += "Z";
    return Date.parse(text);
  }

  function tickElapsed() {
    var now = Date.now();
    document.querySelectorAll("[data-elapsed-from]").forEach(function (el) {
      var from = utcMs(el.getAttribute("data-elapsed-from"));
      if (isNaN(from)) return;
      // An empty `to` is a span still running: it counts up to now.
      var to = utcMs(el.getAttribute("data-elapsed-to"));
      if (isNaN(to)) to = now;
      el.textContent = fmtDuration((to - from) / 1000);
    });
  }

  function toast(message) {
    var el = document.getElementById("toast");
    if (!el) {
      el = document.createElement("div");
      el.id = "toast";
      document.body.appendChild(el);
    }
    el.textContent = message;
    el.classList.add("on");
    clearTimeout(el._timer);
    el._timer = setTimeout(function () { el.classList.remove("on"); }, 2600);
  }

  function escapeHtml(text) {
    return String(text)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  // ---------------------------------------------------------------- clipboard
  //
  // `navigator.clipboard` needs a secure context. This is a local tool served
  // over 127.0.0.1, which counts as one, but the same server can be reached
  // from a phone on the LAN over plain http, where the API is absent — and a
  // copy button that silently does nothing is worse than no button. So the
  // fallback selects the text and tells the operator to press Ctrl+C, which is
  // what they would have done anyway.
  function copyText(text) {
    if (!text) return;
    var done = function () { toast("已复制"); };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done, function () { legacyCopy(text, done); });
    } else {
      legacyCopy(text, done);
    }
  }

  function legacyCopy(text, done) {
    var ta = document.createElement("textarea");
    ta.value = text;
    // Off-screen but still selectable; `display:none` would not be.
    ta.setAttribute("readonly", "");
    ta.style.cssText = "position:fixed;top:0;left:-9999px;opacity:0";
    document.body.appendChild(ta);
    ta.select();
    var ok = false;
    try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
    ta.remove();
    if (ok) done();
    else toast("复制失败，请手动选中");
  }

  // Display labels for the stored enum values. The database keeps the English
  // identifiers (they are the event names on the wire); only the console text
  // the operator reads is translated.
  var ROLE_LABEL = {
    main: "主 agent", unknown: "未分类", triage: "分诊", web: "Web", crypto: "密码", pwn: "Pwn", reverse: "逆向",
    misc: "杂项", forensics: "取证", malware: "恶意代码", ai_ml: "AI/ML",
    osint: "情报", verifier: "验证", writer: "报告"
  };

  var EVENT_LABEL = {
    "run.started": "运行开始", "run.finished": "运行结束",
    "run.needs_input": "等待确认", "run.question": "提问",
    "run.error": "运行出错", "run.warning": "运行警告", "run.writeup": "报告已生成",
    "run.writer_dispatched": "派发报告任务", "run.attachments": "附件更新",
    "skill.distill_dispatched": "派发技能沉淀", "skill.note_added": "技术笔记入库",
    "skill.note_failed": "技术笔记未入库",
    "task.dispatched": "派发任务", "task.started": "任务开始",
    "task.finished": "任务结束", "task.result": "任务结果",
    "fact.added": "黑板记录", "fact.merged": "重复记录已合并",
    "fact.downgraded": "证据不足已降级", "flag.proposed": "提交候选",
    "flag.verdict": "验证结论", "flag.confirmed": "flag 确认",
    "flag.confirm_pending": "等待验证复核", "flag.confirm_refuted": "确认被驳回",
    "challenge.categorized": "题目分类", "challenge.renamed": "题目更名",
    "chat.user": "对话", "main.message": "主 agent", "main.interrupted": "已打断",
    "agent.event": "代理", "session.error": "会话出错",
    "text_delta": "增量文本"
  };

  function roleLabel(role) {
    return ROLE_LABEL[role] || role || "";
  }

  function eventLabel(kind) {
    return EVENT_LABEL[kind] || kind;
  }

  window.ctfagent = { relTime: relTime, toast: toast };

  // ---------------------------------------------------------------- tabs
  //
  // Two independent tab groups share this code, distinguished by their
  // attribute pair: the main chat/report tabs and the side panel's
  // work-product tabs.

  var TAB_GROUPS = [
    { tab: "data-mtab", pane: "data-mpane", store: "ctfagent.mtab" },
    { tab: "data-stab", pane: "data-spane", store: "ctfagent.stab" }
  ];

  function selectTab(group, tab, animate) {
    document.querySelectorAll("[" + group.tab + "]").forEach(function (t) {
      t.classList.toggle("on", t === tab);
    });
    var name = tab.getAttribute(group.tab);
    document.querySelectorAll("[" + group.pane + "]").forEach(function (pane) {
      var show = pane.getAttribute(group.pane) === name;
      pane.hidden = !show;
      if (!show) { pane.classList.remove("tab-in"); return; }
      // Only a real click animates. On first paint the frame has its own
      // fade-in, and running both made the panel arrive twice.
      if (!animate) return;
      // Re-add rather than add: clicking the tab you are already on should
      // still respond, and a class that is already present animates nothing.
      pane.classList.remove("tab-in");
      void pane.offsetWidth;
      pane.classList.add("tab-in");
    });
    try { sessionStorage.setItem(group.store, name); } catch (e) {}
    if (name === "writeup" || name === "report") loadWriteup();
    if (name === "log") scrollConsole();
  }

  function initTabs() {
    TAB_GROUPS.forEach(function (group) {
      var tabs = document.querySelectorAll("[" + group.tab + "]");
      if (!tabs.length) return;
      tabs.forEach(function (tab) {
        tab.addEventListener("click", function () { selectTab(group, tab, true); });
      });
      // A deep link (#report) wins over the remembered tab.
      var fromHash = location.hash.replace("#", "");
      var target = fromHash && document.querySelector("[" + group.tab + '="' + fromHash + '"]');
      if (!target) {
        try {
          var saved = sessionStorage.getItem(group.store);
          if (saved) target = document.querySelector("[" + group.tab + '="' + saved + '"]');
        } catch (e) {}
      }
      if (target) selectTab(group, target, false);
    });
  }

  // ---------------------------------------------------------------- console

  var consoleState = { hideResults: true, events: 0 };

  function describe(kind, payload) {
    if (kind === "agent.event") {
      var role = payload.role || "";
      var inner = payload.kind || "";
      // Two workers of the same role run side by side; the task id is what
      // tells their lines apart.
      var who = roleLabel(role) +
        (payload.task_id ? " #" + payload.task_id : "");
      if (inner === "tool_use") {
        var input = payload.input ? JSON.stringify(payload.input) : "";
        return who + "  ▸ " + (payload.tool || "?") + "  " + input.slice(0, 300);
      }
      if (inner === "tool_result") {
        return who + "  ◂ 返回 " + (payload.is_error ? "（出错）" : "") +
               String(payload.content || "").slice(0, 240).replace(/\s+/g, " ");
      }
      if (inner === "text" || inner === "thinking") {
        return who + "  " + (inner === "text" ? "说" : "想") + "：" + String(payload.text || "").slice(0, 400).replace(/\s+/g, " ");
      }
      if (inner === "result") {
        return who + "  ✓ 结束  $" + Number(payload.cost_usd || 0).toFixed(3);
      }
      if (inner === "init") {
        return who + "  会话就绪" + (payload.skills && payload.skills.length ? "  技能 " + payload.skills.length + " 个" : "");
      }
      return "";
    }
    var summary =
      payload.summary || payload.objective || payload.content || payload.value ||
      payload.text || payload.note || payload.error || payload.reason || "";
    if (payload.task_id !== undefined && payload.task_id !== null && summary) {
      summary = "#" + payload.task_id + "  " + summary;
    }
    if (payload.role && summary && !payload.task_id) summary = roleLabel(payload.role) + "  " + summary;
    return String(summary).slice(0, 600);
  }

  // `enter` marks a line as one that just arrived. The backlog replayed from
  // the server on load must not have it: replaying eighty stored events with
  // the same 200ms ink would make the log flash on every page load. Only the
  // live stream passes it.
  function appendEvent(kind, payload, createdAt, enter) {
    var el = document.getElementById("console");
    if (!el) return;
    var line = describe(kind, payload);
    if (!line) return;
    if (consoleState.hideResults && kind === "agent.event" && payload.kind === "tool_result") return;

    var row = document.createElement("span");
    // A family class so the log can tint by kind without the CSS having to know
    // every event name: agent traffic is the loud one, a confirmed flag is the
    // only one that gets the solved colour.
    row.className = "ev k-" + String(kind || "").split(".")[0] + (enter ? " just-in" : "");
    var t = document.createElement("span");
    t.className = "t";
    t.textContent = (createdAt || "").slice(11, 19) + "  ";
    var k = document.createElement("span");
    k.className = "k";
    k.textContent = "[" + eventLabel(kind) + "] ";
    row.appendChild(t);
    row.appendChild(k);
    row.appendChild(document.createTextNode(line));

    var pinned = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
    el.appendChild(row);
    el.appendChild(document.createTextNode("\n"));
    consoleState.events += 1;
    if (pinned) el.scrollTop = el.scrollHeight;
  }

  function scrollConsole() {
    var el = document.getElementById("console");
    if (el) el.scrollTop = el.scrollHeight;
  }

  function initConsole() {
    var box = document.getElementById("console");
    if (!box) return;
    // The server renders the backlog; move it into the same DOM shape the live
    // stream produces so filtering and scrolling behave identically.
    var raw = box.textContent;
    box.textContent = "";
    raw.split("\n").forEach(function (line) {
      if (!line.trim()) return;
      var m = line.match(/^(\S+ \S+)\s+\[([^\]]+)\]\s+(\{.*\})$/);
      if (m) {
        try {
          appendEvent(m[2], JSON.parse(m[3]), m[1]);
          return;
        } catch (e) { /* fall through to raw */ }
      }
      var span = document.createElement("span");
      span.className = "ev";
      span.textContent = line;
      box.appendChild(span);
      box.appendChild(document.createTextNode("\n"));
    });
    box.scrollTop = box.scrollHeight;

    var toggle = document.getElementById("hide-results");
    if (toggle) {
      try {
        var saved = sessionStorage.getItem("ctfagent.hideResults");
        if (saved !== null) toggle.checked = saved === "1";
      } catch (e) {}
      consoleState.hideResults = toggle.checked;
      toggle.addEventListener("change", function () {
        consoleState.hideResults = toggle.checked;
        try { sessionStorage.setItem("ctfagent.hideResults", toggle.checked ? "1" : "0"); } catch (e) {}
        location.reload();
      });
    }
  }

  // ---------------------------------------------------------------- conversation

  function messagesBox() {
    return document.getElementById("messages");
  }

  // A run with a hundred messages turns the chat's scrollbar into a hairline.
  // Rather than drag it back, offer a button that only exists while the reader
  // is scrolled away from the newest message.
  // The app shells are sized with `calc(100vh - topbar)`, which silently loses
  // whatever else the header stacks on top of them. The stale-code banner is
  // one of those: 40px, only present after an edit, and it pushed the composer
  // exactly 40px below the fold. Publishing its real height as a variable lets
  // the shell subtract it without the stylesheet needing :has().
  function initShellOffset() {
    var banner = document.querySelector(".stale-banner");
    var root = document.documentElement;
    if (!banner) {
      root.style.removeProperty("--banner-h");
      return;
    }
    var apply = function () {
      root.style.setProperty("--banner-h", banner.offsetHeight + "px");
    };
    apply();
    window.addEventListener("resize", apply);
  }

  // ---------------------------------------------------------------- transitions

  // Every nav click is a full page load, so the browser replaces the document in
  // one frame and the shared top bar reads as a header that stayed put while the
  // body was swapped underneath it. Fading the frame out before following the
  // link — and in on arrival, via `.frame { animation: page-in }` — makes the
  // nav read as one surface.
  function initNavTransition() {
    var frame = document.querySelector(".frame");
    if (!frame) return;

    // Delegated rather than bound per link: the rail re-fetches its fragment
    // every 5 seconds and replaces `#rail-list`'s innerHTML, which would drop
    // any listener attached to a link at load time. One listener on the document
    // survives every refresh.
    //
    // The rail's delete button sits outside its <a> (it is a sibling form), so
    // `closest` correctly leaves it to the form submit path.
    document.addEventListener("click", function (e) {
      if (e.defaultPrevented || e.button !== 0) return;
      // Modified clicks open tabs or windows; let the browser own those.
      if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
      var link = e.target.closest
        ? e.target.closest(".topnav a, .brand, .rail-link, .rail-new")
        : null;
      if (!link) return;
      if (link.target && link.target !== "_self") return;
      var href = link.getAttribute("href");
      if (!href || href.charAt(0) === "#") return;
      if (link.href === location.href) return;   // already here
      e.preventDefault();
      showLoadVeil();
      frame.classList.add("leaving");
      setTimeout(function () { location.href = link.href; }, 110);
    });

    // Back and forward restore the page from the cache without re-running the
    // entry animation, so a frame caught mid-fade would stay invisible — and
    // the veil would still be covering it.
    window.addEventListener("pageshow", function (e) {
      if (!e.persisted) return;
      frame.classList.remove("leaving");
      hideLoadVeil();
    });
  }

  function showLoadVeil() {
    var veil = document.getElementById("load-veil");
    if (!veil) return;
    veil.hidden = false;
    // One frame between `hidden` and the class, or the opacity transition is
    // never given a starting value and the veil simply appears.
    requestAnimationFrame(function () { veil.classList.add("on"); });
  }

  function hideLoadVeil() {
    var veil = document.getElementById("load-veil");
    if (!veil) return;
    veil.classList.remove("on");
    veil.hidden = true;
  }

  // A form that posts and then navigates leaves its button looking idle for as
  // long as the server takes. 开跑 and 发送 both wait on real work, which is long
  // enough for the operator to click a second time.
  //
  // Listened for on `document` in the bubble phase, deliberately: the composer
  // intercepts its own submit and posts with fetch, and only by running after it
  // can this tell a navigated submit (`defaultPrevented === false`) from a
  // fetched one.
  // Confirming a flag is a form POST that redirects, so the page reloads before
  // anything can be shown. Post it with fetch instead: the flash plays against
  // the page the operator is still looking at, and the reload that follows
  // brings back the card in its confirmed state.
  //
  // The 820ms is deliberate — the SSE `reloadSoon` path schedules its own reload
  // at 900ms, and being shorter means the two never fight over the same frame.
  // Until when an in-place confirm owns the page. The SSE `flag.confirmed` event
  // is in RERENDER, so without this the reload it schedules would arrive 900ms
  // after the swap and throw away the page the word is still falling over.
  var inPlaceConfirmUntil = 0;

  function currentRunId() {
    var root = document.getElementById("run-root");
    return root ? root.getAttribute("data-run-id") : "";
  }

  // Redraw only the two regions a confirm changes. Both are rendered by the
  // server from the same templates the page uses, so there is no second copy of
  // the flag markup here to fall out of date the next time a state is added.
  function swapRunFragments(runId) {
    var target = document.getElementById("flags-body");
    if (!target || !runId) { location.reload(); return; }
    fetch("/api/runs/" + runId + "/fragments", {
      headers: { "X-Requested-With": "fetch" }
    })
      .then(function (response) {
        if (!response.ok) throw new Error("HTTP " + response.status);
        return response.text();
      })
      .then(function (html) {
        var fresh = new DOMParser().parseFromString(html, "text/html");
        ["flags-body", "run-actions"].forEach(function (id) {
          var live = document.getElementById(id);
          var next = fresh.getElementById(id);
          if (live && next) live.innerHTML = next.innerHTML;
        });
      })
      // If the fragment cannot be fetched, the old behaviour is still correct —
      // just blunt: reload and let the server draw the whole page.
      .catch(function () { location.reload(); });
  }

  function initFlagConfirm() {
    document.addEventListener("submit", function (e) {
      var form = e.target;
      if (!form || form.tagName !== "FORM") return;
      var action = form.getAttribute("action") || "";
      if (!/\/api\/flags\/\d+\/confirm$/.test(action)) return;
      if (!window.fetch) return;          // let the plain POST work
      e.preventDefault();
      var btn = form.querySelector('button[type="submit"]');
      if (btn) { btn.classList.add("busy"); btn.disabled = true; }
      // Claim the page before the request goes out: the server emits
      // `flag.confirmed` while handling it, so the event can arrive before the
      // response does.
      inPlaceConfirmUntil = Date.now() + 4000;
      fetch(action, {
        method: "POST",
        body: new FormData(form),
        headers: { "X-Requested-With": "fetch" },
        credentials: "same-origin"
      })
        .then(function (response) {
          if (!response.ok) throw new Error("HTTP " + response.status);
          flashGet(btn);
          swapRunFragments(currentRunId());
        })
        .catch(function (err) {
          // Nothing was confirmed, so nothing gets celebrated.
          inPlaceConfirmUntil = 0;
          if (btn) { btn.classList.remove("busy"); btn.disabled = false; }
          toast("确认失败：" + err.message);
        });
    });
  }

  function flashGet(anchor) {
    var el = document.createElement("div");
    el.className = "get-flash";
    el.setAttribute("aria-live", "polite");
    var word = document.createElement("span");
    word.textContent = "Get!";
    el.appendChild(word);
    // Land it on the button that was clicked. `top` is the button's top edge, and
    // the container's `translate(-50%, -100%)` puts the word's baseline there, so
    // it comes to rest sitting on the button rather than over the note field.
    if (anchor && anchor.getBoundingClientRect) {
      var rect = anchor.getBoundingClientRect();
      el.style.left = Math.round(rect.left + rect.width / 2) + "px";
      el.style.top = Math.round(rect.top - 2) + "px";
    } else {
      el.style.left = "50%";
      el.style.top = "50%";
    }
    document.body.appendChild(el);
    setTimeout(function () { el.remove(); }, 1100);
  }

  function initBusyForms() {
    document.addEventListener("submit", function (e) {
      if (e.defaultPrevented) return;
      var form = e.target;
      if (!form || form.tagName !== "FORM") return;
      var btn = e.submitter || form.querySelector('button[type="submit"]');
      if (!btn || btn.disabled || btn.classList.contains("busy")) return;
      btn.classList.add("busy");
      btn.setAttribute("aria-busy", "true");
      // Disable after the submit has been dispatched: disabling a submit button
      // inside the handler can stop the browser from sending the form at all.
      setTimeout(function () { btn.disabled = true; }, 0);
    });
  }

  function initToLatest() {
    var box = messagesBox();
    var btn = document.getElementById("to-latest");
    if (!box || !btn) return;

    function sync() {
      var away = box.scrollHeight - box.scrollTop - box.clientHeight;
      btn.hidden = away < 80;
      // Sit directly above the composer: its height depends on attachments,
      // the disabled notice and the viewport, so measure instead of guessing.
      var composer = document.querySelector(".pane-chat .composer");
      if (composer) btn.style.bottom = (composer.offsetHeight + 16) + "px";
    }

    box.addEventListener("scroll", sync, { passive: true });
    window.addEventListener("resize", sync);
    btn.addEventListener("click", function () {
      box.scrollTop = box.scrollHeight;
      sync();
    });
    sync();
  }

  function appendMessage(role, text) {
    var box = messagesBox();
    if (!box) return;
    var empty = document.getElementById("chat-empty");
    if (empty) empty.remove();
    var wrap = document.createElement("div");
    // `just-in` is the ink arriving: only messages this session appends get it,
    // because the ones the server rendered are already on the page and the CSS
    // animation would replay for every bubble on every load.
    wrap.className = "msg " + role + " just-in";
    var who = document.createElement("div");
    who.className = "who";
    who.textContent = role === "user" ? "你" : "主 agent";
    var body = document.createElement("div");
    body.className = "body";
    // The agent writes Markdown (lists, code fences, links); the operator types
    // plain text and should see exactly what they typed.
    if (role === "assistant") {
      body.classList.add("md");
      body.innerHTML = renderMarkdown(text);
    } else {
      body.textContent = text;
    }
    wrap.appendChild(who);
    wrap.appendChild(body);
    box.appendChild(wrap);
    // A finished reply may contain fenced code: colour it and give it a copy
    // button. Not done per streaming delta — the bubble is rebuilt on every
    // token, so the bars would be torn down and re-added dozens of times a
    // second. The bubble is a scratch surface; this message is the durable one.
    if (role === "assistant") decorateCode(body);
    box.scrollTop = box.scrollHeight;
  }

  // ---------------------------------------------------------------- streaming
  //
  // The main agent's text arrives token by token as `text_delta`. Deltas are
  // live-only (never stored), so they grow a bubble that the finished
  // `main.message` keeps as-is instead of appending a second copy.

  function streamBubble() {
    var bubble = document.getElementById("stream-bubble");
    var body = document.getElementById("stream-body");
    if (!bubble || !body) return null;
    if (bubble.hidden) {
      bubble.hidden = false;
      body.textContent = "";
      body.removeAttribute("data-raw");
      var empty = document.getElementById("chat-empty");
      if (empty) empty.remove();
      var box = messagesBox();
      if (box) box.scrollTop = box.scrollHeight;
    }
    return body;
  }

  function appendDelta(text) {
    var body = streamBubble();
    if (!body || !text) return;
    var box = messagesBox();
    // Only follow the tail when the operator is already at the bottom; yanking
    // the view while they read scrollback is the classic chat annoyance.
    var pinned = !box || box.scrollHeight - box.scrollTop - box.clientHeight < 60;
    // The raw text is kept on the element and re-rendered, so a code fence
    // that is still half-open does not flash raw asterisks and backticks.
    body.setAttribute("data-raw", (body.getAttribute("data-raw") || "") + text);
    body.innerHTML = renderMarkdown(body.getAttribute("data-raw"));
    if (pinned && box) box.scrollTop = box.scrollHeight;
  }
  function finishStream(text) {
    // The partial bubble is a scratch surface, not a message: the durable
    // message is what the server just recorded. Swapping the finished text in
    // keeps the live view and a reload identical (the streamed text and the
    // stored text are the same string).
    var bubble = document.getElementById("stream-bubble");
    var body = document.getElementById("stream-body");
    if (bubble) bubble.hidden = true;
    if (body) {
      body.textContent = "";
      body.removeAttribute("data-raw");
    }
    if (text) appendMessage("assistant", text);
  }

  function resetStream() {
    var bubble = document.getElementById("stream-bubble");
    var body = document.getElementById("stream-body");
    if (!bubble || !body) return;
    bubble.hidden = true;
    body.textContent = "";
  }

  // ---------------------------------------------------------------- thinking
  //
  // The lead's reasoning arrives twice: token by token as `thinking_delta`
  // while it is being written, then once as a complete `thinking` block. The
  // deltas grow a turn; the block settles it. Only the lead streams deltas
  // (workers are not asked for partial messages), so this panel is the lead's
  // alone. It stays folded to one line by default — the answer is what the
  // operator reads — but the line previews the tail of the current thought so
  // a folded panel still shows that something is happening.

  var thinkState = { open: false, live: "" };

  function thinkEl(id) {
    return document.getElementById(id);
  }

  function thinkOpen(open) {
    var panel = thinkEl("think-panel");
    var body = thinkEl("think-body");
    var toggle = thinkEl("think-toggle");
    if (!panel || !body) return;
    thinkState.open = !!open;
    panel.classList.toggle("open", thinkState.open);
    body.hidden = !thinkState.open;
    if (toggle) toggle.setAttribute("aria-expanded", thinkState.open ? "true" : "false");
    try { sessionStorage.setItem("ctfagent.thinkingOpen", thinkState.open ? "1" : "0"); } catch (e) {}
    if (thinkState.open) body.scrollTop = body.scrollHeight;
  }

  // Counted from what is on screen, not from a running total: the panel drops
  // old turns to stay a window, and a counter that kept climbing past the
  // visible turns would just be wrong. The turn still being written is not
  // counted until the completed block settles it.
  function thinkCount() {
    var el = thinkEl("think-count");
    var body = thinkEl("think-body");
    if (!el || !body) return;
    var n = body.querySelectorAll(".think-turn:not(#think-live)").length;
    el.textContent = n ? n + " 段" : "";
  }

  function thinkPreview(text) {
    var el = thinkEl("think-preview");
    if (!el) return;
    el.textContent = String(text || "").replace(/\s+/g, " ").slice(-90);
  }

  function thinkReveal() {
    var panel = thinkEl("think-panel");
    if (!panel) return;
    panel.hidden = false;
  }

  function thinkLive() {
    var body = thinkEl("think-body");
    if (!body) return null;
    var el = thinkEl("think-live");
    if (!el) {
      el = document.createElement("div");
      el.className = "think-turn";
      el.id = "think-live";
      body.appendChild(el);
    }
    return el;
  }

  function appendThinking(text) {
    if (!text) return;
    thinkReveal();
    thinkState.live += text;
    var el = thinkLive();
    if (!el) return;
    var body = thinkEl("think-body");
    // Follow the tail only while the operator is reading the tail; the same
    // rule as the chat pane.
    var pinned = !body || body.scrollHeight - body.scrollTop - body.clientHeight < 40;
    el.textContent = thinkState.live;
    if (pinned && body) body.scrollTop = body.scrollHeight;
    thinkPreview(thinkState.live);
    var panel = thinkEl("think-panel");
    if (panel) panel.classList.add("live");
  }

  // The complete block supersedes the deltas that spelled it. Keeping only the
  // block means a reload (which replays the block, never the deltas) shows the
  // same text the live page did.
  function settleThinking(text) {
    var body = thinkEl("think-body");
    var final = String(text || "");
    if (!body || !final) return;
    thinkReveal();
    var live = thinkEl("think-live");
    if (!live) {
      live = document.createElement("div");
      live.className = "think-turn";
      body.appendChild(live);
    }
    live.textContent = final;
    live.removeAttribute("id");
    thinkState.live = "";
    thinkPreview(final);
    var panel = thinkEl("think-panel");
    if (panel) panel.classList.remove("live");
    // A long run can think for hours; the panel is a window on the recent
    // reasoning, not a transcript (the console and the DB keep that).
    while (body.children.length > 60) body.removeChild(body.firstChild);
    thinkCount();
    if (thinkState.open) body.scrollTop = body.scrollHeight;
  }

  // The turn is over even though no completed block arrived (a cut turn, a
  // session error). Stop the caret and keep what was written; dropping the id
  // means the next turn's deltas start a fresh turn instead of appending to a
  // sentence that belongs to a turn already finished.
  function thinkSettled() {
    var panel = thinkEl("think-panel");
    if (panel) panel.classList.remove("live");
    var live = thinkEl("think-live");
    if (live) live.removeAttribute("id");
    thinkState.live = "";
    thinkCount();
  }

  // A thought cut mid-sentence by an interrupt is not a thought the operator
  // can act on; dropping it matches what happens to the half-written reply.
  function resetThinking() {
    var live = thinkEl("think-live");
    if (live) live.remove();
    thinkState.live = "";
    var panel = thinkEl("think-panel");
    if (panel) panel.classList.remove("live");
  }

  function initThinking() {
    var toggle = thinkEl("think-toggle");
    if (!toggle) return;
    var panel = thinkEl("think-panel");
    thinkCount();
    var last = panel ? panel.querySelector(".think-turn:last-child") : null;
    if (last) thinkPreview(last.textContent);
    var saved = null;
    try { saved = sessionStorage.getItem("ctfagent.thinkingOpen"); } catch (e) {}
    thinkOpen(saved === "1");
    toggle.addEventListener("click", function () { thinkOpen(!thinkState.open); });
  }

  function appendUserTurn(text, names) {
    var box = messagesBox();
    if (!box) return;
    var empty = document.getElementById("chat-empty");
    if (empty) empty.remove();
    var wrap = document.createElement("div");
    wrap.className = "msg user";
    var who = document.createElement("div");
    who.className = "who";
    who.textContent = "你";
    wrap.appendChild(who);
    if (text) {
      var body = document.createElement("div");
      body.className = "body";
      body.textContent = text;
      wrap.appendChild(body);
    }
    if (names && names.length) {
      var files = document.createElement("div");
      files.className = "msg-files";
      var runId = (document.getElementById("attach-strip") || {}).getAttribute
        ? document.getElementById("attach-strip").getAttribute("data-run-id")
        : "";
      names.forEach(function (name) {
        var a = document.createElement("a");
        a.className = "file-card";
        a.href = "/api/runs/" + runId + "/attachments/" + encodeURIComponent(name);
        a.title = name;
        var label = document.createElement("span");
        label.className = "fc-name";
        label.textContent = name;
        var kind = document.createElement("span");
        kind.className = "fc-kind";
        kind.textContent = name.indexOf(".") >= 0 ? name.split(".").pop().toLowerCase() : "文件";
        a.appendChild(label);
        a.appendChild(kind);
        files.appendChild(a);
      });
      wrap.appendChild(files);
    }
    box.appendChild(wrap);
    box.scrollTop = box.scrollHeight;
  }

  // The server answers the chat POST with the names it actually stored, so the
  // optimistic bubble shows the same sanitised names a reload would show.
  function readStoredNames(response) {
    var type = response.headers.get("content-type") || "";
    if (type.indexOf("application/json") < 0) return Promise.resolve(null);
    return response.json().then(function (data) { return data.stored || null; })
      .catch(function () { return null; });
  }

  function initComposer() {
    var form = document.getElementById("composer");
    var box = messagesBox();
    if (box) box.scrollTop = box.scrollHeight;
    if (!form) return;

    var textarea = form.querySelector("textarea[name=text]");
    var fileInput = form.querySelector("input[type=file]");
    var chosen = document.getElementById("chosen");
    var button = form.querySelector("button[type=submit]");

    if (fileInput && chosen) {
      fileInput.addEventListener("change", function () {
        var names = Array.prototype.map.call(fileInput.files, function (f) { return f.name; });
        chosen.textContent = names.join("、");
      });
    }

    // Enter sends, Shift+Enter breaks the line — the shortcut every chat has.
    if (textarea) {
      textarea.addEventListener("keydown", function (ev) {
        if (ev.key === "Enter" && !ev.shiftKey && !ev.isComposing) {
          ev.preventDefault();
          form.requestSubmit();
        }
      });
      // Grow with the text, up to a point; past that it scrolls.
      textarea.addEventListener("input", function () {
        textarea.style.height = "auto";
        textarea.style.height = Math.min(textarea.scrollHeight, 220) + "px";
      });
    }

    form.addEventListener("submit", function (ev) {
      if (!window.fetch) return;   // let the plain POST work
      ev.preventDefault();
      var text = textarea ? textarea.value.trim() : "";
      var files = fileInput && fileInput.files ? Array.prototype.slice.call(fileInput.files) : [];
      if (!text && !files.length) return;

      var data = new FormData();
      data.append("text", text);
      files.forEach(function (file) { data.append("files", file, file.name); });
      button.disabled = true;
      resetStream();
      fetch(form.getAttribute("action"), {
        method: "POST",
        body: data,
        headers: { "X-Requested-With": "fetch" }
      })
        .then(function (response) {
          if (!response.ok) throw new Error("HTTP " + response.status);
          return readStoredNames(response);
        })
        .then(function (stored) {
          var names = stored || files.map(function (f) { return f.name; });
          appendUserTurn(text, names);
          if (textarea) {
            textarea.value = "";
            textarea.style.height = "auto";
          }
          if (fileInput) fileInput.value = "";
          if (chosen) chosen.textContent = "";
          loadAttachments();
        })
        .catch(function (err) { toast("发送失败：" + err.message); })
        .then(function () { button.disabled = false; if (textarea) textarea.focus(); });
    });
  }

  function initAttachments() {
    var strip = document.getElementById("attach-strip");
    if (!strip) return;
    loadAttachments();
  }

  // Drag a file anywhere over the conversation and it joins the next message.
  // The veil is the only feedback: without it a drop looks like it did nothing.
  function initDropZone() {
    var form = document.getElementById("composer");
    var veil = document.getElementById("drop-veil");
    var input = form && form.querySelector("input[type=file]");
    if (!form || !input || !veil) return;

    var staged = [];
    var transfer = typeof DataTransfer === "function" ? new DataTransfer() : null;
    var chosen = document.getElementById("chosen");

    function sync() {
      if (transfer) {
        transfer.items.clear();
        staged.forEach(function (f) { transfer.items.add(f); });
        input.files = transfer.files;
      }
      if (chosen) {
        chosen.textContent = staged.map(function (f) { return f.name; }).join("、");
      }
    }

    input.addEventListener("change", function () {
      staged = Array.prototype.slice.call(input.files || []);
      if (chosen) chosen.textContent = staged.map(function (f) { return f.name; }).join("、");
    });

    var depth = 0;
    function hasFiles(ev) {
      var types = ev.dataTransfer && ev.dataTransfer.types;
      return types && Array.prototype.indexOf.call(types, "Files") !== -1;
    }

    document.addEventListener("dragenter", function (ev) {
      if (!hasFiles(ev)) return;
      ev.preventDefault();
      depth += 1;
      veil.hidden = false;
    });
    document.addEventListener("dragover", function (ev) {
      if (!hasFiles(ev)) return;
      ev.preventDefault();
      ev.dataTransfer.dropEffect = "copy";
    });
    document.addEventListener("dragleave", function (ev) {
      if (!hasFiles(ev)) return;
      depth = Math.max(0, depth - 1);
      if (!depth) veil.hidden = true;
    });
    document.addEventListener("drop", function (ev) {
      if (!hasFiles(ev)) return;
      ev.preventDefault();
      depth = 0;
      veil.hidden = true;
      Array.prototype.forEach.call(ev.dataTransfer.files || [], function (file) {
        var duplicate = staged.some(function (other) {
          return other.name === file.name && other.size === file.size;
        });
        if (!duplicate) staged.push(file);
      });
      sync();
      var textarea = form.querySelector("textarea[name=text]");
      if (textarea) textarea.focus();
    });
  }

  function humanSize(bytes) {
    if (bytes < 1024) return bytes + "B";
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + "K";
    return (bytes / 1024 / 1024).toFixed(1) + "M";
  }

  function loadAttachments() {
    var strip = document.getElementById("attach-strip");
    if (!strip) return;
    var runId = strip.getAttribute("data-run-id");
    fetch("/api/runs/" + runId + "/attachments")
      .then(function (r) { return r.json(); })
      .then(function (data) {
        var items = data.attachments || [];
        if (!items.length) { strip.hidden = true; return; }
        strip.hidden = false;
        strip.textContent = "";
        items.forEach(function (item) {
          var a = document.createElement("a");
          a.href = "/api/runs/" + runId + "/attachments/" + encodeURIComponent(item.name);
          a.textContent = item.name;
          var size = document.createElement("span");
          size.className = "size";
          size.textContent = humanSize(item.size || 0);
          a.appendChild(size);
          strip.appendChild(a);
        });
      })
      .catch(function () {});
  }

  // ---------------------------------------------------------------- rail

  // Which folder disclosures the operator has opened or closed, remembered
  // across page loads. The rail is re-rendered from the server on every load
  // and every 5 seconds, and the server can only guess a default; without this
  // the guess won, so a folder the operator had folded (the unfiled group
  // especially, which every new run lands in) sprang open again on the next
  // click or refresh.
  //
  // The stored value is the operator's decision, so it beats the server's
  // "open when something in it is live" default — that default only applies to
  // a folder nobody has touched yet. A browser that blocks storage just gets
  // the server defaults.
  var RAIL_STATE_KEY = "ctfagent.railFold";

  function readRailFold() {
    try {
      var raw = localStorage.getItem(RAIL_STATE_KEY);
      var parsed = raw ? JSON.parse(raw) : null;
      return parsed && typeof parsed === "object" ? parsed : {};
    } catch (e) {
      return {};
    }
  }

  function writeRailFold(state) {
    try { localStorage.setItem(RAIL_STATE_KEY, JSON.stringify(state)); } catch (e) {}
  }

  function applyRailFold(list, state) {
    list.querySelectorAll("details.rail-group").forEach(function (d) {
      var key = d.getAttribute("data-folder") || "";
      if (Object.prototype.hasOwnProperty.call(state, key)) d.open = !!state[key];
    });
  }

  function initRail() {
    var list = document.getElementById("rail-list");
    if (!list || !window.fetch) return;
    var active = list.getAttribute("data-active") || "0";
    var lastHtml = list.innerHTML;
    var fold = readRailFold();

    applyRailFold(list, fold);

    // Record the operator's choice the moment they make it. `toggle` does not
    // bubble, so this listens in the capture phase on the list itself.
    list.addEventListener("toggle", function (event) {
      var d = event.target;
      if (!d || !d.classList || !d.classList.contains("rail-group")) return;
      fold[d.getAttribute("data-folder") || ""] = d.open;
      writeRailFold(fold);
    }, true);

    var refresh = function () {
      // A hidden tab does not need a live rail; every other open page is
      // already polling, and the DB is the source of truth when it comes back.
      if (document.hidden) return;
      fetch("/api/rail?active=" + encodeURIComponent(active))
        .then(function (r) { return r.text(); })
        .then(function (html) {
          if (html === lastHtml) return;
          var atTop = list.scrollTop < 4;
          list.innerHTML = html;
          lastHtml = html;
          // Re-apply the remembered state instead of the server's default.
          applyRailFold(list, fold);
          // The server's HTML carries no "just captured" mark, so a swap that
          // lands during the settle would cut it short. Put it back while the
          // window is still open.
          applyCaptureMarks(list);
          if (atTop) list.scrollTop = 0;
        })
        .catch(function () {});
    };
    setInterval(refresh, 5000);
    document.addEventListener("visibilitychange", refresh);
    refresh();
  }

  // ---------------------------------------------------------------- live strip
  //
  // The lead can go quiet for many minutes while workers run. The chat pane
  // then has nothing new, and the page reads as stuck even though tool calls
  // are streaming. The worker frames are already on this page's SSE feed, so
  // the strip is built from them: no extra endpoint, and it fills in as soon
  // as any worker touches a tool.

  var liveState = {};  // key -> {role, taskId, tool, at}

  function liveKey(payload) {
    return payload.task_id ? "t" + payload.task_id : "r" + (payload.role || "");
  }

  // Every render rebuilds the whole strip, so a "new" marker has to come from
  // what has been *shown* rather than from what exists in the node. A worker
  // that was already listed keeps its line still; only a worker that appears
  // for the first time inks in. Without this the strip would re-animate on
  // every tool call of every other worker.
  var liveSeen = {};

  function renderLive() {
    var el = document.getElementById("live-strip");
    if (!el) return;
    var keys = Object.keys(liveState);
    if (!keys.length) {
      el.hidden = true;
      el.textContent = "";
      liveSeen = {};
      return;
    }
    keys.sort();
    el.hidden = false;
    el.textContent = "";
    keys.forEach(function (key) {
      var t = liveState[key];
      var age = Math.max(0, Math.round((Date.now() - t.at) / 1000));
      var item = document.createElement("span");
      // 300 s is the stall timeout: a worker silent past it is worth a red
      // flag, not just a dim one — unless it is waiting on a background job,
      // which is work in progress and already has the longer clock server-side.
      item.className = "live-item" + (age >= 300 && !t.background ? " stale" : "") +
        (t.background ? " busy" : "") + (liveSeen[key] ? "" : " just-in");
      liveSeen[key] = 1;
      var id = document.createElement("span");
      id.className = "id";
      id.textContent = t.taskId ? "#" + t.taskId : "";
      var role = document.createElement("span");
      role.className = "role";
      role.textContent = roleLabel(t.role);
      var tool = document.createElement("span");
      tool.className = "tool";
      tool.textContent = t.background
        ? "后台任务 ×" + t.background + " 运行中"
        : (t.tool || "刚开始");
      var ageEl = document.createElement("span");
      ageEl.className = "age";
      ageEl.textContent = age + " 秒前";
      item.appendChild(id);
      item.appendChild(role);
      item.appendChild(tool);
      item.appendChild(ageEl);
      el.appendChild(item);
    });
  }

  function noteWorkerEvent(payload) {
    if (!payload || payload.role === "main") return;
    if (payload.kind !== "tool_use" && payload.kind !== "text" &&
        payload.kind !== "thinking" && payload.kind !== "tool_result") return;
    var key = liveKey(payload);
    var entry = liveState[key] || { role: payload.role, taskId: payload.task_id || 0, tool: "" };
    if (payload.kind === "tool_use") entry.tool = String(payload.tool || "");
    entry.at = Date.now();
    liveState[key] = entry;
    renderLive();
  }

  function forgetWorker(payload) {
    if (!payload) return;
    var key = liveKey(payload);
    delete liveState[key];
    // Forget the show-state too, or a worker that finishes and is dispatched
    // again would come back without its arrival — it would look like it never
    // left.
    delete liveSeen[key];
    renderLive();
  }

  function initLiveStrip() {
    var el = document.getElementById("live-strip");
    var root = document.getElementById("run-root");
    if (!el) return;
    // The SSE feed starts at the page's last event id, so a reload would show
    // an empty strip until a worker's next call. Seed it from the server.
    if (root && window.fetch) {
      fetch("/api/runs/" + root.getAttribute("data-run-id") + "/live")
        .then(function (r) { return r.json(); })
        .then(function (data) {
          ((data && data.tasks) || []).forEach(function (t) {
            liveState["t" + t.task_id] = {
              role: t.role,
              taskId: t.task_id,
              tool: t.last_tool || "",
              background: Number(t.background || 0),
              at: Date.now() - Number(t.silent_s || 0) * 1000,
            };
          });
          renderLive();
        })
        .catch(function () {});
    }
    setInterval(renderLive, 1000);
  }

  // ---------------------------------------------------------------- palette
  //
  // Ctrl+K / Cmd+K is one keystroke to any task, page or action. The whole
  // reason this exists on this product is the rail: a competition day puts
  // ninety runs in it, and "find run 121 by scanning a column" is the slowest
  // thing an operator does all afternoon.
  //
  // The commands are declared here rather than rendered by the server: they are
  // navigation, which is the front end's business, and the palette is in
  // base.html so it is available from every page. Tasks are fetched on first
  // open and cached, then filtered in the browser — the list is at most 200
  // rows and a round trip per keystroke would be the wrong trade.

  var paletteOpen = false;
  var paletteItems = [];      // the rows currently on screen, in display order
  var paletteAll = [];        // every match, ranked; paletteItems is a prefix
  var paletteVisible = 40;    // how many of them are rendered right now
  var paletteIndex = 0;
  var paletteRuns = null;     // all /api/runs rows, null until first fetch
  var paletteReturn = null;   // the element focus goes back to on close

  var PAGE_COMMANDS = [
    { kind: "页面", label: "工作台", hint: "全部运行", href: "/" },
    { kind: "页面", label: "新任务", hint: "贴题、给目标", href: "/new" },
    { kind: "页面", label: "比赛", hint: "CTFd 同步与提交", href: "/competitions" },
    { kind: "页面", label: "凭据", hint: "端点与令牌", href: "/settings" },
    { kind: "页面", label: "说明", hint: "怎么用", href: "/docs" }
  ];

  function paletteEl() { return document.getElementById("palette"); }

  function paletteAction(label, hint, href) {
    return { kind: "动作", label: label, hint: hint, href: href };
  }

  // Read from the document instead of a hard-coded list: whether a page has a
  // composer, a log tab or a writeup depends on the page, and the palette should
  // offer exactly what is in front of the operator.
  function pageActions() {
    var out = [];
    var composer = document.querySelector(".composer textarea, .home-composer textarea");
    if (composer && !composer.disabled) {
      out.push({ kind: "动作", label: "聚焦输入框", hint: "开始打字", act: function () { composer.focus(); } });
    }
    if (document.querySelector('[data-stab="log"]')) {
      out.push(paletteAction("打开日志", "事件流", "#log"));
    }
    if (document.querySelector('[data-stab="flags"]')) {
      out.push(paletteAction("打开 Flag", "候选与已确认", "#flags"));
    }
    if (document.querySelector('[data-mtab="report"]')) {
      out.push(paletteAction("打开报告", "本次运行的成文", "#report"));
    }
    if (document.getElementById("console")) {
      out.push({ kind: "动作", label: "复制全部日志", hint: "当前运行的事件流", act: copyConsole });
    }
    return out;
  }

  function copyConsole() {
    var el = document.getElementById("console");
    if (!el) return;
    var text = el.textContent || "";
    if (navigator.clipboard) navigator.clipboard.writeText(text).then(function () { toast("日志已复制"); });
  }

  function statusWord(status) {
    return { running: "运行中", needs_input: "待确认", solved: "已解出",
             failed: "失败", stopped: "已停止", interrupted: "中断",
             queued: "排队" }[status] || status;
  }

  function runItems(runs, query) {
    return runs.map(function (r) {
      return {
        kind: "任务",
        label: "#" + r.id + "  " + (r.challenge_name || "?"),
        hint: statusWord(r.status) + (r.cost_usd ? "  $" + Number(r.cost_usd).toFixed(3) : ""),
        href: "/runs/" + r.id,
        // Both the id and the name are searchable; the name is what people
        // remember, the id is what they paste from a log line.
        terms: ["#" + r.id, String(r.id), r.challenge_name || "", r.category || "", r.objective || ""]
          .join(" ").toLowerCase()
      };
    });
  }

  function score(item, q) {
    var label = item.label.toLowerCase();
    var terms = (item.terms || label).toLowerCase();
    if (!q) return 1;
    var at = label.indexOf(q);
    if (at === 0) return 100;
    if (at > 0) return 80 - Math.min(at, 40);
    if (terms.indexOf(q) !== -1) return 50;
    // Every token must appear somewhere, in any order: "sql 121" is a real
    // thing to type when you remember half of each.
    var tokens = q.split(/\s+/).filter(Boolean);
    if (tokens.length > 1 && tokens.every(function (t) { return terms.indexOf(t) !== -1; })) return 30;
    return 0;
  }

  function buildPaletteItems(q) {
    var pages = PAGE_COMMANDS.filter(function (c) { return score(c, q) > 0; });
    var actions = pageActions().filter(function (c) { return score(c, q) > 0; });
    // Rank every matching task and keep the full list: arrow keys have to be
    // able to reach row 41 without the operator typing a longer query.
    var runs = runItems(paletteRuns || [], q)
      .map(function (item, i) { return { item: item, i: i, s: score(item, q) }; })
      .filter(function (x) { return x.s > 0; })
      .sort(function (a, b) { return b.s - a.s || a.i - b.i; })
      .map(function (x) { return x.item; });
    // Pages and actions first (they answer "take me somewhere" in one keystroke),
    // then tasks. Empty query is not a search: it lists everything in that order.
    return pages.concat(actions, runs);
  }

  function renderPalette(keepWindow) {
    var list = document.getElementById("palette-list");
    var count = document.getElementById("palette-count");
    var input = document.getElementById("palette-input");
    if (!list || !input) return;
    var q = input.value.trim().toLowerCase();
    paletteAll = buildPaletteItems(q);
    // A new query starts the list over at 40 rows; the cursor walks past that by
    // extending the slice rather than by re-scoring, so a move that lands past
    // the current window rebuilds the list with the window it already asked for.
    // Without the flag every extra row was undone by this line on the next
    // render, which pinned the cursor to row 40 of 79 (measured).
    paletteVisible = Math.min(paletteAll.length, keepWindow ? Math.max(paletteVisible, 40) : 40);
    paletteItems = paletteAll.slice(0, paletteVisible);
    if (!keepWindow && paletteIndex >= paletteItems.length) {
      paletteIndex = Math.max(0, paletteItems.length - 1);
    }
    list.textContent = "";
    if (!paletteItems.length) {
      var empty = document.createElement("li");
      empty.className = "palette-empty";
      empty.textContent = paletteRuns === null ? "读取任务…" : "没有匹配";
      list.appendChild(empty);
    }
    paletteItems.forEach(function (item, i) {
      var li = document.createElement("li");
      li.className = "palette-item" + (i === paletteIndex ? " on" : "");
      li.id = "palette-opt-" + i;
      li.setAttribute("role", "option");
      li.setAttribute("aria-selected", i === paletteIndex ? "true" : "false");
      // A click must not steal focus from the input before it is handled, or the
      // palette closes under the pointer.
      li.setAttribute("data-index", String(i));
      var kind = document.createElement("span");
      kind.className = "palette-kind";
      kind.textContent = item.kind;
      var label = document.createElement("span");
      label.className = "palette-label";
      label.textContent = item.label;
      var hint = document.createElement("span");
      hint.className = "palette-hint";
      hint.textContent = item.hint || "";
      li.appendChild(kind);
      li.appendChild(label);
      li.appendChild(hint);
      list.appendChild(li);
    });
    if (count) {
      // The empty list has to say so in both places the eye can land. A blank
      // counter beside "没有匹配" reads as a list that has not finished loading;
      // it is the one state where the number is the answer.
      count.textContent = !paletteAll.length ? (paletteRuns === null ? "" : "0 项")
        : paletteItems.length < paletteAll.length
          ? paletteItems.length + "/" + paletteAll.length + " 项"
          : paletteAll.length + " 项";
    }
    syncActiveDescendant();
  }

  function syncActiveDescendant() {
    var input = document.getElementById("palette-input");
    var on = document.querySelector(".palette-item.on");
    if (input) {
      if (on) input.setAttribute("aria-activedescendant", on.id);
      else input.removeAttribute("aria-activedescendant");
    }
    if (on && on.scrollIntoView) on.scrollIntoView({ block: "nearest" });
  }

  function movePalette(delta) {
    // Arrows walk the whole match list, not the rendered prefix: an operator who
    // knows the run is in there should not have to lengthen the query to reach
    // row 41. Rendering is extended as the cursor approaches the end.
    if (!paletteAll.length) return;
    paletteIndex = (paletteIndex + delta + paletteAll.length) % paletteAll.length;
    if (paletteIndex >= paletteVisible) {
      paletteVisible = Math.min(paletteAll.length, paletteIndex + 1);
      paletteItems = paletteAll.slice(0, paletteVisible);
      // keepWindow: this rebuild must not shrink the list back to 40 under the
      // cursor it was just widened for.
      renderPalette(true);
      return;
    }
    document.querySelectorAll(".palette-item").forEach(function (li, i) {
      var on = i === paletteIndex;
      li.classList.toggle("on", on);
      li.setAttribute("aria-selected", on ? "true" : "false");
      if (on && li.scrollIntoView) li.scrollIntoView({ block: "nearest" });
    });
    syncActiveDescendant();
  }

  function runPaletteItem(item) {
    if (!item) return;
    if (item.act) { closePalette(); item.act(); return; }
    if (item.href && item.href.charAt(0) === "#") {
      // In-page targets are the tab handlers' territory; going through them
      // keeps the remembered-tab store in step with what the operator sees.
      closePalette();
      var name = item.href.slice(1);
      var group = document.querySelector('[data-stab="' + name + '"]') ? "data-stab" : "data-mtab";
      var tab = document.querySelector("[" + group + '="' + name + '"]');
      if (tab) tab.click();
      return;
    }
    closePalette();
    if (item.href) location.href = item.href;
  }

  function openPalette() {
    var el = paletteEl();
    if (!el || paletteOpen) return;
    paletteOpen = true;
    paletteReturn = document.activeElement;
    el.hidden = false;
    document.documentElement.classList.add("scroll-locked");
    var input = document.getElementById("palette-input");
    paletteIndex = 0;
    renderPalette();
    if (input) { input.value = ""; input.focus(); }
    if (paletteRuns === null) {
      fetch("/api/runs")
        .then(function (r) { return r.ok ? r.json() : []; })
        .catch(function () { return []; })
        .then(function (rows) {
          paletteRuns = Array.isArray(rows) ? rows : [];
          if (paletteOpen && document.activeElement === input) renderPalette();
        });
    }
  }

  function closePalette() {
    var el = paletteEl();
    if (!el || !paletteOpen) return;
    paletteOpen = false;
    el.hidden = true;
    document.documentElement.classList.remove("scroll-locked");
    var input = document.getElementById("palette-input");
    if (input) { input.value = ""; }
    if (paletteReturn && paletteReturn.focus) paletteReturn.focus();
    paletteReturn = null;
  }

  function initPalette() {
    var el = paletteEl();
    if (!el) return;
    var input = document.getElementById("palette-input");
    var list = document.getElementById("palette-list");
    var opener = document.getElementById("palette-open");
    if (opener) opener.addEventListener("click", function () { openPalette(); });

    document.addEventListener("keydown", function (ev) {
      if ((ev.ctrlKey || ev.metaKey) && !ev.altKey && (ev.key === "k" || ev.key === "K")) {
        ev.preventDefault();
        if (paletteOpen) closePalette(); else openPalette();
        return;
      }
      if (!paletteOpen) return;
      if (ev.key === "Escape") { ev.preventDefault(); closePalette(); }
      else if (ev.key === "ArrowDown") { ev.preventDefault(); movePalette(1); }
      else if (ev.key === "ArrowUp") { ev.preventDefault(); movePalette(-1); }
      else if (ev.key === "Enter") {
        ev.preventDefault();
        runPaletteItem(paletteItems[paletteIndex]);
      } else if (ev.key === "Tab") {
        // Focus stays in the box: the list is driven by arrows, and tabbing out
        // of a modal into the page behind it is how a keyboard user gets lost.
        ev.preventDefault();
      }
    });
    if (input) input.addEventListener("input", function () { paletteIndex = 0; renderPalette(); });
    if (list) {
      // mousedown, not click: a click would blur the input first.
      list.addEventListener("mousedown", function (ev) {
        var li = ev.target.closest ? ev.target.closest(".palette-item") : null;
        if (!li) return;
        ev.preventDefault();
        runPaletteItem(paletteItems[Number(li.getAttribute("data-index"))]);
      });
    }
    // Clicking the dimmed area outside the box closes; clicking the box does not.
    el.addEventListener("mousedown", function (ev) {
      if (ev.target === el) closePalette();
    });
  }

  // ---------------------------------------------------------------- SSE

  var WATCHED = [
    "run.started", "run.finished", "run.needs_input", "run.question", "run.error",
    "run.warning", "run.writeup", "run.writer_dispatched", "run.attachments",
    "task.dispatched", "task.started", "task.finished", "task.result",
    "fact.added", "fact.merged", "fact.downgraded",
    "flag.proposed", "flag.verdict", "flag.confirmed",
    "chat.user", "main.message", "main.interrupted", "text_delta", "thinking_delta",
    "agent.event", "session.error"
  ];

  // Events that change what the page shows; a reload is the honest way to
  // re-render server-owned state (task list, flags) without duplicating the
  // whole template in JS. Chat is handled in place: reloading on every turn
  // would wipe the message the operator is reading (and the box they type in).
  var RERENDER = {
    "task.dispatched": 1, "task.finished": 1, "task.result": 1,
    "flag.proposed": 1, "flag.verdict": 1, "flag.confirmed": 1,
    "run.finished": 1, "run.needs_input": 1, "run.question": 1,
    "run.writer_dispatched": 1, "run.writeup": 1, "run.error": 1,
    "run.attachments": 1
  };

  function initStream() {
    var root = document.getElementById("run-root");
    if (!root || !window.EventSource) return;
    var runId = root.getAttribute("data-run-id");
    // Start from the last id the server already rendered, so the replay does
    // not duplicate the backlog in the console.
    var since = root.getAttribute("data-last-event") || "0";
    var es = new EventSource(
      "/api/runs/" + runId + "/events/stream?since=" + encodeURIComponent(since)
    );

    var pending = null;
    var reloadSoon = function () {
      if (pending) return;
      pending = setTimeout(function () {
        pending = null;
        // Do not yank the page while the operator is typing.
        var active = document.activeElement;
        if (active && (active.tagName === "TEXTAREA" || active.tagName === "INPUT")) {
          reloadSoon();
          return;
        }
        location.reload();
      }, 900);
    };

    WATCHED.forEach(function (kind) {
      es.addEventListener(kind, function (ev) {
        var payload = {};
        try { payload = JSON.parse(ev.data); } catch (e) {}

        // A token delta is not an event worth a console line; it belongs in the
        // bubble only, or the log drowns in one entry per word.
        if (kind === "text_delta") {
          appendDelta(payload.text || "");
          return;
        }
        if (kind === "thinking_delta") {
          appendThinking(payload.text || "");
          return;
        }

        // `true` here and only here: this is the live feed. The same function
        // replays the stored backlog in `initConsole`, which must stay still.
        appendEvent(kind, payload,
                    new Date().toISOString().replace("T", " ").slice(0, 19), true);

        if (kind === "agent.event") {
          noteWorkerEvent(payload);
          if (payload.role === "main" && payload.kind === "thinking") {
            settleThinking(payload.text || "");
          }
        }
        if (kind === "task.finished") forgetWorker(payload);

        if (kind === "main.message") {
          finishStream(payload.text || "");
          // The reply follows the thought; if the block never arrived, the
          // caret must not keep blinking for a turn that is over.
          thinkSettled();
        }
        // The reply that was being streamed was cut by the operator's own
        // message; the lead is about to answer it in a fresh turn.
        if (kind === "main.interrupted") {
          resetStream();
          resetThinking();
        }
        if (kind === "session.error" || kind === "run.finished" || kind === "run.error") {
          thinkSettled();
        }
        if (kind === "run.warning") toast(payload.warning || "运行警告");
        if (kind === "flag.verdict" && payload.verdict === "reproduced") {
          toast("验证任务已复现候选 flag");
        }
        if (kind === "flag.confirmed") noteCapture(runId);
        if (kind === "flag.confirmed") toast("flag 已确认，正在生成报告");
        if (kind === "run.needs_input") toast("运行暂停，等待你的确认");
        if (kind === "main.interrupted") toast("已打断主 agent，正在读你的消息");
        if (RERENDER[kind]) {
          // A confirm handled in place must not be undone by the reload its own
          // event schedules.
          var owned = kind === "flag.confirmed" && Date.now() < inPlaceConfirmUntil;
          if (!owned) reloadSoon();
        }
      });
    });

    es.onerror = function () {
      // EventSource reconnects by itself and replays from Last-Event-ID.
      // Nothing to do; a permanent failure just leaves the page static.
    };
  }

  // ---------------------------------------------------------------- writeup

  var writeupLoaded = false;

  // ---------------------------------------------------------------- capture

  // The one authored moment in the product: a result coming off the press.
  //
  // Paper is server-rendered, which is the whole point of it (a reload shows
  // exactly the same sheet), and that is also why the animation cannot live on
  // the element: every page load would replay it for every solved row already
  // on screen. So the run that was just confirmed is written down before the
  // re-render reloads the page, and the marks are taken off exactly once when
  // the new page arrives.
  //
  // sessionStorage, not localStorage: this is about the next paint in this tab,
  // not a fact about the machine. A second tab should not light up because a
  // third one saw a flag.
  var CAPTURE_KEY = "ctfagent.captured";

  // The rail re-fetches itself on a timer and replaces `#rail-list` wholesale —
  // including the very first refresh, which fires on boot. That is a new set of
  // nodes, so a mark put on the old ones is gone with them. Each id therefore
  // stays listed for one short window and is re-applied after every swap within
  // it; a timer closes the window and clears the mark.
  var captureMarks = {};  // run id -> 1 while the mark is still live

  // How long the mark stays valid. The settle itself is 340ms; this covers it
  // plus the rail's first swap, and then stops so a later poll cannot replay a
  // capture the operator already watched land.
  var CAPTURE_WINDOW = 1500;

  function applyCaptureMarks(root) {
    Object.keys(captureMarks).forEach(function (id) {
      var row = (root || document).querySelector(
        '.rail-item[data-run-id="' + id + '"].captured'
      );
      if (row) row.classList.add("just-captured");
    });
  }

  function noteCapture(runId) {
    if (!runId) return;
    try {
      var raw = sessionStorage.getItem(CAPTURE_KEY);
      var seen = raw ? JSON.parse(raw) : {};
      if (!seen || typeof seen !== "object") seen = {};
      seen[runId] = Date.now();
      sessionStorage.setItem(CAPTURE_KEY, JSON.stringify(seen));
    } catch (e) { /* storage blocked: the page just does not animate */ }
  }

  function playCaptures() {
    var seen;
    try {
      var raw = sessionStorage.getItem(CAPTURE_KEY);
      if (!raw) return;
      sessionStorage.removeItem(CAPTURE_KEY);
      seen = JSON.parse(raw);
    } catch (e) { return; }
    if (!seen || typeof seen !== "object") return;
    // Two minutes is enough for the reload that `reloadSoon` schedules (900ms)
    // plus whatever the browser spends fetching; beyond it the entry is stale
    // and playing it would animate a result the operator saw come in long ago.
    var cutoff = Date.now() - 120000;
    Object.keys(seen).forEach(function (id) {
      if (!(seen[id] > cutoff)) return;
      // Ids are written by `noteCapture` as run numbers, but storage is shared
      // with whatever else wrote to this key. Anything that is not digits is
      // dropped rather than pasted into a selector, where a quote would throw
      // and take every later capture with it.
      if (!/^\d+$/.test(id)) return;
      var mine = document.querySelectorAll(
        '.rail-item[data-run-id="' + id + '"].captured'
      );
      if (!mine.length) return;
      mine.forEach(function (el) { el.classList.add("just-captured"); });
      // Remember it for one window, because the rail's own first refresh swaps
      // the whole list a moment after boot and would take the mark with it. The
      // timeout is what ends it: after the settle there is nothing left to keep
      // in step, and re-adding a finished animation on a later poll would make
      // an old result jump every five seconds.
      (function (mid) {
        captureMarks[id] = 1;
        setTimeout(function () {
          delete captureMarks[id];
          document.querySelectorAll(".rail-item.just-captured").forEach(
            function (el) {
              if (el.getAttribute("data-run-id") === mid)
                el.classList.remove("just-captured");
            }
          );
        }, CAPTURE_WINDOW);
      })(id);
    });
  }

  // A deliberately small Markdown renderer: headings, fences, lists, tables,
  // inline code, bold/italic, links. It covers what the writer produces and
  // never injects raw HTML.
  function renderMarkdown(src) {
    var lines = src.replace(/\r\n/g, "\n").split("\n");
    var out = [];
    var i = 0;
    var inCode = false, codeBuf = [];
    var codeLang = "";
    var listType = null;
    var tableBuf = [];

    function closeList() {
      if (listType) { out.push(listType === "ul" ? "</ul>" : "</ol>"); listType = null; }
    }
    // The fence's info string, reduced to something that can only be a class
    // name. Same rule as `_fence_lang` in ctfagent/mdrender.py: the two
    // renderers must emit identical markup or a reload changes the message's
    // shape. A language that does not survive the filter gets no colour rather
    // than a broken selector.
    function fenceLang(line) {
      var rest = line.replace(/^\s*/, "").replace(/^`+/, "").trim();
      rest = rest ? rest.split(/\s+/)[0] : "";
      var lang = rest.replace(/[^A-Za-z0-9+#._-]/g, "").slice(0, 24);
      return /^[A-Za-z][A-Za-z0-9+#._-]*$/.test(lang) ? lang : "";
    }
    function flushCode() {
      out.push('<pre' + (codeLang ? ' class="lang-' + codeLang + '"' : '') +
               "><code>" + escapeHtml(codeBuf.join("\n")) + "</code></pre>");
    }
    function inline(text) {
      var s = escapeHtml(text);
      s = s.replace(/`([^`]+)`/g, "<code>$1</code>");
      s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
      s = s.replace(/(^|[^*])\*([^*]+)\*/g, "$1<em>$2</em>");
      s = s.replace(/\[([^\]]+)\]\((https?:[^)]+)\)/g, '<a href="$2" rel="noopener">$1</a>');
      return s;
    }

    // Front matter is metadata, not prose: render it as a compact key/value
    // strip above the rule instead of letting it read like a heading-less
    // paragraph (which is what a naive line-by-line renderer does).
    if (lines[0] === "---") {
      var end = lines.indexOf("---", 1);
      if (end > 0) {
        var pairs = [];
        lines.slice(1, end).forEach(function (line) {
          var m = line.match(/^([A-Za-z_][\w-]*)\s*:\s*(.*)$/);
          if (m) pairs.push([m[1], m[2].replace(/^["']|["']$/g, "")]);
        });
        if (pairs.length) {
          out.push('<dl class="meta-strip">');
          pairs.forEach(function (p) {
            out.push("<dt>" + escapeHtml(p[0]) + "</dt><dd>" + escapeHtml(p[1]) + "</dd>");
          });
          out.push("</dl>");
        }
        lines = lines.slice(end + 1);
        i = 0;
      }
    }
    function flushTable() {
      if (!tableBuf.length) return;
      var rows = tableBuf.filter(function (r) { return !/^\s*\|?[\s:|-]+\|?\s*$/.test(r); });
      out.push("<table>");
      rows.forEach(function (row, idx) {
        var cells = row.replace(/^\s*\|/, "").replace(/\|\s*$/, "").split("|");
        var tag = idx === 0 ? "th" : "td";
        out.push("<tr>" + cells.map(function (c) {
          return "<" + tag + ">" + inline(c.trim()) + "</" + tag + ">";
        }).join("") + "</tr>");
      });
      out.push("</table>");
      tableBuf = [];
    }

    while (i < lines.length) {
      var line = lines[i];

      if (/^\s*```/.test(line)) {
        if (inCode) {
          flushCode();
          codeBuf = []; codeLang = ""; inCode = false;
        } else {
          closeList(); flushTable();
          codeLang = fenceLang(line);
          inCode = true;
        }
        i += 1; continue;
      }
      if (inCode) { codeBuf.push(line); i += 1; continue; }

      if (/^\s*\|.*\|\s*$/.test(line)) { tableBuf.push(line); i += 1; continue; }
      flushTable();

      var heading = line.match(/^(#{1,6})\s+(.*)$/);
      if (heading) {
        closeList();
        var level = Math.min(heading[1].length, 3);
        out.push("<h" + level + ">" + inline(heading[2]) + "</h" + level + ">");
        i += 1; continue;
      }
      if (/^\s*([-*_])\1{2,}\s*$/.test(line)) { closeList(); out.push("<hr>"); i += 1; continue; }
      if (/^\s*>\s?/.test(line)) {
        closeList();
        out.push("<blockquote>" + inline(line.replace(/^\s*>\s?/, "")) + "</blockquote>");
        i += 1; continue;
      }
      var ul = line.match(/^\s*[-*+]\s+(.*)$/);
      if (ul) {
        if (listType !== "ul") { closeList(); out.push("<ul>"); listType = "ul"; }
        out.push("<li>" + inline(ul[1]) + "</li>");
        i += 1; continue;
      }
      var ol = line.match(/^\s*\d+[.)]\s+(.*)$/);
      if (ol) {
        if (listType !== "ol") { closeList(); out.push("<ol>"); listType = "ol"; }
        out.push("<li>" + inline(ol[1]) + "</li>");
        i += 1; continue;
      }
      if (!line.trim()) { closeList(); i += 1; continue; }
      closeList();
      out.push("<p>" + inline(line) + "</p>");
      i += 1;
    }
    if (inCode && codeBuf.length) flushCode();
    closeList(); flushTable();
    return out.join("\n");
  }

  // ---------------------------------------------------------------- code blocks
  //
  // Syntax colouring for fenced code, and a copy button on every block. Both
  // run on the rendered DOM rather than inside `renderMarkdown`, because the
  // same markup arrives from three places — a live stream that re-renders on
  // every token, the server's Jinja filter on reload, and the writeup fetched
  // as text — and only the DOM version can mark both paths at once.
  //
  // The highlighter is deliberately small. It is not a parser: it splits each
  // fence into strings/comments/numbers, then colours words from a keyword list
  // plus three shape rules that hold in nearly every language an agent writes
  // (a capitalised word is a type, `name(` is a call, `@decorator` is a
  // decorator). Wrong guesses are visible but harmless, and the alternative —
  // shipping a parser and a theme per language — is far more page than this
  // product needs.

  // Languages whose `#` starts a comment. Everything else treats `//` as one;
  // Python/Ruby/YAML/shell also get `#`. Kept as a list, not a property of a
  // bigger table, because this is the only place the distinction matters.
  var HASH_COMMENT = { python: 1, py: 1, ruby: 1, rb: 1, yaml: 1, yml: 1,
                       bash: 1, sh: 1, shell: 1, zsh: 1, ps1: 1, powershell: 1,
                       toml: 1, ini: 1, conf: 1, r: 1, perl: 1, pl: 1 };

  var KEYWORDS = ("if else for while return def class import from as try except finally with " +
    "lambda yield pass break continue and or not in is None True False self " +
    "function var let const new this typeof instanceof switch case default throw catch " +
    "await async static public private protected void int char float double bool " +
    "struct enum interface package extends implements null true false undefined " +
    "using namespace template typename unsigned signed long short do delete " +
    "echo export local read fi then done esac elif").split(" ");

  var KW_SET = {};
  KEYWORDS.forEach(function (w) { KW_SET[w] = 1; });

  function isLang(lang, set) { return !!set[lang]; }

  // Split one code string into token spans. Everything is escaped first, so a
  // token can only ever become a span — the input cannot inject markup.
  function highlight(code, lang) {
    var hash = isLang(lang, HASH_COMMENT);
    var out = "";
    var i = 0;
    var n = code.length;
    var buf = "";

    function flushWords(text) {
      // Words and the three shape rules; punctuation passes through as-is.
      return text.replace(/[A-Za-z_$][\w$.]*|0x[0-9a-fA-F]+|\d+(?:\.\d+)?/g,
        function (word, offset, whole) {
          var after = whole.charAt(offset + word.length);
          var before = whole.charAt(offset - 1);
          if (/^\d/.test(word) || /^0x/i.test(word)) return '<span class="tk-n">' + word + "</span>";
          if (KW_SET[word]) return '<span class="tk-k">' + word + "</span>";
          if (after === "(") return '<span class="tk-f">' + word + "</span>";
          if (before === "@") return '<span class="tk-d">' + word + "</span>";
          if (/^[A-Z][a-z0-9]/.test(word) && !whole.slice(Math.max(0, offset - 1), offset).match(/[.$\w]/))
            return '<span class="tk-t">' + word + "</span>";
          return word;
        });
    }

    function emit() {
      if (buf) { out += flushWords(escapeHtml(buf)); buf = ""; }
    }

    while (i < n) {
      var ch = code.charAt(i);
      var two = code.substr(i, 2);

      // Comments: to end of line, but only in a position where a comment can
      // start (not `http://` in an unquoted string — a rare enough case that
      // the colour is allowed to be briefly wrong there).
      if ((two === "//" && !hash) || (ch === "#" && hash)) {
        emit();
        var eol = code.indexOf("\n", i);
        if (eol < 0) eol = n;
        out += '<span class="tk-c">' + escapeHtml(code.slice(i, eol)) + "</span>";
        i = eol;
        continue;
      }
      // Strings: '…' "…" and `…`, honouring a backslash escape. A quote that
      // is never closed swallows the rest of the block in string colour, which
      // reads as "this line is suspect" and is better than dropping the fence.
      if (ch === '"' || ch === "'" || ch === "`") {
        emit();
        var j = i + 1;
        while (j < n && code.charAt(j) !== ch) {
          if (code.charAt(j) === "\\") j += 1;
          j += 1;
        }
        j = Math.min(j + 1, n);
        out += '<span class="tk-s">' + escapeHtml(code.slice(i, j)) + "</span>";
        i = j;
        continue;
      }
      buf += ch;
      i += 1;
    }
    emit();
    return out;
  }

  // Which fences get colour. An unlabelled fence is usually a pasted result
  // (`{"flag": …}`, a hex dump, a command's output) rather than source, and a
  // guessed language colours those wrong; `text`/`plain` mean the same thing
  // explicitly. Everything else is highlighted, because the label is the
  // author saying "this is code".
  var NO_HIGHLIGHT = { "": 1, text: 1, txt: 1, plain: 1, plaintext: 1, output: 1 };

  function codeLangOf(pre) {
    var m = /(?:^|\s)lang-([A-Za-z0-9+#._-]+)/.exec(pre.className || "");
    return m ? m[1].toLowerCase() : "";
  }

  function decorateCode(root) {
    if (!root) return;
    var pres = root.querySelectorAll("pre");
    Array.prototype.forEach.call(pres, function (pre) {
      if (pre.classList.contains("console")) return;   // the event log is not code
      if (pre.getAttribute("data-decorated")) return;
      pre.setAttribute("data-decorated", "1");
      var code = pre.querySelector("code");
      if (!code) return;
      var lang = codeLangOf(pre);
      if (!NO_HIGHLIGHT[lang]) {
        var text = code.textContent;
        try { code.innerHTML = highlight(text, lang); } catch (e) { /* plain text is fine */ }
      }
      addCopyBar(pre, lang);
    });
  }

  // The bar is part of the block, not floating over it: a code block in this
  // product is a plate on the page, and a control that hovers over the text it
  // copies is the one shape that would make it look like a chat product. Bar
  // and plate are wrapped together so they move as one object and the bar's
  // label can sit on the block's own top edge. The label is the language the
  // author put on the fence — this is the only place it is ever shown, and it
  // is data, so it is set in the mono face.
  function addCopyBar(pre, lang) {
    var box = document.createElement("div");
    box.className = "code-block";
    var bar = document.createElement("div");
    bar.className = "code-bar";
    var tag = document.createElement("span");
    tag.className = "code-lang";
    tag.textContent = lang || "code";
    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "code-copy";
    btn.textContent = "复制";
    btn.setAttribute("aria-label", "复制这段代码");
    btn.addEventListener("click", function () {
      // Read at click time from the DOM, never from a captured string: the
      // writeup pane and the chat both re-render their container, so the text
      // living in this node now is the only copy guaranteed to be current.
      var code = pre.querySelector("code");
      copyText(code ? code.textContent : pre.textContent);
    });
    bar.appendChild(tag);
    bar.appendChild(btn);
    pre.parentNode.insertBefore(box, pre);
    box.appendChild(bar);
    box.appendChild(pre);
  }

  function loadWriteup() {
    if (writeupLoaded) return;
    var target = document.getElementById("writeup-body");
    var empty = document.getElementById("writeup-empty");
    if (!target) return;
    // A 404 is the normal answer before the writer has run; the page already
    // knows whether a writeup exists, so do not ask when it does not.
    if (target.getAttribute("data-exists") === "0") {
      if (empty) empty.hidden = false;
      target.hidden = true;
      return;
    }
    fetch("/api/runs/" + target.getAttribute("data-run-id") + "/writeup")
      .then(function (r) {
        if (r.status === 404) {
          target.setAttribute("data-exists", "0");
          throw new Error("no writeup");
        }
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.text();
      })
      .then(function (text) {
        target.innerHTML = renderMarkdown(text);
        decorateCode(target);
        writeupLoaded = true;
        if (empty) empty.hidden = true;
        target.hidden = false;
      })
      .catch(function () {
        if (empty) empty.hidden = false;
        target.hidden = true;
      });
  }

  // ---------------------------------------------------------------- boot

  document.addEventListener("DOMContentLoaded", function () {
    refreshRelTimes();
    setInterval(refreshRelTimes, 30000);
    tickElapsed();
    setInterval(tickElapsed, 1000);
    initTabs();
    initConsole();
    initComposer();
    initAttachments();
    initDropZone();
    initRail();
    initLiveStrip();
    initThinking();
    initStream();
    initToLatest();
    initShellOffset();
    initNavTransition();
    initFlagConfirm();
    initBusyForms();
    initPalette();
    // After the rail exists in the DOM: this marks the one row that was just
    // confirmed, and the mark is read, not recomputed.
    playCaptures();

    // Fenced code: colour and a copy button on every block.
    decorateCode(document);

    // The flag card's copy button reads its text from the sibling `.value`,
    // which already carries the literal in `data-copy`. Delegated from the
    // document so a card re-rendered by the rail's poll needs no re-binding.
    document.addEventListener("click", function (event) {
      var btn = event.target.closest && event.target.closest(".copy-flag");
      if (!btn) return;
      var card = btn.closest(".flag");
      var value = card && card.querySelector(".value");
      if (value) copyText(value.getAttribute("data-copy") || value.textContent.trim());
    });

    // ---- first review batch ------------------------------------------------
    // Clamped fields the server marked .cut carry more text than two lines
    // show. One delegated listener survives every rail/panel re-render.
    document.addEventListener("click", function (event) {
      var field = event.target.closest && event.target.closest(".clampable.cut");
      if (!field) return;
      field.classList.toggle("open");
    });

    // The end-of-run notice is a fact about the session, not a task; once it
    // is read, it stays dismissed for this run and this browser.
    var endedNotice = document.getElementById("ended-notice");
    if (endedNotice) {
      var key = "notice-dismissed-" + endedNotice.getAttribute("data-run-id");
      try {
        if (window.sessionStorage.getItem(key) === "1") endedNotice.hidden = true;
      } catch (err) { /* storage may be unavailable; the notice simply stays */ }
      var dismiss = document.getElementById("notice-dismiss");
      if (dismiss) dismiss.addEventListener("click", function () {
        endedNotice.hidden = true;
        try { window.sessionStorage.setItem(key, "1"); } catch (err) {}
      });
    }

    // Delete is the row's only irreversible action: the first click arms the
    // button in place — its own label names what the second click costs — and
    // leaving it alone disarms it. Same shape as the old confirm(), minus the
    // modal that blocked seeing what is being deleted.
    document.addEventListener("click", function (event) {
      var btn = event.target.closest && event.target.closest(".delete-run button[data-arm]");
      if (!btn) return;
      if (btn.classList.contains("armed")) return; // second click submits
      event.preventDefault();
      if (!btn.getAttribute("data-label")) btn.setAttribute("data-label", btn.textContent);
      btn.textContent = btn.getAttribute("data-arm");
      btn.classList.add("armed");
      window.setTimeout(function () {
        btn.classList.remove("armed");
        btn.textContent = btn.getAttribute("data-label");
      }, 4000);
    });
  });
})();
