// Page-level test: loads the real static/index.html in jsdom with the network stubbed.
// Needs jsdom:  npm install jsdom   (the other tests don't need it). Run:  node tests/js/test_page.js
const fs = require("fs"), path = require("path"), assert = require("assert");
let JSDOM; try { ({ JSDOM } = require("jsdom")); } catch (e) { try { ({ JSDOM } = require("/tmp/jt/node_modules/jsdom")); } catch (e2) { console.log("skipped: jsdom not installed"); process.exit(0); } }

const root = path.join(__dirname, "..", "..", "static");
const storeSrc = fs.readFileSync(path.join(root, "eval-store.js"), "utf8");
const html = fs.readFileSync(path.join(root, "index.html"), "utf8").replace(/<script src="[^"]*"[^>]*><\/script>/g, "");   // no CDN/widget downloads

function boot(storageDump, user = { id: 1, email: "a@x.com", role: "user" }, extraWin = {}) {
  const calls = [];
  const dom = new JSDOM(html, { url: "http://localhost:8000/", runScripts: "dangerously", pretendToBeVisual: true, beforeParse(win) {
    if (storageDump) Object.entries(storageDump).forEach(([k, v]) => win.localStorage.setItem(k, v));
    win.eval(storeSrc);
    win.Chart = function () { this.destroy = () => {}; };
    Object.assign(win, extraWin);
    win.fetch = async (url, opts = {}) => {
      calls.push({ url: String(url), method: opts.method || "GET", body: opts.body ? JSON.parse(opts.body) : null });
      let data = {};
      if (String(url).startsWith("/api/auth/status")) data = { auth_enabled: true, user };
      else if (String(url).startsWith("/api/track/evaluate")) data = { entries: JSON.parse(opts.body).entries.map(e => ({ ...e, status: "target", exit_price: e.target, return_pct: 19.8, exit_date: "2026-10-20", bars: 7 })) };
      else if (String(url).startsWith("/api/tools")) data = { tools: [], url: "x" };
      return { ok: true, status: 200, json: async () => data, clone() { return this; } };
    };
  } });
  return { dom, win: dom.window, calls };
}
const tick = ms => new Promise(r => setTimeout(r, ms));
const item = (s, d = "2026-10-09") => ({ symbol: s, as_of: d, entry: 100, stop_loss: 90, target: 120, score: "4/4", hold_days: 10 });
let passed = 0; const t = async (n, f) => { await f(); passed++; };

