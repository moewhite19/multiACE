
from __future__ import annotations

import re
from collections import deque

DEFAULT_FUZZY = 30

_TOOLCHANGE_RE = re.compile(
    r"^;\s*Change Tool\s*(\d+)\s*->\s*Tool\s*(\d+)", re.MULTILINE)

_PLAN_KEEP_RE = re.compile(
    r'^(;\s*Change Tool|;\s*LAYER_CHANGE|;\s*filament\b|T\d{1,2}\s*$)',
    re.IGNORECASE)

def parse_meta(pp, line_iter):
    """One streaming pass over the gcode lines → everything the report/rewrite
    need from the file metadata. Works on any iterable of lines, so the backend
    can pass an open file handle (memory-friendly for huge files) and the
    browser worker can pass text.splitlines(keepends=True).

    Returns (slicer_colors, slicer_types, num_aces, used, plan_proxy).
    """
    head_lines: list = []
    tail_lines: deque = deque(maxlen=2000)
    plan_lines: list = []
    used: set = set()
    for i, line in enumerate(line_iter):
        if i < 300:
            head_lines.append(line)
        else:
            tail_lines.append(line)
        m = _TOOLCHANGE_RE.match(line)
        if m:
            used.add(int(m.group(1)))
            used.add(int(m.group(2)))
        if _PLAN_KEEP_RE.match(line):
            plan_lines.append(line.rstrip('\n'))
    meta_buf = "".join(head_lines) + "".join(tail_lines)
    plan_proxy = "\n".join(plan_lines)

    slicer_colors = pp.parse_color_names(meta_buf)
    slicer_types  = pp.parse_filament_types(meta_buf)
    num_aces      = pp.infer_num_aces(meta_buf)

    if used:
        slicer_colors = {t: c for t, c in slicer_colors.items() if t in used}
        slicer_types  = {t: m for t, m in slicer_types.items() if t in used}
    return slicer_colors, slicer_types, num_aces, used, plan_proxy

def used_tool_indices(pp, gcode: str) -> set:
    """The set of T-indices actually activated by the gcode (union of every
    'Change Tool X -> Tool Y'); falls back to the post-processor's bare-T scan
    for single-tool prints with no transitions."""
    used: set = set()
    for m in _TOOLCHANGE_RE.finditer(gcode):
        used.add(int(m.group(1)))
        used.add(int(m.group(2)))
    if not used:
        try:
            used = set(pp.parse_toolchanges(gcode))
        except Exception:
            used = set()
    return used

def _slot_to_dict(s):
    if s is None:
        return None
    return {
        "ace":      s.get("ace"),
        "slot":     s.get("slot"),
        "material": s.get("material") or "",
        "color":    s.get("color") or "",
    }

def mapping_from_info(info: dict) -> list:
    out = []
    for t in sorted(info.keys()):
        out.append({
            "t":         t,
            "slot":      _slot_to_dict(info[t]["slot"]),
            "tier":      info[t]["tier"],
            "loose_mat": bool(info[t].get("loose_mat")),
        })
    return out

def _real_swap_count(events, mapping):
    by_t = {m["t"]: m["slot"] for m in mapping if m.get("slot")}
    head_current = {h: (0, h) for h in range(4)}
    swaps = 0
    for t in events:
        slot = by_t.get(t)
        if slot is None:
            continue
        h = slot["slot"]
        key = (slot["ace"], slot["slot"])
        if head_current.get(h) != key:
            swaps += 1
            head_current[h] = key
    return swaps

def _layout_from_head_assignment(c2h, slicer_colors, slicer_types):
    """{color: head} → a mapping list with (ace, slot=head) per colour. ACE
    within each head is first-come-first-served (sorted by T-index)."""
    head_ace = {h: 0 for h in range(4)}
    rows = []
    for c in sorted(c2h.keys(), key=lambda x: (c2h[x], x)):
        h = c2h[c]
        ace = head_ace[h]
        head_ace[h] += 1
        rows.append((ace, h, c, {
            "t":         c,
            "slot": {
                "ace":      ace,
                "slot":     h,
                "material": (slicer_types.get(c) or "") or "",
                "color":    (slicer_colors.get(c) or "").lower(),
            },
            "tier":      "planned",
            "loose_mat": False,
        }))
    rows.sort(key=lambda r: (r[0], r[1], r[2]))
    return [r[3] for r in rows]

