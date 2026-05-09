#!/bin/bash
set -e
INSTALL_WEB=0
KEEP_CONFIG=0
for arg in "$@"; do
    case "$arg" in
        --install-web) INSTALL_WEB=1 ;;
        --keep-config) KEEP_CONFIG=1 ;;
        --help|-h)
            echo "Usage: $0 [--install-web] [--keep-config]"
            echo "  --install-web   Also install multiACE Web (FastAPI + Vue UI)"
            echo "  --keep-config   Don't overwrite existing ace.cfg (keep edits in place)"
            exit 0
            ;;
    esac
done
INSTALL_DIR="$(cd "$(dirname "$0")" && pwd)"
find "$INSTALL_DIR" -name "*.sh" -exec sed -i 's/\r$//' {} +
HOME_DIR="/home/lava"
EXTRAS_DIR="${HOME_DIR}/klipper/klippy/extras"
KINEMATICS_DIR="${HOME_DIR}/klipper/klippy/kinematics"
CONFIG_DIR="${HOME_DIR}/printer_data/config/extended"
MULTIACE_DIR="${CONFIG_DIR}/multiace"
PRINTER_CFG="${HOME_DIR}/printer_data/config/printer.cfg"
LOGFILE="/tmp/multiace_install.log"
log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') [multiACE] $1" | tee -a "$LOGFILE"
}
log "=== multiACE Installation ==="
log "Install from: $INSTALL_DIR"
log "Klipper extras: $EXTRAS_DIR"
log "Klipper kinematics: $KINEMATICS_DIR"
log "Config dir: $CONFIG_DIR"
for f in \
    "klipper/extras/ace.py" \
    "klipper/extras/filament_feed_ace.py" \
    "klipper/extras/filament_switch_sensor_ace.py" \
    "klipper/kinematics/extruder_ace.py" \
    "config/extended/ace.cfg" \
    "config/extended/multiace/ace_mode_switch.sh" \
    "config/extended/multiace/ace_vars.cfg"
do
    if [ ! -f "$INSTALL_DIR/$f" ]; then
        log "ERROR: Missing file: $f"
        exit 1
    fi
done
log "All source files found"
for d in "$EXTRAS_DIR" "$KINEMATICS_DIR" "$CONFIG_DIR"; do
    if [ ! -d "$d" ]; then
        log "ERROR: Target directory not found: $d"
        exit 1
    fi
done
log "Target directories verified"
log "Backing up current files..."
for f in "filament_feed.py" "filament_switch_sensor.py"; do
    if [ -f "$EXTRAS_DIR/$f" ] && [ ! -f "$EXTRAS_DIR/${f%.py}_pre_multiace.py" ]; then
        cp "$EXTRAS_DIR/$f" "$EXTRAS_DIR/${f%.py}_pre_multiace.py"
        chmod 644 "$EXTRAS_DIR/${f%.py}_pre_multiace.py"
        log "  Backed up $f -> ${f%.py}_pre_multiace.py"
    fi
done
if [ -f "$KINEMATICS_DIR/extruder.py" ] && [ ! -f "$KINEMATICS_DIR/extruder_pre_multiace.py" ]; then
    cp "$KINEMATICS_DIR/extruder.py" "$KINEMATICS_DIR/extruder_pre_multiace.py"
    chmod 644 "$KINEMATICS_DIR/extruder_pre_multiace.py"
    log "  Backed up extruder.py -> extruder_pre_multiace.py"
fi
if [ -f "$CONFIG_DIR/ace.cfg" ] && [ ! -f "$CONFIG_DIR/ace_pre_multiace.cfg" ]; then
    cp "$CONFIG_DIR/ace.cfg" "$CONFIG_DIR/ace_pre_multiace.cfg"
    log "  Backed up ace.cfg -> ace_pre_multiace.cfg"
