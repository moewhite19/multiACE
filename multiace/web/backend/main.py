from __future__ import annotations
import asyncio
import json
import logging
import os
import re
import time
from collections import deque
from pathlib import Path
from typing import Any
import websockets
_trace = logging.getLogger('multiace')
_trace.setLevel(logging.INFO)
if not _trace.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter('[multiace] %(message)s'))
    _trace.addHandler(_h)
    _trace.propagate = False
import httpx
from fastapi import FastAPI, File, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
MOONRAKER_URL = os.environ.get('MOONRAKER_URL', 'http://127.0.0.1:7125')
MULTIACE_CFG_PATH = os.environ.get('MULTIACE_CFG_PATH', '/home/lava/printer_data/config/extended/ace.cfg')
SNAPSHOT_DIR = os.environ.get('MULTIACE_SNAPSHOT_DIR', '/home/lava/printer_data/config/extended/multiace/filament_snapshots')
OVERRIDE_FILE = os.environ.get('MULTIACE_OVERRIDE_FILE', '/home/lava/printer_data/config/extended/multiace/slot_overrides.json')
I18N_DIR = os.environ.get('MULTIACE_I18N_DIR', str(Path(__file__).resolve().parent.parent / 'i18n'))
SCREEN_PROBE_URL = os.environ.get('SCREEN_PROBE_URL', 'http://127.0.0.1:8092/snapshot')
DEFAULT_FRONTEND = str(Path(__file__).resolve().parent.parent / 'frontend')
FRONTEND_DIR = os.environ.get('MULTIACE_FRONTEND_DIR', DEFAULT_FRONTEND)
VERSION = os.environ.get('MULTIACE_WEB_VERSION', '0.2.0')
ACE_OBJECTS = ['ace', 'filament_feed left', 'filament_feed right', 'save_variables', 'print_task_config', 'print_stats', 'idle_timeout']

def _slot_state_name(v: Any) -> str:
    if v is None:
        return 'unknown'
    return {0: 'empty', 1: 'ready', 2: 'loading', 3: 'unloading', 4: 'error', 5: 'feeding', 6: 'assist'}.get(v, str(v))

def _resolve_head_source(src: Any) -> tuple[int | None, int | None]:
    if src is None:
        return (None, None)
    if isinstance(src, int):
        return (None, src)
    if isinstance(src, (list, tuple)) and len(src) >= 2:
        return (src[0], src[1])
    if isinstance(src, dict):
        d = src['ace_index'] if 'ace_index' in src else src.get('device')
        return (d, src.get('slot'))
    return (None, None)

def _color_to_hex(c: Any) -> str | None:
    if not isinstance(c, (list, tuple)) or len(c) < 3:
        return None
    r, g, b = (int(c[0]), int(c[1]), int(c[2]))
    if r == 0 and g == 0 and (b == 0):
        return None
    return f'#{r:02x}{g:02x}{b:02x}'

