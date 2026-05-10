import logging
import logging.handlers
import json
import queue
import threading
import traceback
import os
import time
import hashlib
import serial
from serial import SerialException
from .ace_protocol_v1 import AceProtocolV1
from .ace_protocol_v2 import AceProtocolV2
KNOWN_PROTOCOLS = (AceProtocolV1, AceProtocolV2)
MULTIACE_VERSION = '0.92b'
MULTIACE_CODENAME = 'Hotfix2 + WebUI'
MULTIACE_BUILD_TAG = '0.92b'
MULTIACE_BUNDLE_SHA1 = 'c798795'

def _load_i18n_catalog(i18n_dir, lang):
    out = {}
    try:
        en_path = os.path.join(i18n_dir, 'en.json')
        if os.path.isfile(en_path):
            with open(en_path, 'r', encoding='utf-8') as f:
                out = json.load(f)
    except Exception:
        out = {}
    if lang and lang != 'en':
        try:
            lp = os.path.join(i18n_dir, lang + '.json')
            if os.path.isfile(lp):
                with open(lp, 'r', encoding='utf-8') as f:
                    overlay = json.load(f)

                def _merge(base, ov):
                    for k, v in ov.items():
                        if isinstance(v, dict) and isinstance(base.get(k), dict):
                            _merge(base[k], v)
                        else:
                            base[k] = v
                _merge(out, overlay)
        except Exception:
            pass
    return out

def _setup_file_logger(name, filepath, max_bytes=1048576, backup_count=3):
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    if not logger.handlers:
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(filepath, maxBytes=max_bytes, backupCount=backup_count)
        handler.setFormatter(logging.Formatter('%(asctime)s.%(msecs)03d %(message)s', datefmt='%Y-%m-%d %H:%M:%S'))
        logger.addHandler(handler)
    return logger

class AceException(Exception):
    pass
GATE_UNKNOWN = -1
GATE_EMPTY = 0
GATE_AVAILABLE = 1