fi
log "Installing multiACE files..."
cp "$INSTALL_DIR/klipper/extras/ace.py" "$EXTRAS_DIR/ace.py"
cp "$INSTALL_DIR/klipper/extras/ace_protocol.py" "$EXTRAS_DIR/ace_protocol.py"
cp "$INSTALL_DIR/klipper/extras/ace_protocol_v1.py" "$EXTRAS_DIR/ace_protocol_v1.py"
cp "$INSTALL_DIR/klipper/extras/ace_protocol_v2.py" "$EXTRAS_DIR/ace_protocol_v2.py"
cp "$INSTALL_DIR/klipper/extras/filament_feed_ace.py" "$EXTRAS_DIR/filament_feed_ace.py"
cp "$INSTALL_DIR/klipper/extras/filament_switch_sensor_ace.py" "$EXTRAS_DIR/filament_switch_sensor_ace.py"
chmod 644 "$EXTRAS_DIR/ace.py" "$EXTRAS_DIR/ace_protocol.py" "$EXTRAS_DIR/ace_protocol_v1.py" "$EXTRAS_DIR/ace_protocol_v2.py" "$EXTRAS_DIR/filament_feed_ace.py" "$EXTRAS_DIR/filament_switch_sensor_ace.py"
log "  Klipper extras installed"
cp "$INSTALL_DIR/klipper/kinematics/extruder_ace.py" "$KINEMATICS_DIR/extruder_ace.py"
chmod 644 "$KINEMATICS_DIR/extruder_ace.py"
log "  Klipper kinematics installed"
if [ "$KEEP_CONFIG" -eq 1 ] && [ -f "$CONFIG_DIR/ace.cfg" ]; then
    log "  ace.cfg kept (--keep-config)"
else
    cp "$INSTALL_DIR/config/extended/ace.cfg" "$CONFIG_DIR/ace.cfg"
    chmod 644 "$CONFIG_DIR/ace.cfg"
    log "  ace.cfg installed"
fi
mkdir -p "$MULTIACE_DIR"
cp "$INSTALL_DIR/config/extended/multiace/ace_mode_switch.sh" "$MULTIACE_DIR/ace_mode_switch.sh"
chmod +x "$MULTIACE_DIR/ace_mode_switch.sh"
if [ ! -f "$MULTIACE_DIR/ace_vars.cfg" ]; then
    cp "$INSTALL_DIR/config/extended/multiace/ace_vars.cfg" "$MULTIACE_DIR/ace_vars.cfg"
    log "  ace_vars.cfg created (fresh)"
else
    log "  ace_vars.cfg exists, keeping current settings"
fi
log "  multiace config installed"
if [ -d "$INSTALL_DIR/i18n" ]; then
    mkdir -p "$MULTIACE_DIR/i18n"
    cp -a "$INSTALL_DIR/i18n/." "$MULTIACE_DIR/i18n/"
    chown -R lava:lava "$MULTIACE_DIR/i18n"
    log "  i18n catalogs installed to $MULTIACE_DIR/i18n"
fi
if [ -f "$INSTALL_DIR/uninstall_multiace.sh" ]; then
    cp "$INSTALL_DIR/uninstall_multiace.sh" "$MULTIACE_DIR/uninstall_multiace.sh"
    chmod +x "$MULTIACE_DIR/uninstall_multiace.sh"
    log "  Uninstall script installed"
fi
if [ -d "$INSTALL_DIR/tools" ]; then
    mkdir -p "${HOME_DIR}/printer_data/config/tools"
    cp "$INSTALL_DIR/tools/"*.py "${HOME_DIR}/printer_data/config/tools/" 2>/dev/null || true
    log "  Tools installed"
fi
if [ -f "$INSTALL_DIR/tools/multiace_v2d.py" ]; then
    cp "$INSTALL_DIR/tools/multiace_v2d.py" /usr/local/bin/multiace_v2d.py
    chmod 755 /usr/local/bin/multiace_v2d.py
    log "  V2 daemon installed at /usr/local/bin/multiace_v2d.py"
fi
for old_init in /etc/init.d/S55multiace_v2d /etc/init.d/multiace_v2d; do
    if [ -e "$old_init" ]; then
        "$old_init" stop 2>/dev/null || true
        rm -f "$old_init"
        log "  Removed obsolete init script: $old_init"
    fi