def _parse_state(status: dict) -> dict:
    _reload_overrides_if_changed()
    ace = status.get('ace', {}) or {}
    fl = status.get('filament_feed left', {}) or {}
    fr = status.get('filament_feed right', {}) or {}
    device_count = int(ace.get('device_count', 1))
    active_device = int(ace.get('active_device', 0))
    head_source = ace.get('head_source', {}) or {}
    raw_aces = ace.get('aces', []) or []
    ptc = status.get('print_task_config', {}) or {}
    ptc_types = ptc.get('filament_type', []) or []
    ptc_subs = ptc.get('filament_sub_type', []) or []
    ptc_vendors = ptc.get('filament_vendor', []) or []
    ptc_rgbas = ptc.get('filament_color_rgba', []) or []

    def _ptc_at(n: int) -> dict | None:
        if not (n < len(ptc_types) and n < len(ptc_rgbas)):
            return None
        mat = (ptc_types[n] or '').strip()
        rgba = (ptc_rgbas[n] or '').strip()
        if not mat and (not rgba):
            return None
        if mat in ('', 'NONE') and rgba in ('', '00000000', '000000FF'):
            return None
        color_hex = None
        if rgba and len(rgba) >= 6 and (rgba.upper() != '00000000'):
            color_hex = '#' + rgba[:6].lower()
        sub = (ptc_subs[n] or '').strip() if n < len(ptc_subs) else ''
        vendor = (ptc_vendors[n] or '').strip() if n < len(ptc_vendors) else ''
        return {'material': mat if mat != 'NONE' else '', 'sku': sub, 'brand': vendor if vendor != 'NONE' else '', 'color': color_hex}
    SLOT_COUNT = 4
    by_idx = {a.get('idx', n): a for n, a in enumerate(raw_aces) if isinstance(a, dict)}

    def _head_in_op(t: int) -> bool:
        feed = (fl if t < 2 else fr).get(f'extruder{t}' if t > 0 else 'extruder0', {}) or {}
        cs = feed.get('channel_state') or ''
        if cs and (not (cs.endswith('_finish') or cs.endswith('_fail') or cs in ('wait_insert', 'inited', 'test'))):
            if cs.startswith('load_') or cs.startswith('unload_') or cs.startswith('preload_') or cs.startswith('manual_sta_'):
                return True
        src = head_source.get(str(t)) or head_source.get(t)
        if isinstance(src, dict):
            stype = (src.get('type') or '').strip()
            scol = (src.get('color') or '').strip().lstrip('#').upper()
            if not stype or scol in ('', '000000', '00000000'):
                return True
        return False
    loaded_by_source: dict[tuple[int, int], int] = {}
    for t_key, src in (head_source or {}).items():
        d_l, sl_l = _resolve_head_source(src)
        if d_l is None or sl_l is None:
            continue
        try:
            t_idx = int(t_key)
        except (TypeError, ValueError):
            continue
        if _head_in_op(t_idx):
            continue
        loaded_by_source[int(d_l), int(sl_l)] = t_idx
    aces_out: list[dict] = []
    overrides_dirty = False
    for i in range(device_count):
        a = by_idx.get(i, {})
        gate_status = a.get('gate_status') or (ace.get('gate_status', []) if i == active_device else [])
        ace_slots = a.get('slots', []) or []
        slots_by_idx = {s.get('index', n): s for n, s in enumerate(ace_slots)}
        slots_out = []
        for s in range(SLOT_COUNT):
            sd = slots_by_idx.get(s, {}) or {}
            gate = gate_status[s] if s < len(gate_status) else None
            raw_status = sd.get('status', '') or ''
            is_empty = gate == 0 or raw_status.startswith('empty') or (raw_status == '' and gate is None)
            if gate == 0:
                _now = time.time()
                _pending = _eject_pending_since.get((i, s))
                if _pending is None:
                    _eject_pending_since[i, s] = _now
                elif _now - _pending >= EJECT_DEBOUNCE_S:
                    if _drop_override_if_present(i, s):
                        overrides_dirty = True
                    _eject_pending_since.pop((i, s), None)
            else:
                _eject_pending_since.pop((i, s), None)
            override = _override_for(i, s)
            loaded_t = loaded_by_source.get((i, s))
            if override is not None:
                ptc_overlay = {'material': override.get('material', ''), 'sku': override.get('subtype', ''), 'brand': override.get('brand', ''), 'color': override.get('color') or None}
            elif loaded_t is not None:
                ptc_overlay = _ptc_at(loaded_t)
            else:
                ptc_overlay = None
            rfid_status = sd.get('rfid', 0)
            rfid_data = None
            if rfid_status == 2:
                rfid_data = {'material': sd.get('material', '') or sd.get('type', ''), 'brand': sd.get('brand', ''), 'sku': sd.get('sku', ''), 'color': _color_to_hex(sd.get('color'))}
            if is_empty and ptc_overlay is None:
                slots_out.append({'idx': s, 'state': 'empty', 'raw': gate, 'status': raw_status, 'rfid': 0, 'material': '', 'brand': '', 'sku': '', 'color': None, 'color_rgb': None, 'rfid_data': rfid_data})
            elif ptc_overlay is not None:
                slots_out.append({'idx': s, 'state': 'ready' if not is_empty else 'empty', 'raw': gate, 'status': raw_status, 'rfid': rfid_status, 'material': ptc_overlay['material'], 'brand': ptc_overlay['brand'], 'sku': ptc_overlay['sku'], 'color': ptc_overlay['color'], 'color_rgb': None, 'rfid_data': rfid_data})
            else:
                slots_out.append({'idx': s, 'state': _slot_state_name(gate), 'raw': gate, 'status': raw_status, 'rfid': rfid_status, 'material': sd.get('material', '') or sd.get('type', ''), 'brand': sd.get('brand', ''), 'sku': sd.get('sku', ''), 'color': _color_to_hex(sd.get('color')), 'color_rgb': sd.get('color'), 'rfid_data': rfid_data})
        aces_out.append({'idx': i, 'connected': a.get('connected'), 'protocol': a.get('protocol', ''), 'status': a.get('status'), 'temp': a.get('temp'), 'dryer': a.get('dryer_status') or {}, 'feed_assist': a.get('feed_assist', -1), 'slots': slots_out})
    if overrides_dirty:
        _save_overrides_to_disk()
    toolheads = []
    wiring = []
    for t in range(4):
        ext_key = f'extruder{t}' if t > 0 else 'extruder0'
        feed = (fl if t < 2 else fr).get(ext_key, {}) or {}
        d_explicit, sl_explicit = _resolve_head_source(head_source.get(str(t)) or head_source.get(t))
        loaded = bool(feed.get('filament_detected'))
        color = None
        material = ''
        ace_field = None
        slot_field = None
        if d_explicit is not None and sl_explicit is not None:
            ace_field = d_explicit
            slot_field = sl_explicit
            if 0 <= d_explicit < len(aces_out):
                slots_arr = aces_out[d_explicit]['slots']
                if 0 <= sl_explicit < len(slots_arr):
                    slot_obj = slots_arr[sl_explicit]
                    color = slot_obj.get('color')
                    material = slot_obj.get('material', '')
        toolheads.append({'idx': t, 'name': f'T{t}', 'ace': ace_field, 'slot': slot_field, 'filament_detected': feed.get('filament_detected'), 'filament_in_ace': feed.get('filament_in_ace'), 'filament_in_toolhead': feed.get('filament_in_toolhead'), 'filament_at_extruder': feed.get('filament_at_extruder'), 'channel_state': feed.get('channel_state'), 'channel_error': feed.get('channel_error'), 'module_exist': feed.get('module_exist'), 'color': color, 'material': material, 'head_source_known': d_explicit is not None})
        if d_explicit is not None and sl_explicit is not None:
            wiring.append({'ace': d_explicit, 'slot': sl_explicit, 'toolhead': t, 'color': color, 'material': material})
    sv = status.get('save_variables', {})
    sv_vars = sv.get('variables', {}) if isinstance(sv, dict) else {}
    mode = sv_vars.get('ace__mode', 'normal')
    ps = status.get('print_stats', {}) or {}
    it = status.get('idle_timeout', {}) or {}
    ps_state = (ps.get('state') or '').lower()
    if ps_state in ('printing', 'paused', 'complete', 'error'):
        printer_state = ps_state
    else:
        raw_it = (it.get('state') or 'Idle').lower()
        printer_state = 'busy' if raw_it == 'printing' else raw_it
    language = sv_vars.get('ace__language', os.environ.get('MULTIACE_LANGUAGE', 'en'))
    try:
        idx_base = int(sv_vars.get('ace__display_index_base', os.environ.get('MULTIACE_DISPLAY_INDEX_BASE', '0')))
    except (TypeError, ValueError):
        idx_base = 0
    return {'ace_status': ace.get('status'), 'ace_temp': ace.get('temp'), 'printer_state': printer_state, 'active_device': active_device, 'device_count': device_count, 'mode': mode, 'language': language, 'display_index_base': idx_base, 'dryer': ace.get('dryer_status'), 'aces': aces_out, 'toolheads': toolheads, 'wiring': wiring, 'save_variables': sv_vars}

