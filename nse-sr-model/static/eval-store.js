/*
 * The Evaluation list (stocks you ticked, and stocks "Suggest stocks" found) lives on YOUR computer,
 * not on the server.
 *
 *   1. Browser storage: saved automatically on every change and loaded every time you open the app.
 *      It is kept separately for each signed-in user. The browser can clear it, so also do (2) or (3).
 *   2. A JSON file you choose (Chrome and Edge): link it once, and every change is saved to it and it is
 *      loaded again each time you use the app (the browser asks permission once per visit).
 *   3. "Download a copy" and "Open my file": work in every browser.
 *
 * File format: { app, kind: "evaluation-list", version: 1, saved_at, entries: [ ... ] }. A plain array of
 * entries (the old suggestions_log.json) is accepted too.
 *
 * This file has no dependencies and also runs under Node (the tests do that).
 */
(function (root) {
  "use strict";

  var APP = "nse-dashboard", KIND = "evaluation-list", VERSION = 1, MAX_ENTRIES = 2000;
  var JSON_TYPES = { description: "Evaluation list (JSON)", accept: { "application/json": [".json"] } };

  // ------------------------------------------------------------------ entries
  function num(v) {
    if (v === null || v === undefined || v === "" || typeof v === "boolean") return null;
    var n = Number(v);
    return isFinite(n) ? n : null;
  }

  function makeId() {
    try {
      if (root.crypto && root.crypto.randomUUID) return root.crypto.randomUUID().replace(/-/g, "").slice(0, 12);
    } catch (e) { /* fall through */ }
    return Math.random().toString(16).slice(2, 14).padEnd(12, "0");
  }

  /** A clean entry, or null if the input isn't a valid one. Same rules the server applies. */
  function normalise(raw) {
    if (!raw || typeof raw !== "object") return null;
    var symbol = String(raw.symbol || "").trim().toUpperCase();
    if (!/^[A-Z0-9&_-]{1,20}$/.test(symbol)) return null;
    var asOf = String(raw.as_of || "").slice(0, 10);
    if (!/^\d{4}-\d{2}-\d{2}$/.test(asOf) || isNaN(Date.parse(asOf))) return null;
    var entry = num(raw.entry), stop = num(raw.stop_loss), target = num(raw.target);
    if (entry !== null || stop !== null || target !== null) {          // levels are all-or-nothing
      if (entry === null || stop === null || target === null || !(0 < stop && stop < entry && entry < target)) return null;
    }
    var hold = num(raw.hold_days);
    hold = hold === null ? null : Math.max(1, Math.min(120, Math.round(hold)));
    return {
      id: typeof raw.id === "string" && /^[A-Za-z0-9_-]{4,40}$/.test(raw.id) ? raw.id : makeId(),
      symbol: symbol,
      source: raw.source === "system" ? "system" : "user",
      as_of: asOf,
      entry: entry, stop_loss: stop, target: target,
      score: typeof raw.score === "string" ? raw.score.slice(0, 8) : null,
      hold_days: hold,
      logged_at: typeof raw.logged_at === "string" ? raw.logged_at.slice(0, 25) : new Date().toISOString().slice(0, 19),
      taken: raw.taken === true,
      model: raw.model === "sr" ? "sr" : "trend",
      setup: typeof raw.setup === "string" ? raw.setup.slice(0, 24) : null
    };
  }

  // Same stock + date + source can hold one entry per rule set. Trend keys stay as they were, so old lists merge as before.
  function keyOf(e) { return e.symbol + "|" + e.as_of + "|" + e.source + (e.model === "sr" ? "|sr" : ""); }
  function copy(e) { return Object.assign({}, e); }

  /** Union by symbol + date + source. Existing entries win; "taken" is kept if either side has it. */
  function merge(current, incoming) {
    var out = current.map(copy), byKey = {}, ids = {}, added = 0, already = 0, skipped = 0, resultIds = [];
    out.forEach(function (e) { byKey[keyOf(e)] = e; ids[e.id] = true; });
    (incoming || []).forEach(function (raw) {
      var e = normalise(raw);
      if (!e) { skipped++; return; }
      var k = keyOf(e);
      if (byKey[k]) {
        already++;
        if (e.taken) byKey[k].taken = true;
        resultIds.push(byKey[k].id);
        return;
      }
      if (out.length >= MAX_ENTRIES) { skipped++; return; }
      if (ids[e.id]) e.id = makeId();
      ids[e.id] = true; byKey[k] = e; out.push(e); added++; resultIds.push(e.id);
    });
    return { entries: out, added: added, already: already, skipped: skipped, ids: resultIds };
  }

  function serialize(entries) {
    return JSON.stringify({
      app: APP, kind: KIND, version: VERSION, saved_at: new Date().toISOString(),
      note: "Your NSE dashboard evaluation list. In the app: Evaluation tab > Open my file.",
      entries: entries
    }, null, 1);
  }

  /** { entries, skipped } or { error }. Accepts the wrapped format or a plain array (old suggestions_log.json). */
  function parseText(text) {
    if (!String(text || "").trim()) return { entries: [], skipped: 0 };
    var data;
    try { data = JSON.parse(text); } catch (e) { return { error: "That file isn't valid JSON." }; }
    var list = Array.isArray(data) ? data : (data && Array.isArray(data.entries) ? data.entries : null);
    if (!list) return { error: "That file doesn't look like an evaluation list." };
    var entries = [], skipped = 0;
    list.forEach(function (raw) { var e = normalise(raw); if (e) entries.push(e); else skipped++; });
    return { entries: entries, skipped: skipped };
  }

  // ------------------------------------------------------------------ remembering the linked file between visits
  function idbHandleStore(win) {
    if (!win || !win.indexedDB) return null;
    function open() {
      return new Promise(function (res, rej) {
        var r = win.indexedDB.open("nse-dashboard", 1);
        r.onupgradeneeded = function () { r.result.createObjectStore("handles"); };
        r.onsuccess = function () { res(r.result); };
        r.onerror = function () { rej(r.error); };
      });
    }
    function run(mode, fn) {
      return open().then(function (db) {
        return new Promise(function (res, rej) {
          var tx = db.transaction("handles", mode), req = fn(tx.objectStore("handles"));
          tx.oncomplete = function () { db.close(); res(req && req.result); };
          tx.onerror = function () { db.close(); rej(tx.error); };
        });
      });
    }
    return {
      get: function (k) { return run("readonly", function (s) { return s.get(k); }); },
      set: function (k, v) { return run("readwrite", function (s) { return s.put(v, k); }); },
      del: function (k) { return run("readwrite", function (s) { return s.delete(k); }); }
    };
  }

  // ------------------------------------------------------------------ the store
  /**
   * opts.userKey      who is signed in (the list is kept separately per user)
   * opts.storage      defaults to localStorage
   * opts.win          defaults to window (needed for the file picker and IndexedDB)
   * opts.handleStore  where the linked file's handle is remembered; defaults to IndexedDB
   * opts.writeDelayMs wait before writing the linked file after a change (default 250)
   */
  function create(opts) {
    opts = opts || {};
    var win = opts.win || (typeof window !== "undefined" ? window : root);
    var storage = opts.storage || (function () { try { return root.localStorage; } catch (e) { return null; } })();
    var handleStore = opts.handleStore !== undefined ? opts.handleStore : idbHandleStore(win);
    var delay = opts.writeDelayMs === undefined ? 250 : opts.writeDelayMs;
    var KEY = "nseEval:v1:" + (opts.userKey || "local");

    var entries = [], listeners = [], storageOk = true, timer = null, chain = Promise.resolve();
    var meta = { saved_at: null, dirty: false };           // dirty: changes not yet written to the linked file
    var file = { handle: null, name: null, permission: "none", error: null, lastWrite: null };

    function supportsFiles() { return !!(win && win.showOpenFilePicker && win.showSaveFilePicker); }
    function emit() { listeners.slice().forEach(function (fn) { try { fn(); } catch (e) { /* a bad listener must not stop saving */ } }); }

    function loadLocal() {
      try {
        var raw = storage && storage.getItem(KEY);
        if (!raw) return;
        var d = JSON.parse(raw);
        entries = (d.entries || []).map(normalise).filter(Boolean);
        meta.saved_at = d.saved_at || null;
        meta.dirty = d.dirty === true;
      } catch (e) { entries = []; }
    }

    function saveLocal() {
      meta.saved_at = new Date().toISOString();
      try {
        if (storage) storage.setItem(KEY, JSON.stringify({ entries: entries, saved_at: meta.saved_at, dirty: meta.dirty }));
        storageOk = !!storage;
      } catch (e) { storageOk = false; }
    }

    async function writeNow() {
      var h = file.handle;
      if (!h) return;
      try {
        var w = await h.createWritable();
        await w.write(serialize(entries));
        await w.close();
        meta.dirty = false; file.error = null; file.lastWrite = new Date(); file.permission = "granted";
      } catch (e) {
        meta.dirty = true;
        file.error = "Couldn't save to " + (file.name || "the linked file") + ". Your list is still kept in this browser.";
        if (e && e.name === "NotAllowedError") file.permission = "prompt";
      }
      saveLocal();
      emit();
    }

    function queueWrite(immediate) {
      clearTimeout(timer);
      var run = function () { chain = chain.then(writeNow); return chain; };
      if (immediate || !delay) return run();
      timer = setTimeout(run, delay);
      return Promise.resolve();
    }

    /** Called after every change to the list. */
    function changed() {
      meta.dirty = !!file.handle;
      saveLocal();
      if (file.handle && file.permission === "granted") queueWrite(false);
      emit();
    }

    async function link(handle, permission) {
      file.handle = handle; file.name = handle.name; file.permission = permission; file.error = null;
      try { if (handleStore) await handleStore.set(KEY, handle); } catch (e) { /* works this session even if it can't be remembered */ }
    }

    /** Read the linked file and combine it with the browser copy. Never touches a file that isn't an evaluation list. */
    async function syncFromFile() {
      var text;
      try { text = await (await file.handle.getFile()).text(); }
      catch (e) { file.error = "Couldn't read " + (file.name || "the linked file") + "."; return; }
      var parsed = parseText(text);
      if (parsed.error) {
        file.error = (file.name || "The linked file") + " isn't a valid evaluation list, so it was left unchanged.";
        return;
      }
      if (parsed.entries.length > 0) {
        if (meta.dirty) entries = merge(entries, parsed.entries).entries;   // changes made while unlinked: keep both sides
        else entries = parsed.entries;                                        // otherwise the file is the master copy
      }
      meta.dirty = true;
      await queueWrite(true);                                                 // write the result back (also tidies the file)
    }

    var api = {
      onChange: function (fn) { listeners.push(fn); },
      entries: function () { return entries.map(copy); },

      async init() {
        loadLocal();
        if (handleStore && supportsFiles()) {
          try {
            var h = await handleStore.get(KEY);
            if (h) {
              file.handle = h; file.name = h.name;
              file.permission = await h.queryPermission({ mode: "readwrite" });
              if (file.permission === "granted") await syncFromFile();
            }
          } catch (e) { /* no remembered file */ }
        }
        emit();
        return api;
      },

      /** items: [{symbol, as_of, entry?, stop_loss?, target?, score?, hold_days?}]. Returns {added, already, skipped, ids}. */
      add: function (items, source) {
        var now = new Date().toISOString().slice(0, 19);
        var m = merge(entries, (items || []).map(function (i) {
          return Object.assign({}, i, { source: source === "system" ? "system" : "user", id: undefined, taken: false, logged_at: now });
        }));
        if (m.added) { entries = m.entries; changed(); }
        return { added: m.added, already: m.already, skipped: m.skipped, ids: m.ids };
      },
      setTaken: function (id, value) {
        var e = entries.filter(function (x) { return x.id === id; })[0];
        if (e && e.taken !== !!value) { e.taken = !!value; changed(); }
      },
      remove: function (id) {
        var n = entries.length;
        entries = entries.filter(function (x) { return x.id !== id; });
        if (entries.length !== n) changed();
      },

      /** Merge text from a file into the list. */
      mergeText: function (text) {
        var parsed = parseText(text);
        if (parsed.error) return parsed;
        var m = merge(entries, parsed.entries);
        if (m.added) { entries = m.entries; changed(); }
        return { added: m.added, already: m.already, skipped: parsed.skipped + m.skipped };
      },
      exportText: function () { return serialize(entries); },

      fileState: function () {
        return { supported: supportsFiles(), linked: !!file.handle, name: file.name, permission: file.permission,
                 error: file.error, lastWrite: file.lastWrite, dirty: meta.dirty, storageOk: storageOk };
      },

      /** Pick a file, merge it into the list and (where the browser allows) keep it linked. */
      async openFile() {
        try {
          var text, handle = null;
          if (supportsFiles()) {
            handle = (await win.showOpenFilePicker({ multiple: false, types: [JSON_TYPES] }))[0];
            if ((await handle.requestPermission({ mode: "readwrite" })) !== "granted") {
              return { error: "Permission to edit that file wasn't given. You can still use Download a copy." };
            }
            text = await (await handle.getFile()).text();
          } else {
            text = await pickTextWithInput(win);
            if (text === null) return { cancelled: true };
          }
          var parsed = parseText(text);
          if (parsed.error) return parsed;
          var m = merge(entries, parsed.entries);
          entries = m.entries;
          if (handle) { await link(handle, "granted"); meta.dirty = true; saveLocal(); await queueWrite(true); }
          else if (m.added) changed();
          emit();
          return { added: m.added, already: m.already, skipped: parsed.skipped + m.skipped, linked: handle ? handle.name : null };
        } catch (e) {
          if (e && e.name === "AbortError") return { cancelled: true };
          return { error: "Couldn't open that file." };
        }
      },

      /** Make a new file, save the current list into it and keep it linked (Chrome and Edge). */
      async createFile() {
        if (!supportsFiles()) return { error: "This browser can't keep a file linked. Use Download a copy." };
        try {
          var handle = await win.showSaveFilePicker({ suggestedName: "nse-evaluation.json", types: [JSON_TYPES] });
          await link(handle, "granted");
          meta.dirty = true;
          await queueWrite(true);
          return { linked: handle.name };
        } catch (e) {
          if (e && e.name === "AbortError") return { cancelled: true };
          return { error: "Couldn't create that file." };
        }
      },

      /** After the browser asks again for permission in a new visit. */
      async reconnect() {
        if (!file.handle) return { error: "No file is linked." };
        try {
          file.permission = await file.handle.requestPermission({ mode: "readwrite" });
          if (file.permission !== "granted") { emit(); return { error: "Permission wasn't given." }; }
          await syncFromFile();
          emit();
          return { linked: file.name };
        } catch (e) { return { error: "Couldn't reconnect to the file." }; }
      },

      async unlink() {
        file.handle = null; file.name = null; file.permission = "none"; file.error = null; meta.dirty = false;
        try { if (handleStore) await handleStore.del(KEY); } catch (e) { /* ignore */ }
        saveLocal(); emit();
      },

      /** Save a copy through the browser's download (works everywhere). */
      download: function () {
        var doc = win.document;
        var blob = new win.Blob([serialize(entries)], { type: "application/json" });
        var a = doc.createElement("a");
        a.href = win.URL.createObjectURL(blob);
        a.download = "nse-evaluation-" + new Date().toISOString().slice(0, 10) + ".json";
        doc.body.appendChild(a); a.click(); doc.body.removeChild(a);
        setTimeout(function () { win.URL.revokeObjectURL(a.href); }, 1000);
      },

      /** Resolves when pending writes to the linked file are done (for tests). */
      flush: function () { return queueWrite(true); }
    };
    return api;
  }

  function pickTextWithInput(win) {
    return new Promise(function (resolve) {
      var doc = win.document, input = doc.createElement("input");
      input.type = "file"; input.accept = ".json,application/json";
      input.addEventListener("change", function () {
        var f = input.files && input.files[0];
        if (!f) { resolve(null); return; }
        var r = new win.FileReader();
        r.onload = function () { resolve(String(r.result)); };
        r.onerror = function () { resolve(null); };
        r.readAsText(f);
      });
      input.addEventListener("cancel", function () { resolve(null); });
      input.click();
    });
  }

  var EvalStore = { create: create, normalise: normalise, merge: merge, parseText: parseText, serialize: serialize, keyOf: keyOf };
  if (typeof module !== "undefined" && module.exports) module.exports = EvalStore;
  else root.EvalStore = EvalStore;
})(typeof window !== "undefined" ? window : globalThis);
