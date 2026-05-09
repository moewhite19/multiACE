#!/usr/bin/env python3
import argparse
import errno
import logging
import os
import select
import serial
import signal
import socket
import sys
import threading
import time
DEFAULT_SOCKET = '/tmp/multiace_v2.sock'
DEFAULT_BAUD = 230400

def setup_logging(level):
    logging.basicConfig(level=level, format='%(asctime)s %(levelname)s %(message)s', datefmt='%Y-%m-%d %H:%M:%S')

def open_serial(port, baud):
    return serial.Serial(port=port, baudrate=baud, timeout=0.1)

def serve_client(client, ser, stop_global):
    client_stop = threading.Event()

    def socket_to_serial():
        try:
            while not client_stop.is_set() and (not stop_global.is_set()):
                try:
                    data = client.recv(4096)
                except (ConnectionResetError, OSError):
                    break
                if not data:
                    break
                try:
                    ser.write(data)
                except Exception as e:
                    logging.warning('serial write failed: %s', e)
                    break
        finally:
            client_stop.set()

    def serial_to_socket():
        try:
            while not client_stop.is_set() and (not stop_global.is_set()):
                try:
                    chunk = ser.read(256)
                except Exception as e:
                    logging.warning('serial read failed: %s', e)
                    break
                if not chunk:
                    continue
                try:
                    client.sendall(chunk)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    break
        finally:
            client_stop.set()
    t1 = threading.Thread(target=socket_to_serial, daemon=True, name='s2t')
    t2 = threading.Thread(target=serial_to_socket, daemon=True, name='t2s')
    t1.start()
    t2.start()
    t1.join()
    t2.join()

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', required=True, help='Serial port (e.g. /dev/serial/by-path/...)')
    parser.add_argument('--socket', default=DEFAULT_SOCKET, help='Unix socket path (default: %(default)s)')
    parser.add_argument('--baud', type=int, default=DEFAULT_BAUD, help='Baud rate (default: %(default)d)')
    parser.add_argument('--socket-mode', type=lambda s: int(s, 8), default=438, help='Socket file mode in octal (default: 0666)')
    parser.add_argument('--reopen-delay', type=float, default=2.0, help='Delay before reopening serial after error (s)')
    parser.add_argument('--verbose', '-v', action='store_true')
    args = parser.parse_args()
    setup_logging(logging.DEBUG if args.verbose else logging.INFO)
    logging.info('multiace_v2d starting: port=%s socket=%s baud=%d', args.port, args.socket, args.baud)
    stop_global = threading.Event()

    def handle_signal(signum, frame):
        logging.info('received signal %d, shutting down', signum)
        stop_global.set()
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)
    try:
        os.unlink(args.socket)
    except FileNotFoundError:
        pass
    listen = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listen.bind(args.socket)
    os.chmod(args.socket, args.socket_mode)
    listen.listen(1)
    listen.settimeout(1.0)
    logging.info('listening on %s (mode 0%o)', args.socket, args.socket_mode)
    ser = None
    while not stop_global.is_set():
        if ser is None:
            try:
                ser = open_serial(args.port, args.baud)
                logging.info('opened serial %s @ %d', args.port, args.baud)
            except Exception as e:
                logging.warning('serial open failed: %s — retry in %.1fs', e, args.reopen_delay)
                time.sleep(args.reopen_delay)
                continue
        try:
            client, _ = listen.accept()
        except socket.timeout:
            continue
        except OSError as e:
            if e.errno in (errno.EINTR,):
                continue
            logging.warning('accept failed: %s', e)
            time.sleep(0.5)
            continue
        client.setblocking(True)
        logging.info('client connected')
        try:
            serve_client(client, ser, stop_global)
        except Exception as e:
            logging.warning('serve_client raised: %s', e)
        finally:
            try:
                client.close()
            except Exception:
                pass
            logging.info('client disconnected')
    if ser is not None:
        try:
            ser.close()
        except Exception:
            pass
    try:
        listen.close()
    except Exception:
        pass
    try:
        os.unlink(args.socket)
    except Exception:
        pass
    logging.info('multiace_v2d stopped')
if __name__ == '__main__':
    main()