async def _query_state() -> dict:
    qs = '&'.join((o.replace(' ', '%20') for o in ACE_OBJECTS))
    data = await _mr_get(f'/printer/objects/query?{qs}')
    return data.get('result', {}).get('status', {})
app = FastAPI(title='multiACE Web', version=VERSION)

class MacroRequest(BaseModel):
    name: str
    args: dict[str, Any] | None = None

class ConfigUpdate(BaseModel):
    content: str
    restart_klipper: bool = False

class SnapshotSave(BaseModel):
    name: str
    description: str | None = None

class SlotOverride(BaseModel):
    ace: int
    slot: int
    material: str | None = ''
    brand: str | None = ''
    subtype: str | None = ''
    color: str | None = ''

async def _mr_get(path: str) -> dict:
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(f'{MOONRAKER_URL}{path}')
        r.raise_for_status()
        return r.json()

async def _mr_post(path: str, body: dict | None=None, timeout: float=30.0) -> dict:
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.post(f'{MOONRAKER_URL}{path}', json=body or {})
        r.raise_for_status()
        return r.json()

@app.get('/api/health')
async def health() -> dict:
    return {'status': 'ok', 'version': VERSION, 'ts': time.time()}

@app.get('/api/version')
async def version() -> dict:
    return {'web': VERSION, 'moonraker_url': MOONRAKER_URL, 'config_path': MULTIACE_CFG_PATH, 'frontend_dir': FRONTEND_DIR}

@app.post('/api/upload-and-print')
async def upload_and_print(file: UploadFile = File(...)) -> dict:
    raw_name = file.filename or ''
    safe_name = os.path.basename(raw_name)
    if not safe_name or safe_name in ('.', '..') or '/' in safe_name or '\\' in safe_name:
        raise HTTPException(status_code=400, detail='invalid filename')
    if not safe_name.lower().endswith(('.gcode', '.gco', '.g')):
        raise HTTPException(status_code=400, detail='not a g-code file')
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail='empty file')
    files = {'file': (safe_name, data, file.content_type or 'application/octet-stream')}
    payload = {'root': 'gcodes', 'print': 'true'}
    try:
        async with httpx.AsyncClient(timeout=300.0) as client:
            r = await client.post(f'{MOONRAKER_URL}/server/files/upload', data=payload, files=files)
            r.raise_for_status()
            return {'ok': True, 'filename': safe_name, 'moonraker': r.json()}
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code, detail=f'moonraker: {e.response.text}')
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f'moonraker: {e}')