done
rm -f /var/run/multiace_v2d.pid /tmp/multiace_v2.sock 2>/dev/null || true
find "$EXTRAS_DIR/__pycache__" -name "ace*" -delete 2>/dev/null || true
find "$EXTRAS_DIR/__pycache__" -name "filament_feed*" -delete 2>/dev/null || true
find "$EXTRAS_DIR/__pycache__" -name "filament_switch_sensor*" -delete 2>/dev/null || true
find "$KINEMATICS_DIR/__pycache__" -name "extruder*" -delete 2>/dev/null || true
log "Python cache cleared"
if [ -f "$PRINTER_CFG" ]; then
    if ! grep -q "extended/ace.cfg" "$PRINTER_CFG"; then
        if grep -q '^\[' "$PRINTER_CFG"; then
            sed -i '0,/^\[/{s/^\[/[include extended\/ace.cfg]\n\n[/}' "$PRINTER_CFG"
        else
            sed -i '1i [include extended/ace.cfg]\n' "$PRINTER_CFG"
        fi
        if grep -q "extended/ace.cfg" "$PRINTER_CFG"; then
            log "Added [include extended/ace.cfg] to printer.cfg"
        else
            echo -e '\n[include extended/ace.cfg]' >> "$PRINTER_CFG"
            log "Added [include extended/ace.cfg] to end of printer.cfg"
        fi
    else
        log "printer.cfg already includes ace.cfg"
    fi
else
    log "WARNING: printer.cfg not found at $PRINTER_CFG"
fi
sed -i 's/\r$//' "$MULTIACE_DIR/ace_mode_switch.sh"
chmod +x "$MULTIACE_DIR/ace_mode_switch.sh"
log "Mode switch script prepared"
log "Activating ACE file swap..."
bash "$MULTIACE_DIR/ace_mode_switch.sh" ace
log "ACE files activated"
rm -rf "$EXTRAS_DIR/__pycache__"
rm -rf "$KINEMATICS_DIR/__pycache__"
log "Python cache deleted"
log ""
log "Verifying install integrity..."
VERIFY_FAILED=0
verify_match() {
    local src="$1"
    local dst="$2"
    local label="$3"
    if [ ! -f "$dst" ]; then
        log "  FAIL: $label: not found at $dst"
        VERIFY_FAILED=1
        return
    fi
    if ! cmp -s "$src" "$dst"; then
        local src_size dst_size
        src_size=$(wc -c < "$src" 2>/dev/null || echo "?")
        dst_size=$(wc -c < "$dst" 2>/dev/null || echo "?")
        log "  FAIL: $label: content mismatch (src=$src_size, dst=$dst_size bytes)"
        VERIFY_FAILED=1
    else
        log "  OK:   $label"
    fi
}
verify_match "$INSTALL_DIR/klipper/extras/ace.py" \
             "$EXTRAS_DIR/ace.py" "ace.py"
verify_match "$INSTALL_DIR/klipper/extras/ace_protocol.py" \
             "$EXTRAS_DIR/ace_protocol.py" "ace_protocol.py"
verify_match "$INSTALL_DIR/klipper/extras/ace_protocol_v1.py" \
             "$EXTRAS_DIR/ace_protocol_v1.py" "ace_protocol_v1.py"
verify_match "$INSTALL_DIR/klipper/extras/ace_protocol_v2.py" \
             "$EXTRAS_DIR/ace_protocol_v2.py" "ace_protocol_v2.py"
verify_match "$INSTALL_DIR/klipper/extras/filament_feed_ace.py" \
             "$EXTRAS_DIR/filament_feed_ace.py" "filament_feed_ace.py"
verify_match "$INSTALL_DIR/klipper/extras/filament_switch_sensor_ace.py" \
             "$EXTRAS_DIR/filament_switch_sensor_ace.py" "filament_switch_sensor_ace.py"
verify_match "$INSTALL_DIR/klipper/kinematics/extruder_ace.py" \
             "$KINEMATICS_DIR/extruder_ace.py" "extruder_ace.py"
if [ "$KEEP_CONFIG" -eq 1 ] && [ -f "$CONFIG_DIR/ace.cfg" ]; then
    log "  SKIP: ace.cfg (--keep-config, user edits preserved)"
else
    verify_match "$INSTALL_DIR/config/extended/ace.cfg" \
                 "$CONFIG_DIR/ace.cfg" "ace.cfg"
fi
verify_match "$EXTRAS_DIR/filament_feed_ace.py" \
             "$EXTRAS_DIR/filament_feed.py" "filament_feed.py (mode swap)"
verify_match "$EXTRAS_DIR/filament_switch_sensor_ace.py" \
             "$EXTRAS_DIR/filament_switch_sensor.py" "filament_switch_sensor.py (mode swap)"
verify_match "$KINEMATICS_DIR/extruder_ace.py" \
             "$KINEMATICS_DIR/extruder.py" "extruder.py (mode swap)"