def build_one_plan(pp, plan_name, result, mapping,
                   slicer_colors=None, slicer_types=None, num_aces=4):
    """One of the three multi-mode plans (slicer / optimize / layer)."""
    slicer_colors = slicer_colors or {}
    slicer_types  = slicer_types  or {}
    events = result.get("events") or []
    tool_changes = int(result.get("total_changes") or 0)

    if plan_name == "slicer":
        return {
            "feasible":     True,
            "swaps":        _real_swap_count(events, mapping),
            "tool_changes": tool_changes,
            "mapping":      mapping,
        }

    if plan_name == "optimize":
        try:
            c2h, swaps = pp.compute_swap_aware_layout(events, num_aces=num_aces)
        except Exception:
            c2h, swaps = None, None
        if c2h is None:
            return {
                "feasible":     False,
                "swaps":        0,
                "tool_changes": tool_changes,
                "mapping":      [],
                "reason":       "no feasible head assignment",
            }
        return {
            "feasible":     True,
            "swaps":        swaps,
            "tool_changes": tool_changes,
            "mapping":      _layout_from_head_assignment(
                c2h, slicer_colors, slicer_types),
        }

    layer_info = result.get("layer_info") or {}
    layer_color_sets_raw = layer_info.get("layer_color_sets") or []
    layer_color_sets = [set(s) for s in layer_color_sets_raw]
    try:
        c2h, swaps = pp.compute_swap_aware_layout(
            events, num_aces=num_aces,
            layer_color_sets=layer_color_sets if layer_color_sets else None)
    except Exception:
        c2h, swaps = None, None
    if c2h is None:
        reason = "no layer-feasible head assignment"
        max_per = layer_info.get("max_per_layer", 0)
        if max_per > 4:
            reason = ">4 colors in some layer"
        return {
            "feasible":     False,
            "swaps":        0,
            "tool_changes": tool_changes,
            "mapping":      [],
            "reason":       reason,
        }
    return {
        "feasible":     True,
        "swaps":        swaps,
        "tool_changes": tool_changes,
        "mapping":      _layout_from_head_assignment(
            c2h, slicer_colors, slicer_types),
        "reason":       "",
    }

_HEAD_MODE_PP_FUNCS = (
    "compute_head_mode_layout", "compute_head_mode_optimize",
    "head_mode_swap_count", "rewrite_head_mode_to_file")

def ensure_head_mode_support(pp):
    missing = [f for f in _HEAD_MODE_PP_FUNCS if not hasattr(pp, f)]
    if missing:
        raise RuntimeError(
            "post-processor is outdated (missing head-mode support: "
            + ", ".join(missing)
            + "). Re-run install_multiace.sh or reboot so the shipped "
              "post_process_virtual_toolheads.py is refreshed in "
              "printer_data/config/tools/.")

def head_mode_targets(pp, feeders: list, ace_slots: list) -> list:
    """The dropdown universe: each pin-able feeder + each ACE slot, with an id."""
    targets = []
    for f in feeders:
        targets.append({
            "id": "feeder-%d" % f["head"], "kind": "pin", "head": f["head"],
            "material": f["material"], "color": (f["color"] or "").lower(),
            "name": pp.approx_color_name(f["color"]) or ""})
    for s in sorted(ace_slots, key=lambda x: (x["ace"], x["slot"])):
        targets.append({
            "id": "slot-%d-%d" % (s["ace"], s["slot"]), "kind": "ace",
            "ace": s["ace"], "slot": s["slot"],
            "material": s["material"], "color": (s["color"] or "").lower(),
            "name": pp.approx_color_name(s["color"]) or ""})
    return targets

def head_target_id(e: dict):
    if not e:
        return None
    if e.get("kind") == "pin":
        return "feeder-%d" % e["head"]
    if e.get("kind") == "ace":
        return "slot-%d-%d" % (e["ace"], e["slot"])
    return None