class MultiAce:
    VARS_ACE_REVISION = 'ace__revision'
    VARS_ACE_ACTIVE_DEVICE = 'ace__active_device'
    VARS_ACE_HEAD_SOURCE = 'ace__head_source'

    def __init__(self, config):
        self._connected = False
        self._serial = None
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object('gcode')
        self._name = config.get_name()
        self.send_time = None
        self.ace_dev_fd = None
        self.heartbeat_timer = None
        self.gate_status = [GATE_UNKNOWN, GATE_UNKNOWN, GATE_UNKNOWN, GATE_UNKNOWN]
        if self._name.startswith('ace '):
            self._name = self._name[4:]
        self.save_variables = self.printer.lookup_object('save_variables', None)
        if self.save_variables:
            revision_var = self.save_variables.allVariables.get(self.VARS_ACE_REVISION, None)
            if revision_var is None:
                config.error('You have custom [save_variables]. Copy the contents of ace_vars.cfg to your file and remove [save_variables] in ace.cfg')
        else:
            config.error('There is no [save_variables] in the config. Check installation guide')
        self.serial_id = config.get('serial', '')
        self._protocols = {}
        self._ace_path_protocol = {}
        self._ace_models = {}
        self.baud = config.getint('baud', 0, minval=0)
        self._ace_devices = []
        self._active_device_index = 0
        self._ace_canonical = None
        self._ace_startup_failed = False
        self._ace_present = set()
        self.ace_device_count = config.getint('ace_device_count', 1, minval=1, maxval=8)
        cfg_print_mode = config.get('print_mode', None)
        if cfg_print_mode is not None:
            logging.info('[multiACE] print_mode=%s ignored (obsolete in v0.82+)' % cfg_print_mode)
        self.feed_speed = config.getint('feed_speed', 50)
        self.retract_speed = config.getint('retract_speed', 50)
        self.retract_length = config.getint('retract_length', 100)
        self.feed_length = config.getint('feed_length', 0)
        self.load_length = config.getint('load_length', 2000)
        self.load_retry = config.getint('load_retry', 3)
        self.load_retry_retract = config.getint('load_retry_retract', 50)
        self.max_dryer_temperature = config.getint('max_dryer_temperature', 55)
        self.extra_purge_length = config.getfloat('extra_purge_length', 50, minval=0, maxval=200)
        self.seat_overshoot_length = config.getint('seat_overshoot_length', 0, minval=0, maxval=100)
        self.swap_default_temp = config.getint('swap_default_temp', 250, minval=180, maxval=300)
        self.swap_retract_length = config.getint('swap_retract_length', 0, minval=0, maxval=2000)
        self.swap_anti_ooze_retract = config.getint('swap_anti_ooze_retract', 3, minval=0, maxval=50)
        self.extrusion_retry = config.getint('extrusion_retry', 7, minval=0, maxval=10)
        self.extrusion_retry_retract = config.getint('extrusion_retry_retract', 30, minval=5, maxval=200)
        self.extrusion_retry_retract_a = config.getint('extrusion_retry_retract_a', 50, minval=5, maxval=200)
        self.wiggle_scheme = (config.get('wiggle_scheme', 'EAEAEAE') or 'EAEAEAE').upper()
        for c in self.wiggle_scheme:
            if c not in ('E', 'A'):
                raise config.error("wiggle_scheme: invalid char %r (only 'E' and 'A' allowed)" % c)
        config.getint('extrusion_stock_retry', 5, minval=1, maxval=50)
        self.unload_retry = config.getint('unload_retry', 3, minval=1, maxval=10)
        self.dryer_temp = config.getint('dryer_temp', 55, minval=30, maxval=70)
        self.dryer_duration = config.getint('dryer_duration', 240, minval=10, maxval=480)
        self.head_feed_length = {}
        self.head_load_length = {}
        self.head_load_retry = {}
        self.head_load_retry_retract = {}
        for i in range(4):
            self.head_feed_length[i] = config.getint('feed_length_%d' % i, self.feed_length)
            self.head_load_length[i] = config.getint('load_length_%d' % i, self.load_length)
            self.head_load_retry[i] = config.getint('load_retry_%d' % i, self.load_retry)
            self.head_load_retry_retract[i] = config.getint('load_retry_retract_%d' % i, self.load_retry_retract)
        self._ace_section_load_length = {}
        self._ace_section_load_length_slot = {}
        self._ace_section_retract_length = {}
        self._ace_section_retract_length_slot = {}
        for ace_sec in config.get_prefix_sections('ace '):
            sec_name = ace_sec.get_name()
            try:
                ace_i = int(sec_name.split()[1])
            except (IndexError, ValueError):
                continue
            ll = ace_sec.getint('load_length', None, minval=1)
            if ll is not None:
                self._ace_section_load_length[ace_i] = ll
            rl = ace_sec.getint('retract_length', None, minval=1)
            if rl is not None:
                self._ace_section_retract_length[ace_i] = rl
            for slot_i in range(4):
                ll_s = ace_sec.getint('load_length_%d' % slot_i, None, minval=1)
                if ll_s is not None:
                    self._ace_section_load_length_slot[ace_i, slot_i] = ll_s
                rl_s = ace_sec.getint('retract_length_%d' % slot_i, None, minval=1)
                if rl_s is not None:
                    self._ace_section_retract_length_slot[ace_i, slot_i] = rl_s
        self.ace_dryer_temp = {}
        self.ace_dryer_duration = {}
        for i in range(4):
            self.ace_dryer_temp[i] = config.getint('dryer_temp_%d' % i, self.dryer_temp)
            self.ace_dryer_duration[i] = config.getint('dryer_duration_%d' % i, self.dryer_duration)

        def _parse_idx_list(key):
            raw = config.get(key, '').strip()
            out = set()
            if raw:
                for token in raw.split(','):
                    token = token.strip()
                    if token.isdigit():
                        out.add(int(token))
            return out
        self._fa_print_disable = _parse_idx_list('fa_print_disable')
        self._fa_load_disable = _parse_idx_list('fa_load_disable')
        self.fa_debug = config.getboolean('fa_debug', False)
        self._enable_ace_v2 = config.getboolean('enable_ace_v2', False)
        self._feed_assist_index = -1
        self._request_id = 0
        self._serials = {}
        self._connected_per_ace = {}
        self._serial_failed_per_ace = {}
        self._info_per_ace = {}
        self._slot_overrides = {}
        self._slot_overrides_file = '/home/lava/printer_data/config/extended/multiace/slot_overrides.json'
        self._slot_overrides_mtime = 0.0
        self._orig_set_ptc = None
        self._expected_ptc_pushes = []
        self._feed_assist_per_ace = {}
        self._callback_maps = {}
        self._request_ids = {}
        self._read_buffers = {}
        self._ace_dev_fds = {}
        self._heartbeat_timers = {}
        self._connect_timers_per_ace = {}
        self._writer_threads = {}
        self._reader_threads = {}
        self._writer_queues = {}
        self._thread_stop_flags = {}
        self._cb_locks = {}
        self._seq_lock = threading.Lock()
        self._gate_status_per_ace = {}
        self._v2_filament_info_per_ace = {}
        self._v2_filament_info_pending = {}
        self._v2_velocity_timers = {}
        self._v2_velocity_state = {}
        self._v2_static_assist_speed = config.getboolean('static_assist_speed', True)
        self._v2_min_rearm_gap = config.getfloat('rearm_min_gap', 1.0, minval=0.0, maxval=5.0)
        self._enable_web = config.getboolean('enable_web', True)
        self._web_port = config.getint('web_port', 7126, minval=1024, maxval=65535)
        self._web_dir = config.get('web_dir', '/home/lava/multiace_web')
        self._language = config.get('language', 'en')
        self._display_index_base = config.getint('display_index_base', 0, minval=0, maxval=1)
        i18n_primary = '/home/lava/printer_data/config/extended/multiace/i18n'
        i18n_fallback = os.path.join(self._web_dir, 'i18n')
        try:
            if os.path.isdir(i18n_primary):
                self._i18n = _load_i18n_catalog(i18n_primary, self._language)
            else:
                self._i18n = _load_i18n_catalog(i18n_fallback, self._language)
        except Exception as e:
            logging.info('[multiACE] i18n catalog load failed: %s' % e)
            self._i18n = {}
        self._head_source = {0: None, 1: None, 2: None, 3: None}
        self._swap_in_progress = False
        self._test_cancel = False
        self._auto_feed_enabled = False
        self._fa_context = 'idle'
        self._retract_length_override = None
        self._last_unload_ok = True
        self._last_load_ok = True
        self._ghost_heads = set()
        self._hotplug_gone = {}
        self._serial_failed = False
        self._serial_failed_at = 0.0
        self._serial_failed_pause_sent = False
        log_dir = config.get('log_dir', '/home/lava/printer_data/logs')
        self._usb_log = _setup_file_logger('multiace_usb', os.path.join(log_dir, 'multiace_usb.log'))
        self._state_log = _setup_file_logger('multiace_state', os.path.join(log_dir, 'multiace_state.log'))
        self._telemetry_log = _setup_file_logger('multiace_telemetry', os.path.join(log_dir, 'multiace_telemetry.log'))
        self._wiggle_log = _setup_file_logger('multiace_wiggle', os.path.join(log_dir, 'multiace_wiggle.log'))
        self._fa_log = _setup_file_logger('multiace_fa', os.path.join(log_dir, 'multiace_fa.log'))
        self._state_debug_enabled = config.getboolean('state_debug', False)
        self._usb_debug_enabled = config.getboolean('usb_debug', True)
        self._apply_log_levels()
        self._last_switch_auto_ts = None
        self._fa_any_active_since = None
        self._fa_last_active_ts = time.monotonic()
        self._fa_gap_threshold_ms = config.getint('fa_gap_threshold_ms', 3000, minval=100)
        self._fa_settle_after_stop = config.getfloat('fa_settle_after_stop', 2.0, minval=0.0, maxval=10.0)
        self._fa_start_retries = config.getint('fa_start_retries', 5, minval=0, maxval=30)
        self._fa_start_retry_delay = config.getfloat('fa_start_retry_delay', 0.5, minval=0.05, maxval=5.0)
        self._usb_stats = {'scans': 0, 'retries': 0, 'connects': 0, 'connect_failures': 0, 'disconnects': 0, 'errno5_total': 0, 'errno5_recovered': 0, 'errno5_unrecovered': 0, 'cascades': 0, 'start_time': time.monotonic()}
        self._errno5_recent = []
        self._info = {'status': 'ready', 'dryer_status': {'status': 'stop', 'target_temp': 0, 'duration': 0, 'remain_time': 0}, 'temp': 0, 'enable_rfid': 1, 'fan_speed': 7000, 'feed_assist_count': 0, 'cont_assist_time': 0.0, 'slots': [{'index': 0, 'status': 'empty1', 'sku': '', 'type': '', 'rfid': 0, 'brand': '', 'color': [0, 0, 0]}, {'index': 1, 'status': 'empty1', 'sku': '', 'type': '', 'rfid': 0, 'brand': '', 'color': [0, 0, 0]}, {'index': 2, 'status': 'empty1', 'sku': '', 'type': '', 'rfid': 0, 'brand': '', 'color': [0, 0, 0]}, {'index': 3, 'status': 'empty1', 'sku': '', 'type': '', 'rfid': 0, 'brand': '', 'color': [0, 0, 0]}]}
        self.extruder_sensor = None
        self.printer.register_event_handler('klippy:ready', self._handle_ready)
        self.printer.register_event_handler('klippy:disconnect', self._handle_disconnect)
        self.printer.register_event_handler('print_stats:start', self._on_print_start)
        self.printer.register_event_handler('print_stats:stop', self._on_print_end)
        self.gcode.register_command('ACE_START_DRYING', self.cmd_ACE_START_DRYING, desc=self.cmd_ACE_START_DRYING_help)
        self.gcode.register_command('ACE_STOP_DRYING', self.cmd_ACE_STOP_DRYING, desc=self.cmd_ACE_STOP_DRYING_help)
        self.gcode.register_command('ACE_ENABLE_FEED_ASSIST', self.cmd_ACE_ENABLE_FEED_ASSIST, desc=self.cmd_ACE_ENABLE_FEED_ASSIST_help)
        self.gcode.register_command('ACE_DISABLE_FEED_ASSIST', self.cmd_ACE_DISABLE_FEED_ASSIST, desc=self.cmd_ACE_DISABLE_FEED_ASSIST_help)
        self.gcode.register_command('ACE_FEED', self.cmd_ACE_FEED, desc=self.cmd_ACE_FEED_help)
        self.gcode.register_command('ACE_RETRACT', self.cmd_ACE_RETRACT, desc=self.cmd_ACE_RETRACT_help)
        self.gcode.register_command('ACE_SWITCH', self.cmd_ACE_SWITCH, desc=self.cmd_ACE_SWITCH_help)
        self.gcode.register_command('ACE_LIST', self.cmd_ACE_LIST, desc=self.cmd_ACE_LIST_help)
        self.gcode.register_command('ACE_RUN_MODE_SWITCH', self.cmd_ACE_RUN_MODE_SWITCH, desc=self.cmd_ACE_RUN_MODE_SWITCH_help)
        self.gcode.register_command('ACE_LOAD_HEAD', self.cmd_ACE_LOAD_HEAD, desc=self.cmd_ACE_LOAD_HEAD_help)
        self.gcode.register_command('ACE_UNLOAD_HEAD', self.cmd_ACE_UNLOAD_HEAD, desc=self.cmd_ACE_UNLOAD_HEAD_help)
        self.gcode.register_command('ACE_SWAP_HEAD', self.cmd_ACE_SWAP_HEAD, desc=self.cmd_ACE_SWAP_HEAD_help)
        self.gcode.register_command('ACE_HEAD_STATUS', self.cmd_ACE_HEAD_STATUS, desc=self.cmd_ACE_HEAD_STATUS_help)
        self.gcode.register_command('ACE_CLEAR_HEADS', self.cmd_ACE_CLEAR_HEADS, desc=self.cmd_ACE_CLEAR_HEADS_help)
        self.gcode.register_command('ACE_UNLOAD_ALL_HEADS', self.cmd_ACE_UNLOAD_ALL_HEADS, desc=self.cmd_ACE_UNLOAD_ALL_HEADS_help)
        self.gcode.register_command('ACE_TEST', self.cmd_ACE_TEST, desc=self.cmd_ACE_TEST_help)
        self.gcode.register_command('ACE_TEST_CANCEL', self.cmd_ACE_TEST_CANCEL, desc='[multiACE] Cancel a running ACE_TEST after current step')
        self.gcode.register_command('ACE_DRY', self.cmd_ACE_DRY, desc=self.cmd_ACE_DRY_help)
        self.gcode.register_command('ACE_USB_STATS', self.cmd_ACE_USB_STATS, desc=self.cmd_ACE_USB_STATS_help)
        self.gcode.register_command('ACE_DEBUG', self.cmd_ACE_DEBUG, desc=self.cmd_ACE_DEBUG_help)
        self.gcode.register_command('ACE_USB_DEBUG', self.cmd_ACE_USB_DEBUG, desc=self.cmd_ACE_USB_DEBUG_help)
        self.gcode.register_command('ACE_SEQ', self.cmd_ACE_SEQ, desc=self.cmd_ACE_SEQ_help)
        self.gcode.register_command('ACE_PRELOAD', self.cmd_ACE_PRELOAD, desc=self.cmd_ACE_PRELOAD_help)
        self.gcode.register_command('MACE_LOG', self.cmd_MACE_LOG, desc=self.cmd_MACE_LOG_help)
        self.gcode.register_command('ACE_FA_TEST', self.cmd_ACE_FA_TEST, desc=self.cmd_ACE_FA_TEST_help)
        self.gcode.register_command('MULTIACE_REFRESH_OVERRIDES', self.cmd_MULTIACE_REFRESH_OVERRIDES, desc='[multiACE] Re-read slot_overrides.json and push to display')
        for _name in ('DISCOVER', 'INFO', 'STATUS', 'TEMP', 'FEEDINFO', 'KEYSTATE', 'FILAMENT', 'RFID', 'FEED', 'ROLLBACK', 'STOP', 'SPEED', 'DRY', 'DRYSTOP', 'DRYTEMP', 'FAN', 'VALVE', 'FEEDCHECK', 'RAW'):
            self.gcode.register_command('A_' + _name, getattr(self, 'cmd_A_' + _name), desc=getattr(self, 'cmd_A_' + _name + '_help', ''))

    def _refresh_ace_devices(self, context):
        scan = self._scan_ace_devices(context)
        self._ace_present = set(scan)
        if self._ace_canonical is not None:
            self._ace_devices = list(self._ace_canonical)
        else:
            self._ace_devices = scan
        return scan

    def _is_ace_present(self, ace_index):
        if ace_index < 0 or ace_index >= len(self._ace_devices):
            return False
        if self._ace_canonical is None:
            return True
        return self._ace_devices[ace_index] in self._ace_present

    def _ace_path_sort_key(self, path):
        try:
            base = os.path.basename(path)
            segs = base.split(':')
            port_str = segs[1] if len(segs) >= 2 else ''
            port_tuple = tuple((int(x) for x in port_str.split('.') if x != ''))
        except (ValueError, IndexError):
            port_tuple = ()
        return (len(port_tuple), port_tuple, path)

    def _scan_ace_devices(self, context='unknown'):
        scan_start = time.monotonic()
        self._usb_stats['scans'] += 1
        ace_devices = []
        active_protocols = KNOWN_PROTOCOLS if self._enable_ace_v2 else tuple(p for p in KNOWN_PROTOCOLS if p is not AceProtocolV2)
        for protocol_cls in active_protocols:
            for path in protocol_cls.discover():
                if path in ace_devices:
                    continue
                self._ace_path_protocol[path] = protocol_cls
                ace_devices.append(path)
                real_dev = os.path.basename(os.path.realpath(path))
                logging.info('[multiACE] Found device %s (%s) protocol=%s' % (path, real_dev, protocol_cls.NAME))
        ace_devices.sort(key=self._ace_path_sort_key)
        scan_ms = (time.monotonic() - scan_start) * 1000
        self._usb_log.info('SCAN [%s] found=%d time=%.1fms devices=[%s]', context, len(ace_devices), scan_ms, ', '.join(('%s(%s)->%s' % (d, self._ace_path_protocol.get(d, type('_', (), {'NAME': '?'})).NAME, os.path.basename(os.path.realpath(d))) for d in ace_devices)))
        return ace_devices

    def _apply_log_levels(self):
        off = logging.CRITICAL + 1
        self._usb_log.setLevel(logging.DEBUG if self._usb_debug_enabled else off)
        gated = logging.DEBUG if self._state_debug_enabled else off
        self._telemetry_log.setLevel(gated)
        self._wiggle_log.setLevel(gated)
        self._fa_log.setLevel(logging.DEBUG if self.fa_debug else logging.WARNING)

    def _t(self, key, **params):
        v = getattr(self, '_i18n', None) or {}
        for p in key.split('.'):
            if not isinstance(v, dict):
                return key
            v = v.get(p)
            if v is None:
                return key
        if not isinstance(v, str):
            return key
        if not params:
            return v
        try:
            return v.format(**params)
        except Exception:
            return v

    def _disp(self, idx):
        if idx is None:
            return '–'
        try:
            return int(idx) + getattr(self, '_display_index_base', 0)
        except (TypeError, ValueError):
            return idx
    _WEB_PIDFILE = '/tmp/multiace_web.pid'

    def _stop_old_web(self):
        import signal
        try:
            with open(self._WEB_PIDFILE, 'r') as f:
                old_pid = int((f.read() or '0').strip())
        except (FileNotFoundError, ValueError, OSError):
            return
        if old_pid <= 0:
            return
        try:
            os.kill(old_pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        except OSError:
            return
        for _ in range(40):
            try:
                os.kill(old_pid, 0)
            except ProcessLookupError:
                logging.info('[multiACE] web stopped old pid %d', old_pid)
                return
            time.sleep(0.05)
        try:
            os.kill(old_pid, signal.SIGKILL)
            logging.info('[multiACE] web SIGKILLd old pid %d', old_pid)
        except OSError:
            pass

    def _spawn_multiace_web(self):
        if not self._enable_web:
            return
        self._stop_old_web()
        import subprocess
        backend = os.path.join(self._web_dir, 'backend')
        frontend = os.path.join(self._web_dir, 'frontend')
        if not os.path.isdir(backend) or not os.path.isfile(os.path.join(backend, 'main.py')):
            logging.info('[multiACE] web not installed at %s — skip', self._web_dir)
            return
        log_path = '/home/lava/printer_data/logs/multiace_web.log'
        try:
            log_fd = open(log_path, 'a', buffering=1)
        except Exception:
            log_fd = subprocess.DEVNULL
        env = dict(os.environ)
        env.update({'HOME': '/home/lava', 'USER': 'lava', 'PATH': '/home/lava/.local/bin:/usr/bin:/bin', 'PYTHONUNBUFFERED': '1', 'MOONRAKER_URL': 'http://127.0.0.1:7125', 'MULTIACE_CFG_PATH': '/home/lava/printer_data/config/extended/ace.cfg', 'MULTIACE_FRONTEND_DIR': frontend, 'MULTIACE_I18N_DIR': os.path.join(self._web_dir, 'i18n'), 'MULTIACE_LANGUAGE': self._language, 'MULTIACE_DISPLAY_INDEX_BASE': str(self._display_index_base), 'MULTIACE_WEB_VERSION': '%s+%s' % (MULTIACE_VERSION, MULTIACE_BUILD_TAG)})
        try:
            p = subprocess.Popen(['/usr/bin/python3', '-m', 'uvicorn', 'main:app', '--host', '127.0.0.1', '--port', str(self._web_port), '--log-level', 'warning'], cwd=backend, env=env, stdout=log_fd, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, close_fds=True, start_new_session=True)
        except Exception as e:
            logging.info('[multiACE] web spawn failed: %s', e)
            return
        try:
            with open(self._WEB_PIDFILE, 'w') as f:
                f.write(str(p.pid))
        except Exception as e:
            logging.info('[multiACE] web pidfile write failed: %s', e)
        logging.info('[multiACE] web spawned pid=%d on :%d (cwd=%s)', p.pid, self._web_port, backend)
        self.log_always(self._t('msg.web_running'))

    def _handle_ready(self):
        self.toolhead = self.printer.lookup_object('toolhead')
        self._spawn_multiace_web()
        self._refresh_slot_overrides()
        try:
            fd = self.printer.lookup_object('filament_detect', None)
            ptc = self.printer.lookup_object('print_task_config', None)
            if fd is not None and ptc is not None:
                orig_cb = ptc._rfid_filament_info_update_cb

                def _multiace_rfid_cb(channel, info, is_clear=False, _orig=orig_cb):
                    has_content = bool((info.get('VENDOR') or '').strip() or (info.get('MAIN_TYPE') or '').strip() or info.get('OFFICIAL'))
                    if is_clear and (not has_content) and (self._ace_mode != 'normal'):
                        logging.info('[multiACE] suppressing RFID clear on channel %d (mode=%s, multiACE manages)' % (channel, self._ace_mode))
                        return
                    return _orig(channel, info, is_clear)
                cbs = getattr(fd, '_notify_data_update_cb', None)
                if isinstance(cbs, list):
                    replaced = False
                    for i, cb in enumerate(cbs):
                        if cb is orig_cb:
                            cbs[i] = _multiace_rfid_cb
                            replaced = True
                            break
                    if not replaced:
                        cbs.append(_multiace_rfid_cb)
                    logging.info('[multiACE] filament_detect callback hook installed (clear-suppress + capture)')
        except Exception as e:
            logging.info('[multiACE] failed to install filament_detect hook: %s' % e)
        try:
            self._orig_set_ptc = self.gcode.register_command('SET_PRINT_FILAMENT_CONFIG', None)
            if self._orig_set_ptc is not None:
                self.gcode.register_command('SET_PRINT_FILAMENT_CONFIG', self._wrap_set_print_filament_config, desc='[multiACE] wrap SET_PRINT_FILAMENT_CONFIG to capture display edits as picker overrides')
        except Exception as e:
            logging.info('[multiACE] failed to wrap SET_PRINT_FILAMENT_CONFIG: %s' % e)
        for log in (self._state_log, self._usb_log, self._telemetry_log, self._wiggle_log):
            for handler in log.handlers:
                if hasattr(handler, 'doRollover'):
                    try:
                        handler.doRollover()
                    except Exception:
                        pass
        try:
            ace_mtime = os.path.getmtime(os.path.abspath(__file__))
            from datetime import datetime
            ace_timestamp = datetime.fromtimestamp(ace_mtime).strftime('%Y-%m-%d %H:%M:%S')
        except Exception:
            ace_timestamp = 'unknown'
        self.log_always(self._t('msg.version_line', version=MULTIACE_VERSION, codename=MULTIACE_CODENAME, build=MULTIACE_BUILD_TAG, ts=ace_timestamp))
        logging.info('[multiACE] Version %s (%s) build=%s file=%s' % (MULTIACE_VERSION, MULTIACE_CODENAME, MULTIACE_BUILD_TAG, ace_timestamp))
        self._ace_mode = 'normal'
        if self.save_variables:
            self._ace_mode = self.save_variables.allVariables.get('ace__mode', 'normal')
        if self._ace_mode == 'normal':
            logging.info('[multiACE] Normal mode — skipping ACE detection')
            return
        if self._ace_mode == 'multi':
            self._restore_head_source()
            self.printer.register_event_handler('extruder:activate_extruder', self._on_extruder_change)
        else:
            logging.info('[multiACE] SingleACE mode — no head_source tracking')
        self._refresh_ace_devices('startup')
        if self.ace_device_count is not None:
            expected = self.ace_device_count
            if len(self._ace_devices) < expected:
                self.log_always(self._t('msg.waiting_for_devices', expected=expected, count=len(self._ace_devices)))
                deadline = time.monotonic() + 20.0
                attempt = 0
                while time.monotonic() < deadline and len(self._ace_devices) < expected:
                    self.reactor.pause(self.reactor.monotonic() + 1.0)
                    attempt += 1
                    self._refresh_ace_devices('startup_wait_%d' % attempt)
            if len(self._ace_devices) < expected:
                self._ace_startup_failed = True
                self.log_error(self._t('msg.usb_unstable', expected=expected, count=len(self._ace_devices)))
                logging.info('[multiACE] Startup soft-fail (%d/%d ACEs) - skipping connect timer' % (len(self._ace_devices), expected))
                return
            self._ace_canonical = list(self._ace_devices)
            self._ace_present = set(self._ace_canonical)
            self.log_always(self._t('msg.all_expected_found', expected=expected))
        if self._ace_devices:
            logging.info('[multiACE] Found %d device(s): %s' % (len(self._ace_devices), str(self._ace_devices)))
            self.log_always(self._t('msg.found_devices', count=len(self._ace_devices)))
            saved_device = self.save_variables.allVariables.get(self.VARS_ACE_ACTIVE_DEVICE, None)
            if saved_device and saved_device in self._ace_devices:
                self._active_device_index = self._ace_devices.index(saved_device)
                logging.info('[multiACE] Restored active device %d: %s' % (self._active_device_index, saved_device))
            else:
                self._active_device_index = 0
            self.serial_id = self._ace_devices[self._active_device_index]
        elif self.serial_id:
            logging.info('[multiACE] No devices auto-detected, using configured serial: %s' % self.serial_id)
        else:
            self._ace_startup_failed = True
            self.log_error(self._t('msg.no_ace_serial_configured'))
            return
        self._queue = queue.Queue()
        all_ok = True
        CONNECT_ATTEMPTS = 3
        for idx in range(len(self._ace_devices)):
            ok = False
            for attempt in range(CONNECT_ATTEMPTS):
                ok = self._open_ace(idx)
                if ok:
                    break
                if attempt < CONNECT_ATTEMPTS - 1:
                    self._usb_log.info('RETRY [startup_connect] idx=%d attempt=%d/%d failed, retrying in 1s', idx, attempt + 1, CONNECT_ATTEMPTS)
                    time.sleep(1.0)
            if not ok:
                self.log_error(self._t('msg.open_ace_failed_attempts', ace=self._disp(idx), attempts=CONNECT_ATTEMPTS))
                all_ok = False
        if not all_ok:
            self.log_error(self._t('msg.not_all_aces_opened'))
        self._set_active_idx(self._active_device_index)

    def _hotplug_monitor(self, eventtime):
        if self._auto_feed_enabled or self._swap_in_progress:
            return eventtime + 2.0
        try:
            current = set(self._scan_ace_devices('hotplug'))
            known = set(self._ace_devices)
            now = self.reactor.monotonic()
            for dev in known - current:
                if dev not in self._hotplug_gone:
                    self._hotplug_gone[dev] = now
            for dev in list(self._hotplug_gone.keys()):
                if dev in current:
                    gone_time = now - self._hotplug_gone[dev]
                    del self._hotplug_gone[dev]
                    if gone_time >= 5.0:
                        fresh_devices = sorted(current)
                        if dev in fresh_devices:
                            new_index = fresh_devices.index(dev)
                            self.log_always(self._t('msg.ace_returned_switching', ace=self._disp(new_index), seconds=gone_time))
                            self.reactor.register_async_callback(lambda et, idx=new_index: self.gcode.run_script_from_command('ACE_SWITCH TARGET=%d' % idx))
                            return eventtime + 10.0
            for dev, gone_since in list(self._hotplug_gone.items()):
                gone_time = now - gone_since
                if gone_time >= 5.0 and gone_time < 7.0:
                    self.log_always(self._t('msg.ace_removed_reenable'))
        except Exception as e:
            logging.info('[multiACE] Hotplug monitor error: %s' % str(e))
        return eventtime + 2.0

    def _handle_disconnect(self):
        logging.info('[multiACE] Closing all ACE connections')
        for idx in list(self._serials.keys()):
            try:
                self._disconnect_from(idx)
            except Exception:
                pass
        self._queue = None

    def get_load_length(self, ace_idx, slot):
        v = self._ace_section_load_length_slot.get((ace_idx, slot))
        if v is not None:
            return v
        v = self._ace_section_load_length.get(ace_idx)
        if v is not None:
            return v
        return self.head_load_length.get(slot, self.load_length)

    def get_retract_length(self, ace_idx, slot):
        v = self._ace_section_retract_length_slot.get((ace_idx, slot))
        if v is not None:
            return v
        v = self._ace_section_retract_length.get(ace_idx)
        if v is not None:
            return v
        return self.retract_length

    def _fa_trace(self, msg):
        self._fa_log.info(msg)

    def _on_print_start(self, *args):
        if self._ace_mode == 'multi':
            self._ghost_heads = set()
            stale_heads = []
            ghost_heads = []
            for head in range(4):
                sensor = self.printer.lookup_object('filament_motion_sensor e%d_filament' % head, None)
                if sensor is None:
                    continue
                detected = sensor.get_status(0)['filament_detected']
                src = self._head_source.get(head)
                if detected and src is None:
                    ghost_heads.append(head)
                elif not detected and src is not None:
                    stale_heads.append(head)
                    self._head_source[head] = None
            if stale_heads:
                try:
                    self._save_head_source()
                except Exception:
                    pass
                logging.info('[multiACE] Print start: cleared stale head_source for head(s) %s (sensor reports no filament)' % ', '.join(('T%d' % h for h in stale_heads)))
            if ghost_heads:
                self._ghost_heads = set(ghost_heads)
                head_list = ', '.join(('T%d' % h for h in ghost_heads))
                self.log_error(self._t('msg.ghost_heads', heads=head_list))
            for head in range(4):
                source = self._head_source.get(head)
                if source is None:
                    continue
                ace_idx = source['ace_index']
                if ace_idx >= len(self._ace_devices):
                    self.log_error(self._t('msg.print_start_head_needs_unavailable', head=head, ace=self._disp(ace_idx), count=len(self._ace_devices)))
                    continue
                if not self._connected_per_ace.get(ace_idx, False):
                    self.log_error(self._t('msg.print_start_head_needs_disconnected', head=head, ace=self._disp(ace_idx)))
        self._auto_feed_enabled = True
        self._fa_context = 'print'
        logging.info('[multiACE] Print started — auto-feed enabled')
        self._fa_trace('gate OPEN (context=print) via _on_print_start')
        try:
            extruder = self.toolhead.get_extruder()
            head_index = getattr(extruder, 'extruder_index', getattr(extruder, 'extruder_num', None))
        except Exception:
            head_index = None
        if head_index is None:
            return
        source = self._head_source.get(head_index)
        if source is None:
            return
        target_ace = source['ace_index']
        target_slot = source['slot']
        if target_ace >= len(self._ace_devices):
            return
        if not self._connected_per_ace.get(target_ace, False):
            self._audit_state('PRINT_START', {'head': head_index, 'target_ace': target_ace, 'action': 'ace_not_connected'})
            return
        if self._active_device_index != target_ace:
            self._set_active_idx(target_ace)
        try:
            self._arm_fa_for(target_ace, target_slot)
            self.log_always(self._t('msg.print_start_fa_enabled', ace=self._disp(target_ace), slot=self._disp(target_slot), head=head_index))
            self._audit_state('PRINT_START', {'head': head_index, 'target_ace': target_ace, 'target_slot': target_slot, 'action': 'feed_assist_enabled'})
        except Exception as e:
            logging.info('[multiACE] print-start feed_assist enable failed: %s' % e)
            self._audit_state('PRINT_START', {'head': head_index, 'action': 'feed_assist_enable_failed', 'error': str(e)[:200]})

    def _on_print_end(self, *args):
        self._auto_feed_enabled = False
        self._fa_context = 'idle'
        logging.info('[multiACE] Print ended — auto-feed disabled')
        self._fa_trace('gate CLOSE (context=idle) via _on_print_end')
        stopped_any = False
        for idx in range(len(self._ace_devices)):
            if self._feed_assist_per_ace.get(idx, -1) != -1:
                try:
                    self._disarm_fa_for(idx)
                    stopped_any = True
                except Exception as e:
                    logging.info('[multiACE] print-end stop_feed_assist[%d] failed: %s' % (idx, e))
        if stopped_any:
            self._audit_state('PRINT_END', {'action': 'feed_assist_disabled'})

    def _color_message(self, msg):
        try:
            html_msg = msg.format('</span>', '<span style="color:#FFFF00">', '<span style="color:#90EE90">', '<span style="color:#458EFF">', '<b>', '</b>')
        except (IndexError, KeyError, ValueError) as e:
            html_msg = msg
        return html_msg

    def log_warning(self, msg):
        c_msg = self._color_message(f'{{1}}{msg}{{0}}')
        self.gcode.respond_raw(c_msg)

    def log_always(self, msg: str, color=False):
        c_msg = self._color_message(msg) if color else msg
        self.gcode.respond_raw(c_msg)

    def log_error(self, msg):
        self.error_msg = msg
        self.gcode.respond_raw(f'!! {msg}')

    def _restore_pos_for_pause(self, saved_pos):
        if not saved_pos:
            return
        try:
            self.gcode.run_script_from_command('G90')
            self.gcode.run_script_from_command('G0 Z%.3f F600' % (saved_pos[2] + 3.0))
            self.gcode.run_script_from_command('G0 Y%.3f F12000' % saved_pos[1])
            self.gcode.run_script_from_command('G0 X%.3f F12000' % saved_pos[0])
            self.gcode.run_script_from_command('G0 Z%.3f F600' % (saved_pos[2] + 2.0))
            self.toolhead.wait_moves()
            logging.info('[multiACE] Swap PAUSE: restored pos X=%.2f Y=%.2f Z=%.2f (pre-PAUSE, prevents RESUME-traverse ram)' % (saved_pos[0], saved_pos[1], saved_pos[2]))
        except Exception as e:
            logging.info('[multiACE] Swap PAUSE: pos restore failed: %s' % e)

    def _swap_back_to_orig_for_pause(self, switched_head, orig_ext_name):
        if not switched_head:
            return
        try:
            orig_head_idx = 0 if orig_ext_name == 'extruder' else int(orig_ext_name.replace('extruder', ''))
            logging.info('[multiACE] Swap PAUSE: switching active extruder back to %s before pause (was on swap head)' % orig_ext_name)
            self.gcode.run_script_from_command('T%d A0' % orig_head_idx)
            self.toolhead.wait_moves()
        except Exception as e:
            logging.info('[multiACE] Swap PAUSE: T-switch back to %s failed: %s' % (orig_ext_name, e))

    def _pause_for_recovery(self, phase, display_msg, detail_msg, recovery_steps):
        short = display_msg[:20]
        try:
            self.gcode.run_script_from_command('M117 %s' % short)
        except Exception:
            pass
        try:
            self.gcode.run_script_from_command('RESPOND TYPE=error MSG="[multiACE] PAUSE %s: %s"' % (phase, detail_msg.replace('"', "'")))
        except Exception:
            pass
        for i, step in enumerate(recovery_steps, 1):
            try:
                self.gcode.run_script_from_command('RESPOND TYPE=echo MSG="  %d. %s"' % (i, step.replace('"', "'")))
            except Exception:
                pass
        self.error_msg = detail_msg
        self._audit_state('PAUSE_RECOVERY', {'phase': phase, 'display_msg': short, 'detail': detail_msg, 'steps': recovery_steps})
        try:
            short_msg = ('[multiACE] %s: %s' % (phase, detail_msg)).replace('"', "'")
            self.gcode.run_script_from_command('RAISE_EXCEPTION ID=522 INDEX=0 CODE=0 MSG="%s" LEVEL=2' % short_msg[:200])
        except Exception:
            pass
        self.gcode.run_script_from_command('PAUSE')

    def save_variable(self, variable, value, write=False):
        self.save_variables.allVariables[variable] = value
        if write:
            self.write_variables()

    def rgb2hex(self, r, g, b):
        return '%02X%02X%02X' % (r, g, b)

    def delete_variable(self, variable, write=False):
        _ = self.save_variables.allVariables.pop(variable, None)
        if write:
            self.write_variables()

    def write_variables(self):
        mmu_vars_revision = self.save_variables.allVariables.get(self.VARS_ACE_REVISION, 0) + 1
        self.gcode.run_script_from_command(f'SAVE_VARIABLE VARIABLE={self.VARS_ACE_REVISION} VALUE={mmu_vars_revision}')

    def _serial_disconnect(self):
        idx = self._active_device_index
        self._disconnect_from(idx)
        self._serial = None
        self._connected = False
        self.heartbeat_timer = None
        self.ace_dev_fd = None

    def _connect(self, eventtime):
        idx = self._active_device_index
        ok = self._open_ace(idx)
        if ok:
            self._set_active_idx(idx)
            return self.reactor.NEVER
        return eventtime + 1.0

    def _make_default_info(self, idx=None):
        if idx is None:
            idx = self._active_device_index
        protocol = self._protocols.get(idx)
        if protocol is None:
            return AceProtocolV1().make_default_info()
        return protocol.make_default_info()

    def _next_request_id_for(self, idx):
        with self._seq_lock:
            rid = self._request_ids.get(idx, 0) + 1
            if rid >= 300000:
                rid = 1
            self._request_ids[idx] = rid
            return rid

    def _set_active_idx(self, idx):
        if idx < 0 or idx >= len(self._ace_devices):
            return False
        self._active_device_index = idx
        self.serial_id = self._ace_devices[idx]
        self._serial = self._serials.get(idx)
        self._connected = self._connected_per_ace.get(idx, False)
        self._serial_failed = self._serial_failed_per_ace.get(idx, False)
        self._feed_assist_index = self._feed_assist_per_ace.get(idx, -1)
        info = self._info_per_ace.get(idx)
        if info is not None:
            self._info = info
        if idx in self._request_ids:
            self._request_id = self._request_ids[idx]
        gate_list = self._gate_status_per_ace.get(idx)
        if gate_list is not None:
            self.gate_status = gate_list
        self.ace_dev_fd = self._ace_dev_fds.get(idx)
        self.heartbeat_timer = self._heartbeat_timers.get(idx)
        try:
            self.gcode.run_script_from_command('SAVE_VARIABLE VARIABLE=%s VALUE="\'%s\'"' % (self.VARS_ACE_ACTIVE_DEVICE, self.serial_id))
        except Exception:
            pass
        return True

    def _open_ace(self, idx):
        if idx >= len(self._ace_devices):
            return False
        serial_path = self._ace_devices[idx]
        logging.info('[multiACE] Try connecting ACE %d (%s)' % (idx, serial_path))
        self._usb_log.info('CONNECT attempt idx=%d serial=%s', idx, serial_path)
        connect_start = time.monotonic()
        old_ht = self._heartbeat_timers.pop(idx, None)
        if old_ht is not None:
            try:
                self.reactor.unregister_timer(old_ht)
            except Exception:
                pass
        old_vt = self._v2_velocity_timers.pop(idx, None)
        if old_vt is not None:
            try:
                self.reactor.unregister_timer(old_vt)
            except Exception:
                pass
        self._v2_velocity_state.pop(idx, None)
        old_stop = self._thread_stop_flags.pop(idx, None)
        if old_stop is not None:
            old_stop.set()
        old_fd = self._ace_dev_fds.pop(idx, None)
        if old_fd is not None:
            try:
                self.reactor.set_fd_wake(old_fd, False, False)
            except Exception:
                pass
        old_ser = self._serials.pop(idx, None)
        if old_ser is not None:
            try:
                if old_ser.is_open:
                    old_ser.close()
            except Exception:
                pass
        for thread_dict in (self._reader_threads, self._writer_threads):
            old_t = thread_dict.pop(idx, None)
            if old_t is not None:
                try:
                    old_t.join(timeout=0.5)
                except Exception:
                    pass
        self._writer_queues.pop(idx, None)
        self._cb_locks.pop(idx, None)
        self._v2_filament_info_per_ace.pop(idx, None)
        self._v2_filament_info_pending.pop(idx, None)

        def info_callback(self, response):
            if response.get('msg') != 'success':
                self.log_error(self._t('msg.ace_error_generic', error=response.get('msg')))
            result = response.get('result', {})
            model = result.get('model', 'Unknown')
            firmware = result.get('firmware', 'Unknown')
            self._ace_models[idx] = (model, firmware)
            self._usb_log.info('CONNECT info idx=%d model=%s firmware=%s', idx, model, firmware)
            self.log_always(self._t('msg.ace_connected', ace=self._disp(idx), model=model, firmware=firmware), True)
        try:
            protocol_cls = self._ace_path_protocol.get(serial_path, AceProtocolV1)
            protocol = protocol_cls()
            self._protocols[idx] = protocol
            ser = protocol.open_transport(serial_path, self.baud or protocol.DEFAULT_BAUD)
            if not ser.is_open:
                return False
            self._serials[idx] = ser
            self._connected_per_ace[idx] = True
            self._serial_failed_per_ace[idx] = False
            self._request_ids[idx] = 0
            self._callback_maps[idx] = {}
            self._read_buffers[idx] = bytearray()
            self._info_per_ace[idx] = protocol.make_default_info()
            self._feed_assist_per_ace.setdefault(idx, -1)
            self._gate_status_per_ace[idx] = [GATE_UNKNOWN, GATE_UNKNOWN, GATE_UNKNOWN, GATE_UNKNOWN]
            connect_ms = (time.monotonic() - connect_start) * 1000
            self._usb_stats['connects'] += 1
            self._usb_log.info('CONNECT success idx=%d serial=%s time=%.1fms', idx, serial_path, connect_ms)
            logging.info('[multiACE] Connected to ACE %d (%s)' % (idx, serial_path))
            use_threads = protocol.NAME == 'v2'
            if use_threads:
                self._cb_locks[idx] = threading.Lock()
                self._writer_queues[idx] = queue.Queue()
                self._thread_stop_flags[idx] = threading.Event()
                rt = threading.Thread(target=self._make_v2_reader_thread_for(idx, ser, protocol), daemon=True, name='ace%d-reader' % idx)
                wt = threading.Thread(target=self._make_v2_writer_thread_for(idx, ser, protocol), daemon=True, name='ace%d-writer' % idx)
                rt.start()
                wt.start()
                self._reader_threads[idx] = rt
                self._writer_threads[idx] = wt
                self._usb_log.info('CONNECT idx=%d V2 reader+writer threads started', idx)
            else:
                fd = self.reactor.register_fd(ser.fileno(), self._make_reader_cb_for(idx))
                self._ace_dev_fds[idx] = fd
            ht = self.reactor.register_timer(self._make_heartbeat_tick_for(idx), self.reactor.NOW)
            self._heartbeat_timers[idx] = ht
            if protocol.NAME == 'v2':
                vt = self.reactor.register_timer(self._make_v2_velocity_tick_for(idx), self.reactor.NOW)
                self._v2_velocity_timers[idx] = vt

                def _fc_cb(self, response):
                    code = response.get('code', -1) if response else -1
                    msg = response.get('msg', '?') if response else 'no-response'
                    self._fa_log.info('[v2-init] ace=%d SET_FEED_CHECK 110/100 -> code=%d msg=%s' % (idx, code, msg))
                try:
                    self.send_request_to(idx, {'method': 'set_feed_check', 'params': {'check_length': 110, 'error_length': 100}}, _fc_cb)
                except Exception as e:
                    self._fa_log.info('[v2-init] ace=%d SET_FEED_CHECK enqueue failed: %s' % (idx, e))
            handshake_requests = protocol.initial_handshake_requests() or []
            for req in handshake_requests:
                method = req.get('method', '')
                if method == 'get_info':
                    cb = lambda self, response: info_callback(self, response)
                else:
                    cb = lambda self, response: None
                self.send_request_to(idx, request=dict(req), callback=cb)
            return True
        except serial.serialutil.SerialException:
            self._usb_stats['connect_failures'] += 1
            self._usb_log.warning('CONNECT failed idx=%d SerialException', idx)
            logging.info('[multiACE] Conn error idx=%d' % idx)
            return False
        except Exception as e:
            self._usb_stats['connect_failures'] += 1
            self._usb_log.warning('CONNECT failed idx=%d error=%s', idx, str(e))
            logging.info('ACE Error idx=%d: %s' % (idx, str(e)))
            return False

    def _disconnect_from(self, idx):
        self._usb_stats['disconnects'] += 1
        stop = self._thread_stop_flags.pop(idx, None)
        if stop is not None:
            stop.set()
        ser = self._serials.get(idx)
        if ser is not None:
            self._usb_log.info('DISCONNECT idx=%d serial=%s', idx, self._ace_devices[idx] if idx < len(self._ace_devices) else '?')
            try:
                if ser.is_open:
                    ser.close()
            except Exception:
                pass
        for thread_dict in (self._reader_threads, self._writer_threads):
            t = thread_dict.pop(idx, None)
            if t is not None:
                try:
                    t.join(timeout=0.5)
                except Exception:
                    pass
        self._writer_queues.pop(idx, None)
        self._cb_locks.pop(idx, None)
        self._v2_filament_info_per_ace.pop(idx, None)
        self._v2_filament_info_pending.pop(idx, None)
        self._connected_per_ace[idx] = False
        ht = self._heartbeat_timers.pop(idx, None)
        if ht is not None:
            try:
                self.reactor.unregister_timer(ht)
            except Exception:
                pass
        vt = self._v2_velocity_timers.pop(idx, None)
        if vt is not None:
            try:
                self.reactor.unregister_timer(vt)
            except Exception:
                pass
        self._v2_velocity_state.pop(idx, None)
        fd = self._ace_dev_fds.pop(idx, None)
        if fd is not None:
            try:
                self.reactor.set_fd_wake(fd, False, False)
            except Exception:
                pass
        self._serials.pop(idx, None)

    def _make_reader_cb_for(self, idx):

        def _reader(eventtime):
            ser = self._serials.get(idx)
            if ser is None or not ser.is_open:
                return
            try:
                if ser.in_waiting:
                    raw_bytes = ser.read(size=ser.in_waiting)
                    self._process_data_for(idx, raw_bytes)
            except Exception:
                logging.info('ACE[%d] error reading/processing: %s' % (idx, traceback.format_exc()))
                logging.info('Unable to communicate with ACE %d' % idx)
        return _reader

    def _make_v2_writer_thread_for(self, idx, ser, protocol):
        q = self._writer_queues[idx]
        stop = self._thread_stop_flags[idx]

        def _loop():
            while not stop.is_set():
                try:
                    request = q.get(timeout=0.5)
                except queue.Empty:
                    continue
                if stop.is_set():
                    break
                try:
                    if 'id' not in request:
                        request['id'] = self._next_request_id_for(idx)
                    data = protocol.encode_request(request, next_id=lambda: self._next_request_id_for(idx))
                    ser.write(data)
                except Exception as e:
                    if stop.is_set():
                        break
                    logging.info('[multiACE] V2 writer ACE %d error: %s' % (idx, e))
                    time.sleep(0.05)
        return _loop

    def _make_v2_reader_thread_for(self, idx, ser, protocol):
        stop = self._thread_stop_flags[idx]
        buf = bytearray()

        def _loop():
            while not stop.is_set():
                try:
                    chunk = ser.read(256)
                except Exception:
                    if stop.is_set():
                        break
                    time.sleep(0.05)
                    continue
                if stop.is_set():
                    break
                if not chunk:
                    continue
                buf.extend(chunk)
                try:
                    frames = protocol.decode_frames(buf)
                except Exception as e:
                    logging.info('[multiACE] V2 decode error ACE %d: %s' % (idx, e))
                    continue
                for ret in frames:
                    msg_id = ret.get('id')
                    cb = None
                    lock = self._cb_locks.get(idx)
                    if lock is not None:
                        with lock:
                            cb_map = self._callback_maps.get(idx, {})
                            cb = cb_map.pop(msg_id, None)
                    if cb is not None:
                        try:
                            self.reactor.register_async_callback(lambda et, c=cb, r=ret: c(self=self, response=r))
                        except Exception as e:
                            logging.info('[multiACE] V2 async dispatch failed ACE %d: %s' % (idx, e))
        return _loop

    def _process_data_for(self, idx, raw_bytes):
        buf = self._read_buffers.get(idx)
        if buf is None:
            buf = bytearray()
            self._read_buffers[idx] = buf
        buf += raw_bytes
        protocol = self._protocols.get(idx)
        if protocol is None:
            return
        for ret in protocol.decode_frames(buf):
            msg_id = ret.get('id')
            cb_map = self._callback_maps.get(idx, {})
            if msg_id in cb_map:
                callback = cb_map.pop(msg_id)
                callback(self=self, response=ret)

    def send_request_to(self, idx, request, callback):
        info = self._info_per_ace.get(idx)
        if info is None:
            info = self._make_default_info(idx)
            self._info_per_ace[idx] = info
        info['status'] = 'busy'
        msg_id = self._next_request_id_for(idx)
        cb_map = self._callback_maps.setdefault(idx, {})
        method = request.get('method', '?')
        params = request.get('params', {}) or {}
        slot_repr = params.get('index', params.get('slot', '?'))
        len_repr = params.get('length', '?')
        speed_repr = params.get('speed', '?')
        trace_request = method != 'get_status'
        if trace_request:
            self._fa_log.info('SEND ACE %d id=%d method=%s slot=%s len=%s speed=%s' % (idx, msg_id, method, slot_repr, len_repr, speed_repr))
        original_cb = callback

        def _traced_cb(self, response):
            if trace_request:
                try:
                    self._fa_log.info('RESP ACE %d id=%s method=%s slot=%s code=%s msg=%s' % (idx, response.get('id', '?'), method, slot_repr, response.get('code', '?'), response.get('msg', '')))
                except Exception:
                    pass
            original_cb(self=self, response=response)
        request['id'] = msg_id
        protocol = self._protocols.get(idx)
        if protocol is not None and protocol.NAME == 'v2':
            lock = self._cb_locks.get(idx)
            if lock is not None:
                with lock:
                    cb_map[msg_id] = _traced_cb
            else:
                cb_map[msg_id] = _traced_cb
            wq = self._writer_queues.get(idx)
            if wq is not None:
                try:
                    wq.put_nowait(request)
                except Exception as e:
                    logging.error('[multiACE] V2 writer queue put failed for ACE %d: %s' % (idx, e))
            else:
                logging.error('[multiACE] V2 writer queue missing for ACE %d' % idx)
            return
        cb_map[msg_id] = _traced_cb
        self._send_request_to(idx, request)

    def _send_request_to(self, idx, request):
        if 'id' not in request:
            request['id'] = self._next_request_id_for(idx)
        protocol = self._protocols.get(idx)
        if protocol is None:
            raise Exception('[multiACE] no protocol bound for ACE %d' % idx)
        try:
            data = protocol.encode_request(request, next_id=lambda: self._next_request_id_for(idx))
        except ValueError as e:
            logging.error('ACE[%d]: %s' % (idx, str(e)))
            return
        ser = self._serials.get(idx)
        if ser is None or self._serial_failed_per_ace.get(idx, False):
            raise Exception('[multiACE] serial[%d] unavailable' % idx)
        try:
            ser.write(data)
            return
        except Exception as e:
            err_first = str(e)
            logging.info('ACE[%d]: Error writing to serial: %s — attempting reconnect+retry' % (idx, err_first))
            self._usb_stats['errno5_total'] += 1
            now = time.monotonic()
            self._errno5_recent = [(i, t) for i, t in self._errno5_recent if now - t < 1.5]
            self._errno5_recent.append((idx, now))
            distinct_aces = set((i for i, _ in self._errno5_recent))
            if len(distinct_aces) >= 2:
                self._usb_stats['cascades'] += 1
                self.log_error(self._t('msg.cascade_detected', count=len(distinct_aces), total=self._usb_stats['cascades']))
                self._errno5_recent = []
            try:
                self._state_log.warning('SERIAL_WRITE_FAILED_FIRST idx=%d error=%s', idx, err_first)
            except Exception:
                pass
            saved_cb_map = dict(self._callback_maps.get(idx, {}))
            try:
                if ser.is_open:
                    ser.close()
            except Exception:
                pass
            self._connected_per_ace[idx] = False
            reconnected = False
            for attempt, delay in enumerate((0.35, 1.0, 2.0), start=1):
                try:
                    self.reactor.pause(self.reactor.monotonic() + delay)
                except Exception:
                    pass
                try:
                    reconnected = self._open_ace(idx)
                except Exception as ce:
                    logging.info('[multiACE] Sync reconnect[%d] attempt %d raised: %s' % (idx, attempt, str(ce)))
                    reconnected = False
                if reconnected:
                    break
                logging.info('[multiACE] Sync reconnect[%d] attempt %d/3 failed' % (idx, attempt))
            if reconnected:
                new_cb_map = self._callback_maps.setdefault(idx, {})
                for mid, cb in saved_cb_map.items():
                    if mid not in new_cb_map:
                        new_cb_map[mid] = cb
                new_ser = self._serials.get(idx)
                if new_ser is not None:
                    try:
                        new_ser.write(data)
                        self._usb_stats['errno5_recovered'] += 1
                        self.log_always(self._t('msg.serial_write_recovered', ace=self._disp(idx)))
                        try:
                            self._state_log.info('SERIAL_WRITE_RECOVERED idx=%d', idx)
                            self._audit_state('SERIAL_WRITE_RECOVERED', {'idx': idx})
                        except Exception:
                            pass
                        self._serial_failed_per_ace[idx] = False
                        return
                    except Exception as e2:
                        err_second = str(e2)
                else:
                    err_second = 'no_serial_after_reconnect'
            else:
                err_second = 'reconnect_failed'
            self._usb_stats['errno5_unrecovered'] += 1
            try:
                self._state_log.warning('SERIAL_WRITE_FAILED idx=%d error=%s first_error=%s', idx, err_second, err_first)
            except Exception:
                pass
            self._handle_per_ace_failure(idx, err_second)
            raise Exception('[multiACE] serial[%d] write failed (reconnect+retry both failed)' % idx)

    def _handle_per_ace_failure(self, idx, err):
        was_failed = self._serial_failed_per_ace.get(idx, False)
        self._serial_failed_per_ace[idx] = True
        if not was_failed:
            self.log_error(self._t('msg.ace_serial_failed', ace=self._disp(idx), error=err))
            try:
                self._state_log.error('ACE_FAILED idx=%d error=%s', idx, err)
                self._audit_state('ACE_FAILED', {'idx': idx, 'error': err})
            except Exception:
                pass
            try:
                self._disconnect_from(idx)
            except Exception:
                pass
            if not self._serial_failed_pause_sent:
                self._serial_failed_pause_sent = True

                def _do_pause(eventtime):
                    try:
                        self.gcode.run_script('PAUSE')
                    except Exception as pe:
                        logging.info('[multiACE] PAUSE call failed: %s' % str(pe))
                    try:
                        self.printer.invoke_async_shutdown('[multiACE] ACE %d permanently failed — print stopped' % idx)
                    except Exception:
                        pass
                    return self.reactor.NEVER
                try:
                    self.reactor.register_timer(_do_pause, self.reactor.NOW)
                except Exception:
                    pass

    def _arm_fa_for(self, idx, slot):
        self._fa_trace('_arm_fa_for(idx=%d, slot=%d) called; gate=%s context=%s' % (idx, slot, self._auto_feed_enabled, self._fa_context))
        if not self._auto_feed_enabled:
            logging.info('[multiACE] FA suppressed (gate off): idx=%d slot=%d' % (idx, slot))
            return
        if self._fa_context == 'print' and idx in self._fa_print_disable:
            logging.info('[multiACE] FA suppressed for ACE %d during print (fa_print_disable)' % idx)
            return
        if self._fa_context == 'load' and idx in self._fa_load_disable:
            logging.info('[multiACE] FA suppressed for ACE %d during load (fa_load_disable)' % idx)
            return
        prev_slot = self._feed_assist_per_ace.get(idx, -1)
        if prev_slot == slot:
            logging.info('[multiACE] FA _start skipped: prev_slot=%d == slot=%d (already running)' % (prev_slot, slot))
            return
        logging.info('[multiACE] FA _start proceeding: idx=%d slot=%d prev_slot=%d' % (idx, slot, prev_slot))
        any_active_before = any((s != -1 for s in self._feed_assist_per_ace.values()))
        now = time.monotonic()
        if not any_active_before and self._fa_context == 'print':
            gap_ms = int((now - self._fa_last_active_ts) * 1000)
            if gap_ms > self._fa_gap_threshold_ms:
                self._telemetry('FA_GAP', {'gap_ms': gap_ms, 'resumed_ace': idx, 'resumed_slot': slot, 'context': self._fa_context})
        self._fa_last_active_ts = now
        self._feed_assist_per_ace[idx] = slot
        if idx == self._active_device_index:
            self._feed_assist_index = slot
        max_retries = self._fa_start_retries
        retry_delay = self._fa_start_retry_delay
        settle_delay = self._fa_settle_after_stop

        def start_callback_factory(attempt):

            def start_callback(self, response):
                code = response.get('code', 0)
                msg = (response.get('msg', '') or '').lower()
                if not self._auto_feed_enabled:
                    return
                if self._feed_assist_per_ace.get(idx, -1) != slot:
                    return
                if code == 0 and (msg == 'success' or msg == ''):
                    if attempt > 0:
                        self._fa_log.warning('start_feed_assist OK after %d retry(s): ACE %d slot %d' % (attempt, idx, slot))
                    return
                if msg == 'forbidden' and attempt < max_retries:
                    next_attempt = attempt + 1
                    self._fa_log.info('start_feed_assist FORBIDDEN, retry %d/%d in %.1fs: ACE %d slot %d' % (next_attempt, max_retries, retry_delay, idx, slot))

                    def _retry(eventtime):
                        if not self._auto_feed_enabled:
                            return self.reactor.NEVER
                        if self._feed_assist_per_ace.get(idx, -1) != slot:
                            return self.reactor.NEVER
                        try:
                            self.send_request_to(idx, {'method': 'start_feed_assist', 'params': {'index': slot}}, start_callback_factory(next_attempt))
                            vstate = self._v2_velocity_state.get(idx)
                            if vstate is not None:
                                vstate['last_arm_time'] = self.reactor.monotonic()
                            self._fa_log.info('start_feed_assist RETRY %d/%d sent: ACE %d slot %d' % (next_attempt, max_retries, idx, slot))
                        except Exception as e:
                            self.log_error(self._t('msg.fa_retry_send_failed', error=e))
                            self._fa_log.error('start_feed_assist RETRY send failed: %s' % e)
                        return self.reactor.NEVER
                    self.reactor.register_timer(_retry, self.reactor.monotonic() + retry_delay)
                    return
                final_msg = self._t('msg.fa_failed_final', attempts=attempt + 1, ace=self._disp(idx), slot=self._disp(slot), code=code, msg=response.get('msg', ''))
                self.log_error(final_msg)
                self._fa_log.error(final_msg)
            return start_callback

        def _send_start():
            try:
                self.send_request_to(idx, {'method': 'start_feed_assist', 'params': {'index': slot}}, start_callback_factory(0))
                vstate = self._v2_velocity_state.get(idx)
                if vstate is not None:
                    vstate['last_arm_time'] = self.reactor.monotonic()
                logging.info('[multiACE] FA start_feed_assist SENT to ACE %d slot %d' % (idx, slot))
            except Exception as e:
                logging.info('[multiACE] send start_feed_assist to ACE %d failed: %s' % (idx, e))
        if prev_slot != -1:
            try:
                self.send_request_to(idx, {'method': 'stop_feed_assist', 'params': {'index': prev_slot}}, lambda *a, **kw: None)
                logging.info('[multiACE] FA pre-start stop sent: ACE %d slot %d (before start slot %d, settle %.1fs)' % (idx, prev_slot, slot, settle_delay))
            except Exception as e:
                logging.info('[multiACE] pre-start stop_feed_assist failed: %s' % e)

            def _delayed_start(eventtime):
                if not self._auto_feed_enabled:
                    self._fa_trace('post-stop delayed start SUPPRESSED (gate closed): idx=%d slot=%d' % (idx, slot))
                    return self.reactor.NEVER
                if self._feed_assist_per_ace.get(idx, -1) != slot:
                    self._fa_trace('post-stop delayed start SUPPRESSED (slot changed): idx=%d expected=%d actual=%d' % (idx, slot, self._feed_assist_per_ace.get(idx, -1)))
                    return self.reactor.NEVER
                _send_start()
                return self.reactor.NEVER
            self.reactor.register_timer(_delayed_start, self.reactor.monotonic() + settle_delay)
        else:
            _send_start()

    def _disarm_fa_for(self, idx):
        prev_slot = self._feed_assist_per_ace.get(idx, -1)
        if prev_slot == -1:
            return
        self._feed_assist_per_ace[idx] = -1
        if idx == self._active_device_index:
            self._feed_assist_index = -1
        if not any((s != -1 for s in self._feed_assist_per_ace.values())):
            self._fa_last_active_ts = time.monotonic()

        def callback(self, response):
            if response.get('code', 0) != 0:
                self.log_error(self._t('msg.ace_error_stop_fa', ace=self._disp(idx), error=response.get('msg')))
        try:
            self.send_request_to(idx, {'method': 'stop_feed_assist', 'params': {'index': prev_slot}}, callback)
        except Exception as e:
            logging.info('[multiACE] send stop_feed_assist to ACE %d failed: %s' % (idx, e))

    def _disable_feed_assist_all(self):

        def _noop_cb(self, response):
            if response.get('code', 0) != 0:
                self.log_error(self._t('msg.ace_error_generic', error=response.get('msg')))
        any_running = False
        for idx in sorted(list(self._feed_assist_per_ace.keys())):
            slot = self._feed_assist_per_ace.get(idx, -1)
            if slot == -1:
                continue
            if not self._connected_per_ace.get(idx, False):
                logging.info('[multiACE] _disable_feed_assist_all: skip ACE %d (disconnected)' % idx)
                self._feed_assist_per_ace[idx] = -1
                continue
            gate_list = self._gate_status_per_ace.get(idx, [GATE_UNKNOWN] * 4)
            if 0 <= slot < len(gate_list) and gate_list[slot] == GATE_EMPTY:
                logging.info('[multiACE] _disable_feed_assist_all: skip ACE %d slot %d (empty)' % (idx, slot))
                self._feed_assist_per_ace[idx] = -1
                continue
            proto = self._protocols.get(idx) if hasattr(self, '_protocols') else None
            if proto is not None and getattr(proto, 'NAME', None) == 'v2':
                logging.info('[multiACE] _disable_feed_assist_all: keep ACE %d armed (V2 — velocity tracker handles mode switch)' % idx)
                continue
            any_running = True
            try:
                self.wait_ace_ready_on(idx)
                self.send_request_to(idx, {'method': 'unwind_filament', 'params': {'index': slot, 'length': 5, 'speed': 80}}, _noop_cb)
                self.dwell(delay=5.0 / 80.0 + 0.1)
                self.wait_ace_ready_on(idx)
                self._disarm_fa_for(idx)
                self.wait_ace_ready_on(idx)
            except Exception as e:
                logging.info('[multiACE] _disable_feed_assist_all: error on idx %d: %s' % (idx, e))
        if self._feed_assist_index != -1:
            self._feed_assist_index = -1
        if any_running:
            self.dwell(0.3)

    def _enable_feed_assist_for_head(self, head):
        source = self._head_source.get(head)
        if source is None:
            logging.info('[multiACE] _enable_feed_assist_for_head: no head_source for head %d, skipping FA (use ACE_LOAD_HEAD to set source first)' % head)
            return
        target_idx = source['ace_index']
        slot = source['slot']
        self._disable_feed_assist_all()
        if target_idx != self._active_device_index:
            self._set_active_idx(target_idx)
        self.wait_ace_ready_on(target_idx)
        self._arm_fa_for(target_idx, slot)
        self.wait_ace_ready_on(target_idx)
        self.dwell(delay=0.7)

    def _merge_v2_filament_info(self, idx, result):
        protocol = self._protocols.get(idx)
        if protocol is None or getattr(protocol, 'NAME', None) != 'v2':
            return
        cache = self._v2_filament_info_per_ace.setdefault(idx, {})
        pending = self._v2_filament_info_pending.setdefault(idx, set())
        slots = result.get('slots') or []
        for i, slot in enumerate(slots):
            if slot.get('rfid') == 2:
                cached = cache.get(i)
                if cached:
                    slot['type'] = cached.get('type', '')
                    slot['color'] = list(cached.get('color', [0, 0, 0]))
                    slot['brand'] = cached.get('brand', '')
                    slot['sku'] = cached.get('sku', '')
                else:
                    slot['rfid'] = 1
                    if i in pending:
                        continue
                    pending.add(i)

                    def _store(self, response, _idx=idx, _slot=i):
                        self._v2_filament_info_pending.get(_idx, set()).discard(_slot)
                        if response is None:
                            return
                        res = response.get('result') or {}
                        ftype = res.get('type', '')
                        if not ftype:
                            return
                        self._v2_filament_info_per_ace.setdefault(_idx, {})[_slot] = {'type': ftype, 'color': list(res.get('color', [0, 0, 0])), 'brand': res.get('brand', ''), 'sku': res.get('sku', '')}
                    try:
                        self.send_request_to(idx, {'method': 'get_filament_info', 'params': {'index': i}}, _store)
                    except Exception as e:
                        pending.discard(i)
                        logging.info('[multiACE] V2 get_filament_info enqueue failed idx=%d slot=%d: %s', idx, i, e)
            else:
                cache.pop(i, None)
                pending.discard(i)

    def _v2_quantize_velocity(self, v_mm_s):
        v_abs = abs(v_mm_s)
        STEP = 5
        target = int(round(v_abs / STEP) * STEP)
        return max(5, min(50, target))

    def _make_v2_velocity_tick_for(self, idx):
        state = self._v2_velocity_state.setdefault(idx, {'last_quantum': None, 'last_direction': None, 'last_change_time': 0.0, 'last_log_time': 0.0, 'last_armed_slot': None, 'last_arm_time': 0.0})

        def _tick(eventtime):
            proto = self._protocols.get(idx)
            if proto is None or getattr(proto, 'NAME', None) != 'v2':
                return self.reactor.NEVER
            info = self._info_per_ace.get(idx)
            if info is None:
                return eventtime + 0.5
            slots = info.get('slots') or []
            armed_slot = None
            armed_status = None
            for s in slots:
                ss = s.get('slot_status')
                if ss in ('assisting', 'rollback_assisting', 'feeding', 'rollback', 'preloading'):
                    armed_slot = s.get('index')
                    armed_status = ss
                    break
            if armed_slot is None:
                if state['last_armed_slot'] is not None:
                    last_idx = state['last_armed_slot']
                    new_state = 'unknown'
                    for s in slots:
                        if s.get('index') == last_idx:
                            new_state = s.get('slot_status', 'unknown')
                            break
                    self._fa_log.info('[v2-vel] ace=%d disarmed (was slot=%s, now=%s)' % (idx, last_idx, new_state))
                    expected = self._feed_assist_per_ace.get(idx, -1)
                    rearm = state.setdefault('rearm', {'count': 0, 'last_time': 0.0})
                    cooldown_ok = eventtime - rearm['last_time'] >= 2.0
                    is_error_state = isinstance(new_state, str) and new_state.endswith('_error')
                    if expected == last_idx and expected >= 0 and cooldown_ok and is_error_state:
                        rearm['count'] += 1
                        rearm['last_time'] = eventtime
                        attempt_n = rearm['count']
                        self._fa_log.info('[v2-vel] ace=%d AUTO-REARM slot=%d (attempt #%d, was=assisting now=%s) stop+wait+arm' % (idx, last_idx, attempt_n, new_state))

                        def _stop_cb(self, response, _slot=last_idx, _idx=idx, _n=attempt_n):
                            code = response.get('code', -1) if response else -1
                            if code != 0:
                                msg = response.get('msg', '?') if response else 'no-response'
                                self._fa_log.info('[v2-vel] ace=%d AUTO-REARM stop slot=%d attempt#%d code=%d msg=%s' % (_idx, _slot, _n, code, msg))
                        try:
                            self.send_request_to(idx, {'method': 'stop_feed_assist', 'params': {'index': last_idx}}, _stop_cb)
                        except Exception as e:
                            self._fa_log.info('[v2-vel] AUTO-REARM stop enqueue failed: %s' % e)

                        def _delayed_arm(et, _slot=last_idx, _idx=idx, _n=attempt_n):

                            def _arm_cb(self, response, __slot=_slot, __idx=_idx, __n=_n):
                                code = response.get('code', -1) if response else -1
                                msg = response.get('msg', '?') if response else 'no-response'
                                self._fa_log.info('[v2-vel] ace=%d AUTO-REARM slot=%d attempt#%d response: code=%d msg=%s' % (__idx, __slot, __n, code, msg))
                            try:
                                self.send_request_to(_idx, {'method': 'start_feed_assist', 'params': {'index': _slot}}, _arm_cb)
                                vstate = self._v2_velocity_state.get(_idx)
                                if vstate is not None:
                                    vstate['last_arm_time'] = self.reactor.monotonic()
                            except Exception as e:
                                self._fa_log.info('[v2-vel] AUTO-REARM arm enqueue failed: %s' % e)
                            return self.reactor.NEVER
                        delay = self._v2_min_rearm_gap
                        self._fa_log.info('[v2-vel] ace=%d AUTO-REARM scheduled in %.3fs after stop (min_gap=%.2fs)' % (idx, delay, self._v2_min_rearm_gap))
                        self.reactor.register_timer(_delayed_arm, eventtime + delay)
                    state['last_armed_slot'] = None
                    state['last_quantum'] = None
                    state['last_direction'] = None
                return eventtime + 0.5
            if state['last_armed_slot'] != armed_slot:
                self._fa_log.info('[v2-vel] ace=%d armed slot=%d status=%s' % (idx, armed_slot, armed_status))
                state['last_armed_slot'] = armed_slot
            try:
                mr = self.printer.lookup_object('motion_report', None)
                if mr is None:
                    return eventtime + 0.5
                ms = mr.get_status(eventtime)
                v = float(ms.get('live_extruder_velocity', 0.0) or 0.0)
            except Exception as e:
                self._fa_log.info('[v2-vel] ace=%d motion_report read failed: %s' % (idx, e))
                return eventtime + 0.5
            quantum = self._v2_quantize_velocity(v)
            if abs(v) < 1.0:
                direction = 'fwd'
            else:
                direction = 'fwd' if v >= 0 else 'rev'
            quantum_changed = state['last_quantum'] != quantum
            direction_changed = state['last_direction'] != direction and quantum > 0
            if quantum_changed or direction_changed:
                state['last_quantum'] = quantum
                state['last_direction'] = direction
                state['last_change_time'] = eventtime
                self._fa_log.info('[v2-vel] ace=%d slot=%d %s vel=%+.2f q=%d dir=%s' % (idx, armed_slot, armed_status, v, quantum, direction))
            elif eventtime - state['last_log_time'] >= 2.0:
                state['last_log_time'] = eventtime
                self._fa_log.info('[v2-vel] ace=%d slot=%d %s vel=%+.2f q=%d dir=%s (hb)' % (idx, armed_slot, armed_status, v, quantum, direction))
            HYSTERESIS_S = 1.0
            if armed_status in ('assisting', 'rollback_assisting'):
                disp = state.setdefault('dispatch', {'last_speed': None, 'last_mode': 2, 'candidate_speed': quantum, 'candidate_dir': direction, 'candidate_since': eventtime})
                target_mode = 2 if direction == 'fwd' else 3
                if disp['candidate_speed'] != quantum or disp['candidate_dir'] != direction:
                    disp['candidate_speed'] = quantum
                    disp['candidate_dir'] = direction
                    disp['candidate_since'] = eventtime
                sustained = eventtime - disp['candidate_since']
                if sustained >= HYSTERESIS_S:
                    speed_changed = disp['last_speed'] != quantum
                    mode_changed = disp['last_mode'] != target_mode
                    if mode_changed:
                        disp['last_speed'] = quantum
                        disp['last_mode'] = target_mode

                        def _mode_cb(self, response, _q=quantum, _m=target_mode, _s=armed_slot, _i=idx):
                            code = response.get('code', -1) if response else -1
                            msg = response.get('msg', '?') if response else 'no-response'
                            self._fa_log.info('[v2-vel] ace=%d MODE_SWITCH slot=%d mode=%d speed=%d -> code=%d msg=%s' % (_i, _s, _m, _q, code, msg))
                        self._fa_log.info('[v2-vel] ace=%d slot=%d MODE_SWITCH -> mode=%d speed=%d (sustained %.2fs)' % (idx, armed_slot, target_mode, quantum, sustained))
                        try:
                            self.send_request_to(idx, {'method': 'feed_or_rollback_raw', 'params': {'slot': armed_slot, 'speed': quantum, 'length': 0, 'mode': target_mode}}, _mode_cb)
                        except Exception as e:
                            self._fa_log.info('[v2-vel] MODE_SWITCH enqueue failed: %s' % e)
                    elif speed_changed and self._v2_static_assist_speed is False:
                        disp['last_speed'] = quantum

                        def _spd_cb(self, response, _q=quantum, _s=armed_slot, _i=idx):
                            code = response.get('code', -1) if response else -1
                            msg = response.get('msg', '?') if response else 'no-response'
                            if code != 0:
                                self._fa_log.info('[v2-vel] ace=%d UPDATE_SPEED slot=%d speed=%d -> code=%d msg=%s' % (_i, _s, _q, code, msg))
                        self._fa_log.info('[v2-vel] ace=%d slot=%d UPDATE_SPEED -> %d (sustained %.2fs)' % (idx, armed_slot, quantum, sustained))
                        try:
                            self.send_request_to(idx, {'method': 'update_feeding_speed', 'params': {'index': armed_slot, 'speed': quantum}}, _spd_cb)
                        except Exception as e:
                            self._fa_log.info('[v2-vel] UPDATE_SPEED enqueue failed: %s' % e)
                    else:
                        disp['last_speed'] = quantum
            return eventtime + 0.1
        return _tick

    def _make_heartbeat_tick_for(self, idx):

        def _tick(eventtime):
            if self._serial_failed_per_ace.get(idx, False):
                return eventtime + 1.0
            ser = self._serials.get(idx)
            if ser is None or not ser.is_open:
                return eventtime + 1.0
            is_active = idx == self._active_device_index

            def callback(self, response):
                if response is None:
                    return
                result = response.get('result')
                if result is None:
                    return
                self._refresh_slot_overrides_if_changed()
                prev_info = self._info_per_ace.get(idx, self._make_default_info(idx))
                prev_slots = prev_info.get('slots', [])
                self._merge_v2_filament_info(idx, result)
                for i in range(4):
                    try:
                        new_slot = result['slots'][i]
                    except (KeyError, IndexError):
                        continue
                    prev_slot = prev_slots[i] if i < len(prev_slots) else {}
                    if is_active and self._gate_status_per_ace.get(idx, [GATE_UNKNOWN] * 4)[i] == GATE_EMPTY and (new_slot.get('status') != 'empty') and (not self._swap_in_progress):
                        self.log_always(self._t('msg.auto_feed'))
                        self.reactor.register_async_callback(lambda et, c=self._pre_load, gate=i: c(gate))
                    if new_slot.get('rfid') == 2 and prev_slot.get('rfid') != 2 and (not self._swap_in_progress):
                        target_heads = self._get_heads_for_ace_slot(idx, i)
                        if target_heads:
                            self.log_always(self._t('msg.find_rfid_target_heads', ace=self._disp(idx), slot=self._disp(i), heads=target_heads))
                            self.log_always(self._t('msg.raw_slot_dump', slot=new_slot))
                            new_type = new_slot.get('type', 'PLA')
                            new_color_hex = self.rgb2hex(*new_slot.get('color', (0, 0, 0)))
                            new_brand = new_slot.get('brand', 'Generic')
                            head_source_changed = False
                            for head in target_heads:
                                src = self._head_source.get(head)
                                if src is None:
                                    continue
                                if src.get('type') != new_type or src.get('color') != new_color_hex or src.get('brand') != new_brand:
                                    src['type'] = new_type
                                    src['color'] = new_color_hex
                                    src['brand'] = new_brand
                                    head_source_changed = True
                            if head_source_changed:
                                try:
                                    self._save_head_source()
                                except Exception as he:
                                    logging.info('[multiACE] head_source RFID heal save failed: %s' % he)
                            override = self._override_for(idx, i)
                            if override is not None:
                                push_type = override.get('material') or new_type
                                push_color = self._override_color_to_rgba(override.get('color', ''))
                                push_brand = override.get('brand') or new_brand
                                push_subtype = override.get('subtype', '') or ''
                            else:
                                push_type = new_type
                                push_color = new_color_hex
                                push_brand = new_brand
                                push_subtype = ''
                            for head in target_heads:
                                self._expect_ptc_push(head, push_type, push_color, push_brand, push_subtype)
                                self.gcode.run_script_from_command('SET_PRINT_FILAMENT_CONFIG CONFIG_EXTRUDER=%d FILAMENT_TYPE="%s" FILAMENT_COLOR_RGBA=%s VENDOR="%s" FILAMENT_SUBTYPE="%s"' % (head, push_type, push_color, push_brand, push_subtype))
                        elif is_active:
                            source = self._head_source.get(i)
                            if not (source and source['ace_index'] != self._active_device_index):
                                override_a = self._override_for(idx, i)
                                if override_a is not None:
                                    push_type = override_a.get('material') or new_slot.get('type', 'PLA')
                                    push_color = self._override_color_to_rgba(override_a.get('color', ''))
                                    push_brand = override_a.get('brand') or new_slot.get('brand', 'Generic')
                                    push_subtype = override_a.get('subtype', '') or ''
                                else:
                                    push_type = new_slot.get('type', 'PLA')
                                    push_color = self.rgb2hex(*new_slot.get('color', (0, 0, 0)))
                                    push_brand = new_slot.get('brand', 'Generic')
                                    push_subtype = ''
                                self.log_always(self._t('msg.find_rfid_fallback', slot=self._disp(i), head=i))
                                self.log_always(self._t('msg.raw_slot_dump', slot=new_slot))
                                self._expect_ptc_push(i, push_type, push_color, push_brand, push_subtype)
                                self.gcode.run_script_from_command('SET_PRINT_FILAMENT_CONFIG CONFIG_EXTRUDER=%d FILAMENT_TYPE="%s" FILAMENT_COLOR_RGBA=%s VENDOR="%s" FILAMENT_SUBTYPE="%s"' % (i, push_type, push_color, push_brand, push_subtype))
                    gate_list = self._gate_status_per_ace.setdefault(idx, [GATE_UNKNOWN] * 4)
                    gate_list[i] = GATE_EMPTY if new_slot.get('status') == 'empty' else GATE_AVAILABLE
                self._info_per_ace[idx] = result
                if idx == self._active_device_index:
                    self._info = result
                if not self._swap_in_progress:
                    try:
                        ptc = self.printer.lookup_object('print_task_config', None)
                        if ptc is not None:
                            ptc_status = ptc.get_status()
                            ptc_types = ptc_status.get('filament_type', [''] * 4)
                            ptc_vendors = ptc_status.get('filament_vendor', [''] * 4)
                            ptc_rgbas = ptc_status.get('filament_color_rgba', [''] * 4)
                            slots_list = result.get('slots', [])
                            heal_lines = []
                            for slot_idx in range(min(4, len(slots_list))):
                                slot = slots_list[slot_idx]
                                override = self._override_for(idx, slot_idx)
                                has_rfid = slot.get('rfid') == 2
                                if override is None and (not has_rfid):
                                    continue
                                target_heads = self._get_heads_for_ace_slot(idx, slot_idx)
                                if is_active and (not target_heads) and (slot_idx < 4):
                                    src = self._head_source.get(slot_idx)
                                    if not src:
                                        target_heads = [slot_idx]
                                if override is not None:
                                    push_type = override.get('material') or slot.get('type', 'PLA')
                                    push_color = self._override_color_to_rgba(override.get('color', ''))
                                    push_vendor = override.get('brand') or slot.get('brand', 'Generic')
                                    push_subtype = override.get('subtype', '') or ''
                                else:
                                    push_type = slot.get('type', 'PLA')
                                    push_color = self.rgb2hex(*slot.get('color', (0, 0, 0)))
                                    push_vendor = slot.get('brand', 'Generic')
                                    push_subtype = ''
                                for head in target_heads:
                                    cur_type = ptc_types[head] if head < len(ptc_types) else ''
                                    cur_vendor = ptc_vendors[head] if head < len(ptc_vendors) else ''
                                    cur_color = ptc_rgbas[head] if head < len(ptc_rgbas) else ''
                                    needs_heal = cur_type in ('', 'NONE') or cur_vendor in ('', 'NONE')
                                    if override is not None and (cur_type != push_type or cur_vendor != push_vendor or (cur_color or '').upper() != (push_color or '').upper()):
                                        needs_heal = True
                                    if needs_heal:
                                        logging.info('[multiACE] display heal: head %d was "%s"/"%s"/%s, repushing %s/%s/%s' % (head, cur_type, cur_vendor, cur_color, push_type, push_vendor, push_color))
                                        self._expect_ptc_push(head, push_type, push_color, push_vendor, push_subtype)
                                        heal_lines.append('SET_PRINT_FILAMENT_CONFIG CONFIG_EXTRUDER=%d FILAMENT_TYPE="%s" FILAMENT_COLOR_RGBA=%s VENDOR="%s" FILAMENT_SUBTYPE="%s"' % (head, push_type, push_color, push_vendor, push_subtype))
                            if heal_lines:
                                self.gcode.run_script_from_command('\n'.join(heal_lines))
                    except Exception as he:
                        logging.info('[multiACE] display heal error: %s' % he)
            try:
                self.send_request_to(idx, {'method': 'get_status'}, callback)
            except Exception as he:
                logging.info('[multiACE] Heartbeat[%d] send failed: %s' % (idx, str(he)))
            return eventtime + 1.0
        return _tick

    def _handle_serial_failure(self, err, first, first_error=None):
        self._handle_per_ace_failure(self._active_device_index, err)

    def _pre_load(self, gate):
        feed_length = self.head_feed_length[gate]
        if feed_length <= 0:
            return
        self.log_always(self._t('msg.wait_ace_preload'))
        self.wait_ace_ready()
        sensor = self.printer.lookup_object('filament_motion_sensor e%d_filament' % gate, None)
        self._feed(gate, feed_length, self.feed_speed, 0)
        while not self.is_ace_ready():
            self.reactor.pause(self.reactor.monotonic() + 0.105)
            if sensor and sensor.get_status(0)['filament_detected']:
                self._stop_feeding(gate)
                self.wait_ace_ready()
                self.log_always(self._t('msg.filament_detected_preload'))
                break
        if sensor and sensor.get_status(0)['filament_detected']:
            self.log_always(self._t('msg.select_autoload_menu'))

    def send_request(self, request, callback):
        self.send_request_to(self._active_device_index, request, callback)

    def wait_ace_ready(self):
        self.wait_ace_ready_on(self._active_device_index)

    def wait_ace_ready_on(self, idx, timeout=30.0, max_reconnects=2):
        info = self._info_per_ace.get(idx)
        if info is None:
            return
        protocol = self._protocols.get(idx)
        if protocol is not None and getattr(protocol, 'NAME', '') == 'v2':
            timeout = max(timeout, 60.0)
        deadline = time.monotonic() + timeout
        reconnect_count = 0
        while info.get('status') != 'ready':
            if time.monotonic() > deadline:
                if reconnect_count >= max_reconnects:
                    self.log_error(self._t('msg.ace_stuck_powercycle', ace=self._disp(idx), status=info.get('status', '?'), attempts=reconnect_count))
                    self._handle_per_ace_failure(idx, 'stuck_after_reconnects')
                    raise self.printer.command_error('[multiACE] ACE %d firmware stuck — power-cycle required' % idx)
                reconnect_count += 1
                self.log_error(self._t('msg.ace_wait_timeout_reconnect', ace=self._disp(idx), timeout=timeout, status=info.get('status', '?'), attempt=reconnect_count, max=max_reconnects))
                try:
                    self._disconnect_from(idx)
                except Exception:
                    pass
                self.reactor.pause(self.reactor.monotonic() + 0.5)
                if self._open_ace(idx):
                    self.log_always(self._t('msg.ace_reconnected_after_timeout', ace=self._disp(idx)))
                    info = self._info_per_ace.get(idx)
                    if info is None:
                        return
                    deadline = time.monotonic() + timeout
                    continue
                self._handle_per_ace_failure(idx, 'wait_ace_ready_timeout')
                raise self.printer.command_error('[multiACE] ACE %d unresponsive — reconnect failed, operation aborted' % idx)
            curr_ts = self.reactor.monotonic()
            self.reactor.pause(curr_ts + 0.5)
            info = self._info_per_ace.get(idx)
            if info is None:
                return

    def is_ace_ready(self):
        idx = self._active_device_index
        info = self._info_per_ace.get(idx)
        if info is None:
            return False
        return info.get('status') == 'ready'

    def dwell(self, delay=1.0):
        curr_ts = self.reactor.monotonic()
        self.reactor.pause(curr_ts + delay)

    def _extruder_move(self, length, speed):
        pos = self.toolhead.get_position()
        pos[3] += length
        self.toolhead.move(pos, speed)
        return pos[3]
    cmd_ACE_START_DRYING_help = 'Starts ACE Pro dryer'

    def cmd_ACE_START_DRYING(self, gcmd):
        temperature = gcmd.get_int('TEMP')
        duration = gcmd.get_int('DURATION', 240)
        if duration <= 0:
            raise gcmd.error('Wrong duration')
        if temperature <= 0 or temperature > self.max_dryer_temperature:
            raise gcmd.error('Wrong temperature')

        def callback(self, response):
            if response.get('code', 0) != 0:
                self.log_error(self._t('msg.ace_error_generic', error=response.get('msg')))
                return
            self.gcode.respond_info(self._t('msg.dryer_started'))
        self.wait_ace_ready()
        self.send_request(request={'method': 'drying', 'params': {'temp': temperature, 'fan_speed': 7000, 'duration': duration}}, callback=callback)
    cmd_ACE_STOP_DRYING_help = '[multiACE] Stop ACE Pro dryer. Usage: ACE_STOP_DRYING [ACE=N]'

    def cmd_ACE_STOP_DRYING(self, gcmd):
        ace_idx = gcmd.get_int('ACE', self._active_device_index)
        if ace_idx < 0 or ace_idx >= len(self._ace_devices):
            self.log_always(self._t('msg.ace_not_available', ace=self._disp(ace_idx)))
            return

        def callback(self, response):
            if response is None:
                self.log_error(self._t('msg.dryer_no_response_stop', ace=self._disp(ace_idx)))
                return
            if response.get('code', 0) != 0:
                self.log_error(self._t('msg.ace_error_generic', error=response.get('msg')))
                return
            self.gcode.respond_info(self._t('msg.dryer_stopped_on_ace', ace=self._disp(ace_idx)))
        self.wait_ace_ready_on(ace_idx)
        self.send_request_to(ace_idx, {'method': 'drying_stop'}, callback)

    def _enable_feed_assist(self, index):
        if self._feed_assist_index != -1 and self._feed_assist_index != index:
            self.wait_ace_ready()
            self._retract(self._feed_assist_index, 5, 80)
        self.wait_ace_ready()
        self._arm_fa_for(self._active_device_index, index)
        self.wait_ace_ready()
        self.dwell(delay=0.7)
    cmd_ACE_ENABLE_FEED_ASSIST_help = 'Enables ACE feed assist'

    def cmd_ACE_ENABLE_FEED_ASSIST(self, gcmd):
        index = gcmd.get_int('INDEX')
        if index < 0 or index >= 4:
            raise gcmd.error('Wrong index')
        self._enable_feed_assist(index)

    def _disable_feed_assist(self, index=-1):
        rt_index = self._feed_assist_index
        if rt_index == -1:
            return
        self.wait_ace_ready()
        self._disarm_fa_for(self._active_device_index)
        self.wait_ace_ready()
        self._retract(rt_index, 5, 80)
        self.dwell(0.3)
    cmd_ACE_DISABLE_FEED_ASSIST_help = 'Disables ACE feed assist'

    def cmd_ACE_DISABLE_FEED_ASSIST(self, gcmd):
        index = gcmd.get_int('INDEX', self._feed_assist_index)
        if index < 0 or index >= 4:
            raise gcmd.error('Wrong index')
        self._disable_feed_assist(index)

    def _feed(self, index, length, speed, how_wait=None):

        def callback(self, response):
            if response.get('code', 0) != 0:
                self.log_error(self._t('msg.ace_error_generic', error=response.get('msg')))
                return
        self.wait_ace_ready()
        self.send_request(request={'method': 'feed_filament', 'params': {'index': index, 'length': length, 'speed': speed}}, callback=callback)
        if how_wait is not None:
            self.dwell(delay=how_wait / speed + 0.1)
        else:
            self.dwell(delay=length / speed + 0.1)
    cmd_ACE_FEED_help = 'Feeds filament from ACE'

    def cmd_ACE_FEED(self, gcmd):
        index = gcmd.get_int('INDEX')
        length = gcmd.get_int('LENGTH')
        speed = gcmd.get_int('SPEED', self.feed_speed)
        if index < 0 or index >= 4:
            raise gcmd.error('Wrong index')
        if length <= 0:
            raise gcmd.error('Wrong length')
        if speed <= 0:
            raise gcmd.error('Wrong speed')
        self._feed(index, length, speed)

    def _retract(self, index, length, speed):

        def callback(self, response):
            if response.get('code', 0) != 0:
                self.log_error(self._t('msg.ace_error_generic', error=response.get('msg')))
                return
        self.wait_ace_ready()
        self.send_request(request={'method': 'unwind_filament', 'params': {'index': index, 'length': length, 'speed': speed}}, callback=callback)
        self.dwell(delay=length / speed + 0.1)

    def retract_fil(self, index):
        if self._retract_length_override is not None:
            length = self._retract_length_override
        else:
            length = self.get_retract_length(self._active_device_index, index)
        self._retract(index, length, self.retract_speed)
    cmd_ACE_RETRACT_help = 'Retracts filament back to ACE'

    def cmd_ACE_RETRACT(self, gcmd):
        index = gcmd.get_int('INDEX')
        length = gcmd.get_int('LENGTH')
        speed = gcmd.get_int('SPEED', self.retract_speed)
        if index < 0 or index >= 4:
            raise gcmd.error('Wrong index')
        if length <= 0:
            raise gcmd.error('Wrong length')
        if speed <= 0:
            raise gcmd.error('Wrong speed')
        self._retract(index, length, speed)

    def _set_feeding_speed(self, index, speed):

        def callback(self, response):
            if response.get('code', 0) != 0:
                self.log_error(self._t('msg.ace_error_generic', error=response.get('msg')))
        self.send_request(request={'method': 'update_feeding_speed', 'params': {'index': index, 'speed': speed}}, callback=callback)

    def _stop_feeding(self, index):

        def callback(self, response):
            if response.get('code', 0) != 0:
                self.log_error(self._t('msg.ace_error_generic', error=response.get('msg')))
                return
        self.send_request(request={'method': 'stop_feed_filament', 'params': {'index': index}}, callback=callback)
    cmd_ACE_SWITCH_help = 'Switch active ACE unit. Usage: ACE_SWITCH TARGET=0 [AUTOLOAD=1]'
    EXTRUDER_MAP = {0: ('left', 1), 1: ('left', 0), 2: ('right', 0), 3: ('right', 1)}

    def _refresh_slot_overrides(self):
        try:
            import json as _json
            import os as _os
            if not _os.path.exists(self._slot_overrides_file):
                self._slot_overrides = {}
                self._slot_overrides_mtime = 0.0
                return
            with open(self._slot_overrides_file, 'r') as f:
                data = _json.load(f)
            if isinstance(data, dict):
                self._slot_overrides = data
                try:
                    self._slot_overrides_mtime = _os.path.getmtime(self._slot_overrides_file)
                except OSError:
                    pass
        except Exception as e:
            logging.info('[multiACE] _refresh_slot_overrides: keeping previous, error: %s' % e)

    def _refresh_slot_overrides_if_changed(self):
        try:
            import os as _os
            if not _os.path.exists(self._slot_overrides_file):
                if self._slot_overrides:
                    self._slot_overrides = {}
                    self._slot_overrides_mtime = 0.0
                    try:
                        self._push_rfid_info()
                    except Exception as pe:
                        logging.info('[multiACE] re-push after override drop: %s' % pe)
                return
            m = _os.path.getmtime(self._slot_overrides_file)
            if m == self._slot_overrides_mtime:
                return
            old_keys = set(self._slot_overrides.keys())
            self._refresh_slot_overrides()
            new_keys = set(self._slot_overrides.keys())
            if old_keys != new_keys:
                try:
                    self._push_rfid_info()
                except Exception as pe:
                    logging.info('[multiACE] re-push after override change: %s' % pe)
        except OSError:
            pass

    def _override_for(self, ace_idx, slot_idx):
        o = self._slot_overrides.get('%d_%d' % (int(ace_idx), int(slot_idx)))
        if not o:
            return None
        if not (o.get('material') or o.get('color')):
            return None
        return o

    def _override_color_to_rgba(self, hex_color):
        h = (hex_color or '').lstrip('#').upper()
        if len(h) == 6:
            return h + 'FF'
        if len(h) == 8:
            return h
        return 'FFFFFFFF'

    def _ptc_color_to_override_hex(self, c):
        if c is None:
            return ''
        s = str(c).lstrip('#').upper()
        if len(s) >= 6:
            return '#' + s[:6]
        return ''

    def _save_slot_overrides(self):
        try:
            import json as _json
            import os as _os
            d = _os.path.dirname(self._slot_overrides_file)
            if d and (not _os.path.exists(d)):
                _os.makedirs(d, exist_ok=True)
            tmp = self._slot_overrides_file + '.tmp'
            with open(tmp, 'w') as f:
                _json.dump(self._slot_overrides, f, indent=2)
            _os.replace(tmp, self._slot_overrides_file)
            try:
                self._slot_overrides_mtime = _os.path.getmtime(self._slot_overrides_file)
            except OSError:
                pass
        except Exception as e:
            logging.info('[multiACE] _save_slot_overrides: %s' % e)

    def _expect_ptc_push(self, head, ftype, color_rgba, vendor, subtype):
        self._expected_ptc_pushes.append({'head': int(head), 'type': str(ftype or ''), 'color': str(color_rgba or '').upper().lstrip('#'), 'vendor': str(vendor or ''), 'subtype': str(subtype or '')})
        if len(self._expected_ptc_pushes) > 32:
            self._expected_ptc_pushes = self._expected_ptc_pushes[-32:]

    def _wrap_set_print_filament_config(self, gcmd):
        if self._orig_set_ptc is not None:
            self._orig_set_ptc(gcmd)
        try:
            head = gcmd.get_int('CONFIG_EXTRUDER', None)
            if head is None:
                return
            incoming = {'head': int(head), 'type': str(gcmd.get('FILAMENT_TYPE', '') or ''), 'color': str(gcmd.get('FILAMENT_COLOR_RGBA', '') or '').upper().lstrip('#'), 'vendor': str(gcmd.get('VENDOR', '') or ''), 'subtype': str(gcmd.get('FILAMENT_SUBTYPE', '') or '')}
            for i, exp in enumerate(self._expected_ptc_pushes):
                if exp == incoming:
                    self._expected_ptc_pushes.pop(i)
                    return
            self._capture_display_edit(incoming)
        except Exception as e:
            logging.info('[multiACE] _wrap_set_print_filament_config error: %s' % e)

    def _capture_display_edit(self, ev):
        if self._swap_in_progress:
            return
        head = int(ev['head'])
        src = self._head_source.get(head)
        if src:
            src_type = (src.get('type') or '').strip()
            src_color = (src.get('color') or '').strip().lstrip('#').upper()
            if not src_type or src_color in ('', '000000', '00000000'):
                return
            ace_idx = int(src.get('ace_index', 0))
            slot_idx = int(src.get('slot', 0))
        else:
            ace_idx = self._active_device_index
            slot_idx = head
        key = '%d_%d' % (ace_idx, slot_idx)
        existing = self._slot_overrides.get(key) or {}
        ptc = self.printer.lookup_object('print_task_config', None)
        ptc_status = ptc.get_status() if ptc is not None else {}
        ptc_types = ptc_status.get('filament_type', []) or []
        ptc_vendors = ptc_status.get('filament_vendor', []) or []
        ptc_subs = ptc_status.get('filament_sub_type', []) or []
        ptc_rgbas = ptc_status.get('filament_color_rgba', []) or []
        ptc_type = (ptc_types[head] if head < len(ptc_types) else '') or ''
        ptc_vendor = (ptc_vendors[head] if head < len(ptc_vendors) else '') or ''
        ptc_sub = (ptc_subs[head] if head < len(ptc_subs) else '') or ''
        ptc_rgba = (ptc_rgbas[head] if head < len(ptc_rgbas) else '') or ''
        if ptc_type == 'NONE':
            ptc_type = ''
        if ptc_vendor == 'NONE':
            ptc_vendor = ''
        if ptc_rgba.upper() in ('00000000', '000000FF'):
            ptc_rgba = ''
        inc_type = (ev.get('type') or '').strip()
        inc_color_raw = (ev.get('color') or '').strip().lstrip('#').upper()
        inc_vendor = (ev.get('vendor') or '').strip()
        inc_subtype = (ev.get('subtype') or '').strip()
        merged_material = inc_type or existing.get('material') or ptc_type
        merged_brand = inc_vendor or existing.get('brand') or ptc_vendor
        merged_subtype = inc_subtype or existing.get('subtype') or ptc_sub
        if inc_color_raw and inc_color_raw != '00000000':
            merged_color = self._ptc_color_to_override_hex(inc_color_raw)
        elif existing.get('color'):
            merged_color = existing['color']
        elif ptc_rgba:
            merged_color = self._ptc_color_to_override_hex(ptc_rgba)
        else:
            merged_color = ''
        new_override = {'ace': ace_idx, 'slot': slot_idx, 'material': merged_material, 'brand': merged_brand, 'subtype': merged_subtype, 'color': merged_color}
        if existing == new_override:
            return
        self._slot_overrides[key] = new_override
        logging.info('[multiACE] display edit -> override (ACE %d / slot %d): %s' % (ace_idx, slot_idx, new_override))
        self._save_slot_overrides()

    def _push_rfid_info(self):
        logging.info('[multiACE] _push_rfid_info: active_device=%d, head_source=%s' % (self._active_device_index, str({k: v['ace_index'] if v else None for k, v in self._head_source.items()})))
        active = self._active_device_index
        lines = []
        for head in range(4):
            source = self._head_source.get(head)
            if source:
                src_ace = int(source.get('ace_index', 0))
                src_slot = int(source.get('slot', 0))
                ace_info = self._info_per_ace.get(src_ace, {}) or {}
                slots = ace_info.get('slots', []) or []
                slot = slots[src_slot] if src_slot < len(slots) else {}
                override = self._override_for(src_ace, src_slot)
                fallback_type = source.get('type') or slot.get('type', 'PLA')
                fallback_color = source.get('color') or self.rgb2hex(*slot.get('color', (0, 0, 0)))
                fallback_brand = source.get('brand') or slot.get('brand', 'Generic')
                logging.info('[multiACE] _push_rfid_info: head %d - loaded from ACE %d / slot %d, pushing %s' % (head, src_ace, src_slot, 'override' if override is not None else 'source'))
                if override is not None:
                    push_type = override.get('material') or fallback_type
                    push_color = self._override_color_to_rgba(override.get('color', ''))
                    push_brand = override.get('brand') or fallback_brand
                    push_subtype = override.get('subtype', '') or ''
                    self._expect_ptc_push(head, push_type, push_color, push_brand, push_subtype)
                    lines.append('SET_PRINT_FILAMENT_CONFIG CONFIG_EXTRUDER=%d FILAMENT_TYPE="%s" FILAMENT_COLOR_RGBA=%s VENDOR="%s" FILAMENT_SUBTYPE="%s"' % (head, push_type, push_color, push_brand, push_subtype))
                else:
                    self._expect_ptc_push(head, fallback_type, fallback_color, fallback_brand, '')
                    lines.append('SET_PRINT_FILAMENT_CONFIG CONFIG_EXTRUDER=%d FILAMENT_TYPE="%s" FILAMENT_COLOR_RGBA=%s VENDOR="%s" FILAMENT_SUBTYPE=""' % (head, fallback_type, fallback_color, fallback_brand))
            else:
                empty_override = self._override_for(active, head)
                if empty_override is not None:
                    ace_info = self._info_per_ace.get(active, {}) or {}
                    aslots = ace_info.get('slots', []) or []
                    aslot = aslots[head] if head < len(aslots) else {}
                    push_type = empty_override.get('material') or aslot.get('type', 'PLA')
                    push_color = self._override_color_to_rgba(empty_override.get('color', ''))
                    push_brand = empty_override.get('brand') or aslot.get('brand', 'Generic')
                    push_subtype = empty_override.get('subtype', '') or ''
                    logging.info('[multiACE] _push_rfid_info: head %d - unloaded, pushing override (active ACE %d / slot %d)' % (head, active, head))
                    self._expect_ptc_push(head, push_type, push_color, push_brand, push_subtype)
                    lines.append('SET_PRINT_FILAMENT_CONFIG CONFIG_EXTRUDER=%d FILAMENT_TYPE="%s" FILAMENT_COLOR_RGBA=%s VENDOR="%s" FILAMENT_SUBTYPE="%s"' % (head, push_type, push_color, push_brand, push_subtype))
                    continue
                logging.info('[multiACE] _push_rfid_info: head %d - empty, clearing display' % head)
                self._expect_ptc_push(head, '', '000000FF', '', '')
                lines.append('SET_PRINT_FILAMENT_CONFIG CONFIG_EXTRUDER=%d FILAMENT_TYPE="" FILAMENT_COLOR_RGBA=000000FF VENDOR="" FILAMENT_SUBTYPE=""' % head)
        if lines:
            self.gcode.run_script_from_command('\n'.join(lines))
    cmd_MULTIACE_REFRESH_OVERRIDES_help = '[multiACE] Reload slot_overrides.json and push to display'

    def cmd_MULTIACE_REFRESH_OVERRIDES(self, gcmd):
        self._refresh_slot_overrides()
        self._push_rfid_info()

    def cmd_ACE_SWITCH(self, gcmd):
        target = gcmd.get_int('TARGET')
        autoload = gcmd.get_int('AUTOLOAD', 0)
        if self._swap_in_progress:
            self.log_always(self._t('msg.switch_in_progress'))
            return
        self._swap_in_progress = True
        try:
            self._perform_switch(gcmd, target, autoload)
        finally:
            self._swap_in_progress = False

    def _perform_switch(self, gcmd, target, autoload):
        self._refresh_ace_devices('switch')
        if not self._ace_devices:
            self.log_always(self._t('msg.no_ace_devices_detected'))
            return
        if not self._is_ace_present(target):
            self._usb_log.info('RETRY [switch] target=%d not present, starting retries', target)
            for retry in range(5):
                self._usb_stats['retries'] += 1
                self.reactor.pause(self.reactor.monotonic() + 1.0)
                self._refresh_ace_devices('switch_retry_%d' % (retry + 1))
                self._usb_log.info('RETRY [switch] attempt=%d/%d present=%d target=%d', retry + 1, 5, len(self._ace_present), target)
                if self._is_ace_present(target):
                    break
        if not self._is_ace_present(target):
            self.log_always(self._t('msg.ace_not_available_present', ace=self._disp(target), count=len(self._ace_present)))
            return
        switching_ace = target != self._active_device_index
        if not switching_ace and (not autoload):
            self.log_always(self._t('msg.ace_already_active', ace=self._disp(target)))
            return
        if not switching_ace and autoload:
            self.log_always(self._t('msg.ace_already_active_loading', ace=self._disp(target)))
        else:
            if target >= len(self._ace_devices) or not self._connected_per_ace.get(target, False):
                self.log_always(self._t('msg.ace_not_connected', ace=self._disp(target)))
                return
            current_slot = self._feed_assist_per_ace.get(self._active_device_index, -1)
            if current_slot != -1:
                try:
                    self._disarm_fa_for(self._active_device_index)
                except Exception as e:
                    logging.info('[multiACE] switch: stop_feed_assist failed: %s' % e)
                self.wait_ace_ready()
            if autoload:
                self.log_always(self._t('msg.switch_unloading_from', ace=self._disp(self._active_device_index)))
                for gate in range(4):
                    sensor = self.printer.lookup_object('filament_motion_sensor e%d_filament' % gate, None)
                    filament_in_head = sensor and sensor.get_status(0)['filament_detected']
                    module, channel = self.EXTRUDER_MAP[gate]
                    if filament_in_head:
                        self.log_always(self._t('msg.switch_extruder_full_unload', head=gate))
                        self.gcode.run_script_from_command('FEED_AUTO MODULE=%s CHANNEL=%d EXTRUDER=%d UNLOAD=1 STAGE=prepare' % (module, channel, gate))
                        self.gcode.run_script_from_command('FEED_AUTO MODULE=%s CHANNEL=%d EXTRUDER=%d UNLOAD=1 STAGE=doing' % (module, channel, gate))
                    else:
                        self.log_always(self._t('msg.switch_extruder_skip_unload', head=gate))
                machine_state_manager = self.printer.lookup_object('machine_state_manager', None)
                if machine_state_manager is not None:
                    self.gcode.run_script_from_command('SET_MAIN_STATE MAIN_STATE=IDLE ACTION=IDLE')
                self.log_always(self._t('msg.switch_unload_complete'))
            self.log_always(self._t('msg.switch_activating', ace=self._disp(target)))
            self._set_active_idx(target)
            self._push_rfid_info()
        if autoload:
            self.log_always(self._t('msg.switch_loading_from', ace=self._disp(target)))
            loaded_any = False
            for gate in range(4):
                sensor = self.printer.lookup_object('filament_motion_sensor e%d_filament' % gate, None)
                filament_in_head = sensor and sensor.get_status(0)['filament_detected']
                if filament_in_head:
                    self.log_always(self._t('msg.switch_extruder_already_loaded', head=gate))
                elif self.gate_status[gate] == GATE_AVAILABLE:
                    module, channel = self.EXTRUDER_MAP[gate]
                    self.log_always(self._t('msg.switch_extruder_loading', head=gate))
                    self.gcode.run_script_from_command('FEED_AUTO MODULE=%s CHANNEL=%d EXTRUDER=%d LOAD=1' % (module, channel, gate))
                    loaded_any = True
                else:
                    self.log_always(self._t('msg.switch_extruder_no_filament', head=gate))
            if loaded_any:
                self.log_always(self._t('msg.switch_load_complete', ace=self._disp(target)))
            else:
                self.log_always(self._t('msg.switch_nothing_to_load'))
        self._audit_state('SWITCH', {'target': target, 'autoload': autoload})

    def _get_heads_for_ace_slot(self, ace_index, slot):
        heads = []
        for head, source in self._head_source.items():
            if source and source['ace_index'] == ace_index and (source['slot'] == slot):
                heads.append(head)
        return heads

    def _restore_head_source(self):
        saved = self.save_variables.allVariables.get(self.VARS_ACE_HEAD_SOURCE, None)
        if saved and isinstance(saved, dict):
            for head in range(4):
                key = str(head)
                if key in saved and saved[key]:
                    self._head_source[head] = saved[key]
                    logging.info('[multiACE] Restored head %d -> ACE %d / Slot %d' % (head, saved[key]['ace_index'], saved[key]['slot']))

    def _save_head_source(self):
        save_data = {}
        for head in range(4):
            save_data[str(head)] = self._head_source[head]
        value_str = json.dumps(save_data).replace('null', 'None')
        self.gcode.run_script_from_command("SAVE_VARIABLE VARIABLE=%s VALUE='%s'" % (self.VARS_ACE_HEAD_SOURCE, value_str))

    def _ensure_ace_available(self, ace_index):
        for attempt in range(5):
            self._refresh_ace_devices('ensure_%d' % (attempt + 1))
            if self._is_ace_present(ace_index):
                if attempt > 0:
                    self._usb_log.info('ENSURE ace=%d found after %d retries', ace_index, attempt)
                return True
            self._usb_stats['retries'] += 1
            self.reactor.pause(self.reactor.monotonic() + 1.0)
        self._usb_log.warning('ENSURE ace=%d FAILED after 5 attempts (present %d)', ace_index, len(self._ace_present))
        return False

    def _switch_ace_for_head(self, head_index):
        source = self._head_source.get(head_index)
        if not source:
            return False
        target_ace = source['ace_index']
        if target_ace == self._active_device_index:
            self._audit_state('SWITCH_AUTO_NOOP', {'head': head_index, 'target_ace': target_ace, 'reason': 'already_active'})
            return True
        if target_ace >= len(self._ace_devices):
            self.log_always(self._t('msg.ace_out_of_range_for_head', ace=self._disp(target_ace), head=head_index))
            self._audit_state('SWITCH_AUTO_FAILED', {'head': head_index, 'target_ace': target_ace, 'reason': 'ace_out_of_range'})
            return False
        if not self._connected_per_ace.get(target_ace, False):
            self.log_error(self._t('msg.ace_not_connected_for_head', ace=self._disp(target_ace), head=head_index))
            self._audit_state('SWITCH_AUTO_FAILED', {'head': head_index, 'target_ace': target_ace, 'reason': 'not_connected'})
            return False
        self.log_always(self._t('msg.activating_ace_for_head', ace=self._disp(target_ace), head=head_index))
        self._set_active_idx(target_ace)
        self._audit_state('SWITCH_AUTO', {'head': head_index, 'target_ace': target_ace})
        return True

    def _on_extruder_change(self):
        self._fa_trace('_on_extruder_change fired; gate=%s context=%s active_ace=%d' % (self._auto_feed_enabled, self._fa_context, self._active_device_index))
        if not any((self._head_source[h] for h in range(4))):
            return
        try:
            extruder = self.toolhead.get_extruder()
            head_index = getattr(extruder, 'extruder_index', getattr(extruder, 'extruder_num', None))
        except Exception:
            head_index = None
        if head_index is None:
            self._audit_state('SWITCH_AUTO', {'head': None, 'reason': 'no_head_index'})
            return
        source = self._head_source.get(head_index)
        if source is None:
            self._audit_state('SWITCH_AUTO', {'head': head_index, 'reason': 'no_head_source'})
            return
        target_ace = source['ace_index']
        target_slot = source['slot']
        if target_ace >= len(self._ace_devices) or not self._connected_per_ace.get(target_ace, False):
            self._audit_state('SWITCH_AUTO_FAILED', {'head': head_index, 'target_ace': target_ace, 'reason': 'not_connected'})
            self.log_error(self._t('msg.target_ace_not_connected_t', head=head_index, ace=self._disp(target_ace)))
            return
        prev_active = self._active_device_index
        prev_slot = self._feed_assist_per_ace.get(prev_active, -1)
        if prev_active != target_ace and prev_slot != -1:
            try:
                self._disarm_fa_for(prev_active)
            except Exception as e:
                logging.info('[multiACE] stop_feed_assist on ACE %d failed: %s' % (prev_active, e))
        if prev_active != target_ace:
            self._set_active_idx(target_ace)
        current_target_slot = self._feed_assist_per_ace.get(target_ace, -1)
        if current_target_slot != target_slot:
            target_ace_local = target_ace
            target_slot_local = target_slot
            head_index_local = head_index

            def _deferred_fa_start(eventtime):
                if not self._auto_feed_enabled:
                    self._fa_trace('_on_extruder_change deferred start SUPPRESSED (gate closed): head=%d idx=%d slot=%d' % (head_index_local, target_ace_local, target_slot_local))
                    return self.reactor.NEVER
                try:
                    cur_ext = self.toolhead.get_extruder()
                    cur_head = getattr(cur_ext, 'extruder_index', getattr(cur_ext, 'extruder_num', None))
                except Exception:
                    cur_head = None
                if cur_head != head_index_local:
                    self._fa_trace('_on_extruder_change deferred start SUPPRESSED (stale head): expected=%d actual=%s' % (head_index_local, cur_head))
                    return self.reactor.NEVER
                try:
                    self._arm_fa_for(target_ace_local, target_slot_local)
                except Exception as e:
                    logging.info('[multiACE] deferred start_feed_assist ACE %d slot %d failed: %s' % (target_ace_local, target_slot_local, e))
                return self.reactor.NEVER
            self.reactor.register_timer(_deferred_fa_start, self.reactor.monotonic() + 0.1)
        self._audit_state('SWITCH_AUTO', {'head': head_index, 'target_ace': target_ace, 'target_slot': target_slot, 'prev_active': prev_active, 'prev_slot': prev_slot})
        now = time.monotonic()
        gap_ms = None
        if self._last_switch_auto_ts is not None:
            gap_ms = int((now - self._last_switch_auto_ts) * 1000)
        self._last_switch_auto_ts = now
        self._telemetry('SWITCH', {'head': head_index, 'prev_ace': prev_active, 'prev_slot': prev_slot, 'target_ace': target_ace, 'target_slot': target_slot, 'gap_ms_since_last_switch': gap_ms, 'print_active': self._fa_context == 'print', 'ace_changed': prev_active != target_ace})
    cmd_ACE_LOAD_HEAD_help = '[multiACE] Load a toolhead from ACE. Usage: ACE_LOAD_HEAD HEAD=0 [ACE=0] [SLOT=0]'

    def cmd_ACE_LOAD_HEAD(self, gcmd):
        head = gcmd.get_int('HEAD')
        ace_index = gcmd.get_int('ACE', self._active_device_index)
        slot = gcmd.get_int('SLOT', head)
        self._last_load_ok = True
        if head < 0 or head > 3:
            raise gcmd.error('[multiACE] HEAD must be 0-3')
        if ace_index < 0 or not self._ensure_ace_available(ace_index):
            self.log_always(self._t('msg.ace_not_available', ace=self._disp(ace_index)))
            return
        if slot < 0 or slot > 3:
            raise gcmd.error('[multiACE] SLOT must be 0-3')
        sensor = self.printer.lookup_object('filament_motion_sensor e%d_filament' % head, None)
        if sensor and sensor.get_status(0)['filament_detected']:
            if self._head_source.get(head) is not None:
                self.log_always(self._t('msg.load_head_already_loaded', head=head))
                return
            if len(self._ace_devices) == 1:
                only_idx = 0
                info = self._info_per_ace.get(only_idx, self._make_default_info(only_idx))
                slots = info.get('slots', [])
                slot_info = slots[slot] if slot < len(slots) else {}
                self._head_source[head] = {'ace_index': only_idx, 'slot': slot, 'type': slot_info.get('type', 'PLA'), 'color': self.rgb2hex(*slot_info.get('color', (0, 0, 0))), 'brand': slot_info.get('brand', 'Generic')}
                self._save_head_source()
                self.log_always(self._t('msg.load_head_inferred_only_ace', head=head, slot=self._disp(slot)))
            else:
                self.log_error(self._t('msg.load_head_no_source_recorded', head=head, count=len(self._ace_devices)))
            return
        self.log_always(self._t('msg.load_head_starting', head=head, ace=self._disp(ace_index), slot=self._disp(slot)))
        if ace_index != self._active_device_index:
            if not self._switch_ace_for_head_target(ace_index):
                raise gcmd.error('[multiACE] Failed to connect to ACE %d' % ace_index)
        if self.gate_status[slot] != GATE_AVAILABLE:
            self.log_always(self._t('msg.load_slot_no_filament', ace=self._disp(ace_index), slot=self._disp(slot)))
            return
        active_ext = self.toolhead.get_extruder().get_name()
        target_ext = 'extruder' if head == 0 else 'extruder%d' % head
        if active_ext != target_ext:
            logging.info('[multiACE] Load: switching to %s (was %s)' % (target_ext, active_ext))
            self.gcode.run_script_from_command('T%d A0' % head)
            self.toolhead.wait_moves()
        module, channel = self.EXTRUDER_MAP[head]
        self._head_source[head] = {'ace_index': ace_index, 'slot': slot, 'type': '', 'color': '000000', 'brand': ''}
        self._save_head_source()
        self.gcode.run_script_from_command('SET_FILAMENT_SENSOR SENSOR=e%d_filament ENABLE=1' % head)
        ff_module = 'filament_feed %s' % module
        try:
            ff = self.printer.lookup_object(ff_module, None)
            if ff is None:
                logging.info('[multiACE] channel_state reset: %s not loaded' % ff_module)
            elif channel >= len(ff.channel_state):
                logging.info('[multiACE] channel_state reset: channel %d out of range (%d)' % (channel, len(ff.channel_state)))
            else:
                prev_state = ff.channel_state[channel]
                ff.channel_state[channel] = 'inited'
                if 'load_finish' in ff.config:
                    ff.config['load_finish'][channel] = False
                logging.info('[multiACE] channel_state reset: %s ch=%d prev=%s -> inited, load_finish=False' % (ff_module, channel, prev_state))
        except Exception as e:
            logging.info('[multiACE] channel_state reset error: %s' % e)
        wheel_before = self._read_wheel_counts(module, channel)
        try:
            self.gcode.run_script_from_command('FEED_AUTO MODULE=%s CHANNEL=%d EXTRUDER=%d LOAD=1' % (module, channel, head))
        except Exception as e:
            self._audit_state('LOAD_HEAD_FAILED', {'head': head, 'ace': ace_index, 'slot': slot, 'reason': 'feed_auto_error', 'error': str(e)})
            try:
                self._head_source[head] = None
                self._save_head_source()
            except Exception:
                pass
            self._last_load_ok = False
            raise
        rfid_deadline = time.monotonic() + 3.0
        while time.monotonic() < rfid_deadline:
            if self._info['slots'][slot].get('rfid', 0) != 0:
                break
            time.sleep(0.1)
        if self._info['slots'][slot].get('rfid', 0) == 0:
            logging.info('[multiACE] LOAD_HEAD: RFID not ready for slot %d after wait' % slot)
        slot_info = self._info['slots'][slot]
        self._head_source[head] = {'ace_index': ace_index, 'slot': slot, 'type': slot_info.get('type', 'PLA'), 'color': self.rgb2hex(*slot_info.get('color', (0, 0, 0))), 'brand': slot_info.get('brand', 'Generic')}
        self._save_head_source()
        self._ghost_heads.discard(head)
        load_override = self._override_for(ace_index, slot)
        if load_override is not None:
            push_type = load_override.get('material') or self._head_source[head]['type']
            push_color = self._override_color_to_rgba(load_override.get('color', ''))
            push_brand = load_override.get('brand') or self._head_source[head]['brand']
            push_subtype = load_override.get('subtype', '') or ''
        else:
            push_type = self._head_source[head]['type']
            push_color = self._head_source[head]['color']
            push_brand = self._head_source[head]['brand']
            push_subtype = ''
        self._expect_ptc_push(head, push_type, push_color, push_brand, push_subtype)
        self.gcode.run_script_from_command('SET_PRINT_FILAMENT_CONFIG CONFIG_EXTRUDER=%d FILAMENT_TYPE="%s" FILAMENT_COLOR_RGBA=%s VENDOR="%s" FILAMENT_SUBTYPE="%s"' % (head, push_type, push_color, push_brand, push_subtype))
        self.log_always(self._t('msg.load_head_loaded', head=head, ace=self._disp(ace_index), slot=self._disp(slot)))
        self._audit_state('LOAD_HEAD', {'head': head, 'ace': ace_index, 'slot': slot})
    cmd_ACE_UNLOAD_HEAD_help = '[multiACE] Unload a toolhead back to its ACE. Usage: ACE_UNLOAD_HEAD HEAD=0 [RETRACT_LENGTH=<mm>] [KEEP_HEAT=<temp>]'

    def cmd_ACE_UNLOAD_HEAD(self, gcmd):
        head = gcmd.get_int('HEAD')
        retract_override = gcmd.get_int('RETRACT_LENGTH', 0)
        keep_heat = gcmd.get_int('KEEP_HEAT', 0)
        self._last_unload_ok = True
        if head < 0 or head > 3:
            raise gcmd.error('[multiACE] HEAD must be 0-3')
        sensor = self.printer.lookup_object('filament_motion_sensor e%d_filament' % head, None)
        if sensor and (not sensor.get_status(0)['filament_detected']):
            self.log_always(self._t('msg.unload_sensor_no_filament', head=head))
        source = self._head_source.get(head)
        if source:
            ace_index = source['ace_index']
            slot = source['slot']
            self.log_always(self._t('msg.unload_head_starting', head=head, ace=self._disp(ace_index), slot=self._disp(slot)))
            if ace_index != self._active_device_index:
                if not self._switch_ace_for_head_target(ace_index):
                    raise gcmd.error('[multiACE] Failed to connect to ACE %d for unload!' % ace_index)
        else:
            self.log_always(self._t('msg.unload_head_no_mapping', head=head))

        def _noop_cb(self, response):
            pass
        active_idx = self._active_device_index
        stop_slots = set()
        tracked = self._feed_assist_per_ace.get(active_idx, -1)
        if 0 <= tracked <= 3:
            stop_slots.add(tracked)
        if source is not None:
            src_slot = source.get('slot', -1)
            if 0 <= src_slot <= 3:
                stop_slots.add(src_slot)
        for slot_idx in sorted(stop_slots):
            try:
                self.send_request_to(active_idx, {'method': 'stop_feed_assist', 'params': {'index': slot_idx}}, _noop_cb)
            except Exception as e:
                logging.info('[multiACE] targeted stop_feed_assist slot %d failed: %s' % (slot_idx, e))
        self._feed_assist_per_ace[active_idx] = -1
        if active_idx == self._active_device_index:
            self._feed_assist_index = -1
        self.wait_ace_ready()
        self._fa_trace('targeted-stop FA on ACE %d slots=%s before unload' % (active_idx, sorted(stop_slots)))
        if not self._swap_in_progress:
            self.gcode.run_script_from_command('SET_FILAMENT_SENSOR SENSOR=e%d_filament ENABLE=0' % head)
        module, channel = self.EXTRUDER_MAP[head]
        self._retract_length_override = retract_override if retract_override > 0 else None
        try:
            self.gcode.run_script_from_command('FEED_AUTO MODULE=%s CHANNEL=%d EXTRUDER=%d UNLOAD=1 STAGE=prepare' % (module, channel, head))
            self.gcode.run_script_from_command('FEED_AUTO MODULE=%s CHANNEL=%d EXTRUDER=%d UNLOAD=1 STAGE=doing' % (module, channel, head))
        except Exception as e:
            self._audit_state('UNLOAD_HEAD_FAILED', {'head': head, 'reason': 'feed_auto_error', 'error': str(e), 'active_device': self._active_device_index})
            raise
        finally:
            self._retract_length_override = None
        if keep_heat > 0:
            self.gcode.run_script_from_command('M104 S%d' % keep_heat)
        self.gcode.run_script_from_command('SET_FILAMENT_SENSOR SENSOR=e%d_filament ENABLE=1' % head)
        machine_state_manager = self.printer.lookup_object('machine_state_manager', None)
        if machine_state_manager is not None:
            self.gcode.run_script_from_command('SET_MAIN_STATE MAIN_STATE=IDLE ACTION=IDLE')
        self._head_source[head] = None
        self._save_head_source()
        self._push_rfid_info()
        if sensor and sensor.get_status(0)['filament_detected']:
            self.log_error(self._t('msg.unload_filament_still_detected', head=head))
        else:
            self.log_always(self._t('msg.unload_head_success', head=head))
        self._audit_state('UNLOAD_HEAD', {'head': head})
    cmd_ACE_TEST_help = '[multiACE] Run load/unload test. PLAN items (comma-sep): 0:1=load HEAD:ACE, H0:1=swap HEAD to ACE, A0=all from ACE, U=unload all, U0..U3=unload head, S0..S3=switch ACE, W5=wait 5s'

    def cmd_ACE_TEST(self, gcmd):
        plan_str = gcmd.get('PLAN', '')
        do_unload = gcmd.get_int('UNLOAD', 1)
        was_debug = self._state_debug_enabled
        self._state_debug_enabled = True
        self._state_log.info('TEST_START plan="%s" unload=%d', plan_str, do_unload)
        try:
            hs_dump = json.dumps({str(h): self._head_source[h] for h in range(4)})
        except Exception:
            hs_dump = str(self._head_source)
        self._state_log.info('TEST_START head_source=%s active_device=%d', hs_dump, self._active_device_index)
        self._audit_state('TEST_START', {'plan': plan_str, 'unload': do_unload})
        steps = []
        if plan_str:
            for item in plan_str.split(','):
                item = item.strip()
                if not item:
                    continue
                if item == 'U':
                    steps.append({'action': 'UNLOAD_ALL'})
                elif item.startswith('U') and item[1:].isdigit():
                    steps.append({'action': 'UNLOAD', 'head': int(item[1:])})
                elif item.startswith('A') and item[1:].isdigit():
                    ace = int(item[1:])
                    for h in range(4):
                        steps.append({'action': 'LOAD', 'head': h, 'ace': ace})
                elif item.startswith('H') and ':' in item[1:]:
                    parts = item[1:].split(':')
                    if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                        steps.append({'action': 'SWAP', 'head': int(parts[0]), 'ace': int(parts[1])})
                    else:
                        raise gcmd.error('[multiACE] Invalid PLAN item: %s (use H0:1)' % item)
                elif item.startswith('S') and item[1:].isdigit():
                    steps.append({'action': 'SWITCH', 'ace': int(item[1:])})
                elif item.startswith('W') and item[1:].replace('.', '', 1).isdigit():
                    steps.append({'action': 'WAIT', 'seconds': float(item[1:])})
                elif ':' in item:
                    parts = item.split(':')
                    if len(parts) == 2:
                        steps.append({'action': 'LOAD', 'head': int(parts[0]), 'ace': int(parts[1])})
                    else:
                        raise gcmd.error('[multiACE] Invalid PLAN item: %s' % item)
                else:
                    raise gcmd.error('[multiACE] Invalid PLAN item: %s (use HEAD:ACE, A0, U, U0..U3, S0..S3, W<seconds>)' % item)
        else:
            self._refresh_ace_devices('test')
            for i in range(min(len(self._ace_devices), 4)):
                steps.append({'action': 'LOAD', 'head': i, 'ace': i})
        self.log_always(self._t('msg.test_start', steps=len(steps), unload='yes' if do_unload else 'no'))
        try:
            self.gcode.run_script_from_command('G28')
            self.toolhead.wait_moves()
        except Exception as e:
            self.log_always(self._t('msg.test_homing_failed', error=e))
        self._test_cancel = False
        results = []
        step_nr = 0
        for step in steps:
            if self._test_cancel:
                self.log_always(self._t('msg.test_cancelled', step=step_nr))
                results.append({'step': step_nr + 1, 'action': 'CANCEL', 'status': 'CANCELLED'})
                break
            step_nr += 1
            action = step['action']
            if action == 'LOAD':
                head = step['head']
                ace = step['ace']
                self.log_always(self._t('msg.test_step_load', step=step_nr, total=len(steps), head=head, ace=self._disp(ace), slot=self._disp(head)))
                try:
                    self.gcode.run_script_from_command('ACE_LOAD_HEAD HEAD=%d ACE=%d SLOT=%d' % (head, ace, head))
                    sensor = self.printer.lookup_object('filament_motion_sensor e%d_filament' % head, None)
                    detected = sensor and sensor.get_status(0)['filament_detected']
                    src = self._head_source.get(head)
                    if detected and src is not None:
                        results.append({'step': step_nr, 'action': 'LOAD', 'status': 'PASS', 'head': head, 'ace': ace})
                        self.log_always(self._t('msg.test_step_load_pass', step=step_nr))
                    else:
                        reason = []
                        if not detected:
                            reason.append('sensor=no_filament')
                        if src is None:
                            reason.append('mapping=missing')
                        results.append({'step': step_nr, 'action': 'LOAD', 'status': 'FAIL', 'head': head, 'ace': ace, 'reason': ', '.join(reason)})
                        self.log_always(self._t('msg.test_step_fail_reasons', step=step_nr, reason=', '.join(reason)))
                except Exception as e:
                    results.append({'step': step_nr, 'action': 'LOAD', 'status': 'ERROR', 'head': head, 'ace': ace, 'reason': str(e)})
                    self.log_always(self._t('msg.test_step_error', step=step_nr, error=str(e)))
                self.gcode.run_script_from_command('ACE_HEAD_STATUS')
            elif action == 'UNLOAD':
                head = step['head']
                self.log_always(self._t('msg.test_step_unload', step=step_nr, total=len(steps), head=head))
                try:
                    self.gcode.run_script_from_command('ACE_UNLOAD_HEAD HEAD=%d' % head)
                    sensor = self.printer.lookup_object('filament_motion_sensor e%d_filament' % head, None)
                    still_loaded = sensor and sensor.get_status(0)['filament_detected']
                    if not still_loaded:
                        results.append({'step': step_nr, 'action': 'UNLOAD', 'status': 'PASS', 'head': head})
                        self.log_always(self._t('msg.test_step_unload_pass', step=step_nr))
                    else:
                        results.append({'step': step_nr, 'action': 'UNLOAD', 'status': 'FAIL', 'head': head, 'reason': 'filament still detected'})
                        self.log_always(self._t('msg.test_step_unload_fail', step=step_nr))
                except Exception as e:
                    results.append({'step': step_nr, 'action': 'UNLOAD', 'status': 'ERROR', 'head': head, 'reason': str(e)})
                    self.log_always(self._t('msg.test_step_error', step=step_nr, error=str(e)))
                self.gcode.run_script_from_command('ACE_HEAD_STATUS')
            elif action == 'UNLOAD_ALL':
                self.log_always(self._t('msg.test_step_unload_all', step=step_nr, total=len(steps)))
                try:
                    self.gcode.run_script_from_command('ACE_UNLOAD_ALL_HEADS')
                    all_clear = True
                    for h in range(4):
                        sensor = self.printer.lookup_object('filament_motion_sensor e%d_filament' % h, None)
                        if sensor and sensor.get_status(0)['filament_detected']:
                            all_clear = False
                    if all_clear:
                        results.append({'step': step_nr, 'action': 'UNLOAD_ALL', 'status': 'PASS'})
                        self.log_always(self._t('msg.test_step_unload_all_pass', step=step_nr))
                    else:
                        results.append({'step': step_nr, 'action': 'UNLOAD_ALL', 'status': 'FAIL', 'reason': 'filament still detected'})
                        self.log_always(self._t('msg.test_step_unload_fail', step=step_nr))
                except Exception as e:
                    results.append({'step': step_nr, 'action': 'UNLOAD_ALL', 'status': 'ERROR', 'reason': str(e)})
                    self.log_always(self._t('msg.test_step_error', step=step_nr, error=str(e)))
                self.gcode.run_script_from_command('ACE_HEAD_STATUS')
            elif action == 'SWITCH':
                ace = step['ace']
                self.log_always(self._t('msg.test_step_switch', step=step_nr, total=len(steps), ace=self._disp(ace)))
                try:
                    self.gcode.run_script_from_command('ACE_SWITCH TARGET=%d' % ace)
                    if self._active_device_index == ace:
                        results.append({'step': step_nr, 'action': 'SWITCH', 'status': 'PASS', 'ace': ace})
                        self.log_always(self._t('msg.test_step_switch_pass', step=step_nr, ace=self._disp(ace)))
                    else:
                        results.append({'step': step_nr, 'action': 'SWITCH', 'status': 'FAIL', 'ace': ace, 'reason': 'active=%d' % self._active_device_index})
                        self.log_always(self._t('msg.test_step_switch_fail', step=step_nr, active=self._disp(self._active_device_index), expected=self._disp(ace)))
                except Exception as e:
                    results.append({'step': step_nr, 'action': 'SWITCH', 'status': 'ERROR', 'ace': ace, 'reason': str(e)})
                    self.log_always(self._t('msg.test_step_error', step=step_nr, error=str(e)))
            elif action == 'SWAP':
                head = step['head']
                ace = step['ace']
                self.log_always(self._t('msg.test_step_swap', step=step_nr, total=len(steps), head=head, ace=self._disp(ace)))
                try:
                    self.gcode.run_script_from_command('ACE_SWAP_HEAD HEAD=%d ACE=%d' % (head, ace))
                    sensor = self.printer.lookup_object('filament_motion_sensor e%d_filament' % head, None)
                    detected = sensor and sensor.get_status(0)['filament_detected']
                    src = self._head_source.get(head)
                    if detected and src is not None and (src['ace_index'] == ace):
                        results.append({'step': step_nr, 'action': 'SWAP', 'status': 'PASS', 'head': head, 'ace': ace})
                        self.log_always(self._t('msg.test_step_swap_pass', step=step_nr, ace=self._disp(ace)))
                    else:
                        reason = []
                        if not detected:
                            reason.append('sensor=no_filament')
                        if src is None:
                            reason.append('mapping=missing')
                        elif src['ace_index'] != ace:
                            reason.append('mapping=ACE %d (expected %d)' % (src['ace_index'], ace))
                        results.append({'step': step_nr, 'action': 'SWAP', 'status': 'FAIL', 'head': head, 'ace': ace, 'reason': ', '.join(reason)})
                        self.log_always(self._t('msg.test_step_fail_reasons', step=step_nr, reason=', '.join(reason)))
                except Exception as e:
                    results.append({'step': step_nr, 'action': 'SWAP', 'status': 'ERROR', 'head': head, 'ace': ace, 'reason': str(e)})
                    self.log_always(self._t('msg.test_step_error', step=step_nr, error=str(e)))
                self.gcode.run_script_from_command('ACE_HEAD_STATUS')
            elif action == 'WAIT':
                seconds = step['seconds']
                self.log_always(self._t('msg.test_step_wait', step=step_nr, total=len(steps), seconds=seconds))
                try:
                    self.reactor.pause(self.reactor.monotonic() + seconds)
                    results.append({'step': step_nr, 'action': 'WAIT', 'status': 'PASS', 'seconds': seconds})
                except Exception as e:
                    results.append({'step': step_nr, 'action': 'WAIT', 'status': 'ERROR', 'seconds': seconds, 'reason': str(e)})
                    self.log_always(self._t('msg.test_step_error', step=step_nr, error=str(e)))
        if do_unload:
            step_nr += 1
            self.log_always(self._t('msg.test_final_unload_all'))
            try:
                self.gcode.run_script_from_command('ACE_UNLOAD_ALL_HEADS')
                all_clear = True
                for h in range(4):
                    sensor = self.printer.lookup_object('filament_motion_sensor e%d_filament' % h, None)
                    if sensor and sensor.get_status(0)['filament_detected']:
                        all_clear = False
                if all_clear:
                    results.append({'step': 'final', 'action': 'UNLOAD_ALL', 'status': 'PASS'})
                    self.log_always(self._t('msg.test_final_pass'))
                else:
                    results.append({'step': 'final', 'action': 'UNLOAD_ALL', 'status': 'FAIL', 'reason': 'filament still detected'})
                    self.log_always(self._t('msg.test_final_fail'))
            except Exception as e:
                results.append({'step': 'final', 'action': 'UNLOAD_ALL', 'status': 'ERROR', 'reason': str(e)})
                self.log_always(self._t('msg.test_final_error', error=str(e)))
        passed = sum((1 for r in results if r['status'] == 'PASS'))
        failed = sum((1 for r in results if r['status'] == 'FAIL'))
        errors = sum((1 for r in results if r['status'] == 'ERROR'))
        total = len(results)
        self.log_always(self._t('msg.test_complete', passed=passed, total=total, failed=failed, errors=errors))
        self._state_log.info('TEST_RESULT %s', json.dumps(results, default=str))
        self._state_debug_enabled = was_debug

    def _get_swap_temp(self, head):
        try:
            ptc = self.printer.lookup_object('print_task_config', None)
            fp = self.printer.lookup_object('filament_parameters', None)
            if ptc is not None and fp is not None:
                status = ptc.get_status()
                temp = fp.get_load_temp(status['filament_vendor'][head], status['filament_type'][head], status['filament_sub_type'][head])
                if temp and temp >= 170:
                    return int(temp)
        except Exception:
            pass
        try:
            extruder_name = 'extruder' if head == 0 else 'extruder%d' % head
            extruder = self.printer.lookup_object(extruder_name, None)
            if extruder is not None:
                target = extruder.get_heater().target_temp
                if target >= 170:
                    return int(target)
        except Exception:
            pass
        return self.swap_default_temp
    cmd_ACE_SWAP_HEAD_help = '[multiACE] Mid-print filament swap. Usage: ACE_SWAP_HEAD HEAD=0 ACE=1 [SLOT=0]'

    def cmd_ACE_SWAP_HEAD(self, gcmd):
        head = gcmd.get_int('HEAD')
        ace_index = gcmd.get_int('ACE')
        slot = gcmd.get_int('SLOT', head)
        if head < 0 or head > 3:
            raise gcmd.error('[multiACE] HEAD must be 0-3')
        if ace_index < 0 or not self._ensure_ace_available(ace_index):
            raise gcmd.error('ACE %d not available' % ace_index)
        if slot < 0 or slot > 3:
            raise gcmd.error('[multiACE] SLOT must be 0-3')
        if head in self._ghost_heads:
            raise gcmd.error('[multiACE] SWAP refused: head %d is a ghost (filament at toolhead but no head_source mapping recorded). FA routing would have to guess which ACE to drive. Recover: ACEC__Unload_All then ACEB__Load_%d, then restart the print.' % (head, head))
        source = self._head_source.get(head)
        if source and source['ace_index'] == ace_index and (source['slot'] == slot):
            logging.info('[multiACE] Swap: HEAD %d already on ACE %d / Slot %d — skipping' % (head, ace_index, slot))
            swap_temp = self._get_swap_temp(head)
            if swap_temp >= 170:
                heater = 'extruder' if head == 0 else 'extruder%d' % head
                self.gcode.run_script_from_command('SET_HEATER_TEMPERATURE HEATER=%s TARGET=%d' % (heater, swap_temp))
                self.gcode.run_script_from_command('TEMPERATURE_WAIT SENSOR=%s MINIMUM=%d' % (heater, swap_temp - 5))
            return
        if ace_index in self._fa_load_disable:
            self.log_error(self._t('msg.swap_refused_fa_load_disable', ace=self._disp(ace_index), head=head))
            return
        target_gate = self._gate_status_per_ace.get(ace_index)
        if target_gate is not None and slot < len(target_gate) and (target_gate[slot] != GATE_AVAILABLE):
            cur_src = self._head_source.get(head)
            self._telemetry('SWAP_SUMMARY', {'head': head, 'from_ace': cur_src['ace_index'] if cur_src else None, 'from_slot': cur_src['slot'] if cur_src else None, 'to_ace': ace_index, 'to_slot': slot, 'status': 'slot_empty_pre_unload', 'total_ms': 0, 'unload_ms': None, 'load_ms': None, 'context': self._fa_context})
            self._pause_for_recovery(phase='swap slot_empty (pre-unload)', display_msg='A%dS%d leer' % (ace_index, slot), detail_msg='ACE %d Slot %d leer - siehe Fluidd log fuer Recovery' % (ace_index, slot), recovery_steps=['Load filament into ACE %d slot %d' % (ace_index, slot), 'ACE_SWAP_HEAD HEAD=%d ACE=%d SLOT=%d   (re-run swap)' % (head, ace_index, slot), 'RESUME                            (continue the print)'])
            return
        swap_temp = self._get_swap_temp(head)
        self.log_always(self._t('msg.swap_start', head=head, ace=self._disp(ace_index), slot=self._disp(slot), temp=swap_temp))
        swap_start_ts = time.monotonic()
        unload_start_ts = None
        unload_end_ts = None
        load_start_ts = None
        load_end_ts = None
        swap_status = 'ok'
        prev_source = self._head_source.get(head)
        prev_ace_src = prev_source['ace_index'] if prev_source else None
        prev_slot_src = prev_source['slot'] if prev_source else None
        self._swap_in_progress = True
        fa_prev_auto = self._auto_feed_enabled
        fa_prev_context = self._fa_context
        self._auto_feed_enabled = False
        self._fa_context = 'idle'
        self._fa_trace('gate CLOSE for swap unload (was auto=%s context=%s)' % (fa_prev_auto, fa_prev_context))
        try:
            gcode_move = self.printer.lookup_object('gcode_move')
            saved_pos = self.toolhead.get_position()[:3]
            saved_speed = gcode_move.speed
            saved_absolute = gcode_move.absolute_coord
            saved_e_base = gcode_move.base_position[3]
            saved_e_last = gcode_move.last_position[3]
            logging.info('[multiACE] Swap: saved pos X=%.2f Y=%.2f Z=%.2f (pre-T-switch)' % (saved_pos[0], saved_pos[1], saved_pos[2]))
            orig_ext_name = self.toolhead.get_extruder().get_name()
            target_ext = 'extruder' if head == 0 else 'extruder%d' % head
            switched_head = orig_ext_name != target_ext
            if switched_head:
                logging.info('[multiACE] Swap: switching to %s (was %s)' % (target_ext, orig_ext_name))
                self.gcode.run_script_from_command('T%d A0' % head)
                self.toolhead.wait_moves()
            saved_heater_target = 0
            try:
                extruder_obj = self.toolhead.get_extruder()
                if extruder_obj is not None:
                    saved_heater_target = int(extruder_obj.get_heater().target_temp)
            except Exception:
                pass
            logging.info('[multiACE] Swap: saved heater=%d (swap head)' % saved_heater_target)
            prev_ace = self._active_device_index
            if self._feed_assist_per_ace.get(prev_ace, -1) != -1:
                self._disarm_fa_for(prev_ace)
            self.gcode.run_script_from_command('G91')
            self.gcode.run_script_from_command('G1 Z2 F600')
            self.gcode.run_script_from_command('G90')
            self.toolhead.wait_moves()
            self.gcode.run_script_from_command('M83')
            sensor_obj = self.printer.lookup_object('filament_motion_sensor e%d_filament' % head, None)
            sensor_present = sensor_obj is not None and sensor_obj.get_status(0)['filament_detected']
            empty_head = not sensor_present and prev_source is None
            if empty_head:
                logging.info('[multiACE] Swap: head %d is empty (sensor=False, head_source=None) — skipping unload, proceeding directly to load' % head)
                unload_start_ts = time.monotonic()
                unload_end_ts = unload_start_ts
            else:
                logging.info('[multiACE] Swap: delegating unload to ACE_UNLOAD_HEAD')
                unload_start_ts = time.monotonic()
                if self.swap_retract_length > 0:
                    self.gcode.run_script_from_command('ACE_UNLOAD_HEAD HEAD=%d RETRACT_LENGTH=%d KEEP_HEAT=%d' % (head, self.swap_retract_length, swap_temp))
                    logging.info('[multiACE] Swap: unload done (retract %dmm, heat held @ %d)' % (self.swap_retract_length, swap_temp))
                else:
                    self.gcode.run_script_from_command('ACE_UNLOAD_HEAD HEAD=%d KEEP_HEAT=%d' % (head, swap_temp))
                    logging.info('[multiACE] Swap: unload done (per-ACE retract_length, heat held @ %d)' % swap_temp)
                unload_end_ts = time.monotonic()
                if not self._last_unload_ok:
                    swap_status = 'unload_failed'
                    self._swap_back_to_orig_for_pause(switched_head, orig_ext_name)
                    self._pause_for_recovery(phase='swap unload_failed', display_msg='Unload H%d jam' % head, detail_msg='Head %d unload jam - siehe Fluidd log fuer Recovery' % head, recovery_steps=['ACE_UNLOAD_HEAD HEAD=%d           (try unload again)' % head, 'ACE_SWITCH TARGET=%d             (switch to target ACE)' % ace_index, 'ACE_LOAD_HEAD HEAD=%d            (load target filament)' % head, 'RESUME                           (continue the print)'])
                    return
            if ace_index != self._active_device_index:
                self.log_always(self._t('msg.swap_switching_ace', ace=self._disp(ace_index)))
                if not self._switch_ace_for_head_target(ace_index):
                    raise gcmd.error('[multiACE] Failed to connect to ACE %d' % ace_index)
            if self.gate_status[slot] != GATE_AVAILABLE:
                swap_status = 'slot_empty'
                self._swap_back_to_orig_for_pause(switched_head, orig_ext_name)
                self._restore_pos_for_pause(saved_pos)
                self._pause_for_recovery(phase='swap slot_empty (post-unload)', display_msg='A%dS%d leer' % (ace_index, slot), detail_msg='ACE %d Slot %d leer (post-unload) - siehe Fluidd log' % (ace_index, slot), recovery_steps=['Load filament into ACE %d slot %d' % (ace_index, slot), 'ACE_LOAD_HEAD HEAD=%d ACE=%d SLOT=%d   (load head)' % (head, ace_index, slot), 'RESUME                            (continue the print)'])
                return
            logging.info('[multiACE] Swap: delegating load to ACE_LOAD_HEAD (ACE %d / Slot %d)' % (ace_index, slot))
            load_start_ts = time.monotonic()
            try:
                self.gcode.run_script_from_command('ACE_LOAD_HEAD HEAD=%d ACE=%d SLOT=%d' % (head, ace_index, slot))
            except Exception as load_e:
                logging.info('[multiACE] Swap LOAD raised before completion: %s (routing to swap_back+pos_restore+pause)' % load_e)
                self._swap_back_to_orig_for_pause(switched_head, orig_ext_name)
                self._restore_pos_for_pause(saved_pos)
                raise
            load_end_ts = time.monotonic()
            if not self._last_load_ok:
                swap_status = 'load_failed'
                self._swap_back_to_orig_for_pause(switched_head, orig_ext_name)
                self._restore_pos_for_pause(saved_pos)
                self._pause_for_recovery(phase='swap load_failed', display_msg='Load H%d slip' % head, detail_msg='Head %d Load slip - siehe Fluidd log fuer Recovery' % head, recovery_steps=['ACE_UNLOAD_HEAD HEAD=%d           (clear partial filament)' % head, 'ACE_LOAD_HEAD HEAD=%d ACE=%d SLOT=%d   (reload)' % (head, ace_index, slot), 'RESUME                           (continue the print)'])
                return
            logging.info('[multiACE] Swap: load done')
            self._auto_feed_enabled = True
            self._fa_context = fa_prev_context if fa_prev_context in ('print', 'load') else 'print'
            try:
                self._arm_fa_for(ace_index, slot)
                self.wait_ace_ready()
                self._fa_trace('gate RE-OPEN for post-load wipe (context=%s) on ACE %d slot %d' % (self._fa_context, ace_index, slot))
            except Exception as fa_e:
                logging.info('[multiACE] post-load FA re-enable failed: %s' % fa_e)
            self.gcode.run_script_from_command('M109 S%d' % swap_temp)
            self.gcode.run_script_from_command('ROUGHLY_CLEAN_NOZZLE_WITH_DISCARD')
            self.toolhead.wait_moves()
            self.gcode.run_script_from_command('G91')
            if self.swap_anti_ooze_retract > 0:
                self.gcode.run_script_from_command('G1 E-%d F1800' % self.swap_anti_ooze_retract)
            self.gcode.run_script_from_command('G90')
            self.toolhead.wait_moves()
            self.gcode.run_script_from_command('M104 S%d' % saved_heater_target)
            if saved_heater_target >= 190:
                self.gcode.run_script_from_command('M109 S%d' % saved_heater_target)
            logging.info('[multiACE] Swap: restored swap head heater target=%d' % saved_heater_target)
            if switched_head:
                orig_head = 0 if orig_ext_name == 'extruder' else int(orig_ext_name.replace('extruder', ''))
                logging.info('[multiACE] Swap: switching back to %s' % orig_ext_name)
                self.gcode.run_script_from_command('T%d A0' % orig_head)
                self.toolhead.wait_moves()
            e_diff = gcode_move.last_position[3] - saved_e_last
            gcode_move.base_position[3] = saved_e_base + e_diff
            self.gcode.run_script_from_command('G90')
            self.gcode.run_script_from_command('G0 Z%.3f F600' % (saved_pos[2] + 3.0))
            self.gcode.run_script_from_command('G0 Y%.3f F12000' % saved_pos[1])
            self.gcode.run_script_from_command('G0 X%.3f F12000' % saved_pos[0])
            self.gcode.run_script_from_command('G0 Z%.3f F600' % (saved_pos[2] + 2.0))
            self.toolhead.wait_moves()
            try:
                orig_extruder_obj = self.printer.lookup_object(orig_ext_name, None) if orig_ext_name else None
                orig_heater = orig_extruder_obj.get_heater() if orig_extruder_obj is not None else None
                eventtime = self.reactor.monotonic()
                cur_temp, _ = orig_heater.get_temp(eventtime) if orig_heater is not None else (0.0, 0.0)
                min_temp = orig_heater.min_extrude_temp if orig_heater is not None else 170.0
            except Exception:
                cur_temp = 0.0
                min_temp = 170.0
            if cur_temp >= min_temp:
                self.gcode.run_script_from_command('G91')
                self.gcode.run_script_from_command('G1 E%d F1800' % self.swap_anti_ooze_retract)
                self.toolhead.wait_moves()
            else:
                logging.info('[multiACE] Swap: skipping anti-ooze undo G1 E+%d (orig %s at %.1f<%.0f min_extrude_temp); adjusting gcode E base virtually' % (self.swap_anti_ooze_retract, orig_ext_name or '?', cur_temp, min_temp))
                gcode_move.base_position[3] -= float(self.swap_anti_ooze_retract)
            if saved_absolute:
                self.gcode.run_script_from_command('G90')
            e_diff2 = gcode_move.last_position[3] - saved_e_last
            gcode_move.base_position[3] = saved_e_base + e_diff2
            self.gcode.run_script_from_command('G1 F%d' % (saved_speed * 60))
            logging.info('[multiACE] Swap: restored pos X=%.2f Y=%.2f Z=%.2f (+2mm travel hop)' % (saved_pos[0], saved_pos[1], saved_pos[2]))
            self.log_always(self._t('msg.swap_complete', head=head, ace=self._disp(ace_index), slot=self._disp(slot)))
        finally:
            self._swap_in_progress = False
            self._auto_feed_enabled = fa_prev_auto
            self._fa_context = fa_prev_context
            if fa_prev_auto:
                try:
                    active_ext = self.toolhead.get_extruder().get_name()
                    active_head = 0 if active_ext == 'extruder' else int(active_ext.replace('extruder', ''))
                    active_source = self._head_source.get(active_head)
                    if active_source is not None:
                        self._arm_fa_for(active_source['ace_index'], active_source['slot'])
                    else:
                        logging.info('[multiACE] post-swap FA: active head %d has no head_source, skipping start' % active_head)
                except Exception as e:
                    logging.info('[multiACE] post-swap FA start failed: %s' % e)
            self._fa_trace('gate restored (context=%s auto=%s) after ACE_SWAP_HEAD' % (fa_prev_context, fa_prev_auto))
            self._audit_state('SWAP_HEAD', {'head': head, 'ace': ace_index, 'slot': slot})

            def _dur_ms(start, end):
                if start is None or end is None:
                    return None
                return int((end - start) * 1000)
            swap_end_ts = time.monotonic()
            self._telemetry('SWAP_SUMMARY', {'head': head, 'from_ace': prev_ace_src, 'from_slot': prev_slot_src, 'to_ace': ace_index, 'to_slot': slot, 'status': swap_status, 'total_ms': _dur_ms(swap_start_ts, swap_end_ts), 'unload_ms': _dur_ms(unload_start_ts, unload_end_ts), 'load_ms': _dur_ms(load_start_ts, load_end_ts), 'context': fa_prev_context})

    def _switch_ace_for_head_target(self, ace_index):
        if ace_index == self._active_device_index:
            self._audit_state('SWITCH_TARGET_NOOP', {'target_ace': ace_index, 'reason': 'already_active'})
            return True
        if ace_index < 0 or ace_index >= len(self._ace_devices):
            self._audit_state('SWITCH_TARGET_FAILED', {'target_ace': ace_index, 'reason': 'ace_out_of_range'})
            return False
        if not self._connected_per_ace.get(ace_index, False):
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                if self._connected_per_ace.get(ace_index, False):
                    break
                self.reactor.pause(self.reactor.monotonic() + 0.2)
            if not self._connected_per_ace.get(ace_index, False):
                self._audit_state('SWITCH_TARGET_FAILED', {'target_ace': ace_index, 'reason': 'not_connected'})
                return False
        self._set_active_idx(ace_index)
        self._audit_state('SWITCH_TARGET', {'target_ace': ace_index})
        return True
    cmd_ACE_HEAD_STATUS_help = '[multiACE] Show active ACE, detected devices, and head-to-ACE/slot mapping'

    def cmd_ACE_HEAD_STATUS(self, gcmd):
        try:
            ace_mtime = os.path.getmtime(os.path.abspath(__file__))
            from datetime import datetime
            ts = datetime.fromtimestamp(ace_mtime).strftime('%Y-%m-%d %H:%M:%S')
        except Exception:
            ts = 'unknown'
        self.log_always(self._t('msg.version_file', version=MULTIACE_VERSION, codename=MULTIACE_CODENAME, build=MULTIACE_BUILD_TAG, ts=ts))
        actual_bundle = self._compute_bundle_sha1()
        expected_bundle = MULTIACE_BUNDLE_SHA1
        marker = 'MATCH' if expected_bundle == actual_bundle else 'MISMATCH'
        self.log_always(self._t('msg.bundle_status', expected=expected_bundle, actual=actual_bundle, marker=marker))
        s = self._usb_stats
        uptime_min = (time.monotonic() - s['start_time']) / 60.0
        self.log_always(self._t('msg.usb_stats_summary', uptime=uptime_min, errno5=s['errno5_total'], recovered=s['errno5_recovered'], lost=s['errno5_unrecovered'], cascades=s['cascades'], connects=s['connects'], disconnects=s['disconnects']))
        device_count = len(self._ace_devices)
        if device_count == 0:
            self.log_always(self._t('msg.no_ace_devices_detected'))
            return
        self.log_always(self._t('msg.active_ace_of', active=self._disp(self._active_device_index), count=device_count))
        for i, device in enumerate(self._ace_devices):
            marker = ' << ACTIVE' if i == self._active_device_index else ''
            protocol_cls = self._ace_path_protocol.get(device)
            proto_name = protocol_cls.NAME if protocol_cls else '?'
            model, firmware = self._ace_models.get(i, ('?', '?'))
            self.log_always(self._t('msg.ace_list_line', ace=self._disp(i), proto=proto_name, device=device, model=model, firmware=firmware, marker=marker))
        self.log_always(self._t('msg.head_source_mapping'))
        any_loaded = False
        for head in range(4):
            source = self._head_source[head]
            if source:
                any_loaded = True
                self.log_always(self._t('msg.head_mapping_line', head=head, ace=self._disp(source['ace_index']), slot=self._disp(source['slot']), brand=source.get('brand', ''), type=source.get('type', ''), color=source.get('color', '')))
            else:
                self.log_always(self._t('msg.head_mapping_empty', head=head))
        if not any_loaded:
            self.log_always(self._t('msg.head_mapping_none'))

    def _v2_resolve_ace(self, gcmd):
        idx = gcmd.get_int('ACE', -1)
        if idx < 0:
            active = self._active_device_index
            active_proto = self._protocols.get(active)
            if active_proto is not None and getattr(active_proto, 'NAME', None) == 'v2':
                idx = active
        if idx < 0:
            for i, proto in self._protocols.items():
                if proto is not None and getattr(proto, 'NAME', None) == 'v2':
                    idx = i
                    break
        if idx < 0:
            raise gcmd.error('No V2 ACE detected — connect device or pass ACE=<idx>')
        proto = self._protocols.get(idx)
        if proto is None or getattr(proto, 'NAME', None) != 'v2':
            raise gcmd.error('ACE %d is not a V2 device' % idx)
        return idx

    def _v2_dispatch_and_wait(self, gcmd, idx, method, params, timeout=3.0):
        captured = {'response': None, 'done': False}

        def cb(self, response):
            captured['response'] = response
            captured['done'] = True
        try:
            self.send_request_to(idx, {'method': method, 'params': params}, cb)
        except Exception as e:
            raise gcmd.error('V2 dispatch failed: %s' % e)
        reactor = self.printer.get_reactor()
        deadline = reactor.monotonic() + timeout
        while not captured['done'] and reactor.monotonic() < deadline:
            reactor.pause(reactor.monotonic() + 0.05)
        if not captured['done']:
            gcmd.respond_info(self._t('msg.v2_response_timeout', method=method, timeout=timeout))
            return None
        resp = captured['response']
        try:
            text = json.dumps(resp, default=str, sort_keys=True)
        except Exception:
            text = repr(resp)
        gcmd.respond_info(self._t('msg.v2_response_text', method=method, text=text))
        return resp
    cmd_A_DISCOVER_help = '[multiACE] V2 cmd 0 DISCOVER_DEVICE. Usage: A_DISCOVER [ACE=0]'

    def cmd_A_DISCOVER(self, gcmd):
        idx = self._v2_resolve_ace(gcmd)
        self._v2_dispatch_and_wait(gcmd, idx, 'discover_device', {})
    cmd_A_INFO_help = '[multiACE] V2 cmd 7 GET_INFO. Usage: A_INFO [ACE=0]'

    def cmd_A_INFO(self, gcmd):
        idx = self._v2_resolve_ace(gcmd)
        self._v2_dispatch_and_wait(gcmd, idx, 'get_info', {})
    cmd_A_STATUS_help = '[multiACE] V2 cmd 6 GET_STATUS. Usage: A_STATUS [ACE=0]'

    def cmd_A_STATUS(self, gcmd):
        idx = self._v2_resolve_ace(gcmd)
        self._v2_dispatch_and_wait(gcmd, idx, 'get_status', {})
    cmd_A_TEMP_help = '[multiACE] V2 cmd 64 GET_TEMP. Usage: A_TEMP [ACE=0]'

    def cmd_A_TEMP(self, gcmd):
        idx = self._v2_resolve_ace(gcmd)
        self._v2_dispatch_and_wait(gcmd, idx, 'get_temp', {})
    cmd_A_FEEDINFO_help = '[multiACE] V2 cmd 76 GET_FEED_INFO. Usage: A_FEEDINFO [ACE=0]'

    def cmd_A_FEEDINFO(self, gcmd):
        idx = self._v2_resolve_ace(gcmd)
        self._v2_dispatch_and_wait(gcmd, idx, 'get_feed_info', {})
    cmd_A_KEYSTATE_help = '[multiACE] V2 cmd 73 GET_KEY_STATE. Usage: A_KEYSTATE [ACE=0]'

    def cmd_A_KEYSTATE(self, gcmd):
        idx = self._v2_resolve_ace(gcmd)
        self._v2_dispatch_and_wait(gcmd, idx, 'get_key_state', {})
    cmd_A_FILAMENT_help = '[multiACE] V2 cmd 13 GET_FILAMENT_INFO. Usage: A_FILAMENT [ACE=0] [SLOT=0]'

    def cmd_A_FILAMENT(self, gcmd):
        idx = self._v2_resolve_ace(gcmd)
        slot = gcmd.get_int('SLOT', 0)
        self._v2_dispatch_and_wait(gcmd, idx, 'get_filament_info', {'index': slot})
    cmd_A_RFID_help = '[multiACE] V2 cmd 14 SET_RFID_ENABLE. Usage: A_RFID [ACE=0] [SLOT=0] [ENABLE=1]'

    def cmd_A_RFID(self, gcmd):
        idx = self._v2_resolve_ace(gcmd)
        slot = gcmd.get_int('SLOT', 0)
        enable = bool(gcmd.get_int('ENABLE', 1))
        self._v2_dispatch_and_wait(gcmd, idx, 'set_rfid_enable', {'index': slot, 'enable': enable})
    cmd_A_FEED_help = '[multiACE] V2 cmd 8 FEED_OR_ROLLBACK. Usage: A_FEED [ACE=0] SLOT=0 [SPEED=100] [LENGTH=200] [MODE=0]  (mode 0=feed, 1=rollback, 2=assist, 3=rollback_assist)'

    def cmd_A_FEED(self, gcmd):
        idx = self._v2_resolve_ace(gcmd)
        slot = gcmd.get_int('SLOT', 0)
        speed = gcmd.get_int('SPEED', 100)
        length = gcmd.get_int('LENGTH', 200)
        mode = gcmd.get_int('MODE', 0)
        self._v2_dispatch_and_wait(gcmd, idx, 'feed_or_rollback_raw', {'index': slot, 'speed': speed, 'length': length, 'mode': mode})
    cmd_A_ROLLBACK_help = '[multiACE] V2 cmd 8 FEED_OR_ROLLBACK mode=1. Usage: A_ROLLBACK [ACE=0] SLOT=0 [SPEED=50] [LENGTH=100]'

    def cmd_A_ROLLBACK(self, gcmd):
        idx = self._v2_resolve_ace(gcmd)
        slot = gcmd.get_int('SLOT', 0)
        speed = gcmd.get_int('SPEED', 50)
        length = gcmd.get_int('LENGTH', 100)
        self._v2_dispatch_and_wait(gcmd, idx, 'feed_or_rollback_raw', {'index': slot, 'speed': speed, 'length': length, 'mode': 1})
    cmd_A_STOP_help = '[multiACE] V2 cmd 9 STOP_FEED_OR_ROLLBACK. Usage: A_STOP [ACE=0] SLOT=0'

    def cmd_A_STOP(self, gcmd):
        idx = self._v2_resolve_ace(gcmd)
        slot = gcmd.get_int('SLOT', 0)
        self._v2_dispatch_and_wait(gcmd, idx, 'stop_feed_assist', {'index': slot})
    cmd_A_SPEED_help = '[multiACE] V2 cmd 10 UPDATE_SPEED. Usage: A_SPEED [ACE=0] SLOT=0 SPEED=100'

    def cmd_A_SPEED(self, gcmd):
        idx = self._v2_resolve_ace(gcmd)
        slot = gcmd.get_int('SLOT', 0)
        speed = gcmd.get_int('SPEED')
        self._v2_dispatch_and_wait(gcmd, idx, 'update_feeding_speed', {'index': slot, 'speed': speed})
    cmd_A_DRY_help = '[multiACE] V2 cmd 11 DRYING. Usage: A_DRY [ACE=0] [TEMP=50] [DURATION=120] [AUTO_ROLL=1]'

    def cmd_A_DRY(self, gcmd):
        idx = self._v2_resolve_ace(gcmd)
        temp = gcmd.get_int('TEMP', 50)
        duration = gcmd.get_int('DURATION', 120)
        auto_roll = bool(gcmd.get_int('AUTO_ROLL', 1))
        self._v2_dispatch_and_wait(gcmd, idx, 'drying_raw', {'temp': temp, 'duration': duration, 'auto_roll': auto_roll})
    cmd_A_DRYSTOP_help = '[multiACE] V2 cmd 11 DRYING (stop). Usage: A_DRYSTOP [ACE=0]'

    def cmd_A_DRYSTOP(self, gcmd):
        idx = self._v2_resolve_ace(gcmd)
        self._v2_dispatch_and_wait(gcmd, idx, 'drying_stop', {})
    cmd_A_DRYTEMP_help = '[multiACE] V2 cmd 12 SET_DRY_TEMP. Usage: A_DRYTEMP [ACE=0] TEMP=50'

    def cmd_A_DRYTEMP(self, gcmd):
        idx = self._v2_resolve_ace(gcmd)
        temp = gcmd.get_int('TEMP')
        self._v2_dispatch_and_wait(gcmd, idx, 'set_dry_temp', {'temp': temp})
    cmd_A_FAN_help = '[multiACE] V2 cmd 71 SET_FAN. Usage: A_FAN [ACE=0] [SPEED=0] [FAN1=0] [FAN2=0]'

    def cmd_A_FAN(self, gcmd):
        idx = self._v2_resolve_ace(gcmd)
        speed = gcmd.get_int('SPEED', 0)
        fan1 = bool(gcmd.get_int('FAN1', 0))
        fan2 = bool(gcmd.get_int('FAN2', 0))
        self._v2_dispatch_and_wait(gcmd, idx, 'set_fan_raw', {'speed': speed, 'fan1': fan1, 'fan2': fan2})
    cmd_A_VALVE_help = '[multiACE] V2 cmd 66 SET_VALVE. Usage: A_VALVE [ACE=0] [V1=0] [V2=0]'

    def cmd_A_VALVE(self, gcmd):
        idx = self._v2_resolve_ace(gcmd)
        v1 = bool(gcmd.get_int('V1', 0))
        v2 = bool(gcmd.get_int('V2', 0))
        self._v2_dispatch_and_wait(gcmd, idx, 'set_valve', {'v1': v1, 'v2': v2})
    cmd_A_FEEDCHECK_help = '[multiACE] V2 cmd 19 SET_FEED_CHECK. Usage: A_FEEDCHECK [ACE=0] [CHECK=73] [ERROR=77]'

    def cmd_A_FEEDCHECK(self, gcmd):
        idx = self._v2_resolve_ace(gcmd)
        check_len = gcmd.get_int('CHECK', 73)
        error_len = gcmd.get_int('ERROR', 77)
        self._v2_dispatch_and_wait(gcmd, idx, 'set_feed_check', {'check_length': check_len, 'error_length': error_len})
    cmd_A_RAW_help = '[multiACE] V2 raw cmd. Usage: A_RAW [ACE=0] CMD=<id> [HEX=<protobuf hex>]'

    def cmd_A_RAW(self, gcmd):
        idx = self._v2_resolve_ace(gcmd)
        cmd_id = gcmd.get_int('CMD')
        hex_payload = gcmd.get('HEX', '')
        self._v2_dispatch_and_wait(gcmd, idx, 'raw', {'cmd': cmd_id, 'hex': hex_payload})
    cmd_ACE_CLEAR_HEADS_help = '[multiACE] Clear head-to-ACE/slot mapping and display info. Usage: ACE_CLEAR_HEADS [HEAD=0]'

    def cmd_ACE_CLEAR_HEADS(self, gcmd):
        head = gcmd.get_int('HEAD', -1)
        if head >= 0:
            if head > 3:
                raise gcmd.error('[multiACE] HEAD must be 0-3')
            self._head_source[head] = None
            self._clear_filament_display(head)
            self.log_always(self._t('msg.cleared_head_mapping', head=head))
        else:
            self._head_source = {0: None, 1: None, 2: None, 3: None}
            for h in range(4):
                self._clear_filament_display(h)
            self.log_always(self._t('msg.cleared_all_head_mappings'))
        self._save_head_source()
        self._audit_state('CLEAR_HEADS', {'head': head})

    def _push_slot_rfid_to_extruder(self, head):
        try:
            slots = self._info.get('slots', [{}] * 4)
            if head < 0 or head >= len(slots):
                return
            si = slots[head]
            if si.get('rfid') != 2:
                return
            ov = self._override_for(self._active_device_index, head)
            if ov is not None:
                push_type = ov.get('material') or si.get('type', 'PLA')
                push_color = self._override_color_to_rgba(ov.get('color', ''))
                push_brand = ov.get('brand') or si.get('brand', 'Generic')
                push_subtype = ov.get('subtype', '') or ''
            else:
                push_type = si.get('type', 'PLA')
                push_color = self.rgb2hex(*si.get('color', (0, 0, 0)))
                push_brand = si.get('brand', 'Generic')
                push_subtype = ''
            self._expect_ptc_push(head, push_type, push_color, push_brand, push_subtype)
            self.gcode.run_script_from_command('SET_PRINT_FILAMENT_CONFIG CONFIG_EXTRUDER=%d FILAMENT_TYPE="%s" FILAMENT_COLOR_RGBA=%s VENDOR="%s" FILAMENT_SUBTYPE="%s"' % (head, push_type, push_color, push_brand, push_subtype))
        except Exception as e:
            logging.info('[multiACE] _push_slot_rfid_to_extruder(%d) failed: %s' % (head, e))

    def _clear_filament_display(self, head):
        try:
            self._expect_ptc_push(head, '', '00000000', '', '')
            self.gcode.run_script_from_command('SET_PRINT_FILAMENT_CONFIG CONFIG_EXTRUDER=%d FILAMENT_TYPE="" FILAMENT_COLOR_RGBA=00000000 VENDOR="" FILAMENT_SUBTYPE=""' % head)
        except Exception:
            pass
    cmd_ACE_UNLOAD_ALL_HEADS_help = '[multiACE] Unload all toolheads that have filament loaded'

    def cmd_ACE_UNLOAD_ALL_HEADS(self, gcmd):
        if self._feed_assist_index != -1:
            self._disable_feed_assist()
            self.wait_ace_ready()
        unloaded_any = False
        for head in range(4):
            sensor = self.printer.lookup_object('filament_motion_sensor e%d_filament' % head, None)
            if not sensor or not sensor.get_status(0)['filament_detected']:
                continue
            source = self._head_source.get(head)
            if source and source['ace_index'] != self._active_device_index:
                self.log_always(self._t('msg.switching_ace_for_retract', ace=self._disp(source['ace_index']), head=head))
                switched = False
                for attempt in range(5):
                    if self._switch_ace_for_head_target(source['ace_index']):
                        switched = True
                        break
                    self.log_always(self._t('msg.ace_not_reachable_attempt', ace=self._disp(source['ace_index']), attempt=attempt + 1))
                    time.sleep(1.0)
                if not switched:
                    self.log_error(self._t('msg.ace_failed_after_retries', ace=self._disp(source['ace_index']), head=head))
                    continue
            self.log_always(self._t('msg.unloading_head_only', head=head))
            module, channel = self.EXTRUDER_MAP[head]
            self._audit_state('UNLOAD_ALL_STEP', {'head': head, 'active_device': self._active_device_index, 'expected_ace': source['ace_index'] if source else None, 'expected_slot': source['slot'] if source else None})
            try:
                self.gcode.run_script_from_command('FEED_AUTO MODULE=%s CHANNEL=%d EXTRUDER=%d UNLOAD=1 STAGE=prepare' % (module, channel, head))
                self.gcode.run_script_from_command('FEED_AUTO MODULE=%s CHANNEL=%d EXTRUDER=%d UNLOAD=1 STAGE=doing' % (module, channel, head))
            except Exception as e:
                self.log_always(self._t('msg.unload_head_failed_warn', head=head, error=str(e)))
            machine_state_manager = self.printer.lookup_object('machine_state_manager', None)
            if machine_state_manager is not None:
                self.gcode.run_script_from_command('SET_MAIN_STATE MAIN_STATE=IDLE ACTION=IDLE')
            self._head_source[head] = None
            self._push_slot_rfid_to_extruder(head)
            unloaded_any = True
        if unloaded_any:
            self._save_head_source()
            if self._active_device_index != 0 and len(self._ace_devices) > 0:
                self.log_always(self._t('msg.switching_back_ace0'))
                self._switch_ace_for_head_target(0)
            self._push_rfid_info()
            self.log_always(self._t('msg.all_heads_unloaded'))
        else:
            self.log_always(self._t('msg.no_filament_in_any_head'))
        cleared = []
        for h in range(4):
            sensor = self.printer.lookup_object('filament_motion_sensor e%d_filament' % h, None)
            detected = sensor and sensor.get_status(0)['filament_detected']
            if not detected and self._head_source.get(h) is not None:
                self._head_source[h] = None
                cleared.append(h)
        if cleared:
            self._save_head_source()
            self._push_rfid_info()
            self.log_always(self._t('msg.cleared_stale_head_source', heads=', '.join(('T%d' % h for h in cleared))))
        self._audit_state('UNLOAD_ALL')

    def cmd_ACE_TEST_CANCEL(self, gcmd):
        self._test_cancel = True
        self.log_always(self._t('msg.test_cancel_requested'))
    cmd_ACE_DRY_help = '[multiACE] Start drying on ACE. Usage: ACE_DRY ACE=0 [TEMP=] [DURATION=]'

    def cmd_ACE_DRY(self, gcmd):
        ace_idx = gcmd.get_int('ACE')
        if ace_idx < 0 or ace_idx >= len(self._ace_devices):
            self.log_always(self._t('msg.ace_not_available', ace=self._disp(ace_idx)))
            return
        temp = gcmd.get_int('TEMP', self.ace_dryer_temp.get(ace_idx, self.dryer_temp))
        duration = gcmd.get_int('DURATION', self.ace_dryer_duration.get(ace_idx, self.dryer_duration))
        self.gcode.run_script_from_command('ACE_SWITCH TARGET=%d' % ace_idx)
        self.gcode.run_script_from_command('ACE_START_DRYING TEMP=%d DURATION=%d' % (temp, duration))
        self.log_always(self._t('msg.drying_ace_at', ace=self._disp(ace_idx), temp=temp, duration=duration))
    cmd_ACE_RUN_MODE_SWITCH_help = '[multiACE] Switch mode: normal (stock), single (one ACE), multi (multi-ACE)'

    def cmd_ACE_RUN_MODE_SWITCH(self, gcmd):
        mode = gcmd.get('MODE', '').lower()
        if mode not in ('normal', 'single', 'multi'):
            raise gcmd.error('[multiACE] Invalid mode: %s. Use normal, single, or multi.' % mode)
        current = self._ace_mode
        if mode in ('single', 'multi') and current in ('single', 'multi'):
            self.gcode.run_script_from_command('SAVE_VARIABLE VARIABLE=ace__mode VALUE="\'%s\'"' % mode)
            self._ace_mode = mode
            if mode == 'multi':
                self._restore_head_source()
                self.printer.register_event_handler('extruder:activate_extruder', self._on_extruder_change)
            self.log_always(self._t('msg.switched_to_mode', mode=mode.upper()))
            return
        save_vars = self.printer.lookup_object('save_variables')
        vars_path = save_vars.filename
        script_dir = os.path.dirname(os.path.abspath(vars_path))
        script = os.path.join(script_dir, 'ace_mode_switch.sh')
        if not os.path.exists(script):
            raise gcmd.error('[multiACE] Mode switch script not found: %s' % script)
        file_mode = 'normal' if mode == 'normal' else 'ace'
        self.log_always(self._t('msg.running_mode_switch', mode=mode.upper()))
        self.gcode.run_script_from_command('SAVE_VARIABLE VARIABLE=ace__mode VALUE="\'%s\'"' % mode)
        try:
            import subprocess
            result = subprocess.run(['bash', script, file_mode], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
            if result.returncode != 0:
                raise gcmd.error('[multiACE] Mode switch script failed (rc=%d): %s' % (result.returncode, result.stderr.decode('utf-8', 'replace')))
        except subprocess.TimeoutExpired:
            raise gcmd.error('[multiACE] Mode switch script timed out after 30s')
        except Exception as e:
            raise gcmd.error('[multiACE] Failed to run mode switch script: %s' % str(e))
        self.gcode.run_script_from_command('RAISE_EXCEPTION ID=6666 INDEX=6 CODE=6 MESSAGE="[multiACE] Switched to %s mode. Please reboot!" ONESHOT=0 LEVEL=2' % mode.upper())
        raise gcmd.error('[multiACE] Switched to %s mode. Please reboot the printer to activate!' % mode.upper())
    cmd_ACE_LIST_help = 'List all detected ACE devices (up to 4)'

    def cmd_ACE_LIST(self, gcmd):
        if not self._ace_devices:
            self.log_always(self._t('msg.no_ace_devices_detected'))
            return
        self.log_always(self._t('msg.found_n_aces', count=len(self._ace_devices)))
        for i, device in enumerate(self._ace_devices):
            active = ' << ACTIVE' if i == self._active_device_index else ''
            self.log_always(self._t('msg.ace_list_simple', ace=self._disp(i), device=device, active=active))
    cmd_ACE_USB_STATS_help = '[multiACE] Show USB connection statistics'

    def cmd_ACE_USB_STATS(self, gcmd):
        s = self._usb_stats
        uptime = time.monotonic() - s['start_time']
        hours = uptime / 3600
        retry_rate = s['retries'] / s['scans'] * 100 if s['scans'] > 0 else 0
        self.log_always(self._t('msg.usb_stats_header', hours=hours))
        self.log_always(self._t('msg.usb_stats_scans', scans=s['scans'], retries=s['retries'], rate=retry_rate))
        self.log_always(self._t('msg.usb_stats_connects', connects=s['connects'], failures=s['connect_failures'], disconnects=s['disconnects']))
    cmd_ACE_DEBUG_help = '[multiACE] Toggle state audit + telemetry + wiggle logging. Usage: ACE_DEBUG [ENABLE=0|1]'

    def cmd_ACE_DEBUG(self, gcmd):
        enable = gcmd.get_int('ENABLE', -1)
        if enable == -1:
            state = 'enabled' if self._state_debug_enabled else 'disabled'
            self.log_always(self._t('msg.state_debug_status', state=state))
            return
        self._state_debug_enabled = bool(enable)
        self._apply_log_levels()
        state = 'enabled' if self._state_debug_enabled else 'disabled'
        self.log_always(self._t('msg.state_debug_set', state=state))
        self._state_log.info('STATE_DEBUG %s', state)
    cmd_ACE_USB_DEBUG_help = '[multiACE] Toggle USB logging. Usage: ACE_USB_DEBUG [ENABLE=0|1]'

    def cmd_ACE_USB_DEBUG(self, gcmd):
        enable = gcmd.get_int('ENABLE', -1)
        if enable == -1:
            state = 'enabled' if self._usb_debug_enabled else 'disabled'
            self.log_always(self._t('msg.usb_debug_status', state=state))
            return
        self._usb_debug_enabled = bool(enable)
        self._apply_log_levels()
        state = 'enabled' if self._usb_debug_enabled else 'disabled'
        self.log_always(self._t('msg.usb_debug_set', state=state))

    def _file_sha1_short(self, path):
        try:
            if not os.path.isfile(path):
                return 'missing'
            h = hashlib.sha1()
            with open(path, 'rb') as f:
                for chunk in iter(lambda: f.read(65536), b''):
                    h.update(chunk)
            return h.hexdigest()[:7]
        except Exception:
            return 'err'

    def _compute_bundle_sha1(self):
        extras_dir = os.path.dirname(os.path.abspath(__file__))
        kinematics_dir = os.path.join(os.path.dirname(extras_dir), 'kinematics')
        config_dir = '/home/lava/printer_data/config/extended'
        bundle_paths = [os.path.join(extras_dir, 'filament_feed.py'), os.path.join(extras_dir, 'filament_switch_sensor.py'), os.path.join(kinematics_dir, 'extruder.py'), os.path.join(config_dir, 'ace.cfg')]
        h = hashlib.sha1()
        for p in bundle_paths:
            try:
                with open(p, 'rb') as f:
                    for chunk in iter(lambda: f.read(65536), b''):
                        h.update(chunk)
            except Exception:
                h.update(b'<missing:' + p.encode() + b'>')
        return h.hexdigest()[:7]

    def _read_wheel_counts(self, module, channel):
        try:
            feed = self.printer.lookup_object('filament_feed %s' % module, None)
            if feed is None:
                return None
            return {'a': feed.wheel[channel].get_counts(), 'b': feed.wheel_2[channel].get_counts()}
        except Exception as e:
            logging.info('[multiACE] wheel count read failed: %s', str(e))
            return None

    def _wheel_delta(self, before, after):
        if before is None or after is None:
            return None
        return {'a': after['a'] - before['a'], 'b': after['b'] - before['b']}
    cmd_ACE_SEQ_help = '[multiACE] Run scripted load/unload sequence. PLAN: 0:1=load HEAD:ACE, A0=all from ACE, U=unload all, U0=unload head. UNLOAD=0|1 (default 1) runs final ACE_UNLOAD_ALL_HEADS.'

    def cmd_ACE_SEQ(self, gcmd):
        plan_str = gcmd.get('PLAN', '')
        do_unload = gcmd.get_int('UNLOAD', 1)
        was_debug = self._state_debug_enabled
        self._state_debug_enabled = True
        self._state_log.info('SEQ_START plan="%s" unload=%d', plan_str, do_unload)
        try:
            hs_dump = json.dumps({str(h): self._head_source[h] for h in range(4)})
        except Exception:
            hs_dump = str(self._head_source)
        self._state_log.info('SEQ_START head_source=%s active_device=%d', hs_dump, self._active_device_index)
        self._audit_state('SEQ_START', {'plan': plan_str, 'unload': do_unload})
        steps = []
        if plan_str:
            for item in plan_str.split(','):
                item = item.strip()
                if not item:
                    continue
                if item == 'U':
                    steps.append({'action': 'UNLOAD_ALL'})
                elif item.startswith('U') and item[1:].isdigit():
                    steps.append({'action': 'UNLOAD', 'head': int(item[1:])})
                elif item.startswith('A') and item[1:].isdigit():
                    ace = int(item[1:])
                    for h in range(4):
                        steps.append({'action': 'LOAD', 'head': h, 'ace': ace})
                elif ':' in item:
                    parts = item.split(':')
                    if len(parts) == 2:
                        steps.append({'action': 'LOAD', 'head': int(parts[0]), 'ace': int(parts[1])})
                    else:
                        raise gcmd.error('[multiACE] Invalid PLAN item: %s' % item)
                else:
                    raise gcmd.error('[multiACE] Invalid PLAN item: %s (use HEAD:ACE, A0, U, U0)' % item)
        else:
            self._refresh_ace_devices('seq')
            for i in range(min(len(self._ace_devices), 4)):
                steps.append({'action': 'LOAD', 'head': i, 'ace': i})
        self.log_always(self._t('msg.seq_start', steps=len(steps), unload='yes' if do_unload else 'no'))
        results = []
        step_nr = 0
        for step in steps:
            step_nr += 1
            action = step['action']
            if action == 'LOAD':
                head = step['head']
                ace = step['ace']
                self.log_always(self._t('msg.test_step_load', step=step_nr, total=len(steps), head=head, ace=self._disp(ace), slot=self._disp(head)))
                try:
                    self.gcode.run_script_from_command('ACE_LOAD_HEAD HEAD=%d ACE=%d SLOT=%d' % (head, ace, head))
                    sensor = self.printer.lookup_object('filament_motion_sensor e%d_filament' % head, None)
                    detected = sensor and sensor.get_status(0)['filament_detected']
                    src = self._head_source.get(head)
                    if detected and src is not None:
                        results.append({'step': step_nr, 'action': 'LOAD', 'status': 'PASS', 'head': head, 'ace': ace})
                        self.log_always(self._t('msg.test_step_load_pass', step=step_nr))
                    else:
                        reason = []
                        if not detected:
                            reason.append('sensor=no_filament')
                        if src is None:
                            reason.append('mapping=missing')
                        results.append({'step': step_nr, 'action': 'LOAD', 'status': 'FAIL', 'head': head, 'ace': ace, 'reason': ', '.join(reason)})
                        self.log_always(self._t('msg.test_step_fail_reasons', step=step_nr, reason=', '.join(reason)))
                except Exception as e:
                    results.append({'step': step_nr, 'action': 'LOAD', 'status': 'ERROR', 'head': head, 'ace': ace, 'reason': str(e)})
                    self.log_always(self._t('msg.test_step_error', step=step_nr, error=str(e)))
                self.gcode.run_script_from_command('ACE_HEAD_STATUS')
            elif action == 'UNLOAD':
                head = step['head']
                self.log_always(self._t('msg.test_step_unload', step=step_nr, total=len(steps), head=head))
                try:
                    self.gcode.run_script_from_command('ACE_UNLOAD_HEAD HEAD=%d' % head)
                    sensor = self.printer.lookup_object('filament_motion_sensor e%d_filament' % head, None)
                    still_loaded = sensor and sensor.get_status(0)['filament_detected']
                    if not still_loaded:
                        results.append({'step': step_nr, 'action': 'UNLOAD', 'status': 'PASS', 'head': head})
                        self.log_always(self._t('msg.test_step_unload_pass', step=step_nr))
                    else:
                        results.append({'step': step_nr, 'action': 'UNLOAD', 'status': 'FAIL', 'head': head, 'reason': 'filament still detected'})
                        self.log_always(self._t('msg.test_step_unload_fail', step=step_nr))
                except Exception as e:
                    results.append({'step': step_nr, 'action': 'UNLOAD', 'status': 'ERROR', 'head': head, 'reason': str(e)})
                    self.log_always(self._t('msg.test_step_error', step=step_nr, error=str(e)))
                self.gcode.run_script_from_command('ACE_HEAD_STATUS')
            elif action == 'UNLOAD_ALL':
                self.log_always(self._t('msg.test_step_unload_all', step=step_nr, total=len(steps)))
                try:
                    self.gcode.run_script_from_command('ACE_UNLOAD_ALL_HEADS')
                    all_clear = True
                    for h in range(4):
                        sensor = self.printer.lookup_object('filament_motion_sensor e%d_filament' % h, None)
                        if sensor and sensor.get_status(0)['filament_detected']:
                            all_clear = False
                    if all_clear:
                        results.append({'step': step_nr, 'action': 'UNLOAD_ALL', 'status': 'PASS'})
                        self.log_always(self._t('msg.test_step_unload_all_pass', step=step_nr))
                    else:
                        results.append({'step': step_nr, 'action': 'UNLOAD_ALL', 'status': 'FAIL', 'reason': 'filament still detected'})
                        self.log_always(self._t('msg.test_step_unload_fail', step=step_nr))
                except Exception as e:
                    results.append({'step': step_nr, 'action': 'UNLOAD_ALL', 'status': 'ERROR', 'reason': str(e)})
                    self.log_always(self._t('msg.test_step_error', step=step_nr, error=str(e)))
                self.gcode.run_script_from_command('ACE_HEAD_STATUS')
        if do_unload:
            self.log_always(self._t('msg.test_final_unload_all'))
            try:
                self.gcode.run_script_from_command('ACE_UNLOAD_ALL_HEADS')
                all_clear = True
                for h in range(4):
                    sensor = self.printer.lookup_object('filament_motion_sensor e%d_filament' % h, None)
                    if sensor and sensor.get_status(0)['filament_detected']:
                        all_clear = False
                if all_clear:
                    results.append({'step': 'final', 'action': 'UNLOAD_ALL', 'status': 'PASS'})
                    self.log_always(self._t('msg.test_final_pass'))
                else:
                    results.append({'step': 'final', 'action': 'UNLOAD_ALL', 'status': 'FAIL', 'reason': 'filament still detected'})
                    self.log_always(self._t('msg.test_final_fail'))
            except Exception as e:
                results.append({'step': 'final', 'action': 'UNLOAD_ALL', 'status': 'ERROR', 'reason': str(e)})
                self.log_always(self._t('msg.test_final_error', error=str(e)))
        passed = sum((1 for r in results if r['status'] == 'PASS'))
        failed = sum((1 for r in results if r['status'] == 'FAIL'))
        errors = sum((1 for r in results if r['status'] == 'ERROR'))
        total = len(results)
        self.log_always(self._t('msg.seq_complete', passed=passed, total=total, failed=failed, errors=errors))
        result_json = json.dumps(results, default=str)
        self._state_log.info('SEQ_RESULT %s', result_json)
        gcmd.respond_info(self._t('msg.seq_result', json=result_json))
        self._state_debug_enabled = was_debug
    cmd_ACE_PRELOAD_help = '[multiACE] Preload heads from a UI-built plan. Same syntax as ACE_SEQ but UNLOAD defaults to 0 (no final unload).'

    def cmd_ACE_PRELOAD(self, gcmd):
        plan_str = gcmd.get('PLAN', '')
        do_unload = gcmd.get_int('UNLOAD', 0)
        if not plan_str:
            raise gcmd.error('[multiACE] ACE_PRELOAD requires a PLAN parameter')
        self.gcode.run_script_from_command('ACE_SEQ PLAN=%s UNLOAD=%d' % (plan_str, do_unload))
    cmd_MACE_LOG_help = '[multiACE] Emit MSG to klippy.log (diagnostic tracepoint for macros).'

    def cmd_MACE_LOG(self, gcmd):
        msg = gcmd.get('MSG', '')
        logging.info('[mace_log] %s', msg)
    cmd_ACE_FA_TEST_help = '[multiACE] Stress-test FA stop+start across slots without a print. Usage: ACE_FA_TEST [ACE=0] [SCENARIO=cycle|pingpong|burst|matrix] [SLOTS=0,1,2,3] [DELAY=0.5] [REPEATS=2] [INTER=0] [RETRIES=0] [RETRY_DELAY=0.2]'

    def cmd_ACE_FA_TEST(self, gcmd):
        ace_idx = gcmd.get_int('ACE', 0, minval=0)
        scenario = gcmd.get('SCENARIO', 'cycle').lower()
        slots_str = gcmd.get('SLOTS', '0,1,2,3')
        delay = gcmd.get_float('DELAY', 0.5, minval=0.05)
        repeats = gcmd.get_int('REPEATS', 2, minval=1, maxval=200)
        inter = gcmd.get_float('INTER', 0.0, minval=0.0)
        retries = gcmd.get_int('RETRIES', 0, minval=0, maxval=100)
        retry_delay = gcmd.get_float('RETRY_DELAY', 0.2, minval=0.05)
        try:
            slots = [int(s.strip()) for s in slots_str.split(',') if s.strip()]
        except ValueError:
            raise gcmd.error('[ACE_FA_TEST] invalid SLOTS=%r' % slots_str)
        for s in slots:
            if not 0 <= s <= 3:
                raise gcmd.error('[ACE_FA_TEST] slot %d out of range 0..3' % s)
        if ace_idx >= len(self._ace_devices) or not self._connected_per_ace.get(ace_idx, False):
            raise gcmd.error('[ACE_FA_TEST] ACE %d not connected' % ace_idx)
        steps = []
        if scenario == 'cycle':
            seq = list(slots) * repeats
            prev = None
            for s in seq:
                if prev is not None:
                    steps.append(('stop', prev))
                steps.append(('start', s))
                prev = s
            if prev is not None:
                steps.append(('stop', prev))
        elif scenario == 'pingpong':
            if len(slots) < 2:
                raise gcmd.error('[ACE_FA_TEST] pingpong needs at least 2 slots')
            seq = []
            for r in range(repeats):
                for s in slots:
                    seq.append(s)
            prev = None
            for s in seq:
                if prev is not None:
                    steps.append(('stop', prev))
                steps.append(('start', s))
                prev = s
            if prev is not None:
                steps.append(('stop', prev))
        elif scenario == 'burst':
            for s in slots:
                for _ in range(repeats):
                    steps.append(('start', s))
                    steps.append(('stop', s))
        elif scenario == 'matrix':
            for r in range(repeats):
                for f in slots:
                    for t in slots:
                        if t == f:
                            continue
                        steps.append(('start', f))
                        steps.append(('stop', f))
                        steps.append(('start', t))
                        steps.append(('stop', t))
        else:
            raise gcmd.error('[ACE_FA_TEST] unknown SCENARIO=%s (use cycle|pingpong|burst|matrix)' % scenario)
        results = {}
        retry_counts = {}

        def is_forbidden(response):
            if not response:
                return False
            msg = response.get('msg', '') or ''
            return msg.lower() == 'forbidden'

        def is_success(response):
            if not response:
                return False
            code = response.get('code', 0)
            msg = response.get('msg', '') or ''
            return code == 0 and (msg.lower() == 'success' or msg == '')

        def make_callback(step_idx, action, slot, attempt):

            def cb(self=None, response=None, **kw):
                code = response.get('code', 0) if response else None
                msg = response.get('msg', '') if response else ''
                results.setdefault(step_idx, []).append((attempt, action, slot, code, msg))
                logging.info('[ACE_FA_TEST] RESP step=%d attempt=%d %s slot=%d code=%s msg=%s' % (step_idx, attempt, action, slot, code, msg))
                if action == 'start' and is_forbidden(response) and (attempt < retries):
                    next_attempt = attempt + 1
                    retry_counts[step_idx] = next_attempt

                    def retry_send(eventtime):
                        try:
                            self.send_request_to(ace_idx, {'method': 'start_feed_assist', 'params': {'index': slot}}, make_callback(step_idx, action, slot, next_attempt))
                            logging.info('[ACE_FA_TEST] RETRY step=%d attempt=%d %s slot=%d (after FORBIDDEN)' % (step_idx, next_attempt, action, slot))
                        except Exception as e:
                            logging.info('[ACE_FA_TEST] RETRY step=%d attempt=%d %s slot=%d failed: %s' % (step_idx, next_attempt, action, slot, e))
                        return self.reactor.NEVER
                    self.reactor.register_timer(retry_send, self.reactor.monotonic() + retry_delay)
            return cb
        gcmd.respond_info(self._t('msg.fa_test_running', ace=self._disp(ace_idx), scenario=scenario, slots=slots, delay=delay, repeats=repeats, steps=len(steps), inter=inter, retries=retries, retry_delay=retry_delay))
        start_t = self.reactor.monotonic()
        for i, (action, slot) in enumerate(steps):
            t = start_t + (i + 1) * delay + i * inter

            def make_step(step_idx, action, slot):
                method = 'start_feed_assist' if action == 'start' else 'stop_feed_assist'

                def fire(eventtime):
                    try:
                        self.send_request_to(ace_idx, {'method': method, 'params': {'index': slot}}, make_callback(step_idx, action, slot, 0))
                        logging.info('[ACE_FA_TEST] SENT step=%d attempt=0 %s slot=%d' % (step_idx, action, slot))
                    except Exception as e:
                        logging.info('[ACE_FA_TEST] SEND step=%d %s slot=%d failed: %s' % (step_idx, action, slot, e))
                    return self.reactor.NEVER
                return fire
            self.reactor.register_timer(make_step(i, action, slot), t)
        retry_budget = retries * retry_delay if retries else 0.0
        summary_t = start_t + (len(steps) + 1) * delay + len(steps) * inter + retry_budget + 1.0

        def summary(eventtime):
            sent = len(steps)
            recv_steps = len(results)
            no_ack_total = sent - recv_steps
            start_steps = [(i, a, s) for i, (a, s) in enumerate(steps) if a == 'start']
            attempts_hist = {}
            failed = []
            no_ack_starts = []
            for i, _, slot in start_steps:
                attempts = results.get(i, [])
                if not attempts:
                    no_ack_starts.append((i, slot))
                    continue
                final = attempts[-1]
                final_msg = (final[4] or '').lower()
                n_attempts = len(attempts)
                if final_msg == 'success':
                    attempts_hist[n_attempts] = attempts_hist.get(n_attempts, 0) + 1
                else:
                    failed.append((i, slot, n_attempts, final_msg or 'empty'))
            n_starts = len(start_steps)
            n_ok = sum(attempts_hist.values())
            max_att = max(attempts_hist.keys()) if attempts_hist else 0
            self.log_always(self._t('msg.fa_test_done', starts=n_starts, ok=n_ok, failed=len(failed), no_ack=len(no_ack_starts)))
            if attempts_hist:
                hist_str = '  '.join(('%dx=%d' % (k, attempts_hist[k]) for k in sorted(attempts_hist.keys())))
                self.log_always(self._t('msg.fa_test_attempts', hist=hist_str, max=max_att))
            if failed:
                kind = 'FORBIDDEN' if any((f[3] == 'forbidden' for f in failed)) else 'non-success'
                self.log_always(self._t('msg.fa_test_failed_header', kind=kind))
                for step_i, slot, n_att, msg in failed[:10]:
                    self.log_always(self._t('msg.fa_test_failed_line', step=step_i, slot=self._disp(slot), attempts=n_att, msg=msg))
                if len(failed) > 10:
                    self.log_always(self._t('msg.fa_test_more', count=len(failed) - 10))
            if no_ack_starts:
                self.log_always(self._t('msg.fa_test_no_ack_header'))
                for step_i, slot in no_ack_starts[:10]:
                    self.log_always(self._t('msg.fa_test_no_ack_line', step=step_i, slot=self._disp(slot)))
            return self.reactor.NEVER
        self.reactor.register_timer(summary, summary_t)

    def _audit_state(self, action, params=None):
        if not self._state_debug_enabled:
            return
        try:
            state = {'action': action, 'params': params or {}, 'active_device': self._active_device_index, 'device_count': len(self._ace_devices), 'connected': self._connected, 'serial': self.serial_id, 'mode': getattr(self, '_ace_mode', 'unknown'), 'swap_in_progress': self._swap_in_progress, 'auto_feed': self._auto_feed_enabled, 'fa_context': self._fa_context, 'feed_assist': self._feed_assist_index, 'gate_status': self.gate_status[:], 'head_source': {}}
            for h in range(4):
                src = self._head_source.get(h)
                state['head_source'][h] = {'ace': src['ace_index'], 'slot': src['slot'], 'type': src.get('type', ''), 'color': src.get('color', '')} if src else None
            sensors = {}
            for h in range(4):
                sensor = self.printer.lookup_object('filament_motion_sensor e%d_filament' % h, None)
                sensors[h] = sensor.get_status(0)['filament_detected'] if sensor else None
            state['sensors'] = sensors
            ptc = self.printer.lookup_object('print_task_config', None)
            if ptc:
                ptc_status = ptc.get_status()
                ptc_info = {}
                for h in range(4):
                    ptc_info[h] = {'type': ptc_status.get('filament_type', [''] * 4)[h], 'color': ptc_status.get('filament_color', [''] * 4)[h], 'vendor': ptc_status.get('filament_vendor', [''] * 4)[h]}
                state['print_task_config'] = ptc_info
            self._state_log.info('STATE %s', json.dumps(state, default=str))
            warnings = []
            if action == 'LOAD_HEAD':
                head = params.get('head')
                if head is not None:
                    src = self._head_source.get(head)
                    if src is None:
                        warnings.append('head_source[%d] is None after LOAD' % head)
                    if sensors.get(head) is False:
                        warnings.append('sensor[%d] not detecting filament after LOAD' % head)
            elif action == 'UNLOAD_HEAD':
                head = params.get('head')
                if head is not None:
                    src = self._head_source.get(head)
                    if src is not None:
                        warnings.append('head_source[%d] still set after UNLOAD' % head)
            elif action == 'SWITCH':
                target = params.get('target')
                if target is not None and self._active_device_index != target:
                    warnings.append('active_device=%d but target was %d' % (self._active_device_index, target))
                if not self._connected:
                    warnings.append('not connected after SWITCH')
            elif action == 'CLEAR_HEADS':
                head = params.get('head', -1)
                if head >= 0:
                    if self._head_source.get(head) is not None:
                        warnings.append('head_source[%d] not cleared' % head)
                else:
                    for h in range(4):
                        if self._head_source.get(h) is not None:
                            warnings.append('head_source[%d] not cleared' % h)
            elif action == 'UNLOAD_ALL':
                for h in range(4):
                    if sensors.get(h) is True:
                        warnings.append('sensor[%d] still detecting after UNLOAD_ALL' % h)
            if warnings:
                warn_msg = '[multiACE] STATE WARNINGS after %s: %s' % (action, '; '.join(warnings))
                self._state_log.warning(warn_msg)
                logging.warning(warn_msg)
        except Exception as e:
            self._state_log.error('STATE audit error: %s', str(e))

    def _telemetry(self, event, data):
        try:
            self._telemetry_log.info('%s %s', event, json.dumps(data, default=str))
        except Exception as e:
            logging.info('[multiACE] telemetry %s failed: %s' % (event, e))

    def get_status(self, eventtime=None):
        aces = []
        for i in range(len(self._ace_devices)):
            info = self._info_per_ace.get(i, {}) or {}
            slots_out = []
            for n, s in enumerate(info.get('slots', []) or []):
                if not isinstance(s, dict):
                    continue
                slots_out.append({'index': s.get('index', n), 'status': s.get('status', ''), 'sku': s.get('sku', ''), 'material': s.get('type', ''), 'rfid': s.get('rfid', 0), 'brand': s.get('brand', ''), 'color': s.get('color', [0, 0, 0])})
            protocol = self._protocols.get(i)
            aces.append({'idx': i, 'connected': self._connected_per_ace.get(i, False), 'protocol': getattr(protocol, 'NAME', '') if protocol else '', 'status': info.get('status', 'unknown'), 'temp': info.get('temp', 0), 'dryer_status': info.get('dryer_status', {}), 'gate_status': self._gate_status_per_ace.get(i, []), 'feed_assist': self._feed_assist_per_ace.get(i, -1), 'slots': slots_out})
        return {'status': self._info['status'], 'temp': self._info['temp'], 'dryer_status': self._info['dryer_status'], 'gate_status': self.gate_status, 'active_device': self._active_device_index, 'device_count': len(self._ace_devices), 'head_source': self._head_source, 'aces': aces}

def load_config(config):
    return MultiAce(config)