(async () => {
  let { win, calls } = boot();
  await win.eval("evalReady");
  await t("empty list: the page says where it is stored", () => {
    assert.match(win.document.getElementById("evalStorageBox").textContent, /Saved in this browser: 0 stocks/);
    assert.match(win.document.getElementById("trResults").textContent, /No stocks yet/);
  });

  await t("adding stocks draws the table and saves on this computer (no server call)", () => {
    win.eval(`evalStore.add([${JSON.stringify(item("TCS"))}, ${JSON.stringify(item("INFY"))}], "user")`);
    const rows = [...win.document.querySelectorAll("#trResults tbody tr")].map(r => r.textContent);
    assert.ok(rows.some(r => r.includes("TCS")) && rows.some(r => r.includes("INFY")), rows.join("|"));
    const saved = JSON.parse(win.localStorage.getItem("nseEval:v1:a@x.com"));
    assert.strictEqual(saved.entries.length, 2);
    assert.ok(!calls.some(c => c.url.startsWith("/api/track") && c.url !== "/api/auth/status"), "no list data sent to the server");
  });

  await t("Refresh outcomes sends the list to the stateless endpoint and shows results", async () => {
    win.document.getElementById("trRefresh").click(); await tick(60);
    const c = calls.filter(x => x.url === "/api/track/evaluate"); assert.strictEqual(c.length, 1);
    assert.deepStrictEqual(c[0].body.entries.map(e => e.symbol).sort(), ["INFY", "TCS"]);
    assert.ok(c[0].body.entries.every(e => e.entry === 100 && e.stop_loss === 90 && e.target === 120));
    assert.match(win.document.getElementById("trResults").textContent, /Target hit/);
    assert.match(win.document.getElementById("trStatus").textContent, /Updated/);
  });

  await t("'Took it' and delete are saved locally and redraw", () => {
    const box = win.document.querySelector(".evTaken"); box.checked = true; box.dispatchEvent(new win.Event("change"));
    assert.strictEqual(JSON.parse(win.localStorage.getItem("nseEval:v1:a@x.com")).entries.filter(e => e.taken).length, 1);
    win.document.querySelector("button.rm").click();
    assert.strictEqual(JSON.parse(win.localStorage.getItem("nseEval:v1:a@x.com")).entries.length, 1);
    assert.strictEqual(win.document.querySelectorAll("#trResults .evSel").length, 1);
    assert.ok(!calls.some(c => /\/api\/track\/[^e]/.test(c.url)), "no per-entry server calls");
  });

  await t("the assistant is handed the same list (newest 50, safe fields only)", () => {
    const ctx = win.assistantContext(); assert.strictEqual(ctx.watchlist.length, 1);
    assert.deepStrictEqual(Object.keys(ctx.watchlist[0]).sort(), ["as_of", "entry", "hold_days", "source", "stop_loss", "symbol", "taken", "target"]);
  });

  await t("close the page and come back: the list is loaded again", async () => {
    const dump = {}; for (let i = 0; i < win.localStorage.length; i++) { const k = win.localStorage.key(i); dump[k] = win.localStorage.getItem(k); }
    win.close();
    const b = boot(dump); await b.win.eval("evalReady");
    assert.match(b.win.document.getElementById("trResults").textContent, /(TCS|INFY)/);
    assert.match(b.win.document.getElementById("evalStorageBox").textContent, /Saved in this browser: 1 stock\./);
    // a different signed-in user in the same browser starts empty
    const other = boot(dump, { id: 2, email: "b@x.com", role: "user" }); await other.win.eval("evalReady");
    assert.match(other.win.document.getElementById("trResults").textContent, /No stocks yet/);
    other.win.close(); b.win.close();
  });

  await t("suggestions are saved to the list locally (as System picks)", async () => {
    const b = boot(null);
    b.win.fetch = ((orig) => async (url, o) => {
      if (String(url) === "/api/recommend") return { ok: true, status: 200, json: async () => ({ scanned: 3, longs_found: 1, note: "", picks: [{ symbol: "AAA", as_of: "2026-10-09", score: "4/4", rsi: 50, correlation: null, entry: 100, stop_loss: 90, target: 120, hold_days: 12, hold: { status: "proposed", days: 12, reasoning: [] }, week52: { status: "no_data" }, quantity: 10, invested: 1000 }] }), clone() { return this; } };
      return orig(url, o);
    })(b.win.fetch);
    await b.win.eval("evalReady");
    b.win.document.querySelector("#recPanel .recBtn").click(); await tick(120);
    const saved = JSON.parse(b.win.localStorage.getItem("nseEval:v1:a@x.com")).entries;
    assert.deepStrictEqual(saved.map(e => [e.symbol, e.source, e.hold_days]), [["AAA", "system", 12]]);
    assert.match(b.win.document.getElementById("recResults").textContent, /not on the server/);
    b.win.close();
  });

  await t("linking a file from the page: Create a file, then changes are written to it", async () => {
    const file = { name: "nse-evaluation.json", text: "", async createWritable() { let buf = ""; return { write: async s => { buf = s; }, close: async () => { file.text = buf; } }; }, async getFile() { return { text: async () => file.text }; } };
    const b = boot(null, undefined, { showSaveFilePicker: async () => file, showOpenFilePicker: async () => [file] });
    await b.win.eval("evalReady");
    b.win.eval(`evalStore.add([${JSON.stringify(item("TCS"))}], "user")`);
    b.win.document.getElementById("evCreate").click(); await tick(60);
    assert.match(b.win.document.getElementById("evalStorageBox").textContent, /Linked to nse-evaluation\.json/);
    assert.strictEqual(JSON.parse(file.text).entries.length, 1);
    b.win.eval(`evalStore.add([${JSON.stringify(item("INFY"))}], "user")`); await tick(400);
    assert.strictEqual(JSON.parse(file.text).entries.length, 2);
    b.win.close();
  });

  win.close();
  console.log(`page tests passed (${passed})`);
})().catch(e => { console.error(e); process.exit(1); });