def assignment_from_target_ids(target_ids: dict, targets: list, ace_head: int) -> dict:
    """Rebuild {t: entry} from the frontend's {t: target_id} via the universe."""
    by_id = {t["id"]: t for t in targets}
    out = {}
    for k, tid in (target_ids or {}).items():
        try:
            t = int(k)
        except (TypeError, ValueError):
            continue
        tgt = by_id.get(tid)
        if tgt is None:
            out[t] = {"kind": "none"}
        elif tgt["kind"] == "pin":
            out[t] = {"kind": "pin", "head": tgt["head"]}
        else:
            out[t] = {"kind": "ace", "head": ace_head,
                      "ace": tgt["ace"], "slot": tgt["slot"]}
    return out

def _head_proposal_plan(pp, events, slicer_colors, feeder_heads, ace_head,
                        ace_num, num_slots, layer_sets) -> dict:
    """A head-mode PROPOSED-loadout plan (optimize / layer-Belady): the
    swap-minimal FREE assignment that ignores the current physical load. The
    user arranges spools to match before printing → read-only table."""
    try:
        assignment, swaps = pp.compute_head_mode_optimize(
            events, feeder_heads, ace_head, ace_num, num_slots,
            layer_color_sets=layer_sets)
    except Exception:
        assignment, swaps = None, None
    if assignment is None:
        reason = ("no layer-feasible loadout" if layer_sets
                  else "too many colours for the loadout")
        return {"feasible": False, "swaps": 0, "mapping": [], "reason": reason}
    mapping = []
    feasible = True
    for t in sorted(slicer_colors.keys()):
        e = assignment.get(t)
        if not e or e.get("kind") == "none":
            feasible = False
            mapping.append({"t": t, "kind": "none"})
        else:
            mapping.append({"t": t, "kind": e["kind"], "head": e.get("head"),
                            "ace": e.get("ace"), "slot": e.get("slot"),
                            "tier": e.get("tier")})
    return {"feasible": feasible, "swaps": swaps, "mapping": mapping}

def head_mode_preview(pp, token, safe_name, upload_size, slicer_colors,
                      slicer_types, ace_head, feeders, ace_slots, plan_proxy,
                      fuzzy=DEFAULT_FUZZY) -> dict:
    """The head-mode preflight preview: THREE plans, mirroring multi:
      loadout  - match against the currently-loaded feeders + ACE slots (editable)
      optimize - swap-minimal proposed loadout (free, Belady on the ACE head)
      layer    - same with layer-only swaps (Belady-/layer-optimal)
    Plus the colour grids at the top (available targets + slicer colours).
    """
    targets = head_mode_targets(pp, feeders, ace_slots)

    try:
        result = pp.plan_loadout(plan_proxy) or {}
    except Exception:
        result = {}
    events = list(result.get("events") or [])
    if not events:
        try:
            events = list(pp.parse_toolchanges(plan_proxy))
        except Exception:
            events = []
    lcs = (result.get("layer_info") or {}).get("layer_color_sets") or []
    layer_sets = [set(s) for s in lcs] if lcs else None

    layout = pp.compute_head_mode_layout(
        slicer_colors, slicer_types, feeders, ace_slots, ace_head,
        fuzzy_max_distance=fuzzy)
    assignment = layout["assignment"]
    loadout_mapping = []
    for t in sorted(slicer_colors.keys()):
        e = assignment.get(t) or {}
        loadout_mapping.append({"t": t, "target_id": head_target_id(e),
                                "tier": e.get("tier", "no_slot")})
    plans = {
        "loadout": {
            "feasible": layout["feasible"],
            "swaps": pp.head_mode_swap_count(events, assignment),
            "mapping": loadout_mapping},
    }

    feeder_heads = [h for h in range(4) if h != ace_head]
    ace_num = min((s["ace"] for s in ace_slots), default=0)
    num_slots = 4
    plans["optimize"] = _head_proposal_plan(
        pp, events, slicer_colors, feeder_heads, ace_head, ace_num,
        num_slots, None)
    plans["layer"] = _head_proposal_plan(
        pp, events, slicer_colors, feeder_heads, ace_head, ace_num,
        num_slots, layer_sets)

    return {
        "token": token, "filename": safe_name, "size": upload_size,
        "head_mode": True, "ace_head": ace_head,
        "slicer_colors": [
            {"t": t, "hex": (slicer_colors[t] or "").lower(),
             "name": pp.approx_color_name(slicer_colors[t]) or "",
             "material": slicer_types.get(t, "") or ""}
            for t in sorted(slicer_colors.keys())],
        "targets": targets,
        "events": events,
        "plans": plans,
    }