@app.get('/api/state')
async def get_state() -> dict:
    try:
        status = await _query_state()
    except httpx.HTTPError as e:
        return {'error': f'moonraker: {e}'}
    return _parse_state(status)

@app.get('/api/aces')
async def list_aces() -> dict:
    try:
        status = await _query_state()
    except httpx.HTTPError as e:
        return {'aces': [], 'error': f'moonraker: {e}'}
    parsed = _parse_state(status)
    return {'aces': parsed['aces'], 'active_device': parsed['active_device']}

@app.get('/api/debug')
async def get_debug() -> dict:
    try:
        return await _query_state()
    except httpx.HTTPError as e:
        return {'error': f'moonraker: {e}'}
_MACRO_PREFIX = 'gcode_macro '
_MACRO_BUCKETS = (('switch', lambda m: m.startswith('ACEA__Switch')), ('load', lambda m: m.startswith('ACEB__Load') or m.startswith('ACEC__Load')), ('unload', lambda m: m.startswith('ACEC__Unload')), ('dry', lambda m: m.startswith('ACED__Dry')), ('mode', lambda m: m.startswith('ACEF__Mode') or m == 'SET_ACE_MODE'), ('status', lambda m: m.startswith('ACEG__')))

@app.get('/api/macros')
async def list_macros() -> dict:
    try:
        data = await _mr_get('/printer/objects/list')
    except httpx.HTTPError as e:
        return {'all': [], 'categorized': {}, 'error': f'moonraker: {e}'}
    objs = data.get('result', {}).get('objects', []) or []
    macros = sorted((o[len(_MACRO_PREFIX):] for o in objs if isinstance(o, str) and o.startswith(_MACRO_PREFIX) and ('ACE' in o or o.endswith(' SET_ACE_MODE'))))
    cats: dict[str, list[str]] = {name: [] for name, _ in _MACRO_BUCKETS}
    cats['other'] = []
    for m in macros:
        for name, pred in _MACRO_BUCKETS:
            if pred(m):
                cats[name].append(m)
                break
        else:
            cats['other'].append(m)
    return {'all': macros, 'categorized': cats}

@app.post('/api/macro')
async def run_macro(req: MacroRequest) -> dict:
    parts = [req.name]
    if req.args:
        for k, v in req.args.items():
            parts.append(f'{k}={v}')
    script = ' '.join(parts)
    try:
        result = await _mr_post('/printer/gcode/script', {'script': script}, timeout=600.0)
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code, detail=e.response.text)
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f'moonraker: {e}')
    return {'script': script, 'result': result}

def _extract_params(text: str) -> tuple[dict[str, str], dict[int, dict[str, str]]]:
    main: dict[str, str] = {}
    per_ace: dict[int, dict[str, str]] = {}
    section: object = None
    for raw in text.splitlines():
        s = raw.strip()
        if not s or s.startswith('#'):
            continue
        if s.startswith('[') and s.endswith(']'):
            head = s[1:-1].strip()
            if head == 'ace':
                section = 'ace'
            elif head.startswith('ace ') or head.startswith('ace\t'):
                try:
                    section = int(head.split(None, 1)[1])
                except (IndexError, ValueError):
                    section = None
            else:
                section = None
            continue
        if section is None or ':' not in s:
            continue
        k, v = s.split(':', 1)
        key, val = (k.strip(), v.strip())
        if section == 'ace':
            main[key] = val
        else:
            per_ace.setdefault(section, {})[key] = val
    return (main, per_ace)

@app.get('/api/config')
async def get_config() -> dict:
    p = Path(MULTIACE_CFG_PATH)
    if not p.exists():
        raise HTTPException(404, f'config file not found: {MULTIACE_CFG_PATH}')
    text = p.read_text(encoding='utf-8')
    main, per_ace = _extract_params(text)
    return {'path': str(p), 'content': text, 'params': main, 'per_ace_params': per_ace}

@app.put('/api/config')
async def update_config(payload: ConfigUpdate) -> dict:
    p = Path(MULTIACE_CFG_PATH)
    if not p.exists():
        raise HTTPException(404, f'config file not found: {MULTIACE_CFG_PATH}')
    backup = p.with_suffix(p.suffix + '.bak')
    backup.write_text(p.read_text(encoding='utf-8'), encoding='utf-8')
    p.write_text(payload.content, encoding='utf-8')
    restart: dict | None = None
    if payload.restart_klipper:
        try:
            restart = await _mr_post('/printer/restart', {})
        except httpx.HTTPError as e:
            restart = {'error': str(e)}
    return {'path': str(p), 'backup': str(backup), 'restart': restart}
