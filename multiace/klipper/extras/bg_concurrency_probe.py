
import logging

class BgConcurrencyProbe:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.gcode = self.printer.lookup_object('gcode')
        self.gcode.register_command(
            'BG_CONCURRENCY_PROBE', self.cmd_PROBE,
            desc=self.cmd_PROBE_help)

    def _extruders(self):
        out = []
        for i in range(8):
            name = 'extruder' if i == 0 else 'extruder%d' % i
            obj = self.printer.lookup_object(name, None)
            if obj is not None:
                out.append((name, obj))
        return out

    def _stepper_mcu(self, ext):
        try:
            return ext.extruder_stepper.stepper.get_mcu().get_name()
        except Exception as e:
            return '?(%s)' % e

    def _has_own_trapq(self, ext):
        try:
            return ext.get_trapq() is not None
        except Exception:
            return False

    def _active_name(self):
        try:
            act = self.printer.lookup_object('toolhead').get_extruder()
            return getattr(act, 'name', None) or '?'
        except Exception as e:
            return '?(%s)' % e

    def _manual_steppers(self):
        names = []
        try:
            for name, _obj in self.printer.lookup_objects():
                if name == 'manual_stepper' or name.startswith('manual_stepper '):
                    names.append(name)
        except Exception:
            pass
        return names

    cmd_PROBE_help = (
        '[EXPERIMENTAL] Report v2 background-swap concurrency facts (read-only, '
        'no motion). BG_CONCURRENCY_PROBE - logs per-extruder MCU + trapq and '
        'the available non-toolchange move primitives, so we can pick the '
        'mechanism for ACE_BG_SWAP.')

    def cmd_PROBE(self, gcmd):
        exts = self._extruders()

        gcmd.respond_info('[bg-probe] active extruder = %s' % self._active_name())
        for name, obj in exts:
            gcmd.respond_info(
                '[bg-probe]   %-9s  mcu=%s  own_trapq=%s'
                % (name, self._stepper_mcu(obj), self._has_own_trapq(obj)))

        mcus = {self._stepper_mcu(o) for _, o in exts}
        all_trapq = all(self._has_own_trapq(o) for _, o in exts) and exts
        gcmd.respond_info(
            '[bg-probe] distinct extruder MCUs = %d ; every extruder has its '
            'own trapq = %s' % (len(mcus), bool(all_trapq)))

        fm = self.printer.lookup_object('force_move', None)
        fm_enabled = False
        if fm is not None:
            fm_enabled = bool(getattr(fm, 'enable_force_move', False))
        ms = self._manual_steppers()
        gcmd.respond_info(
            '[bg-probe] primitives: force_move=%s (enabled=%s) ; '
            'manual_stepper objects=%s'
            % (fm is not None, fm_enabled, ms or 'none'))

        if all_trapq and len(mcus) > 1:
            gcmd.respond_info(
                '[bg-probe] VERDICT: per-head MCU + per-extruder trapq present '
                '-> independent stepper queues are possible. Missing piece = a '
                'NON-toolchange way to append moves to the parked extruder trapq '
                '(ACTIVATE_EXTRUDER does a physical toolchange on the U1).')
            opts = []
            if fm_enabled:
                opts.append('force_move (enabled) - direct stepper move')
            else:
                opts.append('force_move present but DISABLED (enable_force_move '
                            'in [force_move]) - then FORCE_MOVE/STEPPER moves')
            if ms:
                opts.append('manual_stepper - but extruder pins are owned by '
                            '[extruder], so a manual_stepper would need free pins')
            opts.append('low-level trapq_append on the parked extruder + drive '
                        'its step generator (sidecar-style; most work, no extra '
                        'config) - the likely real path')
            for o in opts:
                gcmd.respond_info('[bg-probe]   candidate: %s' % o)
        else:
            gcmd.respond_info(
                '[bg-probe] VERDICT: independent queues NOT clearly available '
                '(check the MCU/trapq lines above).')

        logging.info('[bg-probe] mcus=%s all_trapq=%s force_move=%s/%s ms=%s'
                     % (mcus, bool(all_trapq), fm is not None, fm_enabled, ms))

def load_config(config):
    return BgConcurrencyProbe(config)