def build_report(pp, *, slicer_colors, slicer_types, num_aces, plan_proxy,
                 live_slots, head_ctx, token, filename, size,
                 fuzzy=DEFAULT_FUZZY) -> dict:
    """Build the full preflight report dict (the /api/preflight payload).

    head_ctx = {"mode": "normal"|"multi"|"head", "ace_head": int,
                "feeders": [{"head","material","color"}, ...]}.
    The caller has already fetched live_slots and resolved head_ctx (printer in
    the backend, /multiace/api/state in the browser). Mirrors main.py's old
    inline /api/preflight body 1:1 so backend and Pyodide produce identical
    reports.
    """
    num_aces = max(num_aces, max((s["ace"] for s in live_slots), default=0) + 1)

    if (head_ctx or {}).get("mode") == "head":
        ensure_head_mode_support(pp)
        return head_mode_preview(
            pp, token, filename, size, slicer_colors, slicer_types,
            int(head_ctx.get("ace_head", 3) or 3),
            head_ctx.get("feeders") or [], live_slots, plan_proxy, fuzzy=fuzzy)

    missing_mats = pp.check_material_availability(slicer_types, live_slots)

    out = {
        "token":         token,
        "filename":      filename,
        "size":          size,
        "num_aces":      num_aces,
        "slicer_colors": [
            {"t": t, "hex": (slicer_colors[t] or "").lower(),
             "name": pp.approx_color_name(slicer_colors[t]) or "",
             "material": slicer_types.get(t, "") or ""}
            for t in sorted(slicer_colors.keys())
        ],
        "live_slots": [
            {"ace": s["ace"], "slot": s["slot"],
             "material": s["material"], "color": s["color"],
             "name": pp.approx_color_name(s["color"]) or ""}
            for s in sorted(live_slots, key=lambda x: (x["ace"], x["slot"]))
        ],
        "missing_materials": missing_mats,
        "plans": {},
    }
    if not missing_mats:
        remap, info, _ = pp.match_colors_to_slots(
            slicer_colors, live_slots, num_heads=4,
            filament_types=slicer_types,
            strict_color=False,
            fuzzy_max_distance=fuzzy,
        )
        mapping = mapping_from_info(info)
        proxy_remapped = pp.apply_remap(plan_proxy, remap) if remap else plan_proxy
        result = pp.plan_loadout(proxy_remapped, num_aces=num_aces) or {}

        out["events"] = list(result.get("events") or [])
        for mode in ("slicer", "optimize", "layer"):
            out["plans"][mode] = build_one_plan(
                pp, mode, result, mapping,
                slicer_colors=slicer_colors, slicer_types=slicer_types,
                num_aces=num_aces)
    return out

def _noop_stage(stage, percent):
    pass

def _noop_stage_cb(base, span):
    def cb(done, total):
        pass
    return cb