_LANG_NAME_RE = re.compile('^[A-Za-z]{2}(-[A-Za-z]{2})?$')

def _load_catalog(lang: str) -> dict:
    if not _LANG_NAME_RE.match(lang):
        raise HTTPException(400, 'invalid language code')
    p = Path(I18N_DIR) / f'{lang}.json'
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding='utf-8'))
    except Exception:
        return {}

def _merge_dicts(base: dict, overlay: dict) -> dict:
    out = dict(base)
    for k, v in overlay.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge_dicts(out[k], v)
        else:
            out[k] = v
    return out

@app.get('/api/i18n/{lang}')
async def get_i18n(lang: str) -> dict:
    en = _load_catalog('en')
    if lang == 'en':
        return en
    catalog = _load_catalog(lang)
    if not catalog:
        raise HTTPException(404, f'language not found: {lang}')
    return _merge_dicts(en, catalog)

@app.get('/api/i18n')
async def list_i18n() -> dict:
    d = Path(I18N_DIR)
    if not d.is_dir():
        return {'languages': []}
    langs = []
    for p in sorted(d.glob('*.json')):
        try:
            data = json.loads(p.read_text(encoding='utf-8'))
            meta = data.get('_meta', {}) or {}
            langs.append({'code': p.stem, 'name': meta.get('name', p.stem), 'fallback': meta.get('fallback')})
        except Exception:
            continue
    return {'languages': langs}

@app.get('/api/screen-available')
async def screen_available() -> dict:
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            r = await client.head(SCREEN_PROBE_URL)
            return {'available': r.status_code < 500}
    except httpx.HTTPError as e:
        return {'available': False, 'error': str(e)}
_SNAP_NAME_RE = re.compile('^[A-Za-z0-9_\\- ]{1,64}$')

def _snap_path(name: str) -> Path:
    if not _SNAP_NAME_RE.match(name):
        raise HTTPException(400, 'name must match [A-Za-z0-9_- ]{1,64}')
    return Path(SNAPSHOT_DIR) / f'{name}.json'

def _capture_snapshot(now_status: dict) -> dict:
    parsed = _parse_state(now_status)
    toolheads = []
    for t in parsed['toolheads']:
        if not t.get('filament_detected'):
            continue
        ace = t.get('ace')
        slot = t.get('slot')
        if ace is None or slot is None:
            continue
        slot_obj = None
        if ace is not None and 0 <= ace < len(parsed['aces']):
            slots = parsed['aces'][ace]['slots']
            if slot is not None and 0 <= slot < len(slots):
                slot_obj = slots[slot]
        toolheads.append({'idx': t['idx'], 'ace': ace, 'slot': slot, 'material': (slot_obj or {}).get('material', ''), 'brand': (slot_obj or {}).get('brand', ''), 'color': (slot_obj or {}).get('color'), 'color_rgb': (slot_obj or {}).get('color_rgb'), 'sku': (slot_obj or {}).get('sku', '')})
    return {'toolheads': toolheads}

@app.get('/api/snapshots')
async def list_snapshots() -> dict:
    d = Path(SNAPSHOT_DIR)
    d.mkdir(parents=True, exist_ok=True)
    items = []
    for p in sorted(d.glob('*.json')):
        try:
            data = json.loads(p.read_text(encoding='utf-8'))
            items.append({'name': p.stem, 'saved': data.get('saved'), 'description': data.get('description'), 'toolheads': data.get('toolheads', [])})
        except Exception as e:
            items.append({'name': p.stem, 'error': str(e)})
    return {'snapshots': items}

@app.post('/api/snapshots')
async def save_snapshot(req: SnapshotSave) -> dict:
    p = _snap_path(req.name)
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        status = await _query_state()
    except httpx.HTTPError as e:
        raise HTTPException(502, f'moonraker: {e}')
    snap = _capture_snapshot(status)
    snap['name'] = req.name
    snap['description'] = req.description
    snap['saved'] = time.strftime('%Y-%m-%dT%H:%M:%S')
    p.write_text(json.dumps(snap, indent=2), encoding='utf-8')
    return {'ok': True, 'path': str(p), 'snapshot': snap}

@app.get('/api/snapshots/{name}')
async def get_snapshot(name: str) -> dict:
    p = _snap_path(name)
    if not p.exists():
        raise HTTPException(404, 'snapshot not found')
    return json.loads(p.read_text(encoding='utf-8'))

@app.delete('/api/snapshots/{name}')
async def delete_snapshot(name: str) -> dict:
    p = _snap_path(name)
    if not p.exists():
        raise HTTPException(404, 'snapshot not found')
    p.unlink()
    return {'ok': True}

