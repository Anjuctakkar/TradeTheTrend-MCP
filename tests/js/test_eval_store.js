// Run with:  node tests/js/test_eval_store.js
const assert = require("assert");
const EvalStore = require("../../static/eval-store.js");

const memStorage = () => { const m = {}; return { getItem: k => (k in m ? m[k] : null), setItem: (k, v) => { m[k] = String(v); }, raw: m }; };
const memHandles = () => { const m = {}; return { get: async k => m[k], set: async (k, v) => { m[k] = v; }, del: async k => { delete m[k]; }, m }; };
function fakeFile(name, text = "", perm = "granted") {
  const f = { name, text, perm, writes: 0, failWrites: false,
    async getFile() { return { text: async () => f.text }; },
    async createWritable() { if (f.failWrites) { const e = new Error("denied"); e.name = "NotAllowedError"; throw e; }
      let buf = ""; return { write: async s => { buf = s; }, close: async () => { f.text = buf; f.writes++; } }; },
    async queryPermission() { return f.perm; },
    async requestPermission() { if (f.perm === "prompt") f.perm = f.grantOnRequest === false ? "denied" : "granted"; return f.perm; } };
  return f;
}
const fakeWin = (extra = {}) => ({ showOpenFilePicker: async () => [extra.open], showSaveFilePicker: async () => extra.save, ...extra });
const mk = (o = {}) => EvalStore.create({ userKey: "a@x.com", storage: memStorage(), win: {}, handleStore: null, writeDelayMs: 0, ...o });
const item = (s, d = "2026-10-09", extra = {}) => ({ symbol: s, as_of: d, entry: 100, stop_loss: 90, target: 120, score: "4/4", hold_days: 10, ...extra });
let passed = 0; const t = async (name, fn) => { await fn(); passed++; };