def rewrite_pipeline(pp, *, src_path, tmp_a, tmp_b, slicer_colors, slicer_types,
                     num_aces, live_slots, head_ctx, mode,
                     remap_override=None, head_assignment=None,
                     head_plan="loadout", fuzzy=DEFAULT_FUZZY,
                     set_stage=None, stage_cb=None) -> str:
    """Run the rewrite pipeline on src_path, ping-ponging between tmp_a/tmp_b,
    and return the path holding the final print-ready gcode.

    Pure: operates only on file paths (real temp files in the backend, MEMFS
    paths under Pyodide) + the post-processor primitives. The caller handles
    the Moonraker upload and any SET_PRINT_PREFERENCES prepend afterwards.

    set_stage(stage, percent)  — coarse stage marker (optional).
    stage_cb(base, span) -> (done,total)->None — fine per-stage progress factory
    that the streaming *_to_file functions call (optional).
    Raises RuntimeError on an infeasible plan / missing material.
    """
    set_stage = set_stage or _noop_stage
    stage_cb  = stage_cb  or _noop_stage_cb
    num_aces = max(num_aces, max((s["ace"] for s in live_slots), default=0) + 1)

    if mode != "head":

        missing_mats = pp.check_material_availability(slicer_types, live_slots)
        if missing_mats:
            raise RuntimeError(
                "required material(s) not loaded: " + ", ".join(missing_mats))

    if mode == "head":
        ensure_head_mode_support(pp)
        ace_head = int((head_ctx or {}).get("ace_head", 3) or 3)
        feeders  = (head_ctx or {}).get("feeders") or []
        targets = head_mode_targets(pp, feeders, live_slots)
        if head_plan in ("optimize", "layer"):
            set_stage(head_plan, 1.0)
            hm_result = pp.plan_loadout_from_file(str(src_path), num_aces) or {}
            hm_events = list(hm_result.get("events") or [])
            hm_layer_sets = None
            if head_plan == "layer":
                lcs = (hm_result.get("layer_info") or {}).get(
                    "layer_color_sets") or []
                hm_layer_sets = [set(s) for s in lcs] if lcs else None
            feeder_heads = [h for h in range(4) if h != ace_head]
            ace_num = min((s["ace"] for s in live_slots), default=0)
            assignment, _hm_swaps = pp.compute_head_mode_optimize(
                hm_events, feeder_heads, ace_head, ace_num, 4,
                layer_color_sets=hm_layer_sets)
            if assignment is None:
                raise RuntimeError(
                    "no feasible head-mode loadout for %s plan" % head_plan)
        elif head_assignment:
            assignment = assignment_from_target_ids(
                head_assignment, targets, ace_head)
        else:
            layout = pp.compute_head_mode_layout(
                slicer_colors, slicer_types, feeders, live_slots,
                ace_head, fuzzy_max_distance=fuzzy)
            assignment = layout["assignment"]

        set_stage("rewrite", 10.0)
        pp.rewrite_head_mode_to_file(
            str(src_path), str(tmp_a), assignment, ace_head,
            stage_cb(10.0, 60.0))
        cur, nxt = tmp_a, tmp_b

        set_stage("inject_auto_load", 70.0)
        pp.inject_auto_load_to_file(
            str(cur), str(nxt), stage_cb(70.0, 12.0), {ace_head})
        cur, nxt = nxt, cur
        return str(cur)

    if mode == "slicer":
        if remap_override is not None:

            remap = {}
            for k, v in remap_override.items():
                try:
                    ik, iv = int(k), int(v)
                except (TypeError, ValueError):
                    continue
                if 0 <= iv <= 15 and ik != iv:
                    remap[ik] = iv
        else:
            remap, _info, _ = pp.match_colors_to_slots(
                slicer_colors, live_slots, num_heads=4,
                filament_types=slicer_types,
                strict_color=False,
                fuzzy_max_distance=fuzzy,
            )
    else:
        set_stage(mode, 1.0)
        sa_result = pp.plan_loadout_from_file(str(src_path), num_aces) or {}
        sa_events = sa_result.get("events") or []
        sa_layer_sets = None
        if mode == "layer":
            lcs = (sa_result.get("layer_info") or {}).get("layer_color_sets") or []
            sa_layer_sets = [set(s) for s in lcs] if lcs else None
        c2h, _sa_swaps = pp.compute_swap_aware_layout(
            sa_events, num_aces=num_aces, layer_color_sets=sa_layer_sets)
        if c2h is None:
            raise RuntimeError("no feasible head assignment for %s mode" % mode)
        head_ace_counter = {h: 0 for h in range(4)}
        remap = {}
        for c in sorted(c2h.keys(), key=lambda x: (c2h[x], x)):
            h = c2h[c]
            remap[c] = head_ace_counter[h] * 4 + h
            head_ace_counter[h] += 1

    set_stage("apply_remap", 5.0)
    pp.apply_remap_to_file(str(src_path), str(tmp_a), remap, stage_cb(5.0, 25.0))
    cur, nxt = tmp_a, tmp_b

    set_stage("rewrite", 45.0)
    pp.rewrite_to_file(str(cur), str(nxt), stage_cb(45.0, 30.0))
    cur, nxt = nxt, cur

    set_stage("inject_auto_load", 75.0)
    pp.inject_auto_load_to_file(str(cur), str(nxt), stage_cb(75.0, 10.0))
    cur, nxt = nxt, cur
    return str(cur)