@app.post('/api/snapshots/{name}/apply')
async def apply_snapshot(name: str) -> dict:
    p = _snap_path(name)
    if not p.exists():
        raise HTTPException(404, 'snapshot not found')
    snap = json.loads(p.read_text(encoding='utf-8'))
    try:
        status = await _query_state()
    except httpx.HTTPError as e:
        raise HTTPException(502, f'moonraker: {e}')
    cur = _parse_state(status)
    cur_th = {t['idx']: t for t in cur['toolheads']}
    desired = {t['idx']: t for t in snap.get('toolheads', [])}
    cur_aces = cur.get('aces', []) or []

    def _slot_view(ace_i, slot_i):
        if ace_i is None or slot_i is None:
            return None
        if not 0 <= ace_i < len(cur_aces):
            return None
        slots = cur_aces[ace_i].get('slots') or []
        if not 0 <= slot_i < len(slots):
            return None
        return slots[slot_i]
    errors: list[dict] = []
    warnings: list[dict] = []
    for idx, dt in desired.items():
        ace_i = dt.get('ace')
        slot_i = dt.get('slot')
        sv = _slot_view(ace_i, slot_i)
        if sv is None or sv.get('raw') == 0 or (sv.get('state') or '').startswith('empty'):
            errors.append({'head': idx, 'ace': ace_i, 'slot': slot_i, 'kind': 'empty', 'message': f"T{idx}: ACE {ace_i} / Slot {slot_i} ist leer ({dt.get('material') or '?'} erwartet)"})
            continue
        want_mat = (dt.get('material') or '').strip()
        have_mat = (sv.get('material') or '').strip()
        want_col = dt.get('color') or ''
        have_col = sv.get('color') or ''
        want_brand = (dt.get('brand') or '').strip()
        have_brand = (sv.get('brand') or '').strip()
        if want_mat and have_mat and (want_mat != have_mat):
            warnings.append({'head': idx, 'ace': ace_i, 'slot': slot_i, 'kind': 'material', 'want': want_mat, 'have': have_mat, 'message': f"T{idx}: Snapshot will {want_mat}, ACE {ace_i} / Slot {slot_i} hat {have_mat or '?'}"})
        elif want_col and have_col and (want_col.lower() != have_col.lower()):
            warnings.append({'head': idx, 'ace': ace_i, 'slot': slot_i, 'kind': 'color', 'want': want_col, 'have': have_col, 'message': f'T{idx}: Farbabweichung — Snapshot {want_col}, Slot {have_col}'})
        elif want_brand and have_brand and (want_brand != have_brand):
            warnings.append({'head': idx, 'ace': ace_i, 'slot': slot_i, 'kind': 'brand', 'want': want_brand, 'have': have_brand, 'message': f'T{idx}: Hersteller-Abweichung — Snapshot {want_brand}, Slot {have_brand}'})
    actions: list[dict] = []
    for idx, ct in cur_th.items():
        if not ct.get('head_source_known'):
            continue
        d = desired.get(idx)
        if d is None or d.get('ace') != ct.get('ace') or d.get('slot') != ct.get('slot'):
            actions.append({'name': 'ACE_UNLOAD_HEAD', 'args': {'HEAD': idx}})
    by_ace: dict[int, list[int]] = {}
    for idx, dt in desired.items():
        ace_idx = dt.get('ace')
        if ace_idx is None:
            continue
        ct = cur_th.get(idx, {})
        if ct.get('head_source_known') and ct.get('ace') == ace_idx and (ct.get('slot') == dt.get('slot')):
            continue
        by_ace.setdefault(ace_idx, []).append(idx)
    for ace_idx in sorted(by_ace):
        for head in sorted(by_ace[ace_idx]):
            actions.append({'name': 'ACE_LOAD_HEAD', 'args': {'HEAD': head, 'ACE': ace_idx}})
    override_proposals: list[dict] = []
    for idx, dt in desired.items():
        ace_i = dt.get('ace')
        slot_i = dt.get('slot')
        if ace_i is None or slot_i is None:
            continue
        material = (dt.get('material') or '').strip()
        color = (dt.get('color') or '').strip()
        if not material and (not color):
            continue
        override_proposals.append({'ace': ace_i, 'slot': slot_i, 'material': material, 'brand': (dt.get('brand') or '').strip(), 'subtype': (dt.get('sku') or '').strip(), 'color': color})
    return {'snapshot': name, 'actions': actions, 'errors': errors, 'warnings': warnings, 'override_proposals': override_proposals}
_slot_overrides: dict[str, dict] = {}
_last_head_source: dict[int, tuple[int, int] | None] = {}
_overrides_mtime: float = 0.0

def _override_key(ace: int, slot: int) -> str:
    return f'{int(ace)}_{int(slot)}'