(async () => {
  await t("normalise accepts old server entries and rejects bad ones", () => {
    const ok = EvalStore.normalise({ id: "4f5baae2ff", symbol: "rsystems", source: "system", as_of: "2026-10-09", entry: 274.3, stop_loss: 235.62, target: 351.66, score: "4/4", hold_days: 28, logged_at: "2026-10-09T22:16:44", taken: false });
    assert.strictEqual(ok.symbol, "RSYSTEMS"); assert.strictEqual(ok.source, "system"); assert.strictEqual(ok.id, "4f5baae2ff");
    assert.strictEqual(EvalStore.normalise({ symbol: "KOTAKBANK", as_of: "2026-10-09", entry: null, stop_loss: null, target: null }).entry, null);   // watch-only
    for (const bad of [null, "x", { symbol: "bad symbol!", as_of: "2026-10-09" }, { symbol: "TCS", as_of: "yesterday" },
                       { symbol: "TCS", as_of: "2026-10-09", entry: 100, stop_loss: 110, target: 120 },       // stop above entry
                       { symbol: "TCS", as_of: "2026-10-09", entry: 100 }])                                   // levels half given
      assert.strictEqual(EvalStore.normalise(bad), null, JSON.stringify(bad));
    assert.strictEqual(EvalStore.normalise({ symbol: "TCS", as_of: "2026-10-09", taken: "yes" }).taken, false);   // only real true counts
  });

  await t("parseText: wrapped file, bare array (old suggestions_log.json), empty, garbage", () => {
    const wrapped = EvalStore.serialize([EvalStore.normalise(item("TCS"))]);
    assert.strictEqual(EvalStore.parseText(wrapped).entries.length, 1);
    assert.strictEqual(EvalStore.parseText(JSON.stringify([item("TCS"), item("INFY"), { symbol: "!!" }])).skipped, 1);
    assert.deepStrictEqual(EvalStore.parseText("  ").entries, []);
    assert.ok(EvalStore.parseText("{not json").error); assert.ok(EvalStore.parseText('{"a":1}').error);
  });

  await t("add: dedupes by stock+date+source, returns ids, keeps both sources apart", () => {
    const s = mk();
    const r = s.add([item("TCS"), item("INFY"), item("TCS")], "user");
    assert.deepStrictEqual([r.added, r.already], [2, 1]); assert.strictEqual(r.ids.length, 3); assert.strictEqual(r.ids[0], r.ids[2]);
    assert.strictEqual(s.add([item("TCS")], "user").added, 0);
    assert.strictEqual(s.add([item("TCS")], "system").added, 1);                 // same stock suggested by the system is separate
    assert.strictEqual(s.entries().length, 3);
  });

  await t("saved in browser storage and loaded next time; each user has their own list", async () => {
    const storage = memStorage();
    const a = EvalStore.create({ userKey: "a@x.com", storage, win: {}, handleStore: null }); a.add([item("TCS")], "user");
    const b = EvalStore.create({ userKey: "b@x.com", storage, win: {}, handleStore: null }); await b.init();
    assert.strictEqual(b.entries().length, 0);                                    // same browser, other user: empty
    const a2 = EvalStore.create({ userKey: "a@x.com", storage, win: {}, handleStore: null }); await a2.init();
    assert.deepStrictEqual(a2.entries().map(e => e.symbol), ["TCS"]);
    a2.setTaken(a2.entries()[0].id, true);
    const a3 = EvalStore.create({ userKey: "a@x.com", storage, win: {}, handleStore: null }); await a3.init();
    assert.strictEqual(a3.entries()[0].taken, true);
    a3.remove(a3.entries()[0].id);
    const a4 = EvalStore.create({ userKey: "a@x.com", storage, win: {}, handleStore: null }); await a4.init();
    assert.strictEqual(a4.entries().length, 0);
  });

  await t("corrupt or tampered browser data is ignored, not trusted", async () => {
    const storage = memStorage(); storage.setItem("nseEval:v1:a@x.com", "{oops");
    const s = EvalStore.create({ userKey: "a@x.com", storage, win: {}, handleStore: null }); await s.init(); assert.strictEqual(s.entries().length, 0);
    storage.setItem("nseEval:v1:a@x.com", JSON.stringify({ entries: [item("TCS"), { symbol: "<script>", as_of: "2026-10-09" }] }));
    const s2 = EvalStore.create({ userKey: "a@x.com", storage, win: {}, handleStore: null }); await s2.init(); assert.deepStrictEqual(s2.entries().map(e => e.symbol), ["TCS"]);
  });

  await t("storage that refuses writes is reported, and the list still works in memory", () => {
    const s = mk({ storage: { getItem: () => null, setItem: () => { throw new Error("full"); } } });
    s.add([item("TCS")], "user"); assert.strictEqual(s.fileState().storageOk, false); assert.strictEqual(s.entries().length, 1);
  });

  await t("mergeText keeps both sides and ORs 'taken'", () => {
    const s = mk(); s.add([item("TCS")], "user");
    const r = s.mergeText(JSON.stringify([item("TCS", "2026-10-09", { source: "user", taken: true }), item("INFY", "2026-10-09", { source: "user" }), { symbol: "??" }]));
    assert.deepStrictEqual([r.added, r.already, r.skipped], [1, 1, 1]);
    assert.strictEqual(s.entries().find(e => e.symbol === "TCS").taken, true);
    assert.ok(s.mergeText("nope").error);
  });

  await t("listeners fire on changes", () => {
    const s = mk(); let n = 0; s.onChange(() => n++); s.add([item("TCS")], "user"); s.setTaken(s.entries()[0].id, true); s.remove(s.entries()[0].id);
    assert.strictEqual(n, 3);
    s.add([item("TCS")], "user"); const before = n; s.add([item("TCS")], "user"); assert.strictEqual(n, before);   // nothing new: no event
  });

  await t("create a file: every change is written to it", async () => {
    const f = fakeFile("nse-evaluation.json"), handles = memHandles();
    const s = mk({ win: fakeWin({ save: f }), handleStore: handles }); s.add([item("TCS")], "user");
    assert.deepStrictEqual(await s.createFile(), { linked: "nse-evaluation.json" });
    assert.strictEqual(JSON.parse(f.text).entries.length, 1); assert.strictEqual(JSON.parse(f.text).kind, "evaluation-list");
    s.add([item("INFY")], "user"); await s.flush();
    assert.strictEqual(JSON.parse(f.text).entries.length, 2);
    s.remove(s.entries()[0].id); await s.flush(); assert.strictEqual(JSON.parse(f.text).entries.length, 1);   // deletions reach the file too
    assert.strictEqual(s.fileState().dirty, false); assert.ok(handles.m["nseEval:v1:a@x.com"]);
  });

  await t("next visit: the remembered file is loaded (file is the master copy when nothing is unsaved)", async () => {
    const f = fakeFile("list.json", EvalStore.serialize([EvalStore.normalise(item("TCS")), EvalStore.normalise(item("INFY"))]));
    const storage = memStorage(), handles = memHandles(); await handles.set("nseEval:v1:a@x.com", f);
    const s = EvalStore.create({ userKey: "a@x.com", storage, win: fakeWin(), handleStore: handles, writeDelayMs: 0 });
    await s.init();
    assert.deepStrictEqual(s.entries().map(e => e.symbol).sort(), ["INFY", "TCS"]);
    assert.strictEqual(s.fileState().permission, "granted");
  });

  await t("permission lost: changes stay in the browser, then merge into the file on reconnect", async () => {
    const f = fakeFile("list.json", EvalStore.serialize([EvalStore.normalise(item("TCS"))]), "prompt");
    const storage = memStorage(), handles = memHandles(); await handles.set("nseEval:v1:a@x.com", f);
    const s = EvalStore.create({ userKey: "a@x.com", storage, win: fakeWin(), handleStore: handles, writeDelayMs: 0 });
    await s.init();
    assert.strictEqual(s.fileState().permission, "prompt"); assert.strictEqual(s.entries().length, 0);   // not read without permission
    s.add([item("WIPRO")], "user");                                                                       // work done while disconnected
    assert.strictEqual(s.fileState().dirty, true); assert.strictEqual(JSON.parse(f.text).entries.length, 1);   // file untouched
    assert.deepStrictEqual(await s.reconnect(), { linked: "list.json" });
    assert.deepStrictEqual(s.entries().map(e => e.symbol).sort(), ["TCS", "WIPRO"]);                      // nothing lost on either side
    assert.deepStrictEqual(JSON.parse(f.text).entries.map(e => e.symbol).sort(), ["TCS", "WIPRO"]);
    assert.strictEqual(s.fileState().dirty, false);
  });

  await t("reconnect denied: nothing is read or written", async () => {
    const f = fakeFile("list.json", "", "prompt"); f.grantOnRequest = false;
    const handles = memHandles(); await handles.set("nseEval:v1:a@x.com", f);
    const s = EvalStore.create({ userKey: "a@x.com", storage: memStorage(), win: fakeWin(), handleStore: handles, writeDelayMs: 0 }); await s.init();
    assert.ok((await s.reconnect()).error); assert.strictEqual(f.writes, 0);
  });

  await t("a linked file that is not an evaluation list is never overwritten", async () => {
    const f = fakeFile("notes.json", '{"my":"other data"}'); const handles = memHandles(); await handles.set("nseEval:v1:a@x.com", f);
    const s = EvalStore.create({ userKey: "a@x.com", storage: memStorage(), win: fakeWin(), handleStore: handles, writeDelayMs: 0 }); await s.init();
    assert.ok(s.fileState().error); assert.strictEqual(f.text, '{"my":"other data"}'); assert.strictEqual(f.writes, 0);
    const o = await mk({ win: fakeWin({ open: fakeFile("bad.json", "not json") }), handleStore: memHandles() }).openFile();
    assert.ok(o.error);
  });

  await t("open my file: merges with the current list and links it", async () => {
    const f = fakeFile("mine.json", JSON.stringify([item("TCS"), item("HDFCBANK")]));
    const s = mk({ win: fakeWin({ open: f }), handleStore: memHandles() }); s.add([item("INFY")], "user");
    const r = await s.openFile();
    assert.deepStrictEqual([r.added, r.already, r.linked], [2, 0, "mine.json"]);
    assert.deepStrictEqual(JSON.parse(f.text).entries.map(e => e.symbol).sort(), ["HDFCBANK", "INFY", "TCS"]);      // old-format file upgraded, nothing lost
  });

  await t("a failing write is reported and retried as 'unsaved', not lost", async () => {
    const f = fakeFile("list.json"); const s = mk({ win: fakeWin({ save: f }), handleStore: memHandles() }); await s.createFile();
    f.failWrites = true; s.add([item("TCS")], "user"); await s.flush();
    assert.ok(s.fileState().error); assert.strictEqual(s.fileState().dirty, true); assert.strictEqual(s.fileState().permission, "prompt");
    assert.strictEqual(s.entries().length, 1);
  });

  await t("unlink forgets the file but keeps the list", async () => {
    const f = fakeFile("list.json"), handles = memHandles(); const s = mk({ win: fakeWin({ save: f }), handleStore: handles });
    s.add([item("TCS")], "user"); await s.createFile(); await s.unlink();
    assert.strictEqual(s.fileState().linked, false); assert.strictEqual(handles.m["nseEval:v1:a@x.com"], undefined); assert.strictEqual(s.entries().length, 1);
  });

  await t("browsers without the file picker still work (browser storage + download/open)", () => {
    const s = mk(); assert.strictEqual(s.fileState().supported, false);
    return s.createFile().then(r => assert.ok(r.error));
  });

  console.log(`eval-store tests passed (${passed})`);
})().catch(e => { console.error(e); process.exit(1); });
