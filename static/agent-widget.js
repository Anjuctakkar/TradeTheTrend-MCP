/*
 * NSE assistant chat widget. Add to any page:
 *   <script src="https://YOUR-AGENT-HOST/agent-widget.js" defer></script>
 * Optional attributes on the script tag:
 *   data-api="https://YOUR-AGENT-HOST"   (defaults to the script's own origin)
 *   data-title="Market assistant"   data-accent="#2f6fed"
 * Runs in a shadow root, so your site's CSS can't break it and it won't leak styles.
 */
(function () {
  var script = document.currentScript;
  var API = (script && script.dataset.api) || (script ? new URL(script.src).origin : "");
  var TITLE = (script && script.dataset.title) || "Market assistant";
  var ACCENT = (script && script.dataset.accent) || "#2f6fed";
  var SETTINGS = !!(script && script.dataset.settings === "true");
  var SETUP = { no_key: 1, invalid_key: 1, billing: 1, model: 1, permission: 1 };
  var history = [], busy = false;

  var host = document.createElement("div");
  host.style.cssText = "position:fixed;right:20px;bottom:20px;z-index:2147483000;";
  var root = host.attachShadow({ mode: "open" });
  root.innerHTML =
    "<style>" +
    ":host{all:initial} *{box-sizing:border-box;font-family:system-ui,-apple-system,'Segoe UI',Roboto,sans-serif}" +
    ".fab{width:54px;height:54px;border-radius:50%;border:none;background:" + ACCENT + ";color:#fff;font-size:22px;cursor:pointer;box-shadow:0 4px 14px rgba(0,0,0,.28)}" +
    ".panel{display:none;flex-direction:column;width:380px;max-width:calc(100vw - 24px);height:560px;max-height:calc(100vh - 96px);background:#fff;color:#16202a;border-radius:12px;box-shadow:0 10px 36px rgba(0,0,0,.3);overflow:hidden;margin-bottom:12px}" +
    ".open .panel{display:flex}" +
    ".head{display:flex;align-items:center;justify-content:space-between;padding:12px 14px;background:" + ACCENT + ";color:#fff;font-weight:600;font-size:15px}" +
    ".head button{background:none;border:none;color:#fff;font-size:20px;cursor:pointer;line-height:1}" +
    ".head .tools{display:flex;gap:10px;align-items:center}" +
    ".log{flex:1;overflow-y:auto;padding:14px;background:#f5f7fa;display:flex;flex-direction:column;gap:10px}" +
    ".m{max-width:88%;padding:9px 12px;border-radius:10px;font-size:14px;line-height:1.45;word-wrap:break-word;white-space:pre-wrap}" +
    ".m.user{align-self:flex-end;background:" + ACCENT + ";color:#fff;border-bottom-right-radius:3px}" +
    ".m.bot{align-self:flex-start;background:#fff;border:1px solid #dfe5ec;border-bottom-left-radius:3px}" +
    ".m.err{align-self:flex-start;background:#fdecec;border:1px solid #f3c2c2;color:#8a1f1f}" +
    ".m button.fix{display:block;margin-top:8px;border:1px solid #c98a8a;background:#fff;color:#8a1f1f;border-radius:6px;padding:5px 11px;font-size:13px;cursor:pointer}" +
    ".m ul{margin:4px 0 4px 18px;padding:0;white-space:normal} .m li{margin:2px 0}" +
    ".chips{display:flex;flex-wrap:wrap;gap:6px}" +
    ".chip{background:#fff;border:1px solid #cfd8e3;border-radius:999px;padding:5px 11px;font-size:13px;cursor:pointer;color:#16202a}" +
    ".chip:hover{border-color:" + ACCENT + "}" +
    ".typing{font-size:13px;color:#5b6b7b;align-self:flex-start}" +
    ".used{margin-top:7px;font-size:11px;color:#5b6b7b;white-space:normal}" +
    ".form{display:flex;gap:8px;padding:10px;border-top:1px solid #dfe5ec;background:#fff}" +
    ".form input{flex:1;border:1px solid #cfd8e3;border-radius:8px;padding:9px 10px;font-size:14px;outline:none}" +
    ".form input:focus{border-color:" + ACCENT + "}" +
    ".form button{border:none;background:" + ACCENT + ";color:#fff;border-radius:8px;padding:0 14px;font-size:14px;cursor:pointer}" +
    ".form button:disabled{opacity:.5;cursor:not-allowed}" +
    ".note{padding:6px 12px 9px;font-size:11px;color:#5b6b7b;background:#fff;line-height:1.35}" +
    "button:focus-visible,input:focus-visible{outline:2px solid " + ACCENT + ";outline-offset:2px}" +
    "</style>" +
    "<div class='wrap'><div class='panel' role='dialog' aria-label='" + TITLE + "'>" +
    "<div class='head'><span>" + TITLE + "</span><span class='tools'>" + (SETTINGS ? "<button class='gear' aria-label='Settings' title='API key and model settings'>&#9881;</button>" : "") + "<button class='x' aria-label='Close'>&times;</button></span></div>" +
    "<div class='log' aria-live='polite'></div>" +
    "<form class='form'><input type='text' maxlength='2000' placeholder='Ask about a stock or a sector' aria-label='Message'><button type='submit'>Send</button></form>" +
    "<div class='note'>Information only, not investment advice. Data is from NSE and may be delayed. Signals come from simple fixed rules and can be wrong.</div>" +
    "</div><button class='fab' aria-label='Open " + TITLE + "'>&#128172;</button></div>";
  var wrap = root.querySelector(".wrap");
  var log = root.querySelector(".log"), input = root.querySelector("input"), send = root.querySelector(".form button");

  function el(tag, cls, text) { var e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }

  // Tiny safe renderer: **bold**, "- " bullets, newlines. Everything goes through textContent.
  function inline(parent, text) {
    text.split(/(\*\*[^*]+\*\*)/g).forEach(function (part) {
      if (/^\*\*[^*]+\*\*$/.test(part)) parent.appendChild(el("strong", null, part.slice(2, -2)));
      else if (part) parent.appendChild(document.createTextNode(part));
    });
  }
  function render(target, text) {
    var ul = null;
    text.split("\n").forEach(function (line) {
      var b = line.match(/^\s*[-*]\s+(.*)$/);
      if (b) { if (!ul) { ul = el("ul"); target.appendChild(ul); } var li = el("li"); inline(li, b[1]); ul.appendChild(li); }
      else { ul = null; inline(target, line); target.appendChild(document.createTextNode("\n")); }
    });
  }
  function add(role, text) {
    var m = el("div", "m " + role);
    if (role === "bot") render(m, text.trim()); else m.textContent = text;
    log.appendChild(m); log.scrollTop = log.scrollHeight; return m;
  }

  function welcome() {
    add("bot", "Hi! I can look up NSE stocks, show the latest rule-based setups, backtest them, show today's top movers, and read sector momentum, sector rotation and stocks to review from the strong sectors (load sectors in the Sectors tab first). What would you like to see?");
    var chips = el("div", "chips");
    ["Analyse TCS", "Which sectors are improving?", "Stocks to review from strong sectors", "Sector rotation this week", "Support and resistance for TCS", "Backtest INFY", "Today's top gainers and losers"].forEach(function (q) {
      var c = el("button", "chip", q); c.type = "button"; c.onclick = function () { chips.remove(); ask(q); }; chips.appendChild(c);
    });
    log.appendChild(chips);
  }

  // Which data the answer came from, in plain words, so you can see (for example) that the sector tools were used.
  var TOOL_LABELS = { search_symbol: "symbol search", analyze_stock: "stock analysis", backtest_stock: "backtest", sr_levels: "support/resistance",
    sector_momentum: "sector momentum", sector_rotation: "sector rotation", sector_picks: "sector picks", suggest_stocks: "stock suggestions",
    live_quote: "live quote", market_movers: "market movers", my_watchlist: "your evaluation list" };
  function showUsed(m, used) {
    var seen = {}, names = [];
    (used || []).forEach(function (t) { var n = TOOL_LABELS[t] || t; if (!seen[n]) { seen[n] = 1; names.push(n); } });
    if (names.length) m.appendChild(el("div", "used", "Used: " + names.join(", ")));
  }

  async function ask(text) {
    if (busy || !text.trim()) return;
    busy = true; send.disabled = true; input.value = "";
    add("user", text); history.push({ role: "user", content: text });
    var typing = el("div", "typing", "Checking NSE data…"); log.appendChild(typing); log.scrollTop = log.scrollHeight;
    try {
      var res = await fetch(API + "/api/agent/chat", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(Object.assign({ messages: history.slice(-20) }, (function () {
          // The page may share the user's own evaluation list (kept on their computer) so the assistant can discuss it.
          try { var c = typeof window.assistantContext === "function" ? window.assistantContext() : null;
            return c && Array.isArray(c.watchlist) ? { watchlist: c.watchlist.slice(0, 50) } : {}; } catch (e) { return {}; }
        })())) });
      var data = await res.json().catch(function () { return {}; });
      if (!res.ok) { var err = new Error(typeof data.detail === "string" ? data.detail : "Something went wrong. Please try again."); err.code = data.code; throw err; }
      history.push({ role: "assistant", content: data.reply }); showUsed(add("bot", data.reply), data.tools_used);
    } catch (e) {
      history.pop();
      var m = add("err", e.message === "Failed to fetch" ? "Couldn't reach the assistant. Check your connection and try again." : e.message);
      if (SETTINGS && e.code && SETUP[e.code]) {
        var b = el("button", "fix", "Open settings"); b.type = "button";
        b.onclick = function () { toggle(false); document.dispatchEvent(new CustomEvent("assistant:open-settings")); };
        m.appendChild(b);
      }
    } finally { typing.remove(); busy = false; send.disabled = false; input.focus(); }
  }

  var opened = false;
  function toggle(open) {
    wrap.classList.toggle("open", open);
    root.querySelector(".fab").style.display = open ? "none" : "block";
    if (open && !opened) { opened = true; welcome(); }
    if (open) input.focus();
  }
  root.querySelector(".fab").onclick = function () { toggle(true); };
  root.querySelector(".x").onclick = function () { toggle(false); };
  var gear = root.querySelector(".gear");
  if (gear) gear.onclick = function () { toggle(false); document.dispatchEvent(new CustomEvent("assistant:open-settings")); };
  root.querySelector(".form").onsubmit = function (e) { e.preventDefault(); ask(input.value); };
  document.addEventListener("keydown", function (e) { if (e.key === "Escape") toggle(false); });
  document.body.appendChild(host);
})();