def _reload_overrides_if_changed() -> None:
    global _overrides_mtime
    p = Path(OVERRIDE_FILE)
    if not p.exists():
        if _slot_overrides:
            _slot_overrides.clear()
        _overrides_mtime = 0.0
        return
    try:
        m = p.stat().st_mtime
    except OSError:
        return
    if m == _overrides_mtime:
        return
    _load_overrides_from_disk()
    _overrides_mtime = m

def _load_overrides_from_disk() -> None:
    global _overrides_mtime
    p = Path(OVERRIDE_FILE)
    if not p.exists():
        return
    try:
        data = json.loads(p.read_text(encoding='utf-8'))
        if isinstance(data, dict):
            _slot_overrides.clear()
            _slot_overrides.update(data)
        try:
            _overrides_mtime = p.stat().st_mtime
        except OSError:
            pass
    except Exception:
        pass

def _save_overrides_to_disk() -> None:
    global _overrides_mtime
    p = Path(OVERRIDE_FILE)
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        tmp = p.with_suffix(p.suffix + '.tmp')
        tmp.write_text(json.dumps(_slot_overrides, indent=2), encoding='utf-8')
        os.replace(str(tmp), str(p))
        try:
            _overrides_mtime = p.stat().st_mtime
        except OSError:
            pass
    except Exception:
        pass

def _drop_override_if_present(ace: int, slot: int) -> bool:
    key = _override_key(ace, slot)
    if key in _slot_overrides:
        old = _slot_overrides.pop(key, None)
        _trace.info('override DROP gate==0 ACE %d / slot %d (was %s)', ace, slot, old)
        return True
    return False
EJECT_DEBOUNCE_S = 0.5
_eject_pending_since: dict[tuple[int, int], float] = {}

def _override_for(ace: int, slot: int) -> dict | None:
    o = _slot_overrides.get(_override_key(ace, slot))
    if not o:
        return None
    mat = (o.get('material') or '').strip()
    col = (o.get('color') or '').strip()
    if not mat and (not col):
        return None
    return o

def _track_unload_clears(head_source: dict) -> None:
    changed = False
    for t in range(4):
        cur = head_source.get(str(t)) or head_source.get(t)
        d, sl = _resolve_head_source(cur)
        prev = _last_head_source.get(t)
        if prev is not None and (d, sl) != prev and (d is None) and (sl is None):
            key = _override_key(prev[0], prev[1])
            if key in _slot_overrides:
                old = _slot_overrides.pop(key, None)
                _trace.info('override DROP unload T%d (was loaded from ACE %d / slot %d): %s', t, prev[0], prev[1], old)
                changed = True
        _last_head_source[t] = (d, sl) if d is not None and sl is not None else None
    if changed:
        _save_overrides_to_disk()

@app.get('/api/slot-override')
async def list_slot_overrides() -> dict:
    return {'overrides': _slot_overrides}

@app.post('/api/slot-override')
async def set_slot_override(req: SlotOverride) -> dict:
    key = _override_key(req.ace, req.slot)
    new = {'ace': req.ace, 'slot': req.slot, 'material': req.material or '', 'brand': req.brand or '', 'subtype': req.subtype or '', 'color': req.color or ''}
    old = _slot_overrides.get(key)
    _slot_overrides[key] = new
    _trace.info('override SET via picker POST ACE %d / slot %d: %s -> %s', req.ace, req.slot, old, new)
    _save_overrides_to_disk()
    return {'ok': True, 'key': key, 'override': _slot_overrides[key]}

@app.delete('/api/slot-override/{ace}/{slot}')
async def delete_slot_override(ace: int, slot: int) -> dict:
    key = _override_key(ace, slot)
    if key in _slot_overrides:
        old = _slot_overrides.pop(key, None)
        _trace.info('override DROP via picker DELETE ACE %d / slot %d (was %s)', ace, slot, old)
        _save_overrides_to_disk()
    return {'ok': True}
_load_overrides_from_disk()
_notifications: deque = deque(maxlen=50)
_next_notification_id = int(time.time() * 1000)
_notifications_lock = asyncio.Lock()
_NOTIF_ONLY_MULTIACE = os.environ.get('MULTIACE_NOTIF_ONLY_MULTIACE', '1') in ('1', 'true', 'yes')

def _is_error_gcode_response(text: str) -> bool:
    if not isinstance(text, str):
        return False
    s = text.strip()
    if not s:
        return False
    body = s[3:].strip() if s.startswith('// ') else s
    is_error = body.startswith('!!') or 'Error:' in body or body.lower().startswith('aborting')
    if _NOTIF_ONLY_MULTIACE:
        return is_error and '[multiACE]' in s
    if is_error:
        return True
    if body.lower().startswith('unknown command'):
        return True
    return False

