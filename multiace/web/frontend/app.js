const { createApp, ref, reactive, computed, onMounted, onUnmounted, watch, nextTick } = Vue;
const API = "/multiace/api";
const WS_URL = (location.protocol === "https:" ? "wss://" : "ws://")
             + location.host + "/multiace/ws";
const SCREEN = "/screen";
createApp({
  setup() {
    const _validTabs = new Set(["dashboard", "config"]);
    const _storedTab = localStorage.getItem("multiace.tab");
    const _isPluginTab = (s) => typeof s === "string" && s.startsWith("plugin:");
    const tab = ref(
      (_validTabs.has(_storedTab) || _isPluginTab(_storedTab))
        ? _storedTab
        : "dashboard"
    );
    watch(tab, (v) => localStorage.setItem("multiace.tab", v));
    const plugins = reactive({items: [], loaded: false});
    async function refreshPlugins() {
      try {
        const r = await fetch(`${API}/integrations`);
        if (!r.ok) return;
        const j = await r.json();
        plugins.items = j.plugins || [];
      } catch (_) {
      } finally {
        plugins.loaded = true;
        if (_isPluginTab(tab.value)) {
          const pname = tab.value.slice("plugin:".length);
          if (!plugins.items.find(p => p.name === pname)) {
            tab.value = "dashboard";
          }
        }
      }
    }
    function pluginIframeSrc(p) {
      const u = (p && p.ui_url) || "/";
      return `/plugin/${p.name}${u.startsWith("/") ? u : "/" + u}`;
    }
    const language = ref(localStorage.getItem("multiace.lang") || "en");
    const languages = ref([{code: "en", name: "English"}]);
    const catalog = reactive({});
    const indexBase = ref(0);
    function t(key, params) {
      const parts = key.split('.');
      let v = catalog;
      for (const p of parts) {
        if (v == null) return key;
        v = v[p];
      }
      if (typeof v !== "string") return key;
      if (!params) return v;
      return v.replace(/\{(\w+)\}/g, (_, k) => params[k] != null ? params[k] : `{${k}}`);
    }
    function dispIdx(n) {
      if (n == null) return "–";
      return Number(n) + indexBase.value;
    }
    function subText(sku) {
      const s = (sku || "").trim();
      if (!s || ["basic", "generic"].includes(s.toLowerCase())) return "";
      return s;
    }
    function sourceLabel(src) {
      if (src === "rfid") return t("ui.common.source_rfid");
      if (src === "override") return t("ui.common.source_override");
      if (src === "derived") return t("ui.common.source_derived");
      return "";
    }
    async function loadCatalog(lang) {
      try {
        const r = await fetch(`${API}/i18n/${lang}`);
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const data = await r.json();
        for (const k of Object.keys(catalog)) delete catalog[k];
        Object.assign(catalog, data);
        document.documentElement.lang = lang;
        if (conn.value.state === "init" || conn.value.state === "warn") {
          conn.value = {
            state: conn.value.state,
            text: conn.value.state === "ok"   ? t("ui.header.live")
                : conn.value.state === "warn" ? t("ui.header.offline")
                : conn.value.state === "err"  ? t("ui.header.ws_error")
                :                               t("ui.header.connecting"),
          };
        }
      } catch (e) {
        console.warn("i18n load failed", e);
      }
    }
    async function loadLanguageList() {
      try {
        const r = await fetch(`${API}/i18n`);
        if (!r.ok) return;
        const j = await r.json();
        if (Array.isArray(j.languages) && j.languages.length) {
          languages.value = j.languages;
        }
      } catch (_) {}
    }
    async function setLanguage(lang) {
      language.value = lang;
      localStorage.setItem("multiace.lang", lang);
      await loadCatalog(lang);
      try {
        await fetch(`${API}/macro`, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({name: "MULTIACE_SET_LANGUAGE", args: {LANG: lang}}),
        });
      } catch (_) {}
    }
    const version = ref("");
    const printerName = ref("");
    const printerFw = ref("");
    const conn = ref({state: "init", text: ""});
    const connClass = computed(() => ({
      ok:   conn.value.state === "ok",
      warn: conn.value.state === "warn",
      err:  conn.value.state === "err",
    }));
    const connText = computed(() => conn.value.text);
    const screenAvailable = ref(false);
    const state = reactive({
      ace_status: null, ace_temp: null,
      printer_state: null,
      active_device: null, device_count: 0,
      mode: "normal",
      ace_head: 3,
      dryer: null,
      swap_in_progress: false,
      aces: [], toolheads: [], wiring: [],
      save_variables: {},
    });
    const loadError = ref("");
    const notifications = ref([]);
    const _notifIds = new Set();
    function _addNotif(n) {
      if (!n || n.id == null) return;
      if (_notifIds.has(n.id)) return;
      _notifIds.add(n.id);
      notifications.value.push(n);
      if (notifications.value.length > 20) {
        const dropped = notifications.value.splice(0, notifications.value.length - 20);
        for (const d of dropped) _notifIds.delete(d.id);
      }
    }
    function onGcodeError(m) {
      _addNotif({id: m.id, ts: m.ts, msg: m.msg, raw: m.raw, level: m.level || 'error'});
    }
    async function loadNotifications() {
      try {
        const r = await fetch(`${API}/notifications`);
        if (!r.ok) return;
        const j = await r.json();
        for (const n of (j.notifications || [])) _addNotif(n);
      } catch (_) {}
    }
    async function dismissNotification(id) {
      const idx = notifications.value.findIndex(n => n.id === id);
      if (idx >= 0) {
        notifications.value.splice(idx, 1);
        _notifIds.delete(id);
      }
      try { await fetch(`${API}/notifications/${id}`, {method: "DELETE"}); } catch (_) {}
    }
    async function dismissAllNotifications() {
      const ids = notifications.value.map(n => n.id);
      notifications.value = [];
      for (const id of ids) _notifIds.delete(id);
      try { await fetch(`${API}/notifications`, {method: "DELETE"}); } catch (_) {}
    }
    function applyState(s) {
      if (!s) return;
      state.ace_status    = s.ace_status ?? null;
      state.ace_temp      = s.ace_temp ?? null;
      state.printer_state = s.printer_state ?? null;
      state.active_device = s.active_device ?? null;
      state.device_count  = s.device_count ?? 0;
      state.mode          = s.mode || "normal";
      state.ace_head      = (typeof s.ace_head === "number") ? s.ace_head : 3;
      if (!aceHeadInit && typeof s.ace_head === "number") {
        aceHeadSel.value = s.ace_head;
        aceHeadInit = true;
      }
      state.dryer         = s.dryer ?? null;
      state.swap_in_progress = !!s.swap_in_progress;
      state.aces          = Array.isArray(s.aces) ? s.aces : [];
      state.toolheads     = Array.isArray(s.toolheads) ? s.toolheads : [];
      state.wiring        = Array.isArray(s.wiring) ? s.wiring : [];
      state.save_variables = s.save_variables || {};
      if (typeof s.display_index_base === "number") {
        indexBase.value = s.display_index_base;
      }
      for (const a of state.aces) {
        if (!dryerCfg[a.idx]) dryerCfg[a.idx] = {temp: 50, duration: 240};
      }
    }
    async function reloadState() {
      try {
        const r = await fetch(`${API}/state`);
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const j = await r.json();
        loadError.value = j.error || "";
        applyState(j);
      } catch (e) {
        loadError.value = String(e);
      }
    }
    const macroLog = ref("");
    let _macroLogTimer = null;
    function setMacroLog(msg) {
      macroLog.value = msg || "";
      if (_macroLogTimer) { clearTimeout(_macroLogTimer); _macroLogTimer = null; }
      if (msg) {
        _macroLogTimer = setTimeout(() => {
          macroLog.value = "";
          _macroLogTimer = null;
        }, 5000);
      }
    }
    const dryerCfg = reactive({});
    const cmdQueue = ref([]);
    const visibleQueue = computed(() => cmdQueue.value.filter(it => !it.silent));
    const cmdPaused = ref(false);
    let cmdQueueRunning = false;
    function _newId() {
      return Date.now().toString(36) + Math.random().toString(36).slice(2, 6);
    }
    function _argsKey(args) {
      const a = args || {};
      return Object.keys(a).sort().map(k => `${k}=${a[k]}`).join('|');
    }
    function enqueue(name, args, opts) {
      return new Promise((resolve) => {
        const key = _argsKey(args);
        const dup = cmdQueue.value.find(it =>
          (it.status === 'queued' || it.status === 'running')
          && it.cmd === name
          && _argsKey(it.args) === key);
        if (dup) { resolve(false); return; }
        const it = reactive({
          id: _newId(),
          cmd: name,
          args: args || {},
          status: 'queued',
          error: '',
          silent: !!(opts && opts.silent),
          _resolve: resolve,
        });
        cmdQueue.value.unshift(it);
        _scheduleAdvance();
      });
    }
    function removeFromQueue(id) {
      const idx = cmdQueue.value.findIndex(i => i.id === id);
      if (idx < 0) return;
      const it = cmdQueue.value[idx];
      if (it.status === 'running') return;
      cmdQueue.value.splice(idx, 1);
      if (it._resolve) it._resolve(false);
      _scheduleAdvance();
    }
    function pauseQueue() { cmdPaused.value = true; }
    function resumeQueue() {
      cmdPaused.value = false;
      _scheduleAdvance();
    }
    function _scheduleAdvance() {
      if (cmdQueueRunning) return;
      if (cmdPaused.value) return;
      if (cmdQueue.value.length === 0) return;
      if (state.swap_in_progress) return;
      const arr = cmdQueue.value;
      let target = null;
      for (let i = arr.length - 1; i >= 0; i--) {
        if (arr[i].status === 'queued') { target = arr[i]; break; }
        if (arr[i].status === 'error')  { return; }
      }
      if (!target) {
        _scheduleIdleClear();
        return;
      }
      _runItem(target);
    }
    function _scheduleIdleClear() {
      const stillActive = cmdQueue.value.some(
        it => it.status === 'queued' || it.status === 'running');
      if (stillActive) return;
      if (cmdPaused.value) cmdPaused.value = false;
    }
    async function _runItem(it) {
      cmdQueueRunning = true;
      it.status = 'running';
      const parts = [it.cmd];
      for (const [k, v] of Object.entries(it.args || {})) {
        parts.push(`${k}=${v}`);
      }
      const script = parts.join(' ');
      try {
        const r = await fetch(`${API}/macro`, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({name: it.cmd, args: it.args || {}}),
        });
        const j = await r.json();
        if (!r.ok || j.detail) {
          it.status = 'error';
          it.error = String(j.detail || `HTTP ${r.status}`);
          it.silent = false;
          cmdPaused.value = true;
        } else {
          const idx = cmdQueue.value.indexOf(it);
          if (idx >= 0) cmdQueue.value.splice(idx, 1);
          it.status = 'done';
        }
      } catch (e) {
        it.status = 'error';
        it.error = String(e);
        it.silent = false;
        cmdPaused.value = true;
      } finally {
        cmdQueueRunning = false;
        if (it._resolve) it._resolve(it.status !== 'error');
      }
      _scheduleAdvance();
    }
    function run(name, args) { return enqueue(name, args); }
    function clearAllErrors() {
      cmdQueue.value = cmdQueue.value.filter(it => it.status !== 'error');
      cmdPaused.value = false;
      if (notifications.value.length) {
        dismissAllNotifications();
      }
      _scheduleAdvance();
    }
    const sendingAll = ref(false);
    async function sendAllToPrinter() {
      const items = cmdQueue.value.filter(it => it.status === 'queued');
      if (!items.length) return;
      const commands = items.map(it => ({name: it.cmd, args: it.args || {}}));
      sendingAll.value = true;
      try {
        const r = await fetch(`${API}/macro-batch`, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({commands}),
        });
        if (!r.ok) {
          let msg = `${r.status} ${r.statusText}`;
          try { const j = await r.json(); if (j.detail) msg = j.detail; } catch (_) {}
          throw new Error(msg);
        }
        for (const it of items) {
          const idx = cmdQueue.value.indexOf(it);
          if (idx >= 0) cmdQueue.value.splice(idx, 1);
          if (it._resolve) it._resolve(true);
        }
        setMacroLog(t("ui.queue.send_all_done", {count: commands.length}));
      } catch (e) {
        setMacroLog(`${t("ui.queue.send_all_failed")}: ${e.message || e}`);
        confirm({
          title: t("ui.queue.send_all_failed"),
          message: String(e.message || e),
          dismissOnly: true, okLabel: "OK", onOk: () => {},
        });
      } finally {
        sendingAll.value = false;
      }
    }
    function fmtArgs(args) {
      if (!args) return "";
      const parts = [];
      for (const [k, v] of Object.entries(args)) {
        const s = String(v);
        parts.push(`${k}=${s.length > 12 ? s.slice(0, 12) + '…' : s}`);
      }
      return parts.join(' ');
    }
    function cmdLabel(it) {
      const a = it.args || {};
      const di = (n) => dispIdx(Number(n));
      switch (it.cmd) {
        case 'SET_PRINT_FILAMENT_CONFIG':
          return `Display T${di(a.CONFIG_EXTRUDER ?? 0)}`;
        case 'ACE_LOAD_HEAD':
          return `Load T${di(a.HEAD ?? 0)} ← ACE ${di(a.ACE ?? 0)}`;
        case 'ACE_SWAP_HEAD':
          return `Swap T${di(a.HEAD ?? 0)} ← ACE ${di(a.ACE ?? 0)}`;
        case 'ACE_UNLOAD_HEAD':
          return `Unload T${di(a.HEAD ?? 0)}`;
        case 'ACE_UNLOAD_ALL_HEADS':
          return 'Unload all';
        case 'ACE_SWITCH':
          return `ACE ${di(a.TARGET ?? 0)}` + ((a.AUTOLOAD == 1 || a.AUTOLOAD === true) ? ' (auto-load)' : '');
        case 'ACE_DRY':
          return `Dry ACE ${di(a.ACE ?? 0)} ${a.TEMP}°C / ${a.DURATION}min`;
        case 'ACE_STOP_DRYING':
          return `Stop dry ACE ${di(a.ACE ?? 0)}`;
      }
      return null;
    }
    function slotTitle(ace, slot) {
      const bits = [`ACE ${dispIdx(ace.idx)} / Slot ${dispIdx(slot.idx)}`];
      if (slot.material) bits.push(slot.material);
      if (slot.brand) bits.push(slot.brand);
      bits.push(slot.state);
      if (slot.color) bits.push(slot.color);
      return bits.join(" · ");
    }
    const wiringContainerEl = ref(null);
    const slotEls = {};
    const thEls = {};
    const layoutTick = ref(0);
    function setSlotEl(ace, slot, el) {
      const k = `${ace}_${slot}`;
      if (el) slotEls[k] = el; else delete slotEls[k];
    }
    function setThEl(idx, el) {
      if (el) thEls[idx] = el; else delete thEls[idx];
    }
    const wiringPaths = ref([]);
    const wiringViewBox = ref("0 0 100 100");
    function recomputeWiring() {
      const c = wiringContainerEl.value;
      if (!c) { wiringPaths.value = []; return; }
      const cb = c.getBoundingClientRect();
      wiringViewBox.value = `0 0 ${cb.width} ${cb.height}`;
      const lines = [];
      for (const w of state.wiring) {
        const sEl = slotEls[`${w.ace}_${w.slot}`];
        const tEl = thEls[w.toolhead];
        if (!sEl || !tEl) continue;
        const sb = sEl.getBoundingClientRect();
        const tb = tEl.getBoundingClientRect();
        const x1 = sb.left + sb.width / 2 - cb.left;
        const y1 = sb.bottom - cb.top;
        const x2 = tb.left + tb.width / 2 - cb.left;
        const y2 = tb.top - cb.top;
        const midY = (y1 + y2) / 2;
        lines.push({
          d: `M${x1},${y1} C${x1},${midY} ${x2},${midY} ${x2},${y2}`,
          color: w.color || "#888",
        });
      }
      wiringPaths.value = lines;
    }
    function scheduleWiringRecompute() {
      nextTick(() => {
        recomputeWiring();
        requestAnimationFrame(recomputeWiring);
      });
    }
    watch(() => state.swap_in_progress, (v) => { if (!v) _scheduleAdvance(); });

    watch(() => state.wiring, scheduleWiringRecompute, {deep: true});
    watch(() => state.aces.length, scheduleWiringRecompute);
    watch(() => state.toolheads.length, scheduleWiringRecompute);
    watch(() => tab.value, (v) => { if (v === "dashboard") scheduleWiringRecompute(); });
    function switchAce(idx) {
      run("ACE_SWITCH", {TARGET: idx});
    }
    function loadAll(idx) {
      run("ACE_SWITCH", {TARGET: idx, AUTOLOAD: 1});
    }
    function _phaseFor(channelState) {
      if (!channelState) return null;
      const s = String(channelState);
      if (s.endsWith('_finish') || s.endsWith('_fail')) return null;
      if (s === 'wait_insert' || s === 'inited' || s === 'test') return null;
      if (s.startsWith('unload_')) return 'unloading';
      if (s.startsWith('load_'))   return 'loading';
      if (s.startsWith('preload_')) return 'loading';
      if (s.startsWith('manual_sta_')) return 'loading';
      return null;
    }
    const toolheadOps = computed(() => {
      const ops = {};
      for (const t of state.toolheads) {
        const p = _phaseFor(t.channel_state);
        if (p) ops[t.idx] = p;
      }
      return ops;
    });
    function isToolheadOccupied(aceIdx, slotIdx) {
      const th = state.toolheads.find(tt => tt.idx === slotIdx);
      if (!th) return false;
      if (th.head_source_known) return th.ace === aceIdx;
      return !!th.filament_at_extruder;
    }
    function needsReload(aceIdx, slotIdx) {
      if (state.printer_state !== 'paused') return false;
      const th = state.toolheads.find(tt => tt.idx === slotIdx);
      if (!th || !th.head_source_known) return false;
      return th.ace === aceIdx && th.filament_at_extruder === false;
    }
    function unloadHead(idx) {
      run("ACE_UNLOAD_HEAD", {HEAD: idx});
    }
    async function setHeadManual(idx, enable) {
      try {
        await fetch(`${API}/head-manual`, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({head: idx, enable: !!enable}),
        });
      } catch (_) {}
      reloadState();
    }
    function loadSlot(aceIdx, slotIdx) {
      if (state.mode === "head") {
        const h = state.ace_head;
        const th = state.toolheads.find(tt => tt.idx === h);
        if (th && th.head_source_known) {
          enqueue("ACE_UNLOAD_HEAD", {HEAD: h});
        }
        enqueue("ACE_LOAD_HEAD", {HEAD: h, ACE: aceIdx, SLOT: slotIdx});
        return;
      }
      const th = state.toolheads.find(tt => tt.idx === slotIdx);
      if (th && th.head_source_known && th.ace !== aceIdx) {
        enqueue("ACE_UNLOAD_HEAD", {HEAD: slotIdx});
        enqueue("ACE_LOAD_HEAD",   {HEAD: slotIdx, ACE: aceIdx});
        return;
      }
      enqueue("ACE_LOAD_HEAD", {HEAD: slotIdx, ACE: aceIdx});
    }
    function loadFeederHead(h) {
      enqueue("ACE_LOAD_HEAD", {HEAD: h});
    }
    function slotLoadedInHead(slotIdx) {
      if (state.mode !== "head") return false;
      const th = state.toolheads.find(tt => tt.idx === state.ace_head);
      return !!(th && th.head_source_known && th.slot === slotIdx);
    }
    const pickerMaterials = ref([
      "PLA", "PLA-CF",
      "PETG", "PETG-CF", "PETG-HF",
      "ABS", "ASA",
      "TPU",
      "PA", "PA-CF", "PA-GF", "PA6-CF", "PA6-GF",
      "PC", "PC-ABS",
      "PVA",
    ]);
    const pickerDb = ref({});
    async function loadMaterials() {
      try {
        const r = await fetch(`${API}/materials`);
        if (r.ok) {
          const j = await r.json();
          if (Array.isArray(j.materials) && j.materials.length) {
            pickerMaterials.value = j.materials;
          }
          if (j.db && typeof j.db === "object") {
            pickerDb.value = j.db;
          }
        }
      } catch (_) {}
    }
    const pickerVendors = computed(() => {
      const v = Object.keys(pickerDb.value[picker.material] || { Generic: [] });
      return v.includes("Generic") ? ["Generic", ...v.filter(x => x !== "Generic")] : v;
    });
    const currentSubtypes = computed(() => {
      const byVendor = pickerDb.value[picker.material] || {};
      const extra = byVendor[picker.vendor] || [];
      return ["Basic", ...extra];
    });
    const picker = reactive({
      show: false,
      ace: 0,
      slot: 0,
      material: "PLA",
      subtype: "Basic",
      vendor: "Generic",
      color: "#ffffff",
    });
    watch(() => picker.material, () => {
      if (!pickerVendors.value.includes(picker.vendor)) {
        picker.vendor = pickerVendors.value[0] || "Generic";
      }
      if (!currentSubtypes.value.includes(picker.subtype)) {
        picker.subtype = "Basic";
      }
    });
    watch(() => picker.vendor, () => {
      if (!currentSubtypes.value.includes(picker.subtype)) {
        picker.subtype = "Basic";
      }
    });
    function openPicker(ace, slot) {
      picker.ace = ace.idx;
      picker.slot = slot.idx;
      picker.material = (slot.material || "PLA");
      picker.subtype = slot.subtype || "Basic";
      picker.vendor = slot.brand || "Generic";
      picker.color = slot.color || "#ffffff";
      picker.show = true;
    }
    function closePicker() { picker.show = false; }
    function _pickerSlot() {
      const a = state.aces.find(x => x.idx === picker.ace);
      if (!a) return null;
      return (a.slots || []).find(s => s.idx === picker.slot) || null;
    }
    const pickerHasRfid = computed(() => {
      if (!picker.show) return false;
      const s = _pickerSlot();
      return !!(s && s.rfid === 2 && s.rfid_data);
    });
    const pickerRfidStyle = computed(() => {
      if (!pickerHasRfid.value) return {};
      const c = (_pickerSlot()?.rfid_data?.color || "").trim();
      if (!/^#[0-9a-fA-F]{6}$/.test(c)) return {};
      const r = parseInt(c.slice(1, 3), 16);
      const g = parseInt(c.slice(3, 5), 16);
      const b = parseInt(c.slice(5, 7), 16);
      const lum = (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255;
      return {
        background: c,
        borderColor: c,
        color: lum > 0.55 ? "#001619" : "#ffffff",
      };
    });
    const pickerHasOverride = computed(() => {
      if (!picker.show) return false;
      const s = _pickerSlot();
      return !!(s && s.source === "override");
    });
    function readPickerRfid() {
      const s = _pickerSlot();
      const r = s && s.rfid_data;
      if (!r) return;
      if (r.material) picker.material = r.material;
      if (r.subtype)  picker.subtype  = r.subtype;
      if (r.brand)    picker.vendor   = r.brand;
      if (r.color)    picker.color    = r.color;
    }
    function _ptcGcodeFor(aceIdx, slotIdx, mat, brand, sub, colorHex) {
      const dq = (s) => `"${String(s || "").replace(/"/g, "")}"`;
      const hex = (colorHex || "#ffffff").replace("#", "");
      const colorRGBA = hex.toUpperCase() + "FF";
      return {
        CONFIG_EXTRUDER: slotIdx,
        FILAMENT_TYPE:   dq(mat || "PLA"),
        FILAMENT_COLOR_RGBA: colorRGBA,
        VENDOR:          dq(brand || "Generic"),
        FILAMENT_SUBTYPE: dq(sub || ""),
      };
    }
    function _ovNorm(s) { return String(s || "").trim().toLowerCase(); }
    function _ovVendor(s) { const v = _ovNorm(s); return v === "generic" ? "" : v; }
    function _ovSub(s) { const v = _ovNorm(s); return (v === "basic" || v === "generic") ? "" : v; }
    function _ovColor(s) { return _ovNorm(s).replace(/^#/, ""); }
    function _pickerMatchesRfid() {
      const s = _pickerSlot();
      const r = (s && s.rfid === 2) ? s.rfid_data : null;
      if (!r) return false;
      return _ovNorm(picker.material) === _ovNorm(r.material)
          && _ovVendor(picker.vendor) === _ovVendor(r.brand)
          && _ovSub(picker.subtype) === _ovSub(r.subtype)
          && _ovColor(picker.color) === _ovColor(r.color);
    }
    async function savePicker(loadAfter) {
      const aceIdx = picker.ace;
      const slotIdx = picker.slot;
      if (_pickerMatchesRfid()) {
        try {
          await fetch(`${API}/slot-override/${aceIdx}/${slotIdx}`, {method: "DELETE"});
        } catch (e) {
          setMacroLog(`${t("ui.common.error")}: ${e}`);
        }
        closePicker();
        enqueue("MULTIACE_REFRESH_OVERRIDES", {}, {silent: true});
        if (loadAfter) {
          loadSlot(aceIdx, slotIdx);
        }
        reloadState();
        return;
      }
      try {
        await fetch(`${API}/slot-override`, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({
            ace: aceIdx,
            slot: slotIdx,
            material: picker.material || "",
            brand:    picker.vendor || "",
            subtype:  picker.subtype || "",
            color:    picker.color || "",
          }),
        });
      } catch (e) {
        setMacroLog(`${t("ui.common.error")}: ${e}`);
      }
      closePicker();
      enqueue("MULTIACE_REFRESH_OVERRIDES", {}, {silent: true});
      if (loadAfter) {
        loadSlot(aceIdx, slotIdx);
      }
      reloadState();
    }
    async function clearPickerOverride() {
      const aceIdx = picker.ace;
      const slotIdx = picker.slot;
      try {
        await fetch(`${API}/slot-override/${aceIdx}/${slotIdx}`, {method: "DELETE"});
      } catch (e) {
        setMacroLog(`${t("ui.common.error")}: ${e}`);
      }
      closePicker();
      enqueue("MULTIACE_REFRESH_OVERRIDES", {}, {silent: true});
      reloadState();
    }
    let _lastActive = null;
    watch(() => state.active_device, (newAce) => {
      _lastActive = newAce;
    });
    const dryOpenAce = ref(null);
    function toggleDryPanel(aceIdx) {
      dryOpenAce.value = (dryOpenAce.value === aceIdx) ? null : aceIdx;
    }
    function aceDrying(ace) {
      const d = ace && ace.dryer;
      return !!(d && d.status && d.status !== 'stop');
    }
    function dryStart(aceIdx) {
      const cfg = dryerCfg[aceIdx] || {temp: 50, duration: 240};
      run("ACE_DRY", {ACE: aceIdx, TEMP: cfg.temp, DURATION: cfg.duration});
    }
    function dryStop(aceIdx) {
      run("ACE_STOP_DRYING", {ACE: aceIdx});
    }
    const snapshots = ref([]);
    const selectedSnapshot = ref("");
    const snapshotPreview = computed(() => snapshots.value.find(s => s.name === selectedSnapshot.value));
    async function reloadSnapshots() {
      try {
        const r = await fetch(`${API}/snapshots`);
        if (!r.ok) return;
        const j = await r.json();
        snapshots.value = j.snapshots || [];
      } catch (_) {}
    }
    async function _doSaveSnapshot(name) {
      try {
        const r = await fetch(`${API}/snapshots`, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({name}),
        });
        if (!r.ok) {
          setMacroLog(t("ui.log.snapshot_save_failed", {error: await r.text()}));
          return;
        }
        setMacroLog(t("ui.log.snapshot_saved", {name}));
        await reloadSnapshots();
        selectedSnapshot.value = name;
      } catch (e) { setMacroLog(`${t("ui.common.error")}: ${e}`); }
    }
    async function saveSnapshot() {
      if (selectedSnapshot.value) {
        const name = selectedSnapshot.value;
        confirm({
          title: t("ui.dialog.overwrite_snapshot_title", {name}),
          message: t("ui.dialog.overwrite_snapshot_msg", {name}),
          okLabel: t("ui.common.save"),
          onOk: () => _doSaveSnapshot(name),
        });
        return;
      }
      const name = prompt(t("ui.dashboard.snapshot_name_prompt"));
      if (!name) return;
      await _doSaveSnapshot(name);
    }
    async function deleteSnapshot() {
      if (!selectedSnapshot.value) return;
      if (!confirmSync(t("ui.dialog.delete_snapshot", {name: selectedSnapshot.value}))) return;
      try {
        await fetch(`${API}/snapshots/${encodeURIComponent(selectedSnapshot.value)}`, {method: "DELETE"});
        selectedSnapshot.value = "";
        await reloadSnapshots();
      } catch (e) { setMacroLog(`${t("ui.common.error")}: ${e}`); }
    }
    async function loadSnapshot() {
      if (!selectedSnapshot.value) return;
      const name = selectedSnapshot.value;
      let plan;
      try {
        const r = await fetch(`${API}/snapshots/${encodeURIComponent(name)}/apply`, {method: "POST"});
        plan = await r.json();
      } catch (e) {
        setMacroLog(`${t("ui.common.error")}: ${e}`);
        return;
      }
      const errs = plan.errors || [];
      const warns = plan.warnings || [];
      const actions = plan.actions || [];
      if (errs.length) {
        confirm({
          title: t("ui.dialog.snapshot_errors_title"),
          message: errs.map(e => "• " + e.message).join("<br>"),
          okLabel: "OK",
          dismissOnly: true,
          onOk: () => {},
        });
        return;
      }
      const proposals = plan.override_proposals || [];
      const writeOverridesAndEnqueue = async (writeOverrides) => {
        if (writeOverrides && proposals.length) {
          for (const o of proposals) {
            try {
              await fetch(`${API}/slot-override`, {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify(o),
              });
            } catch (e) {
              setMacroLog(`${t("ui.common.error")}: ${e}`);
            }
          }
          enqueue("MULTIACE_REFRESH_OVERRIDES", {}, {silent: true});
        }
        for (const a of actions) {
          enqueue(a.name, a.args || {});
        }
      };
      if (warns.length) {
        confirm({
          title: t("ui.dialog.snapshot_warnings_title"),
          message: warns.map(w => "• " + w.message).join("<br>")
                   + "<br><br>" + t("ui.dialog.snapshot_warnings_hint"),
          okLabel: t("ui.dialog.apply_anyway"),
          checkboxLabel: proposals.length
            ? t("ui.dialog.set_filaments_per_snapshot")
            : null,
          checkboxDefault: false,
          onOk: ({checked}) => { writeOverridesAndEnqueue(checked); },
        });
        return;
      }
      confirm({
        title: t("ui.dialog.apply_snapshot_title", {name}),
        message: t("ui.dialog.apply_snapshot_msg"),
        okLabel: t("ui.common.apply"),
        onOk: () => { writeOverridesAndEnqueue(false); },
      });
    }
    const confirmDialog = reactive({
      show: false, title: "", message: "",
      okLabel: "OK",  _onOk:  null,
      altLabel: null, _onAlt: null,
      dismissOnly: false,
      checkboxLabel: null, checkboxChecked: false,
    });
    function confirm(opts) {
      confirmDialog.show = true;
      confirmDialog.title = opts.title || t("ui.common.confirm");
      confirmDialog.message = opts.message || "";
      confirmDialog.okLabel = opts.okLabel || "OK";
      confirmDialog._onOk   = opts.onOk || (()=>{});
      confirmDialog.altLabel = opts.altLabel || null;
      confirmDialog._onAlt   = opts.onAlt || null;
      confirmDialog.dismissOnly = !!opts.dismissOnly;
      confirmDialog.checkboxLabel = opts.checkboxLabel || null;
      confirmDialog.checkboxChecked = !!opts.checkboxDefault;
    }
    function okConfirm() {
      const cb = confirmDialog._onOk;
      const checked = confirmDialog.checkboxChecked;
      confirmDialog.show = false;
      if (cb) cb({checked});
    }
    function altConfirm() {
      const cb = confirmDialog._onAlt;
      confirmDialog.show = false;
      if (cb) cb();
    }
    function cancelConfirm() { confirmDialog.show = false; }
    function confirmSync(msg) { return window.confirm(msg); }
    const config = reactive({path: "", content: "", params: {}, restartKlipper: false});
    const configLog = ref("");
    const configLoadError = ref("");
    const showRawConfig = ref(false);
    const configForm = reactive({
      ace_device_count: 1,
      feed_speed: 80,
      retract_speed: 80,
      load_length: 2100,
      retract_length: 1950,
      swap_retract_length: '',
      swap_purge_length: '',
      dryer_temp: '',
      dryer_duration: '',
      display_index_base: 0,
      v2_order: 'first',
      load_retry: '',
      extrusion_retry: '',
      unload_retry: '',
      state_debug: false,
      usb_debug: false,
      fa_debug: false,
      perAce: [],
    });
    function _makePerAceEntry() {
      const perSlot = [];
      for (let s = 0; s < 4; s++) {
        perSlot.push({load_length: '', retract_length: '', swap_retract_length: ''});
      }
      return {
        dryer_temp: '', dryer_duration: '',
        feed_speed: '', retract_speed: '',
        load_length: '', retract_length: '', swap_retract_length: '',
        perSlot,
      };
    }
    function _ensurePerAceLength() {
      const n = Math.max(0, Math.min(4, configForm.ace_device_count | 0));
      while (configForm.perAce.length < n) {
        configForm.perAce.push(_makePerAceEntry());
      }
      while (configForm.perAce.length > n) {
        configForm.perAce.pop();
      }
    }
    watch(() => configForm.ace_device_count, _ensurePerAceLength, {immediate: true});
    const modeChangePending = ref(false);
    const aceHeadSel = ref(3);
    let aceHeadInit = false;
    function paramsToForm(params, perAceParams) {
      if (!params) return;
      const num  = (k) => params[k] != null ? Number(params[k]) : configForm[k];
      const bool = (k) => params[k] != null ? params[k] === 'true' : configForm[k];
      const numOrEmpty = (v) => (v != null && v !== '') ? Number(v) : '';
      configForm.ace_device_count = num('ace_device_count');
      configForm.feed_speed     = num('feed_speed');
      configForm.retract_speed  = num('retract_speed');
      configForm.load_length    = num('load_length');
      configForm.retract_length = num('retract_length');
      configForm.swap_retract_length = numOrEmpty(params.swap_retract_length);
      configForm.swap_purge_length = numOrEmpty(params.swap_purge_length);
      configForm.dryer_temp        = numOrEmpty(params.dryer_temp);
      configForm.dryer_duration    = numOrEmpty(params.dryer_duration);
      configForm.display_index_base = numOrEmpty(params.display_index_base);
      configForm.v2_order = (params.v2_order === 'last') ? 'last' : 'first';
      configForm.load_retry        = numOrEmpty(params.load_retry);
      configForm.extrusion_retry   = numOrEmpty(params.extrusion_retry);
      configForm.unload_retry      = numOrEmpty(params.unload_retry);
      configForm.state_debug    = bool('state_debug');
      configForm.usb_debug      = bool('usb_debug');
      configForm.fa_debug       = bool('fa_debug');
      _ensurePerAceLength();
      const pa = perAceParams || {};
      for (let i = 0; i < configForm.perAce.length; i++) {
        const t = params[`dryer_temp_${i}`];
        const d = params[`dryer_duration_${i}`];
        configForm.perAce[i].dryer_temp     = numOrEmpty(t);
        configForm.perAce[i].dryer_duration = numOrEmpty(d);
        const aceSec = pa[i] || pa[String(i)] || {};
        configForm.perAce[i].feed_speed     = numOrEmpty(aceSec.feed_speed);
        configForm.perAce[i].retract_speed  = numOrEmpty(aceSec.retract_speed);
        configForm.perAce[i].load_length    = numOrEmpty(aceSec.load_length);
        configForm.perAce[i].retract_length = numOrEmpty(aceSec.retract_length);
        configForm.perAce[i].swap_retract_length = numOrEmpty(aceSec.swap_retract_length);
        for (let s = 0; s < 4; s++) {
          configForm.perAce[i].perSlot[s].load_length    = numOrEmpty(aceSec[`load_length_${s}`]);
          configForm.perAce[i].perSlot[s].retract_length = numOrEmpty(aceSec[`retract_length_${s}`]);
          configForm.perAce[i].perSlot[s].swap_retract_length = numOrEmpty(aceSec[`swap_retract_length_${s}`]);
        }
      }
    }
    function formToCfgContent(content) {
      const lines = content.split('\n');
      const numStr = (v) => (v === '' || v == null) ? '' : String(v);
      const mainRepl = {
        ace_device_count:   numStr(configForm.ace_device_count),
        feed_speed:         numStr(configForm.feed_speed),
        retract_speed:      numStr(configForm.retract_speed),
        load_length:        numStr(configForm.load_length),
        retract_length:     numStr(configForm.retract_length),
        swap_retract_length: numStr(configForm.swap_retract_length),
        swap_purge_length:   numStr(configForm.swap_purge_length),
        dryer_temp:         numStr(configForm.dryer_temp),
        dryer_duration:     numStr(configForm.dryer_duration),
        display_index_base: numStr(configForm.display_index_base),
        v2_order:           configForm.v2_order === 'last' ? 'last' : 'first',
        load_retry:         numStr(configForm.load_retry),
        extrusion_retry:    numStr(configForm.extrusion_retry),
        unload_retry:       numStr(configForm.unload_retry),
        state_debug:        configForm.state_debug ? 'true' : 'false',
        usb_debug:          configForm.usb_debug   ? 'true' : 'false',
        fa_debug:           configForm.fa_debug    ? 'true' : 'false',
      };
      for (let i = 0; i < configForm.perAce.length; i++) {
        const p = configForm.perAce[i];
        mainRepl[`dryer_temp_${i}`]     = numStr(p.dryer_temp);
        mainRepl[`dryer_duration_${i}`] = numStr(p.dryer_duration);
      }
      const perAceRepl = {};
      for (let i = 0; i < configForm.perAce.length; i++) {
        const p = configForm.perAce[i];
        const sec = {};
        sec.feed_speed     = numStr(p.feed_speed);
        sec.retract_speed  = numStr(p.retract_speed);
        sec.load_length    = numStr(p.load_length);
        sec.retract_length = numStr(p.retract_length);
        sec.swap_retract_length = numStr(p.swap_retract_length);
        for (let s = 0; s < 4; s++) {
          sec[`load_length_${s}`]    = numStr(p.perSlot[s].load_length);
          sec[`retract_length_${s}`] = numStr(p.perSlot[s].retract_length);
          sec[`swap_retract_length_${s}`] = numStr(p.perSlot[s].swap_retract_length);
        }
        perAceRepl[i] = sec;
      }
      const keyRegex = /^\s*#?\s*([A-Za-z_][A-Za-z0-9_]*)\s*:/;
      const sectionRegex = /^\s*\[(.+?)\]\s*$/;
      const out = [];
      let curSection = null;
      const sectionEnd = {};
      const seenInSection = {};
      const seenSet = (sec) => {
        const k = sec === 'ace' ? 'ace' : `ace${sec}`;
        if (!seenInSection[k]) seenInSection[k] = new Set();
        return seenInSection[k];
      };
      const closeSection = () => {
        if (curSection === null) return;
        const k = curSection === 'ace' ? 'ace' : `ace${curSection}`;
        sectionEnd[k] = out.length;
      };
      for (const raw of lines) {
        const sm = raw.match(sectionRegex);
        if (sm) {
          closeSection();
          const head = sm[1].trim();
          if (head === 'ace') {
            curSection = 'ace';
          } else if (head.startsWith('ace ') || head.startsWith('ace\t')) {
            const idx = parseInt(head.split(/\s+/, 2)[1], 10);
            curSection = isNaN(idx) ? null : idx;
          } else {
            curSection = null;
          }
          out.push(raw);
          continue;
        }
        if (curSection === 'ace') {
          const m = raw.match(keyRegex);
          if (m && (m[1] in mainRepl)) {
            const key = m[1];
            const val = mainRepl[key];
            seenSet('ace').add(key);
            if (val === '' || val == null) continue;
            out.push(`${key}: ${val}`);
            continue;
          }
        } else if (typeof curSection === 'number') {
          const sec = perAceRepl[curSection];
          if (sec) {
            const m = raw.match(keyRegex);
            if (m && (m[1] in sec)) {
              const key = m[1];
              const val = sec[key];
              seenSet(curSection).add(key);
              if (val === '' || val == null) continue;
              out.push(`${key}: ${val}`);
              continue;
            }
          }
        }
        out.push(raw);
      }
      closeSection();
      const insertMissing = (sectionLabel, repl, seen) => {
        const missing = Object.keys(repl)
          .filter(k => !seen.has(k))
          .filter(k => repl[k] !== '' && repl[k] != null);
        if (!missing.length) return;
        const sectionKey = sectionLabel === '[ace]' ? 'ace'
          : `ace${sectionLabel.match(/\[ace (\d+)\]/)[1]}`;
        const endIdx = sectionEnd[sectionKey];
        const block = missing.map(k => `${k}: ${repl[k]}`);
        if (endIdx != null) {
          out.splice(endIdx, 0, ...block);
          for (const k of Object.keys(sectionEnd)) {
            if (sectionEnd[k] > endIdx) sectionEnd[k] += block.length;
          }
        } else {
          out.push('', sectionLabel, ...block);
        }
      };
      insertMissing('[ace]', mainRepl, seenSet('ace'));
      for (let i = 0; i < configForm.perAce.length; i++) {
        insertMissing(`[ace ${i}]`, perAceRepl[i], seenSet(i));
      }
      const cleaned = [];
      for (let i = 0; i < out.length; i++) {
        const m = out[i].match(/^\s*\[ace\s+\d+\]\s*$/);
        if (!m) { cleaned.push(out[i]); continue; }
        let j = i + 1;
        let hasContent = false;
        while (j < out.length && !/^\s*\[.+\]\s*$/.test(out[j])) {
          const s = out[j].trim();
          if (s !== '' && !s.startsWith('#') && !s.startsWith(';')) {
            hasContent = true;
          }
          j++;
        }
        if (hasContent) {
          cleaned.push(out[i]);
          continue;
        }
        if (cleaned.length && cleaned[cleaned.length - 1].trim() === '') {
          cleaned.pop();
        }
        i = j - 1;
      }
      return cleaned.join('\n');
    }
    const updateState = reactive({
      current: "",
      latest: "",
      statusText: "",
      canApply: false,
      busy: null,
      log: "",
    });
    const debugState = reactive({
      enabled: false,
      busy: false,
      rebootPrompt: false,
    });
    async function refreshDebugState() {
      try {
        const r = await fetch(`${API}/debug-mode`);
        const j = await r.json();
        if (r.ok) debugState.enabled = !!j.enabled;
      } catch (e) {
      }
    }
    async function debugEnable() {
      if (debugState.busy) return;
      debugState.busy = true;
      try {
        const r = await fetch(`${API}/debug-mode/enable`, {method: "POST"});
        const j = await r.json();
        if (!r.ok) throw new Error(j.detail || `HTTP ${r.status}`);
        debugState.enabled = !!j.enabled;
        debugState.rebootPrompt = debugState.enabled;
      } catch (e) {
        setMacroLog(`${t("ui.config.debug_enable_failed")}: ${e.message || e}`);
      } finally {
        debugState.busy = false;
      }
    }
    async function debugDisable() {
      if (debugState.busy) return;
      confirm({
        title: t("ui.config.debug_disable_title"),
        message: t("ui.config.debug_disable_msg"),
        okLabel: t("ui.config.debug_disable_btn"),
        onOk: async () => {
          debugState.busy = true;
          try {
            const r = await fetch(`${API}/debug-mode/disable`, {method: "POST"});
            const j = await r.json();
            if (!r.ok) throw new Error(j.detail || `HTTP ${r.status}`);
            debugState.enabled = !!j.enabled;
            debugState.rebootPrompt = false;
          } catch (e) {
            setMacroLog(`${t("ui.config.debug_disable_failed")}: ${e.message || e}`);
          } finally {
            debugState.busy = false;
          }
        },
      });
    }
    function _parseUpdateResult(r) {
      const lines = r.status_lines || [];
      let cur = updateState.current, lat = updateState.latest;
      let canApply = false, statusText = "";
      for (const line of lines) {
        const mCur = line.match(/current=(\S+)/);
        if (mCur) cur = mCur[1];
        const mLat = line.match(/latest=(\S+)/);
        if (mLat) lat = mLat[1];
        const mTo = line.match(/to=(\S+)/);
        if (mTo) lat = mTo[1];
        if (line.startsWith("update_available")) canApply = true;
        if (line.startsWith("up_to_date") || line.startsWith("done")
            || line.startsWith("refusing_downgrade")) canApply = false;
        statusText = line;
      }
      updateState.current = cur || updateState.current;
      updateState.latest = lat || updateState.latest;
      updateState.canApply = canApply;
      updateState.statusText = statusText;
      updateState.log = r.stdout || "";
    }
    async function updateCheck() {
      if (updateState.busy) return;
      updateState.busy = "check";
      try {
        const r = await fetch(`${API}/update/check`);
        const j = await r.json();
        if (!r.ok) throw new Error(j.detail || `HTTP ${r.status}`);
        _parseUpdateResult(j);
      } catch (e) {
        updateState.statusText = `${t("ui.config.update_failed")}: ${e.message || e}`;
        setMacroLog(`${t("ui.config.update_failed")}: ${e.message || e}`);
      } finally {
        updateState.busy = "";
      }
    }
    async function updateApply() {
      if (updateState.busy) return;
      confirm({
        title: t("ui.config.update_apply_title"),
        message: t("ui.config.update_apply_msg", {
          from: updateState.current || "?",
          to:   updateState.latest  || "latest",
        }),
        okLabel: t("ui.config.update_apply_btn"),
        onOk: async () => {
          updateState.busy = "apply";
          try {
            const r = await fetch(`${API}/update/apply`, {method: "POST"});
            const j = await r.json();
            if (!r.ok) throw new Error(j.detail || `HTTP ${r.status}`);
            _parseUpdateResult(j);
            if (j.ok) {
              setMacroLog(t("ui.config.update_done"));
            }
          } catch (e) {
            updateState.statusText = `${t("ui.config.update_failed")}: ${e.message || e}`;
            setMacroLog(`${t("ui.config.update_failed")}: ${e.message || e}`);
          } finally {
            updateState.busy = "";
          }
        },
      });
    }
    async function loadConfig() {
      configLoadError.value = "";
      try {
        const r = await fetch(`${API}/config`);
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const j = await r.json();
        config.path = j.path || "";
        config.content = j.content || "";
        config.params = j.params || {};
        paramsToForm(j.params, j.per_ace_params || {});
      } catch (e) {
        configLoadError.value = t("ui.log.config_load_failed", {error: e});
      }
    }
    async function saveConfigForm() {
      configLog.value = t("ui.common.saving");
      const newContent = formToCfgContent(config.content);
      try {
        const r = await fetch(`${API}/config`, {
          method: "PUT",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({content: newContent, restart_klipper: true}),
        });
        if (!r.ok) throw new Error(`HTTP ${r.status} ${await r.text()}`);
        const j = await r.json();
        config.content = newContent;
        configLog.value = `✓ ${j.path}\nBackup: ${j.backup}\n${t("ui.log.klipper_restart_requested")}`;
      } catch (e) { configLog.value = `${t("ui.common.error")}: ${e}`; }
    }
    async function saveConfigRaw() {
      configLog.value = t("ui.log.saving_raw");
      try {
        const r = await fetch(`${API}/config`, {
          method: "PUT",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({content: config.content, restart_klipper: config.restartKlipper}),
        });
        if (!r.ok) throw new Error(`HTTP ${r.status} ${await r.text()}`);
        configLog.value = JSON.stringify(await r.json(), null, 2);
      } catch (e) { configLog.value = `${t("ui.common.error")}: ${e}`; }
    }
    async function setMode(m) {
      const headChanged = (m === "head" && state.ace_head !== aceHeadSel.value);
      if (state.mode === m && !headChanged) return;
      const cur = state.mode || "normal";
      if ((cur === "normal") !== (m === "normal")) {
        const loaded = (state.toolheads || []).filter(th => th.filament_at_extruder);
        if (loaded.length) {
          const heads = loaded.map(th => th.name || ("T" + th.idx)).join(", ");
          confirm({
            title: t("ui.config.mode_locked_title"),
            message: t("ui.config.mode_locked_msg", {heads}),
            okLabel: "OK",
            dismissOnly: true,
          });
          return;
        }
      }
      const args = (m === "head")
        ? {MODE: "head", HEAD: aceHeadSel.value}
        : {MODE: m};
      confirm({
        title: t("ui.dialog.switch_mode_title", {mode: m}),
        message: t("ui.dialog.switch_mode_msg", {mode: m}),
        okLabel: t("ui.dialog.switch"),
        onOk: async () => {
          const ok = await run("SET_ACE_MODE", args);
          if (ok) {
            modeChangePending.value = (m === "normal" || state.mode === "normal");
          }
        },
      });
    }
    const screenCanvas = ref(null);
    const floatScreenCanvas = ref(null);
    const screenPopout = ref(false);
    const screenFps = ref(0);
    const screenEtag = ref("");
    let frameCount = 0;
    let lastFpsTs = performance.now();
    let pollScreenBusy = false;
    function _liveScreenCanvases() {
      return [screenCanvas.value, floatScreenCanvas.value].filter(Boolean);
    }
    async function pollScreen() {
      if (pollScreenBusy) return;
      const targets = _liveScreenCanvases();
      if (!targets.length) return;
      pollScreenBusy = true;
      try {
        const headers = {};
        if (screenEtag.value) headers["If-None-Match"] = `"${screenEtag.value}"`;
        const r = await fetch(`${SCREEN}/snapshot`, {headers, cache: "no-store"});
        if (r.status === 304) {  }
        else if (r.ok) {
          screenEtag.value = (r.headers.get("ETag") || "").replace(/"/g, "");
          const blob = await r.blob();
          const img = await createImageBitmap(blob);
          for (const c of targets) {
            if (img.width !== c.width || img.height !== c.height) {
              c.width = img.width;
              c.height = img.height;
            }
            c.getContext("2d").drawImage(img, 0, 0);
          }
          frameCount += 1;
          const now = performance.now();
          if (now - lastFpsTs >= 1000) {
            screenFps.value = (frameCount * 1000) / (now - lastFpsTs);
            frameCount = 0;
            lastFpsTs = now;
          }
        }
      } catch (_) {  }
      finally { pollScreenBusy = false; }
    }
    function screenCoords(ev) {
      const c = ev.currentTarget;
      const rect = c.getBoundingClientRect();
      return {
        x: Math.round((ev.clientX - rect.left) * c.width / rect.width),
        y: Math.round((ev.clientY - rect.top) * c.height / rect.height),
      };
    }
    async function sendTouch(action, x, y) {
      try { await fetch(`${SCREEN}/touch?a=${action}&x=${x}&y=${y}`, {method: "POST"}); } catch (_) {}
    }
    function screenDown(ev) {
      ev.currentTarget?.setPointerCapture?.(ev.pointerId);
      const {x, y} = screenCoords(ev); sendTouch("down", x, y);
    }
    function screenMove(ev) {
      if (ev.buttons === 0) return;
      const {x, y} = screenCoords(ev); sendTouch("move", x, y);
    }
    function screenUp(ev) {
      const {x, y} = screenCoords(ev); sendTouch("up", x, y);
    }
    function toggleScreenPopout() {
      screenPopout.value = !screenPopout.value;
    }
    const popoutPos = reactive({
      x: parseFloat(localStorage.getItem("multiace.popout.x")) || null,
      y: parseFloat(localStorage.getItem("multiace.popout.y")) || null,
    });
    const popoutStyle = computed(() => {
      if (popoutPos.x == null || popoutPos.y == null) return {};
      return {
        left: popoutPos.x + "px",
        top:  popoutPos.y + "px",
        right: "auto",
        bottom: "auto",
      };
    });
    let _popoutDrag = null;
    function popoutDragStart(ev) {
      if (ev.target.closest(".screen-popout-close")) return;
      const panel = ev.currentTarget.parentElement;
      const rect = panel.getBoundingClientRect();
      _popoutDrag = {
        offX: ev.clientX - rect.left,
        offY: ev.clientY - rect.top,
        panel,
      };
      ev.currentTarget.setPointerCapture?.(ev.pointerId);
      ev.preventDefault();
    }
    function popoutDragMove(ev) {
      if (!_popoutDrag) return;
      const p = _popoutDrag;
      const w = p.panel.offsetWidth;
      const h = p.panel.offsetHeight;
      const maxX = window.innerWidth - w;
      const maxY = window.innerHeight - h;
      popoutPos.x = Math.max(0, Math.min(maxX, ev.clientX - p.offX));
      popoutPos.y = Math.max(0, Math.min(maxY, ev.clientY - p.offY));
    }
    function popoutDragEnd(ev) {
      if (!_popoutDrag) return;
      _popoutDrag = null;
      ev.currentTarget?.releasePointerCapture?.(ev.pointerId);
      if (popoutPos.x != null) localStorage.setItem("multiace.popout.x", String(popoutPos.x));
      if (popoutPos.y != null) localStorage.setItem("multiace.popout.y", String(popoutPos.y));
    }
    let ws = null;
    let wsReconnectTimer = null;
    function wsConnect() {
      try { ws = new WebSocket(WS_URL); }
      catch (e) { conn.value = {state: "err", text: `WS: ${e}`}; scheduleReconnect(); return; }
      ws.onopen = () => { conn.value = {state: "ok", text: t("ui.header.live")}; };
      ws.onmessage = (ev) => {
        try {
          const m = JSON.parse(ev.data);
          if (m.type === "state") applyState(m);
          else if (m.type === "gcode_error") onGcodeError(m);
          else if (m.type === "error") conn.value = {state: "warn", text: m.error || t("ui.header.ws_error")};
        } catch (_) {}
      };
      ws.onclose = () => { conn.value = {state: "warn", text: t("ui.header.offline")}; scheduleReconnect(); };
      ws.onerror = () => { conn.value = {state: "err", text: t("ui.header.ws_error")}; };
    }
    function scheduleReconnect() {
      clearTimeout(wsReconnectTimer);
      wsReconnectTimer = setTimeout(wsConnect, 3000);
    }
    let screenTimer = null;
    function _updateScreenTimer() {
      clearInterval(screenTimer);
      const wantPoll = screenAvailable.value && screenPopout.value;
      if (wantPoll) screenTimer = setInterval(pollScreen, 200);
    }
    watch([screenPopout, screenAvailable], _updateScreenTimer, {immediate: true});
    const uploading = ref(false);
    const uploadInput = ref(null);
    const preflight = reactive({
      open:    false,
      busy:    false,
      sending: "",
      report:  null,
      error:   "",
      progress: null,
      slicerOverrides: {},
      slicerSwaps: null,
      slicerDirty: false,
      headOverrides: {},
      headSwaps: null,
      headDirty: false,
    });
    function triggerUpload() { uploadInput.value && uploadInput.value.click(); }
    function tierLabel(tier) {
      const t_map = {
        exact_hex:        "exact",
        name_exact:       "name",
        name_base:        "name·base",
        name_canon:       "name·synonym",
        fuzzy:            "fuzzy",
        fallback:         "fallback ⚠",
        duplicate:        "duplicate ⚠",
        no_slot:          "no slot ⚠",
      };
      return t_map[tier] || tier;
    }
    function tierWarn(tier) {
      return tier && (tier === "fallback"
                      || tier === "duplicate"
                      || tier === "no_slot");
    }
    function rgbDec(hex) {
      const s = (hex || "").replace(/^#/, "");
      if (s.length < 6) return "";
      const r = parseInt(s.slice(0, 2), 16);
      const g = parseInt(s.slice(2, 4), 16);
      const b = parseInt(s.slice(4, 6), 16);
      return `${r},${g},${b}`;
    }
    function sortedMapping(plan) {
      const rows = (plan && plan.mapping) || [];
      return rows.slice().sort((a, b) => {
        const sa = a.slot, sb = b.slot;
        if (!sa && !sb) return a.t - b.t;
        if (!sa) return  1;
        if (!sb) return -1;
        if (sa.ace !== sb.ace)   return sa.ace  - sb.ace;
        if (sa.slot !== sb.slot) return sa.slot - sb.slot;
        return a.t - b.t;
      });
    }
    function slotKey(slot) { return slot ? (slot.ace + "-" + slot.slot) : ""; }
    function textOn(hex) {
      const s = (hex || "").replace(/^#/, "");
      if (s.length < 6) return "#e8e8e8";
      const r = parseInt(s.slice(0, 2), 16);
      const g = parseInt(s.slice(2, 4), 16);
      const b = parseInt(s.slice(4, 6), 16);
      return (0.299 * r + 0.587 * g + 0.114 * b) > 150 ? "#111" : "#fff";
    }
    function _liveSlotByKey(key) {
      const [a, s] = (key || "").split("-").map(Number);
      return (preflight.report?.live_slots || [])
        .find(ls => ls.ace === a && ls.slot === s) || null;
    }
    function _slicerColorMat(tt) {
      const c = (preflight.report?.slicer_colors || []).find(x => x.t === tt);
      return ((c && c.material) || "").trim().toLowerCase();
    }
    function slicerSlotOptions(tt) {
      const mat = _slicerColorMat(tt);
      return (preflight.report?.live_slots || []).filter(ls => {
        const m = (ls.material || "").trim().toLowerCase();
        return !mat || !m || m === mat;
      });
    }
    function _slicerBaseSlot(tt) {
      const plan = preflight.report && preflight.report.plans
                 && preflight.report.plans.slicer;
      const row = ((plan && plan.mapping) || []).find(m => m.t === tt);
      return (row && row.slot) || null;
    }
    function slicerEffectiveSlot(tt) {
      const ov = preflight.slicerOverrides[tt];
      return ov ? _liveSlotByKey(ov) : _slicerBaseSlot(tt);
    }
    function onSlicerSlotChange(tt, key) {
      const base = _slicerBaseSlot(tt);
      if (base && slotKey(base) === key) delete preflight.slicerOverrides[tt];
      else preflight.slicerOverrides[tt] = key;
      preflight.slicerDirty = true;
    }
    function _slicerEffectiveMapping() {
      const plan = preflight.report && preflight.report.plans
                 && preflight.report.plans.slicer;
      return ((plan && plan.mapping) || [])
        .map(m => ({t: m.t, slot: slicerEffectiveSlot(m.t)}));
    }
    function realSwapCount(events, mapping) {
      const byT = {};
      for (const m of mapping) if (m.slot) byT[m.t] = m.slot;
      const head = {0: [0, 0], 1: [0, 1], 2: [0, 2], 3: [0, 3]};
      let swaps = 0;
      for (const tt of (events || [])) {
        const slot = byT[tt];
        if (!slot) continue;
        const cur = head[slot.slot];
        if (!cur || cur[0] !== slot.ace || cur[1] !== slot.slot) {
          swaps++;
          head[slot.slot] = [slot.ace, slot.slot];
        }
      }
      return swaps;
    }
    function recalcSlicer() {
      preflight.slicerSwaps = realSwapCount(
        (preflight.report && preflight.report.events) || [],
        _slicerEffectiveMapping());
      preflight.slicerDirty = false;
    }
    function slicerSwapsDisplay() {
      if (preflight.slicerSwaps !== null) return preflight.slicerSwaps;
      const plan = preflight.report && preflight.report.plans
                 && preflight.report.plans.slicer;
      return (plan && plan.swaps) || 0;
    }
    function headTargets() {
      return (preflight.report && preflight.report.targets) || [];
    }
    function _headTargetById(id) {
      return headTargets().find(tg => tg.id === id) || null;
    }
    function headTargetOptions(tt) {
      const mat = _slicerColorMat(tt);
      return headTargets().filter(tg => {
        const m = (tg.material || "").trim().toLowerCase();
        return !mat || !m || m === mat;
      });
    }
    function _headBaseTargetId(tt) {
      const plan = preflight.report && preflight.report.plans
                 && preflight.report.plans.loadout;
      const row = ((plan && plan.mapping) || []).find(m => m.t === tt);
      return (row && row.target_id) || "";
    }
    function headEffectiveTargetId(tt) {
      const ov = preflight.headOverrides[tt];
      return ov !== undefined ? ov : _headBaseTargetId(tt);
    }
    function headTargetLabel(tg) {
      if (!tg) return "";
      const mat = tg.material || "?";
      if (tg.kind === "pin") return t("ui.preflight.feeder") + " " + dispIdx(tg.head) + " · " + mat;
      return "ACE " + dispIdx(tg.ace) + " Slot " + dispIdx(tg.slot) + " · " + mat;
    }
    function headTargetColor(id) {
      const tg = _headTargetById(id);
      return (tg && tg.color) || "#444";
    }
    function onHeadTargetChange(tt, id) {
      const base = _headBaseTargetId(tt);
      if (id === base) delete preflight.headOverrides[tt];
      else preflight.headOverrides[tt] = id;
      preflight.headDirty = true;
    }
    function _headEffectiveAssignment() {
      const out = {};
      for (const c of ((preflight.report && preflight.report.slicer_colors) || [])) {
        out[c.t] = headEffectiveTargetId(c.t);
      }
      return out;
    }
    function headSwapCount(events, assignment) {
      let cur = null, swaps = 0;
      for (const tt of (events || [])) {
        const tg = _headTargetById(assignment[tt]);
        if (!tg || tg.kind !== "ace") continue;
        const key = tg.ace + "-" + tg.slot;
        if (cur !== key) { swaps++; cur = key; }
      }
      return swaps;
    }
    function recalcHead() {
      preflight.headSwaps = headSwapCount(
        (preflight.report && preflight.report.events) || [],
        _headEffectiveAssignment());
      preflight.headDirty = false;
    }
    function headSwapsDisplay() {
      if (preflight.headSwaps !== null) return preflight.headSwaps;
      const plan = preflight.report && preflight.report.plans
                 && preflight.report.plans.loadout;
      return (plan && plan.swaps) || 0;
    }
    function headFeasible() {
      const asn = _headEffectiveAssignment();
      return Object.values(asn).every(id => !!_headTargetById(id));
    }
    function headPlanFeasible(hp) {
      if (hp === "loadout") return headFeasible();
      const p = preflight.report && preflight.report.plans
              && preflight.report.plans[hp];
      return !!(p && p.feasible);
    }
    function headPlanSwaps(hp) {
      if (hp === "loadout") return headSwapsDisplay();
      const p = preflight.report && preflight.report.plans
              && preflight.report.plans[hp];
      return (p && p.swaps) || 0;
    }
    function _headSlicerColor(tt) {
      return ((preflight.report && preflight.report.slicer_colors) || [])
        .find(c => c.t === tt) || null;
    }
    function headSlicerHex(tt) {
      const c = _headSlicerColor(tt);
      return (c && c.hex) || "#444";
    }
    function headSlicerMat(tt) {
      const c = _headSlicerColor(tt);
      return (c && c.material) || "?";
    }
    function headProposalLabel(m) {
      if (!m || m.kind === "none") return "";
      if (m.kind === "pin") return t("ui.preflight.feeder") + " " + dispIdx(m.head);
      return "ACE " + dispIdx(m.ace) + " Slot " + dispIdx(m.slot);
    }
    function onUploadGcode(fileList) {
      const f = fileList && fileList[0];
      if (uploadInput.value) uploadInput.value.value = "";
      if (!f) return;
      const lower = f.name.toLowerCase();
      if (!(lower.endsWith(".gcode") || lower.endsWith(".gco") || lower.endsWith(".g"))) {
        confirm({
          title: t("ui.upload.title"),
          message: t("ui.upload.bad_ext"),
          dismissOnly: true, okLabel: "OK", onOk: () => {},
        });
        return;
      }
      if (state.toolheads.some(th => th.manual)) {
        confirm({
          title: t("ui.upload.title"),
          message: t("ui.preflight.manual_disabled"),
          dismissOnly: true, okLabel: "OK", onOk: () => {},
        });
        return;
      }
      _runPreflight(f);
    }
    async function _runPreflight(f) {
      preflight.open    = true;
      preflight.busy    = true;
      preflight.sending = "";
      preflight.report  = null;
      preflight.error   = "";
      uploading.value   = true;
      try {
        const fd = new FormData();
        fd.append("file", f, f.name);
        const r = await fetch(`${API}/preflight`, {method: "POST", body: fd});
        if (!r.ok) {
          let msg = `${r.status} ${r.statusText}`;
          try { const j = await r.json(); if (j.detail) msg = j.detail; } catch (_) {}
          throw new Error(msg);
        }
        preflight.report = await r.json();
        preflight.slicerOverrides = {};
        preflight.slicerSwaps = null;
        preflight.slicerDirty = false;
        preflight.headOverrides = {};
        preflight.headSwaps = null;
        preflight.headDirty = false;
      } catch (e) {
        preflight.error = e.message || String(e);
      } finally {
        uploading.value = false;
        preflight.busy  = false;
      }
    }
    function closePreflight() {
      preflight.open    = false;
      preflight.report  = null;
      preflight.error   = "";
      preflight.sending = "";
    }
    function stageLabel(stage) {
      const map = {
        queued:            t("ui.preflight.stage_queued"),
        analyze:           t("ui.preflight.stage_analyze"),
        apply_remap:       t("ui.preflight.stage_apply_remap"),
        optimize:          t("ui.preflight.stage_optimize"),
        layer:             t("ui.preflight.stage_layer"),
        print_prefs:       t("ui.preflight.stage_print_prefs"),
        rewrite:           t("ui.preflight.stage_rewrite"),
        inject_auto_load:  t("ui.preflight.stage_inject_auto_load"),
        upload:            t("ui.preflight.stage_upload"),
        done:              t("ui.preflight.stage_done"),
      };
      return map[stage] || stage || "";
    }
    async function startPreflightPrint(mode, headPlan) {
      if (preflight.busy || preflight.sending) return;
      const rep = preflight.report;
      if (!rep || !rep.token) return;
      preflight.sending = (mode === "head") ? (headPlan || "loadout") : mode;
      preflight.error   = "";
      preflight.progress = {percent: 0, stage: "queued", running: true};
      const startedAt = Date.now();
      const MIN_VISIBLE_MS = 1500;
      const FIRST_POLL_MS  = 250;
      const POLL_MS        = 500;
      try {
        const body = {token: rep.token, mode};
        if (mode === "slicer") {
          const remap = {};
          for (const m of _slicerEffectiveMapping()) {
            if (!m.slot) continue;
            const synth = m.slot.ace * 4 + m.slot.slot;
            if (synth !== m.t) remap[String(m.t)] = synth;
          }
          body.remap = remap;
        } else if (mode === "head") {
          const hp = headPlan || "loadout";
          body.head_plan = hp;
          if (hp === "loadout") {
            const asn = {};
            const eff = _headEffectiveAssignment();
            for (const k of Object.keys(eff)) {
              if (eff[k]) asn[String(k)] = eff[k];
            }
            body.head_assignment = asn;
          }
        }
        const r = await fetch(`${API}/preflight/print`, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify(body),
        });
        const j = await r.json().catch(() => ({}));
        if (!r.ok) {
          throw new Error(j.detail || `${r.status} ${r.statusText}`);
        }
        const jobId = j.job_id;
        let last;
        let pollDelay = FIRST_POLL_MS;
        for (;;) {
          await new Promise(res => setTimeout(res, pollDelay));
          pollDelay = POLL_MS;
          let sr;
          try {
            sr = await fetch(`${API}/preflight/print/status?job_id=${encodeURIComponent(jobId)}`);
          } catch (_) {
            continue;
          }
          if (!sr.ok) {
            const sj = await sr.json().catch(() => ({}));
            throw new Error(sj.detail || `${sr.status} ${sr.statusText}`);
          }
          last = await sr.json();
          preflight.progress = {
            percent: Number(last.percent || 0),
            stage:   String(last.stage || ""),
            running: !last.done,
          };
          if (last.done) break;
        }
        if (last.error) throw new Error(last.error);
        preflight.progress = {percent: 100, stage: "done", running: true};
        const elapsed = Date.now() - startedAt;
        const wait = Math.max(0, MIN_VISIBLE_MS - elapsed);
        if (wait > 0) {
          await new Promise(res => setTimeout(res, wait));
        }
        setMacroLog(t("ui.upload.started", {name: rep.filename}));
        closePreflight();
      } catch (e) {
        preflight.error = e.message || String(e);
      } finally {
        preflight.sending = "";
        if (preflight.progress) preflight.progress.running = false;
      }
    }
    let resizeObserver = null;
    onMounted(async () => {
      await loadLanguageList();
      if (!localStorage.getItem("multiace.lang")) {
        try {
          const r = await fetch(`${API}/state`);
          if (r.ok) {
            const s = await r.json();
            if (s && s.language) language.value = s.language;
          }
        } catch (_) {}
      }
      await loadCatalog(language.value);
      try {
        const r = await fetch(`${API}/version`);
        if (r.ok) {
          const j = await r.json();
          version.value = `v${j.web}`;
          const p = j.printer || {};
          printerName.value = p.device_name || "";
          printerFw.value   = p.firmware_version || "";
        }
      } catch (_) {}
      try { const r = await fetch(`${API}/screen-available`); if (r.ok) screenAvailable.value = (await r.json()).available; } catch (_) {}
      await reloadState();
      await reloadSnapshots();
      await loadConfig();
      await loadMaterials();
      await loadNotifications();
      await refreshDebugState();
      await refreshPlugins();
      if (state.mode === "normal" && tab.value === "dashboard") tab.value = "config";
      wsConnect();
      if (window.ResizeObserver && wiringContainerEl.value) {
        resizeObserver = new ResizeObserver(() => recomputeWiring());
        resizeObserver.observe(wiringContainerEl.value);
      } else {
        window.addEventListener("resize", recomputeWiring);
      }
      scheduleWiringRecompute();
      window.addEventListener("beforeunload", _onBeforeUnload);
    });
    function _onBeforeUnload(ev) {
      const pending = cmdQueue.value.some(
        it => it.status === 'queued' || it.status === 'running');
      if (!pending) return;
      ev.preventDefault();
      ev.returnValue = '';
      return '';
    }
    onUnmounted(() => {
      clearTimeout(wsReconnectTimer);
      clearInterval(screenTimer);
      try { ws?.close(); } catch (_) {}
      try { resizeObserver?.disconnect(); } catch (_) {}
      window.removeEventListener("resize", recomputeWiring);
      window.removeEventListener("beforeunload", _onBeforeUnload);
    });
    return {
      subText,
      sourceLabel,
      tab, version, printerName, printerFw, connClass, connText, screenAvailable,
      state, loadError, run, macroLog,
      slotTitle, switchAce, loadSlot, loadFeederHead, slotLoadedInHead, loadAll, unloadHead, setHeadManual, isToolheadOccupied, needsReload, toolheadOps,
      dryerCfg, dryStart, dryStop, dryOpenAce, toggleDryPanel, aceDrying,
      snapshots, selectedSnapshot, snapshotPreview, saveSnapshot, loadSnapshot, deleteSnapshot,
      config, configLog, configLoadError, showRawConfig, configForm, modeChangePending,
      aceHeadSel,
      loadConfig, saveConfigForm, saveConfigRaw, setMode,
      preflight, closePreflight, startPreflightPrint, stageLabel,
      tierLabel, tierWarn, rgbDec, sortedMapping,
      slotKey, textOn, slicerSlotOptions, slicerEffectiveSlot, onSlicerSlotChange,
      recalcSlicer, slicerSwapsDisplay,
      headTargets, headTargetOptions, headEffectiveTargetId, headTargetLabel,
      headTargetColor, onHeadTargetChange, recalcHead, headSwapsDisplay,
      headFeasible, headPlanFeasible, headPlanSwaps, headSlicerHex,
      headSlicerMat, headProposalLabel,
      updateState, updateCheck, updateApply,
      debugState, debugEnable, debugDisable,
      plugins, refreshPlugins, pluginIframeSrc,
      notifications, dismissNotification, dismissAllNotifications,
      confirmDialog, okConfirm, altConfirm, cancelConfirm,
      screenCanvas, floatScreenCanvas, screenPopout, toggleScreenPopout,
      popoutStyle, popoutDragStart, popoutDragMove, popoutDragEnd,
      screenFps, screenEtag,
      screenDown, screenMove, screenUp,
      wiringContainerEl, setSlotEl, setThEl, wiringPaths, wiringViewBox,
      t, dispIdx, language, languages, setLanguage,
      picker, openPicker, closePicker, savePicker, clearPickerOverride, pickerMaterials,
      pickerDb, pickerVendors, currentSubtypes,
      pickerHasRfid, pickerHasOverride, pickerRfidStyle, readPickerRfid,
      cmdQueue, visibleQueue, cmdPaused, removeFromQueue, pauseQueue, resumeQueue, clearAllErrors,
      sendingAll, sendAllToPrinter,
      fmtArgs, cmdLabel,
      uploading, uploadInput, triggerUpload, onUploadGcode,
    };
  },
}).mount("#app");