if [ -f "$PRINTER_CFG" ]; then
    if grep -q "extended/ace.cfg" "$PRINTER_CFG"; then
        log "  OK:   printer.cfg include"
    else
        log "  FAIL: printer.cfg missing [include extended/ace.cfg]"
        VERIFY_FAILED=1
    fi
fi
if [ "$VERIFY_FAILED" = "1" ]; then
    log ""
    log "========================================================"
    log "  INSTALL VERIFICATION FAILED"
    log ""
    log "  One or more files did not persist after copy."
    log "  This almost always means ADVANCED MODE is NOT enabled"
    log "  on the Snapmaker U1 display."
    log ""
    log "  To enable:"
    log "    Settings > About > tap firmware version 10 times"
    log "    > Advanced Mode > Root Access"
    log ""
    log "  Then re-run: bash install_multiace.sh"
    log "========================================================"
    log ""
    exit 1
fi
log "All files verified OK."
if [ "$INSTALL_WEB" = "1" ]; then
    log ""
    log "=== Installing multiACE Web ==="
    WEB_SRC="$INSTALL_DIR/web"
    WEB_DEST="${HOME_DIR}/multiace_web"
    NGINX_DROPIN="/etc/nginx/fluidd.d/multiace-web.conf"
    INITD_SCRIPT="/etc/init.d/S98multiace-web"
    if [ ! -d "$WEB_SRC" ]; then
        log "ERROR: $WEB_SRC not found — multiace/web/ missing in install bundle"
        exit 1
    fi
    mkdir -p "$WEB_DEST/backend" "$WEB_DEST/frontend" "$WEB_DEST/i18n"
    cp -a "$WEB_SRC/backend/."  "$WEB_DEST/backend/"
    cp -a "$WEB_SRC/frontend/." "$WEB_DEST/frontend/"
    if [ -d "$INSTALL_DIR/i18n" ]; then
        cp -a "$INSTALL_DIR/i18n/." "$WEB_DEST/i18n/"
    fi
    rm -rf "$WEB_DEST/backend/__pycache__"
    chown -R lava:lava "$WEB_DEST"
    log "  Copied web/ to $WEB_DEST (incl. i18n catalogs)"
    if su - lava -c "command -v pip3 >/dev/null"; then
        log "  Installing Python deps (fastapi, uvicorn, httpx) for user lava ..."
        su - lava -c "pip3 install --user --upgrade -r '$WEB_DEST/backend/requirements.txt'" \
            >>"$LOGFILE" 2>&1 || log "  WARN: pip install reported errors — see $LOGFILE"
    else
        log "  WARN: pip3 nicht gefunden — install backend dependencies manually"
    fi
    mkdir -p "${HOME_DIR}/printer_data/logs"
    touch    "${HOME_DIR}/printer_data/logs/multiace_web.log"
    chown lava:lava "${HOME_DIR}/printer_data/logs/multiace_web.log"
    if [ -d /etc/nginx/fluidd.d ]; then
        cp "$WEB_SRC/deploy/multiace-web.nginx.conf" "$NGINX_DROPIN"
        log "  Installed nginx drop-in: $NGINX_DROPIN"
        if nginx -t >>"$LOGFILE" 2>&1; then
            nginx -s reload >>"$LOGFILE" 2>&1 && log "  nginx reloaded"
        else
            log "  WARN: nginx -t failed — drop-in installiert aber nicht aktiv"
        fi
    else
        log "  WARN: /etc/nginx/fluidd.d nicht vorhanden — nginx-Config nicht installiert"
    fi
    cp "$WEB_SRC/deploy/S98multiace-web" "$INITD_SCRIPT"
    sed -i 's/\r$//' "$INITD_SCRIPT"
    chmod +x "$INITD_SCRIPT"
    log "  Installed init script: $INITD_SCRIPT"
    "$INITD_SCRIPT" stop  >>"$LOGFILE" 2>&1 || true
    "$INITD_SCRIPT" start >>"$LOGFILE" 2>&1 || log "  WARN: start fehlgeschlagen — see $LOGFILE"
    sleep 1
    if "$INITD_SCRIPT" status | grep -q "running"; then
        log "  multiACE Web running"
        log "  -> http://<printer-ip>/multiace/"
    else
        log "  WARN: multiACE Web not running — check $LOGFILE and $WEB_DEST/backend/"
    fi
fi
log ""
log "=== Installation complete ==="
log "Please reboot the printer to activate multiACE."
log ""