def _record_notification(text: str) -> dict | None:
    global _next_notification_id
    if not _is_error_gcode_response(text):
        return None
    _next_notification_id += 1
    msg = text.strip()
    for prefix in ('// !! ', '// Error:', '// ', '!! ', '!!', 'Error:'):
        if msg.startswith(prefix):
            msg = msg[len(prefix):].strip()
            break
    if msg.startswith('[multiACE] '):
        msg = msg[len('[multiACE] '):].strip()
    elif msg.startswith('[multiACE]'):
        msg = msg[len('[multiACE]'):].strip()
    note = {'id': _next_notification_id, 'ts': time.time(), 'msg': msg, 'raw': text.strip(), 'level': 'error'}
    _notifications.append(note)
    _trace.info('notification %d captured: %s', note['id'], note['msg'])
    return note

async def _moonraker_log_listener() -> None:
    url = MOONRAKER_URL.replace('http://', 'ws://').replace('https://', 'wss://').rstrip('/') + '/websocket'
    backoff = 1.0
    debug_recv = os.environ.get('MULTIACE_WS_DEBUG', '0') in ('1', 'true', 'yes')
    while True:
        try:
            _trace.info('moonraker WS connecting to %s ...', url)
            async with websockets.connect(url, ping_interval=None, close_timeout=5) as ws:
                _trace.info('moonraker WS connected')
                try:
                    await ws.send(json.dumps({'jsonrpc': '2.0', 'method': 'server.connection.identify', 'params': {'client_name': 'multiace_web', 'version': VERSION, 'type': 'agent', 'url': 'https://github.com/decay71/multiACE'}, 'id': 1}))
                    _trace.info('moonraker WS identify sent')
                except Exception as ie:
                    _trace.warning('moonraker WS identify failed: %s', ie)
                backoff = 1.0
                msg_count = 0
                async for raw in ws:
                    msg_count += 1
                    if debug_recv:
                        _trace.warning('moonraker WS recv #%d: %s', msg_count, str(raw)[:240])
                    try:
                        msg = json.loads(raw)
                    except (TypeError, ValueError):
                        continue
                    method = msg.get('method')
                    if method != 'notify_gcode_response':
                        continue
                    params = msg.get('params') or []
                    if not params:
                        continue
                    text = params[0]
                    rec = _record_notification(text)
                    if rec is not None:
                        _trace.warning('Klipper error captured: %s', rec['msg'])
                _trace.info('moonraker WS loop ended after %d messages', msg_count)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            _trace.warning('moonraker WS error: %s; reconnect in %.1fs', e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2.0, 30.0)
        else:
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2.0, 30.0)

@app.on_event('startup')
async def _start_log_listener() -> None:
    asyncio.create_task(_moonraker_log_listener())

@app.get('/api/notifications')
async def list_notifications() -> dict:
    return {'notifications': list(_notifications)}

@app.post('/api/notifications/test')
async def test_notification(payload: dict | None=None) -> dict:
    msg = (payload or {}).get('msg') if payload else None
    text = '!! ' + (msg or 'Test notification from /api/notifications/test')
    rec = _record_notification(text)
    return {'ok': rec is not None, 'notification': rec}

@app.delete('/api/notifications/{nid}')
async def dismiss_notification(nid: int) -> dict:
    async with _notifications_lock:
        before = len(_notifications)
        keep = [n for n in _notifications if n['id'] != nid]
        _notifications.clear()
        _notifications.extend(keep)
    return {'ok': True, 'dismissed': before - len(_notifications)}

@app.delete('/api/notifications')
async def clear_notifications() -> dict:
    async with _notifications_lock:
        n = len(_notifications)
        _notifications.clear()
    return {'ok': True, 'cleared': n}

@app.websocket('/ws')
async def ws(websocket: WebSocket) -> None:
    await websocket.accept()
    last_seen_notif_id = 0
    try:
        last_ts = 0.0
        while True:
            now = time.time()
            for n in list(_notifications):
                if n['id'] > last_seen_notif_id:
                    try:
                        await websocket.send_json({'type': 'gcode_error', 'ts': n['ts'], 'id': n['id'], 'msg': n['msg'], 'raw': n['raw'], 'level': n['level']})
                    except Exception:
                        return
                    last_seen_notif_id = n['id']
            if now - last_ts >= 1.0:
                try:
                    status = await _query_state()
                    payload = _parse_state(status)
                    payload['type'] = 'state'
                    payload['ts'] = now
                    await websocket.send_json(payload)
                except Exception as e:
                    await websocket.send_json({'type': 'error', 'ts': now, 'error': str(e)})
                last_ts = now
            await asyncio.sleep(0.25)
    except WebSocketDisconnect:
        return
    except Exception:
        return
if Path(FRONTEND_DIR).is_dir():
    app.mount('/', StaticFiles(directory=FRONTEND_DIR, html=True), name='frontend')
