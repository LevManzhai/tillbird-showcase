(function () {
  "use strict";

  var scriptEl = document.currentScript;
  if (!scriptEl) return;

  var storeId = scriptEl.getAttribute("data-store");
  if (!storeId) return;

  var apiBase;
  try {
    apiBase = new URL(scriptEl.src).origin;
  } catch (e) {
    return;
  }

  // Embed-time options, set by whoever places the <script> tag (unlike
  // store_name/brand_color/etc below, which come from the server's own
  // per-store config) — these describe how THIS page wants the widget
  // presented, not anything about the store itself.
  var embedMode = scriptEl.getAttribute("data-mode") === "inline" ? "inline" : "floating";
  var targetId = scriptEl.getAttribute("data-target");
  var targetEl = embedMode === "inline" && targetId ? document.getElementById(targetId) : null;
  if (embedMode === "inline" && !targetEl) embedMode = "floating"; // bad/missing target — fail back rather than silently do nothing

  var suggestedQuestions = [];
  var questionsAttr = scriptEl.getAttribute("data-questions");
  if (questionsAttr) {
    try {
      var parsed = JSON.parse(questionsAttr);
      if (Array.isArray(parsed)) suggestedQuestions = parsed.filter(function (q) { return typeof q === "string" && q; });
    } catch (e) {
      /* malformed data-questions — just show no chips */
    }
  }

  var cannedAnswersUrl = scriptEl.getAttribute("data-canned-answers");

  // Questions to play back automatically on load, in order — each one MUST
  // have a canned answer (see below); the demo never calls the live API for
  // these, and playback stops the instant the visitor does anything.
  var autoplayQuestions = [];
  var autoplayAttr = scriptEl.getAttribute("data-autoplay");
  if (autoplayAttr) {
    try {
      var parsedAutoplay = JSON.parse(autoplayAttr);
      if (Array.isArray(parsedAutoplay)) {
        autoplayQuestions = parsedAutoplay.filter(function (q) {
          return typeof q === "string" && q;
        });
      }
    } catch (e) {
      /* malformed data-autoplay — just skip autoplay */
    }
  }

  var HINT_KEY = "tillbird_hint_shown_" + storeId;
  // localStorage, not sessionStorage — the point is surviving a closed
  // tab/browser (per-store, so the hero demo and a floating "Ask about
  // Tillbird" widget on the same page never share history — they use
  // different storeIds and therefore different keys).
  var CONV_KEY = "tillbird_conv_" + storeId;
  var CONV_MAX_AGE_MS = 24 * 60 * 60 * 1000; // 24h of inactivity

  function safeStorageGet(key) {
    try {
      return sessionStorage.getItem(key);
    } catch (e) {
      return null;
    }
  }
  function safeStorageSet(key, value) {
    try {
      sessionStorage.setItem(key, value);
    } catch (e) {
      /* ignore */
    }
  }

  function newSessionId() {
    return window.crypto && window.crypto.randomUUID
      ? window.crypto.randomUUID()
      : "sess-" + Date.now() + "-" + Math.random().toString(36).slice(2);
  }

  // Every localStorage read/write is wrapped in try/catch — private
  // browsing, disabled storage, or a full quota must never break the
  // widget, it just won't persist that time.
  function loadConversation() {
    try {
      var raw = localStorage.getItem(CONV_KEY);
      if (!raw) return null;
      var parsed = JSON.parse(raw);
      if (!parsed || typeof parsed !== "object") return null;
      if (typeof parsed.sessionId !== "string" || !parsed.sessionId) return null;
      if (!Array.isArray(parsed.messages)) return null;
      if (typeof parsed.updatedAt !== "number" || Date.now() - parsed.updatedAt > CONV_MAX_AGE_MS) return null;
      return parsed;
    } catch (e) {
      return null;
    }
  }

  function saveConversation(sessId, messages) {
    try {
      localStorage.setItem(CONV_KEY, JSON.stringify({ sessionId: sessId, messages: messages, updatedAt: Date.now() }));
    } catch (e) {
      /* storage unavailable/full — this turn just won't persist */
    }
  }

  function clearConversation() {
    try {
      localStorage.removeItem(CONV_KEY);
    } catch (e) {
      /* ignore */
    }
  }

  var savedConversation = loadConversation();
  var sessionId = (savedConversation && savedConversation.sessionId) || newSessionId();

  var DEFAULTS = {
    store_name: "",
    brand_color: "#1a1a2e",
    greeting: "Hi! How can I help you today?",
    hint_text: "",
    privacy_policy_url: null,
    tillbird_privacy_url: null,
    widget_tagline: null,
  };

  // If /api/chat hasn't answered by this point, stop waiting rather than
  // leave the customer staring at a spinner forever — matches the server's
  // own worst case (ANTHROPIC_REQUEST_TIMEOUT_SECONDS=20s x
  // (ANTHROPIC_MAX_RETRIES=1 + 1) + a turn_timeout check, see
  // app/agent/loop.py), with a little headroom for network latency.
  var CHAT_TIMEOUT_MS = 45000;

  var CSS =
    ":host{all:initial}" +
    "*{box-sizing:border-box;margin:0;padding:0}" +
    ".launcher{position:fixed;right:20px;bottom:20px;width:56px;height:56px;border-radius:50%;" +
    "background:var(--brand);color:var(--brand-text);border:none;cursor:pointer;display:flex;align-items:center;" +
    "justify-content:center;box-shadow:0 4px 16px rgba(0,0,0,.25);z-index:1}" +
    ".launcher svg{width:26px;height:26px}" +
    "@media (max-width:749px){.launcher{bottom:84px}}" +
    ".hint{position:fixed;right:86px;bottom:28px;max-width:220px;background:#fff;color:#1a1a1a;" +
    "font:14px/1.4 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;" +
    "padding:10px 12px;border-radius:10px;box-shadow:0 4px 16px rgba(0,0,0,.2);display:flex;" +
    "align-items:center;gap:8px}" +
    "@media (max-width:749px){.hint{bottom:92px;right:auto;left:16px;right:86px}}" +
    ".hint button{background:none;border:none;color:#888;font-size:16px;line-height:1;cursor:pointer;padding:0 2px}" +
    ".panel{background:#fff;display:none;flex-direction:column;overflow:hidden;" +
    "font:14px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#1a1a1a}" +
    ".panel.open{display:flex}" +
    // Floating (default) positioning — an inline-embedded panel (.inline)
    // instead sizes to fill its host container, set further down.
    ".panel:not(.inline){position:fixed;right:20px;bottom:88px;width:360px;max-width:calc(100vw - 32px);" +
    "height:min(560px,calc(100vh - 120px));border-radius:16px;box-shadow:0 8px 32px rgba(0,0,0,.3)}" +
    "@media (max-width:749px){.panel:not(.inline){right:0;bottom:0;left:0;top:0;width:100%;max-width:100%;" +
    "height:100vh;height:100dvh;border-radius:0}}" +
    // min-height here is only a fallback for a host page that forgets to
    // give its target element a height (where height:100% would resolve to
    // auto and collapse to 0) — it must stay at or below the smallest
    // intended real size (currently 400px, set on .demo-widget-target),
    // or it'll force the panel taller than its container and get clipped.
    ".panel.inline{position:relative;width:100%;height:100%;min-height:360px;border-radius:14px;" +
    "border:1px solid #e3e0da;box-shadow:0 1px 3px rgba(0,0,0,.06)}" +
    ".chips{display:flex;flex-wrap:wrap;gap:6px;padding:0 16px 10px;flex-shrink:0}" +
    // Once the conversation has started, chips collapse to a single
    // non-wrapping scrollable row so they stop competing with the message
    // list (flex:1 above) for the panel's height — see setChipsCollapsed().
    ".chips.collapsed{flex-wrap:nowrap;overflow-x:auto;padding-bottom:8px;" +
    "-webkit-overflow-scrolling:touch}" +
    ".chips.collapsed .chip{white-space:nowrap;flex-shrink:0}" +
    ".chip{background:#fff;border:1px solid var(--brand);color:var(--brand);border-radius:14px;" +
    "padding:6px 12px;font-size:12.5px;cursor:pointer;text-align:left;line-height:1.3;font-family:inherit}" +
    ".chip:hover{background:var(--brand);color:var(--brand-text)}" +
    ".msg-tag{font-size:10px;text-transform:uppercase;letter-spacing:.04em;color:#9a9a9a;" +
    "align-self:flex-start;margin-top:-4px}" +
    ".header{background:var(--brand);color:var(--brand-text);padding:12px 16px;display:flex;align-items:center;" +
    "justify-content:space-between;flex-shrink:0;gap:8px}" +
    ".header .title-group{display:flex;flex-direction:column;gap:2px;min-width:0}" +
    ".header .title{font-weight:600;font-size:15px}" +
    ".header .tagline{font-size:11px;font-weight:400;opacity:.85}" +
    ".header .disclosure{font-size:10px;font-weight:400;opacity:.75}" +
    ".header .disclosure a{color:inherit;text-decoration:underline}" +
    ".header button{background:rgba(255,255,255,.15);border:none;color:var(--brand-text);width:28px;height:28px;" +
    "border-radius:50%;cursor:pointer;font-size:16px;line-height:1}" +
    // min-height:0 is load-bearing: a flex item's default min-height is
    // "auto" (its content's intrinsic size), which overrides flex:1's
    // shrink-to-fit-container behaviour and lets .body grow to fit every
    // message instead of scrolling — pushing the footer (input, chips,
    // send button) out of view below the panel's clipped bounds. Without
    // this the panel silently grows to whatever height its content needs
    // and gets clipped by whatever container it's sitting in.
    ".body{flex:1;min-height:0;overflow-y:auto;padding:14px 16px;display:flex;flex-direction:column;gap:10px;" +
    "-webkit-overflow-scrolling:touch}" +
    ".msg{max-width:80%;padding:9px 12px;border-radius:14px;white-space:pre-wrap;word-wrap:break-word;" +
    "overflow-wrap:anywhere}" +
    ".msg.bot{background:#f0f0f2;align-self:flex-start;border-bottom-left-radius:4px}" +
    ".msg.user{background:var(--brand);color:var(--brand-text);align-self:flex-end;border-bottom-right-radius:4px}" +
    ".typing{align-self:flex-start;display:flex;gap:4px;padding:10px 12px}" +
    ".typing span{width:6px;height:6px;border-radius:50%;background:#bbb;animation:blink 1.2s infinite}" +
    ".typing span:nth-child(2){animation-delay:.2s}.typing span:nth-child(3){animation-delay:.4s}" +
    "@keyframes blink{0%,80%,100%{opacity:.3}40%{opacity:1}}" +
    ".footer{border-top:1px solid #eee;flex-shrink:0}" +
    ".human-form{padding:12px 16px;display:flex;flex-direction:column;gap:8px}" +
    ".back-to-chat{background:none;border:none;color:var(--brand);font-size:12px;cursor:pointer;" +
    "padding:0;text-align:left;align-self:flex-start}" +
    ".human-form input,.human-form textarea{font-size:16px;padding:8px 10px;border:1px solid #ddd;" +
    "border-radius:8px;font-family:inherit;width:100%;resize:vertical}" +
    ".human-form .row{display:flex;gap:8px}" +
    ".human-form .human-send{background:var(--brand);color:var(--brand-text);border:none;border-radius:8px;padding:8px 14px;" +
    "font-size:14px;cursor:pointer;align-self:flex-start}" +
    ".human-form .human-send:disabled{opacity:.5;cursor:default}" +
    ".input-row{display:flex;gap:8px;padding:10px 12px;align-items:flex-end}" +
    ".input-row textarea{flex:1;font-size:16px;border:1px solid #ddd;border-radius:18px;padding:9px 14px;" +
    "font-family:inherit;resize:none;max-height:100px;line-height:1.3}" +
    ".input-row button{background:var(--brand);color:var(--brand-text);border:none;width:36px;height:36px;border-radius:50%;" +
    "cursor:pointer;flex-shrink:0;display:flex;align-items:center;justify-content:center}" +
    ".input-row button:disabled{opacity:.5;cursor:default}" +
    ".footer-links-row{display:flex;align-items:center;justify-content:space-between;gap:12px;" +
    "padding:8px 16px 0}" +
    ".footer-links-row button{background:none;border:none;font-size:12px;cursor:pointer;padding:0;" +
    "text-decoration:underline;flex-shrink:0;font-family:inherit}" +
    ".talk-human{color:var(--brand)}" +
    ".new-chat{color:#999}" +
    ".privacy{font-size:11px;color:#999;padding:0 16px 10px;line-height:1.4}" +
    ".privacy a{color:#999}" +
    ".error-banner{background:#fff3f0;color:#8a3b2f;font-size:12px;padding:8px 16px;flex-shrink:0}" +
    // Author-stylesheet [hidden] rules above (e.g. .hint, .human-form both set
    // their own `display`) have equal specificity to the UA default
    // `[hidden]{display:none}` and win by source order, so the element stays
    // visible despite the hidden attribute. This re-asserts it unconditionally.
    "[hidden]{display:none!important}";

  function iconSvg() {
    return (
      '<svg viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">' +
      '<path d="M4 4h16v12H7l-3 3V4z" fill="currentColor"/></svg>'
    );
  }

  function escapeHtml(s) {
    var div = document.createElement("div");
    div.textContent = s == null ? "" : String(s);
    return div.innerHTML;
  }

  // WCAG 2.x relative luminance + contrast ratio — see
  // www.w3.org/TR/WCAG21/#dfn-relative-luminance. Used to pick white vs
  // dark text/icon colour against a store's own brand_color background
  // (header, user message bubbles, the send button) — a pale brand colour
  // (confirmed live: riversidewoodcraft.co.uk's #faf5f0) made white text
  // on it unreadable. contrastWithWhite(x) >= 4.5 is WCAG AA for normal text.
  function relativeLuminance(hex) {
    var m = /^#?([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(hex || "");
    if (!m) return null;
    var channels = [m[1], m[2], m[3]].map(function (h) {
      var c = parseInt(h, 16) / 255;
      return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
    });
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2];
  }

  function contrastWithWhite(hex) {
    var l = relativeLuminance(hex);
    if (l == null) return null;
    return 1.05 / (l + 0.05);
  }

  // Near-black, not the brand default navy (#1a1a2e) — this is a plain
  // text/icon colour, unrelated to any particular brand.
  var DARK_TEXT = "#1a1a1a";

  function pickBrandTextColor(hex) {
    var ratio = contrastWithWhite(hex);
    // An unparseable colour (shouldn't happen — brand_color is always a
    // hex string) fails open to white, the previous unconditional
    // behaviour, rather than guessing.
    if (ratio == null) return "#fff";
    return ratio >= 4.5 ? "#fff" : DARK_TEXT;
  }

  function build(config) {
    var inline = embedMode === "inline";
    var host = document.createElement("div");
    if (inline) {
      host.style.display = "block";
      host.style.width = "100%";
      host.style.height = "100%";
      targetEl.appendChild(host);
    } else {
      host.style.position = "fixed";
      host.style.top = "0";
      host.style.left = "0";
      host.style.width = "0";
      host.style.height = "0";
      host.style.zIndex = "2147483647";
      document.body.appendChild(host);
    }

    var shadow = host.attachShadow({ mode: "open" });
    var style = document.createElement("style");
    var brandColor = config.brand_color || DEFAULTS.brand_color;
    style.textContent =
      ":host{--brand:" + brandColor + ";--brand-text:" + pickBrandTextColor(brandColor) + "}" + CSS;
    shadow.appendChild(style);

    var chipsHtml = "";
    if (suggestedQuestions.length) {
      chipsHtml = '<div class="chips">';
      for (var qi = 0; qi < suggestedQuestions.length; qi++) {
        chipsHtml += '<button type="button" class="chip" data-question="' + escapeHtml(suggestedQuestions[qi]) + '">' +
          escapeHtml(suggestedQuestions[qi]) + "</button>";
      }
      chipsHtml += "</div>";
    }

    var wrap = document.createElement("div");
    // This plain wrapper sits between the shadow root and .panel. Without an
    // explicit height it defaults to auto (content-sized), which breaks
    // .panel.inline's height:100% — a percentage height against an
    // auto-height ancestor resolves as auto too, so the panel silently grows
    // to fit all its content instead of staying pinned to the host's real
    // size, and gets clipped by whatever fixed-height container it's in.
    wrap.style.height = "100%";
    wrap.innerHTML =
      (inline
        ? ""
        : '<button class="launcher" type="button" aria-label="Open chat">' + iconSvg() + "</button>" +
          '<div class="hint" hidden></div>') +
      '<div class="panel' + (inline ? " inline open" : "") + '" role="dialog" aria-label="' +
      escapeHtml(config.store_name || "Chat") +
      '">' +
      '<div class="header"><div class="title-group"><span class="title">' +
      escapeHtml(config.store_name || "Chat with us") +
      "</span>" +
      (config.widget_tagline ? '<span class="tagline">' + escapeHtml(config.widget_tagline) + "</span>" : "") +
      '<span class="disclosure">AI assistant · may make mistakes' +
      (config.tillbird_privacy_url
        ? ' · <a href="' + escapeHtml(config.tillbird_privacy_url) + '" target="_blank" rel="noopener">Privacy</a>'
        : "") +
      "</span>" +
      "</div>" +
      (inline ? "" : '<button type="button" class="close" aria-label="Close chat">&times;</button>') +
      "</div>" +
      '<div class="body"></div>' +
      chipsHtml +
      '<div class="footer">' +
      '<div class="error-banner" hidden></div>' +
      '<div class="chat-footer">' +
      '<div class="footer-links-row">' +
      '<button type="button" class="talk-human">Talk to a person</button>' +
      '<button type="button" class="new-chat">Start new chat</button>' +
      "</div>" +
      '<div class="input-row">' +
      '<textarea rows="1" placeholder="Type a message…" class="chat-input"></textarea>' +
      '<button type="button" class="send" aria-label="Send">' +
      '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" xmlns="http://www.w3.org/2000/svg">' +
      '<path d="M3 11l18-8-8 18-2-8-8-2z" fill="currentColor"/></svg>' +
      "</button>" +
      "</div>" +
      "</div>" +
      '<div class="human-form" hidden>' +
      '<button type="button" class="back-to-chat">&lsaquo; Back to chat</button>' +
      '<input type="email" class="human-email" placeholder="Your email" autocomplete="email">' +
      '<textarea class="human-desc" rows="2" placeholder="Briefly, what\'s up?"></textarea>' +
      '<div class="row"><button type="button" class="human-send">Send to a person</button></div>' +
      "</div>" +
      (config.privacy_policy_url
        ? '<div class="privacy">AI assistant. Messages are stored to improve support. ' +
          '<a href="' +
          escapeHtml(config.privacy_policy_url) +
          '" target="_blank" rel="noopener">Privacy policy</a></div>'
        : '<div class="privacy">AI assistant. Messages are stored to improve support.</div>') +
      "</div>";
    shadow.appendChild(wrap);

    var launcher = wrap.querySelector(".launcher");
    var panel = wrap.querySelector(".panel");
    var closeBtn = wrap.querySelector(".close");
    var body = wrap.querySelector(".body");
    var input = wrap.querySelector(".chat-input");
    var sendBtn = wrap.querySelector(".send");
    var chatFooter = wrap.querySelector(".chat-footer");
    var talkHumanBtn = wrap.querySelector(".talk-human");
    var newChatBtn = wrap.querySelector(".new-chat");
    var humanForm = wrap.querySelector(".human-form");
    var backToChatBtn = wrap.querySelector(".back-to-chat");
    var humanEmail = wrap.querySelector(".human-email");
    var humanDesc = wrap.querySelector(".human-desc");
    var humanSend = wrap.querySelector(".human-send");
    var errorBanner = wrap.querySelector(".error-banner");
    var hintEl = wrap.querySelector(".hint");
    var chipsEl = wrap.querySelector(".chips"); // absent entirely if no suggestedQuestions were configured

    // Chips start in their normal wrapped layout (room to read each one) —
    // once the visitor has sent a first message, the conversation itself
    // needs the panel's height more than the chips do, so they collapse
    // into a single horizontally-scrollable row rather than competing with
    // the message list for space. "Start new chat" reverses this.
    function setChipsCollapsed(collapsed) {
      if (!chipsEl) return;
      chipsEl.classList.toggle("collapsed", collapsed);
    }

    var opened = false;
    var greeted = false;
    // Restored from localStorage (see loadConversation() above) if present
    // and not yet expired — otherwise starts empty, same as before.
    var messageLog = savedConversation && savedConversation.messages ? savedConversation.messages.slice() : [];

    // Pure DOM rendering, no persistence — used both for real new messages
    // (via addMessage() below) and for replaying restored history on load,
    // which must never re-trigger a save (that would bump updatedAt just
    // from viewing a restored conversation, when "24h of inactivity" should
    // mean since the last real message, not the last page view).
    function renderMessage(role, text, tag) {
      var m = document.createElement("div");
      m.className = "msg " + (role === "user" ? "user" : "bot");
      m.textContent = text;
      body.appendChild(m);
      // Small, honest disclosure on canned demo replies (see sendCanned())
      // so a visitor clicking an example question isn't misled into
      // thinking it round-tripped to the live AI — never applied to real
      // replies, which is the norm and needs no label of its own.
      if (tag) {
        var tagEl = document.createElement("div");
        tagEl.className = "msg-tag";
        tagEl.textContent = tag;
        body.appendChild(tagEl);
      }
      body.scrollTop = body.scrollHeight;
      return m;
    }

    function addMessage(role, text, tag) {
      var m = renderMessage(role, text, tag);
      messageLog.push({ role: role, text: text, tag: tag || null });
      saveConversation(sessionId, messageLog);
      // Collapse chips the moment the visitor's own first message goes in
      // (typed, a chip click, or an autoplay step — all go through here) —
      // from then on the conversation needs the panel's height more than
      // the suggestions do.
      if (role === "user") setChipsCollapsed(true);
      return m;
    }

    // Replay restored history (if any) before anything else touches .body,
    // so it's already there whichever way the panel gets opened (inline is
    // always open; floating only renders it lazily when clicked). Skips the
    // greeting below (it's already message #1 in a restored log) and skips
    // autoplay entirely — a returning visitor sees their own conversation,
    // not the demo script again.
    var restoredExistingConversation = messageLog.length > 0;
    if (restoredExistingConversation) {
      for (var ri = 0; ri < messageLog.length; ri++) {
        renderMessage(messageLog[ri].role, messageLog[ri].text, messageLog[ri].tag);
      }
      greeted = true;
      if (messageLog.some(function (m) { return m.role === "user"; })) setChipsCollapsed(true);
    }

    function showTyping() {
      var t = document.createElement("div");
      t.className = "typing";
      t.innerHTML = "<span></span><span></span><span></span>";
      body.appendChild(t);
      body.scrollTop = body.scrollHeight;
      return t;
    }

    function showError(msg) {
      errorBanner.textContent = msg;
      errorBanner.hidden = false;
    }
    function clearError() {
      errorBanner.hidden = true;
    }

    function openPanel() {
      opened = true;
      panel.classList.add("open");
      // The panel's own header already has a close (×) button; hiding the
      // launcher while open avoids it floating on top of the panel content
      // (most visible on mobile, where the panel goes full-screen and the
      // launcher — z-index:1, always above the panel's default stacking —
      // would otherwise sit on top of the input row). Inline embeds have
      // neither element (nothing to close back to), hence the null checks.
      if (launcher) launcher.hidden = true;
      if (hintEl) hintEl.hidden = true;
      if (!greeted && config.greeting) {
        addMessage("bot", config.greeting);
        greeted = true;
      }
      if (!inline) input.focus(); // don't yank scroll/focus on page load for an inline embed
    }
    function closePanel() {
      opened = false;
      panel.classList.remove("open");
      if (launcher) launcher.hidden = false;
    }

    if (launcher) {
      launcher.addEventListener("click", function () {
        opened ? closePanel() : openPanel();
      });
    }
    if (closeBtn) closeBtn.addEventListener("click", closePanel);

    function autoGrow() {
      input.style.height = "auto";
      input.style.height = Math.min(input.scrollHeight, 100) + "px";
    }
    input.addEventListener("input", autoGrow);
    input.addEventListener("keydown", function (e) {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        send();
      }
    });

    // Same spirit as the server's own fallback for a turn that ran out of
    // time (see app.agent.loop._timeout_fallback_reply) — used here for the
    // case the SERVER couldn't even finish in time to say so itself (e.g.
    // gunicorn killed the worker outright). The store's contact email isn't
    // in this public config payload (deliberately, to avoid it being
    // scraped off an unauthenticated endpoint), so this stays generic.
    var CHAT_TIMEOUT_FALLBACK = "Sorry, I'm taking too long on that one. Please try again shortly.";

    var sending = false;
    function send() {
      var text = input.value.trim();
      if (!text || sending) return;
      addMessage("user", text);
      input.value = "";
      autoGrow();
      sending = true;
      sendBtn.disabled = true;
      var typingEl = showTyping();

      var controller = typeof AbortController !== "undefined" ? new AbortController() : null;
      var timedOut = false;
      var timer = controller
        ? setTimeout(function () {
            timedOut = true;
            controller.abort();
          }, CHAT_TIMEOUT_MS)
        : null;

      fetch(apiBase + "/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ store_id: storeId, session_id: sessionId, message: text }),
        signal: controller ? controller.signal : undefined,
      })
        .then(function (res) {
          if (!res.ok) throw new Error("bad status");
          return res.json();
        })
        .then(function (data) {
          typingEl.remove();
          clearError();
          addMessage("bot", data.reply || "…");
        })
        .catch(function () {
          typingEl.remove();
          if (timedOut) {
            addMessage("bot", CHAT_TIMEOUT_FALLBACK);
          } else {
            showError("Sorry, something went wrong sending that. Please try again.");
          }
        })
        .finally(function () {
          if (timer) clearTimeout(timer);
          sending = false;
          sendBtn.disabled = false;
        });
    }
    sendBtn.addEventListener("click", send);

    // Suggested-question chips (data-questions on the <script> tag). If a
    // static canned answer was loaded for this exact question text (see
    // config.cannedAnswers, populated only when data-canned-answers is set —
    // used by the tillbird.co.uk site demo to keep the example questions
    // free to click without spending real API calls), play that back
    // instead of hitting the live API. Anything without a canned match —
    // including every chip on a normal embed with no canned-answers file —
    // sends live, exactly like typing it in.
    function sendCanned(question, answer) {
      addMessage("user", question);
      var typingEl = showTyping();
      var delay = 600 + Math.random() * 700; // a beat, not an instant reply — feels considered, not obviously canned
      setTimeout(function () {
        typingEl.remove();
        addMessage("bot", answer, "Example answer");
      }, delay);
    }

    // Auto-played scripted conversation (data-autoplay on the <script> tag —
    // used by the tillbird.co.uk hero so the demo shows itself off before a
    // visitor touches anything). Reuses the same canned-answer data as the
    // chips and NEVER calls the live API — if a scripted question has no
    // canned match, playback just stops rather than ever falling back to a
    // real request. Cancelled the instant the visitor does anything (see
    // the stopAutoplay() wiring below).
    var autoplayTimers = [];
    // Also skip autoplay entirely if we just restored a real conversation —
    // a returning visitor should see their own history, not the demo script.
    var autoplayStopped = autoplayQuestions.length === 0 || restoredExistingConversation;

    function stopAutoplay() {
      if (autoplayStopped) return;
      autoplayStopped = true;
      for (var ti = 0; ti < autoplayTimers.length; ti++) clearTimeout(autoplayTimers[ti]);
      autoplayTimers = [];
    }

    function scheduleAutoplay(fn, delay) {
      var id = setTimeout(function () {
        if (autoplayStopped) return;
        fn();
      }, delay);
      autoplayTimers.push(id);
    }

    function playAutoplayQuestion(index) {
      if (autoplayStopped || index >= autoplayQuestions.length) return;
      var question = autoplayQuestions[index];
      var canned =
        config.cannedAnswers && Object.prototype.hasOwnProperty.call(config.cannedAnswers, question)
          ? config.cannedAnswers[question]
          : null;
      if (!canned) return; // no canned match for this scripted step — stop rather than ever call the API

      addMessage("user", question);
      var typingEl = showTyping();
      scheduleAutoplay(function () {
        typingEl.remove();
        addMessage("bot", canned, "Example answer");
        scheduleAutoplay(function () {
          playAutoplayQuestion(index + 1);
        }, 2200);
      }, 1100);
    }

    if (!autoplayStopped) {
      input.addEventListener("focus", stopAutoplay);
      input.addEventListener("input", stopAutoplay);
      panel.addEventListener("click", stopAutoplay);
    }

    var chipButtons = wrap.querySelectorAll(".chip");
    for (var ci = 0; ci < chipButtons.length; ci++) {
      chipButtons[ci].addEventListener("click", function (e) {
        stopAutoplay();
        var question = e.currentTarget.getAttribute("data-question");
        var canned =
          config.cannedAnswers && Object.prototype.hasOwnProperty.call(config.cannedAnswers, question)
            ? config.cannedAnswers[question]
            : null;
        if (canned) {
          sendCanned(question, canned);
        } else {
          input.value = question;
          send();
        }
      });
    }

    function showHumanForm(show) {
      humanForm.hidden = !show;
      chatFooter.hidden = show;
      clearError();
      // The handoff form is taller than the normal input row, so the message
      // list (flex:1) shrinks when it opens — keep the latest message in view
      // rather than letting it end up clipped below the fold.
      body.scrollTop = body.scrollHeight;
      if (show) humanEmail.focus();
      else input.focus();
    }

    talkHumanBtn.addEventListener("click", function () {
      showHumanForm(true);
    });
    backToChatBtn.addEventListener("click", function () {
      showHumanForm(false);
    });

    // Clears the persisted conversation and starts a genuinely fresh one
    // (new session_id too, so server-side session caps also reset) — the
    // old conversation's messages stay in the store's DB under the old
    // session_id, just no longer shown or continued client-side.
    newChatBtn.addEventListener("click", function () {
      stopAutoplay();
      clearConversation();
      sessionId = newSessionId();
      messageLog = [];
      body.innerHTML = "";
      greeted = false;
      clearError();
      showHumanForm(false);
      setChipsCollapsed(false);
      if (config.greeting) {
        addMessage("bot", config.greeting);
        greeted = true;
      }
      input.focus();
    });

    var humanSending = false;
    humanSend.addEventListener("click", function () {
      var email = humanEmail.value.trim();
      var desc = humanDesc.value.trim();
      if (!email) {
        humanEmail.focus();
        return;
      }
      if (humanSending) return;
      humanSending = true;
      humanSend.disabled = true;

      fetch(apiBase + "/api/handoff", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          store_id: storeId,
          session_id: sessionId,
          customer_email: email,
          summary: desc,
        }),
      })
        .then(function (res) {
          if (!res.ok) throw new Error("bad status");
          return res.json();
        })
        .then(function (data) {
          humanEmail.value = "";
          humanDesc.value = "";
          showHumanForm(false);
          addMessage("bot", data.reply || "Thanks — we'll be in touch.");
        })
        .catch(function () {
          showError("Sorry, something went wrong sending that. Please try again.");
        })
        .finally(function () {
          humanSending = false;
          humanSend.disabled = false;
        });
    });

    // Hint bubble, once per session, only if configured and not dismissed
    // already. Floating mode only — an inline embed is already open and
    // has no launcher for the hint to point at.
    if (!inline && config.hint_text && !safeStorageGet(HINT_KEY)) {
      setTimeout(function () {
        if (opened) return;
        hintEl.hidden = false;
        hintEl.innerHTML =
          "<span>" + escapeHtml(config.hint_text) + '</span><button type="button" aria-label="Dismiss">&times;</button>';
        hintEl.querySelector("button").addEventListener("click", function (e) {
          e.stopPropagation();
          hintEl.hidden = true;
          safeStorageSet(HINT_KEY, "1");
        });
        hintEl.addEventListener("click", openPanel);
        safeStorageSet(HINT_KEY, "1");
      }, 25000);
    }

    // Inline embeds have no launcher to click — they're always "open".
    if (inline) openPanel();

    if (!autoplayStopped) {
      scheduleAutoplay(function () {
        playAutoplayQuestion(0);
      }, 1300);
    }
  }

  function init(config) {
    // A trial past its end date, or a paused/cancelled live store (see
    // StoreConfig.is_access_blocked) — stay completely silent, no
    // launcher bubble at all, rather than show one that just refuses to
    // answer once clicked.
    if (config.disabled) return;
    try {
      build(config);
    } catch (e) {
      /* fail silently: never break the host page */
    }
  }

  function fetchJson(url) {
    return fetch(url).then(function (res) {
      if (!res.ok) throw new Error("bad status");
      return res.json();
    });
  }

  // Loaded in parallel with the store config, never blocking it — a missing
  // or broken canned-answers file should just mean chips fall back to
  // sending live, not that the whole widget fails to load.
  var cannedAnswersPromise = cannedAnswersUrl
    ? fetchJson(cannedAnswersUrl).catch(function () {
        return null;
      })
    : Promise.resolve(null);

  fetchJson(apiBase + "/api/widget-config/" + encodeURIComponent(storeId))
    .then(function (data) {
      return cannedAnswersPromise.then(function (canned) {
        init({
          store_name: data.store_name || DEFAULTS.store_name,
          brand_color: data.brand_color || DEFAULTS.brand_color,
          greeting: data.greeting || DEFAULTS.greeting,
          hint_text: data.hint_text || DEFAULTS.hint_text,
          privacy_policy_url: data.privacy_policy_url || DEFAULTS.privacy_policy_url,
          tillbird_privacy_url: data.tillbird_privacy_url || DEFAULTS.tillbird_privacy_url,
          widget_tagline: data.widget_tagline || DEFAULTS.widget_tagline,
          disabled: !!data.disabled,
          cannedAnswers: canned,
        });
      });
    })
    .catch(function () {
      cannedAnswersPromise.then(function (canned) {
        init({
          store_name: DEFAULTS.store_name,
          brand_color: DEFAULTS.brand_color,
          greeting: DEFAULTS.greeting,
          hint_text: DEFAULTS.hint_text,
          privacy_policy_url: DEFAULTS.privacy_policy_url,
          tillbird_privacy_url: DEFAULTS.tillbird_privacy_url,
          widget_tagline: DEFAULTS.widget_tagline,
          cannedAnswers: canned,
        });
      });
    });
})();
