#!/usr/bin/env python3
"""
POS - Prologix OpenGPIB Scanner

A desktop front end for Prologix GPIB-USB and GPIB-ETHERNET controllers (and
compatible clones such as AR488). Finds controllers on the serial bus and the
local network, opens a tab per controller, walks the GPIB bus behind it, and
keeps a SQLite record of every instrument it has ever seen.

    python3 pos.py [--db path/to/gpib_devices.db] [--debug]

Standard library plus pyserial: tkinter + sqlite3 + sockets + serial.
"""

from __future__ import annotations

import argparse
import csv
import datetime
import json
import os
import queue
import socket
import sqlite3
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable, Optional

import serial
import serial.tools.list_ports

APP_NAME = "POS"
APP_TITLE = "Prologix OpenGPIB Scanner"
__version__ = VERSION = "1.3.0"

PAD = 6
MUTED = "#555555"
OK_FG = "#0b6b2f"
BAD_FG = "#a11111"

# ==========================================================================
# Paths and constants
# ==========================================================================

# Anchor data files to the program's own directory (script or frozen exe),
# so the DB/config don't scatter based on the launch directory.
if getattr(sys, "frozen", False):
    _APP_DIR = os.path.dirname(sys.executable)
else:
    _APP_DIR = os.path.dirname(os.path.abspath(__file__))

DEFAULT_DB_PATH = os.path.join(_APP_DIR, "gpib_devices.db")
CONFIG_FILE = os.path.join(_APP_DIR, "scanner_config.json")

# Rebound by --db and by File ▸ Open database. Every database helper reads the
# global at call time, so switching files needs no other plumbing.
DB_NAME = DEFAULT_DB_PATH

DB_TIMEOUT = 5.0  # Seconds to wait if database is locked by another thread
PROLOGIX_TCP_PORT = 1234
NETFINDER_UDP_PORT = 3040

DEBUG_MODE = "--debug" in sys.argv


def dprint(*args, **kwargs) -> None:
    """Print only when --debug was passed."""
    if DEBUG_MODE:
        print(*args, **kwargs)


def decode_status_byte(sb) -> str:
    """Format a serial-poll status byte with decoded IEEE-488 flag bits.
    SRQ = device requesting service, ESB = event/error summary (488.2),
    MAV = message available (unread data in the output queue)."""
    if sb is None or sb == "":
        return ""
    try:
        sb = int(sb)
    except (TypeError, ValueError):
        return ""
    flags = []
    if sb & 0x40:
        flags.append("SRQ")
    if sb & 0x20:
        flags.append("ESB")
    if sb & 0x10:
        flags.append("MAV")
    return f"0x{sb:02X}" + (f" ({','.join(flags)})" if flags else "")


def now_stamp() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ==========================================================================
# Configuration file
# ==========================================================================

def load_config() -> dict:
    """Load application settings from a JSON file."""
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except Exception as exc:
            dprint(f"Error loading config: {exc}")
    return {"tc_enabled": False, "tc_path": ""}


def save_config(config: dict) -> None:
    """Save application settings to a JSON file."""
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as fh:
            json.dump(config, fh, indent=4)
    except Exception as exc:
        dprint(f"Error saving config: {exc}")


# ==========================================================================
# Database
# ==========================================================================

def init_db() -> None:
    """Initialise the SQLite schema and apply migrations."""
    with sqlite3.connect(DB_NAME, timeout=DB_TIMEOUT) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS devices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                adapter_type TEXT,
                connection_port TEXT,
                adapter_serial TEXT,
                gpib_address INTEGER,
                idn_response TEXT,
                status TEXT,
                last_seen TIMESTAMP
            )
        """)
        # Migration: tc_config_file
        try:
            cursor.execute("ALTER TABLE devices ADD COLUMN tc_config_file TEXT")
        except sqlite3.OperationalError:
            pass
        # Migration: status_byte (serial poll result)
        try:
            cursor.execute("ALTER TABLE devices ADD COLUMN status_byte INTEGER")
        except sqlite3.OperationalError:
            pass
        conn.commit()


def upsert_devices_batch(devices_list) -> None:
    """Insert or update multiple device records in a single transaction."""
    if not devices_list:
        return
    now = now_stamp()
    with sqlite3.connect(DB_NAME, timeout=DB_TIMEOUT) as conn:
        cursor = conn.cursor()
        for dev in devices_list:
            adapter_type, port, serial_num, gpib_addr, idn_response, status, status_byte = dev
            cursor.execute(
                "SELECT id FROM devices WHERE adapter_serial = ? AND gpib_address = ?",
                (serial_num, gpib_addr))
            row = cursor.fetchone()
            if row:
                cursor.execute("""
                    UPDATE devices
                    SET idn_response = ?, status = ?, last_seen = ?, adapter_type = ?,
                        connection_port = ?, status_byte = ?
                    WHERE id = ?
                """, (idn_response, status, now, adapter_type, port, status_byte, row[0]))
            else:
                cursor.execute("""
                    INSERT INTO devices (adapter_type, connection_port, adapter_serial,
                                         gpib_address, idn_response, status, last_seen, status_byte)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (adapter_type, port, serial_num, gpib_addr, idn_response, status,
                      now, status_byte))
        conn.commit()


def mark_missing_devices(serial_num: str, found_addresses,
                         first: int = 0, last: int = 30) -> None:
    """Mark previously discovered devices 'NotFound' if they missed this scan.

    Bounded by the address range that was actually walked: a partial scan of
    0-10 must not declare an instrument at 22 missing when nobody asked it.
    """
    with sqlite3.connect(DB_NAME, timeout=DB_TIMEOUT) as conn:
        cursor = conn.cursor()
        sql = ("UPDATE devices SET status = 'NotFound' "
               "WHERE adapter_serial = ? AND gpib_address BETWEEN ? AND ?")
        params = [serial_num, first, last]
        if found_addresses:
            placeholders = ",".join("?" for _ in found_addresses)
            sql += f" AND gpib_address NOT IN ({placeholders})"
            params += list(found_addresses)
        cursor.execute(sql, params)
        conn.commit()


def fetch_all_devices(adapter_serial: Optional[str] = None):
    """Fetch devices, optionally filtered by adapter serial."""
    columns = ("adapter_type, connection_port, adapter_serial, gpib_address, "
               "idn_response, status, last_seen, tc_config_file, status_byte")
    with sqlite3.connect(DB_NAME, timeout=DB_TIMEOUT) as conn:
        cursor = conn.cursor()
        if adapter_serial:
            cursor.execute(f"SELECT {columns} FROM devices WHERE adapter_serial = ? "
                           f"ORDER BY gpib_address", (adapter_serial,))
        else:
            cursor.execute(f"SELECT {columns} FROM devices "
                           f"ORDER BY adapter_serial, gpib_address")
        return cursor.fetchall()


def fetch_devices_for_matching():
    with sqlite3.connect(DB_NAME, timeout=DB_TIMEOUT) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, adapter_type, gpib_address, idn_response FROM devices "
                       "ORDER BY adapter_serial, gpib_address")
        return cursor.fetchall()


def delete_device_records(records) -> None:
    """Delete specific device records in a batch."""
    if not records:
        return
    with sqlite3.connect(DB_NAME, timeout=DB_TIMEOUT) as conn:
        cursor = conn.cursor()
        cursor.executemany(
            "DELETE FROM devices WHERE gpib_address = ? AND adapter_serial = ?", records)
        conn.commit()


def batch_update_tc_configs(updates) -> None:
    """Batch-update TestController matched configs."""
    if not updates:
        return
    with sqlite3.connect(DB_NAME, timeout=DB_TIMEOUT) as conn:
        cursor = conn.cursor()
        cursor.executemany("UPDATE devices SET tc_config_file = ? WHERE id = ?", updates)
        conn.commit()


def known_adapters():
    """Every adapter the database has ever recorded, newest sighting first."""
    with sqlite3.connect(DB_NAME, timeout=DB_TIMEOUT) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT adapter_type, connection_port, adapter_serial,
                   COUNT(*), MAX(last_seen)
            FROM devices
            GROUP BY adapter_serial
            ORDER BY MAX(last_seen) DESC
        """)
        return cursor.fetchall()


# ==========================================================================
# Transport layer
# ==========================================================================

class PrologixAdapter:
    def __init__(self, port_or_ip: str, adapter_type: str, serial_num: str):
        self.port_or_ip = port_or_ip
        self.adapter_type = adapter_type
        self.serial_num = serial_num

    def write(self, command: str) -> None: pass
    def read(self) -> str: return ""
    def flush_input(self) -> None: pass
    def close(self) -> None: pass

    @property
    def key(self) -> str:
        return f"{self.adapter_type}:{self.port_or_ip}"


class USBAdapter(PrologixAdapter):
    def __init__(self, port: str, serial_num: str):
        super().__init__(port, "USB", serial_num)
        self.ser = serial.Serial(port, baudrate=9600, timeout=0.5)

    def write(self, command: str) -> None:
        try:
            self.ser.write((command + "\n").encode("ascii"))
        except serial.SerialException as exc:
            dprint(f"USB Write Error: {exc}")

    def read(self) -> str:
        try:
            return self.ser.readline().decode("ascii", errors="ignore").strip()
        except serial.SerialException:
            return ""

    def flush_input(self) -> None:
        """Discard stale/late data sitting in the receive buffer."""
        try:
            self.ser.reset_input_buffer()
        except serial.SerialException as exc:
            dprint(f"USB flush error: {exc}")

    def close(self) -> None:
        try:
            if self.ser and self.ser.is_open:
                self.ser.close()
                dprint(f"Closed USB port {self.port_or_ip}")
        except Exception as exc:
            dprint(f"Error closing USB port {self.port_or_ip}: {exc}")


class EthernetAdapter(PrologixAdapter):
    def __init__(self, ip: str, mac_address: str):
        super().__init__(ip, "Ethernet", mac_address)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(0.5)
        self.sock.connect((ip, PROLOGIX_TCP_PORT))

    def write(self, command: str) -> None:
        try:
            self.sock.sendall((command + "\n").encode("ascii"))
        except (socket.timeout, TimeoutError, OSError):
            pass

    def read(self) -> str:
        try:
            data = b""
            while True:
                chunk = self.sock.recv(1024)
                if not chunk:
                    break
                data += chunk
                if b"\n" in data or b"\r" in data:
                    break
            return data.decode("ascii", errors="ignore").strip()
        except (socket.timeout, TimeoutError, OSError):
            return ""

    def flush_input(self) -> None:
        """Discard stale/late data sitting in the receive buffer."""
        try:
            self.sock.setblocking(False)
            while True:
                try:
                    chunk = self.sock.recv(1024)
                    if not chunk:
                        break
                except (BlockingIOError, OSError):
                    break
        finally:
            # Restore the normal blocking timeout for subsequent reads
            self.sock.settimeout(0.5)

    def close(self) -> None:
        try:
            if self.sock:
                self.sock.close()
                dprint(f"Closed socket for {self.port_or_ip}")
        except Exception as exc:
            dprint(f"Error closing socket {self.port_or_ip}: {exc}")


# ==========================================================================
# Headless protocol work - no widget ever appears below this line until the
# GUI section. Everything here is callable from a worker thread or a script.
# ==========================================================================

def list_serial_ports():
    """[(device, description, serial_number)] for every visible serial port."""
    out = []
    for port in serial.tools.list_ports.comports():
        serial_num = port.serial_number or f"Unknown_USB_{port.device}"
        out.append((port.device, port.description or "", serial_num))
    return out


def netfinder_discover(timeout: float = 5.0, emit: Optional[Callable[[str], None]] = None):
    """Broadcast the Prologix NetFinder probe and collect the replies.

    Returns [(ip, mac)]. `emit` receives progress lines if supplied.
    """
    def say(line: str) -> None:
        dprint(line)
        if emit:
            emit(line)

    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(timeout)

        discovery_packet = bytes.fromhex("5A 00 5A 9E FF FF FF FF FF FF 00 00")

        try:
            sock.sendto(discovery_packet, ("255.255.255.255", NETFINDER_UDP_PORT))
            say("Probe sent to 255.255.255.255")
        except OSError as exc:
            say(f"Global broadcast refused: {exc}")

        # Some stacks drop the all-ones broadcast; repeat per interface subnet.
        try:
            _, _, ips = socket.gethostbyname_ex(socket.gethostname())
            for ip in ips:
                parts = ip.split(".")
                if len(parts) == 4:
                    subnet_bcast = f"{parts[0]}.{parts[1]}.{parts[2]}.255"
                    try:
                        sock.sendto(discovery_packet, (subnet_bcast, NETFINDER_UDP_PORT))
                        say(f"Probe sent to {subnet_bcast}")
                    except OSError:
                        pass
        except OSError:
            pass

        while True:
            try:
                data, _addr = sock.recvfrom(1024)
                if len(data) >= 24 and data.startswith(b"\x5a\x01\x5a\x9e"):
                    mac_str = ":".join(f"{b:02X}" for b in data[4:10])
                    ip_str = socket.inet_ntoa(data[20:24])
                    if ip_str not in seen:
                        seen.add(ip_str)
                        found.append((ip_str, mac_str))
                        say(f"Reply from {ip_str} (MAC {mac_str})")
            except socket.timeout:
                break
            except OSError:
                break
    finally:
        if sock is not None:
            sock.close()
    say(f"Discovery finished: {len(found)} adapter(s).")
    return found


def probe_controller(adapter: PrologixAdapter, location_desc: str):
    """Decide whether `adapter` really is a Prologix-compatible controller.

    Returns (verdict, message) where verdict is "ok", "ask" or "reject". No
    dialogs are raised here so the check can run on a worker thread; the caller
    turns "ask" into a prompt on the main thread.

    A Prologix-compatible controller has two testable properties:
      1. It is SILENT unless queried (a streaming device fails this), and
      2. It answers the same query with the same response every time
         (a streaming device's 'responses' are just whatever data happened
         to be in flight, so they differ between queries).
    """
    # Let the device settle after port-open (some controllers, e.g.
    # Arduino-based AR488, reset on open and emit boot text), then discard
    # anything that arrived unprompted so far.
    time.sleep(0.3)
    adapter.flush_input()

    # Check 1: silence. Read with NO command sent.
    unsolicited = (adapter.read() or "").strip()
    if unsolicited:
        return "reject", (
            f"The device at {location_desc} is transmitting data without being "
            f"queried:\n\n'{unsolicited[:120]}'\n\n"
            f"A Prologix-compatible controller only responds to commands. "
            f"This looks like a different kind of serial device.")

    # Check 2: consistent answers to ++ver, queried twice.
    responses = []
    for _ in range(2):
        adapter.flush_input()
        adapter.write("++ver")
        time.sleep(0.2)
        responses.append((adapter.read() or "").strip())
    r1, r2 = responses

    if not r1 and not r2:
        return "reject", (
            f"The device at {location_desc} did not respond to ++ver at all.\n"
            f"It does not appear to be a Prologix-compatible controller.")

    if r1 != r2:
        return "reject", (
            f"The device at {location_desc} gave inconsistent responses to the "
            f"same ++ver query:\n\n'{r1[:80]}'\n'{r2[:80]}'\n\n"
            f"A controller answers identically each time; this looks like a "
            f"device streaming unrelated data.")

    if "Prologix" in r1:
        return "ok", r1

    # Consistent, non-empty, non-Prologix: likely a compatible clone.
    return "ask", (
        f"The device at {location_desc} responded consistently to ++ver but did "
        f"not identify as Prologix:\n\n'{r1}'\n\n"
        f"This may be a compatible controller (e.g. AR488). Connect anyway?")


SCAN_PRECONDITIONS = ("++mode 1", "++auto 0", "++eos 3", "++eoi 1", "++read_tmo_ms 200")


def scan_bus(adapter: PrologixAdapter, first: int = 0, last: int = 30,
             stop_event: Optional[threading.Event] = None,
             emit: Optional[Callable[..., None]] = None):
    """Two-phase bus walk: serial-poll every address, then *IDN? the responders.

    Returns (records, present_addresses). `emit(kind, *args)` is called with
    ("log", text), ("progress", done, total) and ("row", record) as it goes.
    """
    def send(kind: str, *args) -> None:
        if emit:
            emit(kind, *args)

    stop_event = stop_event or threading.Event()

    # Enforce scan preconditions (session-only; deliberately no ++savecfg).
    # ++read_tmo_ms MUST be shorter than the 0.5 s transport read timeout,
    # otherwise late replies arrive in the NEXT iteration's read and cause
    # phantom/duplicated devices at the wrong addresses.
    for cmd in SCAN_PRECONDITIONS:
        adapter.write(cmd)
    time.sleep(0.2)
    adapter.flush_input()
    send("log", "Session set to Controller / Auto off / EOS none / EOI on / 200 ms "
                "(not saved to EEPROM).")

    addresses = list(range(first, last + 1))
    total = len(addresses)

    # Phase 1: fast presence detection via serial poll (IEEE-488.1). Every GPIB
    # device answers spoll at the interface-chip level, so this is safe for
    # pre-488.2 instruments and much faster on empty addresses. A valid spoll
    # reply is a decimal status byte (0-255); anything else is stale data or an
    # error string and is NOT treated as presence.
    present: list[int] = []
    status_bytes: dict[int, int] = {}
    for done, addr in enumerate(addresses, start=1):
        if stop_event.is_set():
            send("log", "Stopped during serial poll.")
            return [], present
        adapter.flush_input()
        adapter.write(f"++spoll {addr}")
        response = (adapter.read() or "").strip()
        dprint(f"spoll {addr}: '{response}'")
        if response.isdigit() and 0 <= int(response) <= 255:
            present.append(addr)
            status_bytes[addr] = int(response)
            send("log", f"  {addr:>2}  present, status byte {decode_status_byte(int(response))}")
        send("progress", done, total)

    send("log", f"Serial poll complete: {len(present)} of {total} addresses answered.")

    # Phase 2: identify only the devices that answered the poll.
    records = []
    for addr in present:
        if stop_event.is_set():
            send("log", "Stopped before identification finished.")
            break
        adapter.flush_input()
        adapter.write(f"++addr {addr}")
        adapter.write("*IDN?")
        adapter.write("++read eoi")
        response = (adapter.read() or "").strip()
        dprint(f"*IDN? {addr}: '{response}'")
        idn = response or "(present, no *IDN? response - pre-488.2 device)"
        record = (adapter.adapter_type, adapter.port_or_ip, adapter.serial_num,
                  addr, idn, "Found", status_bytes.get(addr))
        records.append(record)
        send("log", f"  {addr:>2}  {idn}")
        send("row", record)

    return records, present


# ==========================================================================
# TestController device-definition matching
# ==========================================================================

def tc_devices_path(base_path: str) -> Optional[str]:
    """The Devices folder inside a TestController install, or None."""
    if not base_path:
        return None
    for folder_name in ("Devices", "devices"):
        dev_path = os.path.join(base_path, folder_name)
        if os.path.isdir(dev_path):
            return dev_path
    return None


def tc_validate_install(directory: str):
    """(ok, problem) for a candidate TestController directory."""
    if not os.path.isdir(directory):
        return False, "The selected path is not a valid directory."
    has_jar = any(f.lower() == "testcontroller.jar" for f in os.listdir(directory))
    has_devices = tc_devices_path(directory) is not None
    if has_jar and has_devices:
        return True, ""
    problems = ["Selected directory is missing required files:\n"]
    if not has_jar:
        problems.append("- TestController.jar not found")
    if not has_devices:
        problems.append("- 'Devices' folder not found")
    return False, "\n".join(problems)


def tc_read_definitions(dev_path: str) -> dict:
    """{filename: [idString, ...]} for every definition file in the folder."""
    configs: dict[str, list[str]] = {}
    for filename in sorted(os.listdir(dev_path)):
        if not filename.endswith(".txt"):
            continue
        configs[filename] = []
        try:
            with open(os.path.join(dev_path, filename), "r",
                      encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    stripped = line.strip()
                    if stripped.lower().startswith("#idstring"):
                        configs[filename].append(stripped[9:].strip().strip(","))
        except OSError:
            continue
    return configs


def tc_match(idn: str, configs: dict) -> Optional[str]:
    """First definition file whose #idString fields all appear in `idn`."""
    idn_lower = (idn or "").lower()
    for filename, id_strings in configs.items():
        for id_string in id_strings:
            parts = [p.strip().lower() for p in id_string.split(",") if p.strip()]
            if parts and all(p in idn_lower for p in parts):
                return filename
    return None


# ==========================================================================
# Small shared helpers
# ==========================================================================

class QueuedFrame(ttk.Frame):
    """A frame whose background workers post events to a queue that the Tk
    main loop drains. Workers never touch a widget directly."""

    def __init__(self, master, **kw):
        super().__init__(master, **kw)
        self.q: queue.Queue = queue.Queue()
        self._pump_id: Optional[str] = None
        self._alive = True
        self._pump()

    def _pump(self) -> None:
        if not self._alive:
            return
        try:
            while True:
                event = self.q.get_nowait()
                try:
                    self.on_event(*event)
                except Exception as exc:            # a bad event must not stop the pump
                    print(f"event error: {exc}")
        except queue.Empty:
            pass
        self._pump_id = self.after(80, self._pump)

    def on_event(self, kind: str, *args) -> None:
        raise NotImplementedError

    def shutdown(self) -> None:
        self._alive = False
        if self._pump_id:
            try:
                self.after_cancel(self._pump_id)
            except tk.TclError:
                pass


class LogPane(ttk.Frame):
    def __init__(self, master, height: int = 6):
        super().__init__(master)
        self.text = tk.Text(self, height=height, wrap="none", state="disabled",
                            font=("TkFixedFont", 9))
        bar = ttk.Scrollbar(self, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=bar.set)
        self.text.grid(row=0, column=0, sticky="nsew")
        bar.grid(row=0, column=1, sticky="ns")
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

    def write(self, line: str) -> None:
        self.text.configure(state="normal")
        self.text.insert("end", line.rstrip() + "\n")
        self.text.see("end")
        self.text.configure(state="disabled")

    def clear(self) -> None:
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")


class FieldDialog(tk.Toplevel):
    """Modal editor for a handful of single-line fields."""

    def __init__(self, master, title: str, fields: dict):
        super().__init__(master)
        self.title(title)
        self.transient(master)
        self.resizable(False, False)
        self.result: Optional[dict] = None
        self.vars: dict = {}
        body = ttk.Frame(self, padding=PAD * 2)
        body.pack(fill="both", expand=True)
        for row, (label, value) in enumerate(fields.items()):
            ttk.Label(body, text=label).grid(row=row, column=0, sticky="w",
                                             pady=3, padx=(0, PAD))
            var = tk.StringVar(value=value)
            self.vars[label] = var
            entry = ttk.Entry(body, textvariable=var, width=48)
            entry.grid(row=row, column=1, sticky="ew", pady=3)
            if row == 0:
                entry.focus_set()
        buttons = ttk.Frame(body)
        buttons.grid(row=len(fields), column=0, columnspan=2, sticky="e", pady=(PAD * 2, 0))
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side="right", padx=(PAD, 0))
        ttk.Button(buttons, text="Save", command=self._save).pack(side="right")
        self.bind("<Return>", lambda _e: self._save())
        self.bind("<Escape>", lambda _e: self.destroy())
        self.grab_set()
        self.wait_window(self)

    def _save(self) -> None:
        self.result = {k: v.get().strip() for k, v in self.vars.items()}
        self.destroy()


def sortable(tree: ttk.Treeview, columns: tuple, numeric: tuple = ()) -> None:
    """Click a heading to sort by it."""
    state = {"col": None, "reverse": False}

    def sort_by(col: str) -> None:
        reverse = (not state["reverse"]) if state["col"] == col else False
        state.update(col=col, reverse=reverse)
        rows = [(tree.set(k, col), k) for k in tree.get_children("")]

        def key(pair):
            value = pair[0]
            if col in numeric:
                try:
                    return (0, float(value))
                except ValueError:
                    return (1, 0.0)
            return (0, value.lower())

        rows.sort(key=key, reverse=reverse)
        for index, (_v, k) in enumerate(rows):
            tree.move(k, "", index)

    for col in columns:
        tree.heading(col, command=lambda c=col: sort_by(c))


def build_tree(parent, spec, stretch=(), selectmode="browse"):
    """A Treeview plus its scrollbar, gridded into a fresh container frame.

    `spec` is ((key, heading, width), ...). Returns (container, tree).
    """
    container = ttk.Frame(parent)
    container.columnconfigure(0, weight=1)
    container.rowconfigure(0, weight=1)
    cols = tuple(c[0] for c in spec)
    tree = ttk.Treeview(container, columns=cols, show="headings", selectmode=selectmode)
    for key, title, width in spec:
        tree.heading(key, text=title)
        tree.column(key, width=width, anchor="w", stretch=(key in stretch))
    vbar = ttk.Scrollbar(container, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=vbar.set)
    tree.grid(row=0, column=0, sticky="nsew")
    vbar.grid(row=0, column=1, sticky="ns")
    sortable(tree, cols, numeric=("addr", "gpib", "devices"))
    return container, tree


# ==========================================================================
# Connections tab
# ==========================================================================

CONNECTION_COLUMNS = (
    ("kind", "Type", 90), ("where", "Port / address", 220),
    ("detail", "Description / MAC", 300), ("serial", "Adapter ID", 200),
    ("devices", "Records", 75), ("state", "State", 110),
    ("last_seen", "Last seen", 160),
)


class ConnectionsTab(QueuedFrame):
    """Serial ports and NetFinder replies in one table. Open a controller and
    it gets its own tab, the way a gateway does in LGI."""

    def __init__(self, app: "App"):
        super().__init__(app.notebook, padding=PAD)
        self.app = app
        self.worker: Optional[threading.Thread] = None
        self.busy = False
        self.live: dict[str, dict] = {}     # iid -> discovered/enumerated entry

        controls = ttk.LabelFrame(self, text="Find controllers", padding=PAD)
        controls.grid(row=0, column=0, sticky="ew")
        controls.columnconfigure(6, weight=1)

        self.serial_var = tk.BooleanVar(value=True)
        self.netfinder_var = tk.BooleanVar(value=True)
        self.timeout_var = tk.DoubleVar(value=5.0)

        ttk.Checkbutton(controls, text="Serial ports", variable=self.serial_var).grid(
            row=0, column=0, sticky="w")
        ttk.Checkbutton(controls, text="NetFinder broadcast",
                        variable=self.netfinder_var).grid(row=0, column=1, sticky="w",
                                                          padx=(PAD, 2))
        ttk.Label(controls, text="Listen (s)").grid(row=0, column=2, padx=(PAD * 2, 2))
        ttk.Spinbox(controls, from_=1, to=30, increment=0.5, width=5,
                    textvariable=self.timeout_var).grid(row=0, column=3)

        buttons = ttk.Frame(controls)
        buttons.grid(row=0, column=7, sticky="e")
        self.find_btn = ttk.Button(buttons, text="Find controllers", command=self.start)
        self.find_btn.pack(side="left")
        ttk.Button(buttons, text="Add by address…", command=self.add_manual).pack(
            side="left", padx=(PAD, 0))

        self.progress = ttk.Progressbar(self, mode="determinate")
        self.progress.grid(row=1, column=0, sticky="ew", pady=(PAD, 0))

        table, self.tree = build_tree(self, CONNECTION_COLUMNS,
                                      stretch=("detail", "last_seen"))
        table.grid(row=2, column=0, sticky="nsew", pady=PAD)
        self.tree.tag_configure("live", foreground=OK_FG)
        self.tree.tag_configure("stale", foreground="#666666")
        self.tree.tag_configure("open", foreground="#0b3f6b")
        self.tree.bind("<Double-1>", lambda _e: self.open_selected())

        actions = ttk.Frame(self)
        actions.grid(row=3, column=0, sticky="ew")
        ttk.Button(actions, text="Connect", command=self.open_selected).pack(side="left")
        ttk.Button(actions, text="Forget records", command=self.forget_selected).pack(
            side="left", padx=(PAD, 0))
        ttk.Label(actions, text="Double-click a controller to open it in its own tab.",
                  foreground=MUTED).pack(side="right")

        self.log = LogPane(self, height=7)
        self.log.grid(row=4, column=0, sticky="nsew", pady=(PAD, 0))

        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=3)
        self.rowconfigure(4, weight=1)

        self.refresh()
        self.log.write(f"{APP_NAME} {VERSION} - press Find controllers (F5) to enumerate "
                       f"serial ports and broadcast for Ethernet adapters.")

    # -- table -------------------------------------------------------------
    def refresh(self) -> None:
        """Merge what the database remembers with what is answering now."""
        selected = self.tree.selection()
        keep = selected[0] if selected else None
        self.tree.delete(*self.tree.get_children(""))

        rows = {}
        for kind, where, serial_num, count, last_seen in known_adapters():
            if not serial_num:
                continue
            rows[serial_num] = {
                "kind": kind or "", "where": where or "", "detail": "",
                "serial": serial_num, "devices": count, "state": "recorded",
                "last_seen": last_seen or "",
            }
        for entry in self.live.values():
            merged = rows.get(entry["serial"], {}).copy()
            merged.update(entry)
            merged.setdefault("devices", 0)
            merged.setdefault("last_seen", "")
            rows[entry["serial"]] = merged

        for serial_num, row in sorted(rows.items(), key=lambda kv: (kv[1]["kind"],
                                                                   kv[1]["where"])):
            if serial_num in self.app.adapter_tabs:
                tag = "open"
                row["state"] = "connected"
            elif row["state"] in ("responding", "present"):
                tag = "live"
            else:
                tag = "stale"
            self.tree.insert("", "end", iid=serial_num, tags=(tag,),
                             values=(row["kind"], row["where"], row["detail"],
                                     serial_num, row["devices"], row["state"],
                                     row["last_seen"]))
        if keep and self.tree.exists(keep):
            self.tree.selection_set(keep)

    def selected_row(self) -> Optional[dict]:
        selection = self.tree.selection()
        if not selection:
            return None
        values = self.tree.item(selection[0])["values"]
        return {"kind": str(values[0]), "where": str(values[1]),
                "detail": str(values[2]), "serial": str(values[3])}

    # -- discovery ---------------------------------------------------------
    def start(self) -> None:
        if self.busy:
            return
        if not (self.serial_var.get() or self.netfinder_var.get()):
            messagebox.showinfo(APP_NAME, "Choose serial ports, NetFinder, or both.")
            return
        self.busy = True
        self.find_btn.configure(state="disabled")
        self.progress.configure(value=0, maximum=100)
        self.log.write("─" * 60)
        self.app.set_status("Searching for controllers…")
        self.worker = threading.Thread(
            target=self._work, daemon=True,
            kwargs=dict(do_serial=self.serial_var.get(),
                        do_net=self.netfinder_var.get(),
                        timeout=self.timeout_var.get()))
        self.worker.start()

    def _work(self, do_serial: bool, do_net: bool, timeout: float) -> None:
        try:
            if do_serial:
                self.q.put(("log", "Enumerating serial ports…"))
                for device, description, serial_num in list_serial_ports():
                    self.q.put(("serial_port", device, description, serial_num))
                self.q.put(("progress", 40))
            if do_net:
                self.q.put(("log", f"NetFinder broadcast, listening {timeout:g} s…"))
                found = netfinder_discover(timeout, emit=lambda t: self.q.put(("log", t)))
                for ip, mac in found:
                    self.q.put(("netfinder", ip, mac))
            self.q.put(("progress", 100))
        except Exception as exc:
            self.q.put(("log", f"Discovery failed: {exc}"))
        finally:
            self.q.put(("done",))

    def on_event(self, kind: str, *args) -> None:
        if kind == "log":
            self.log.write(args[0])
        elif kind == "progress":
            self.progress.configure(value=args[0])
        elif kind == "serial_port":
            device, description, serial_num = args
            self.live[serial_num] = {"kind": "USB", "where": device,
                                     "detail": description, "serial": serial_num,
                                     "state": "present"}
            self.log.write(f"Serial port {device} - {description}")
        elif kind == "netfinder":
            ip, mac = args
            self.live[mac] = {"kind": "Ethernet", "where": ip, "detail": f"MAC {mac}",
                              "serial": mac, "state": "responding"}
        elif kind == "done":
            self.busy = False
            self.find_btn.configure(state="normal")
            self.progress.configure(value=100)
            self.refresh()
            self.app.set_status("Discovery finished")
        elif kind == "connect_failed":
            desc, message = args
            self.log.write(f"! {desc}: {message}")
            messagebox.showerror("Connection Error", f"{desc}\n\n{message}")
            self.app.set_status("Connection failed")
        elif kind == "connect_result":
            adapter, desc, verdict, message = args
            self._finish_connect(adapter, desc, verdict, message)

    # -- connecting --------------------------------------------------------
    def add_manual(self) -> None:
        dialog = FieldDialog(self, "Add a controller by address",
                             {"Host or IP": "", "Port": str(PROLOGIX_TCP_PORT)})
        if not dialog.result:
            return
        host = dialog.result["Host or IP"].strip()
        if not host:
            return
        self.connect_ethernet(host, self.live.get(host, {}).get("serial", f"Manual_{host}"))

    def open_selected(self) -> None:
        row = self.selected_row()
        if row is None:
            messagebox.showinfo(APP_NAME, "Select a controller first.")
            return
        if row["serial"] in self.app.adapter_tabs:
            self.app.focus_adapter_tab(row["serial"])
            return
        if row["kind"] == "USB":
            self.connect_serial(row["where"], row["serial"])
        elif row["kind"] == "Ethernet":
            self.connect_ethernet(row["where"], row["serial"])
        else:
            messagebox.showinfo(APP_NAME, "This record has no usable connection type.")

    def connect_serial(self, port: str, serial_num: str) -> None:
        self._connect(lambda: USBAdapter(port, serial_num), port)

    def connect_ethernet(self, ip: str, mac: str) -> None:
        self._connect(lambda: EthernetAdapter(ip, mac), ip)

    def _connect(self, factory, desc: str) -> None:
        self.log.write(f"Opening {desc}…")
        self.app.set_status(f"Opening {desc}…")
        threading.Thread(target=self._connect_work, args=(factory, desc),
                         daemon=True).start()

    def _connect_work(self, factory, desc: str) -> None:
        """Open and validate off the main thread; the GUI decides what to do."""
        try:
            adapter = factory()
        except Exception as exc:
            self.q.put(("connect_failed", desc, str(exc)))
            return
        try:
            verdict, message = probe_controller(adapter, desc)
        except Exception as exc:
            adapter.close()
            self.q.put(("connect_failed", desc, f"Validation failed: {exc}"))
            return
        self.q.put(("connect_result", adapter, desc, verdict, message))

    def _finish_connect(self, adapter, desc: str, verdict: str, message: str) -> None:
        if verdict == "reject":
            adapter.close()
            self.log.write(f"! {desc} rejected by validation.")
            messagebox.showerror("Hardware Validation Failed", message)
            self.app.set_status("Validation failed")
            return
        if verdict == "ask" and not messagebox.askyesno("Unrecognized Controller", message):
            adapter.close()
            self.log.write(f"{desc}: connection declined.")
            self.app.set_status("Ready")
            return
        if verdict == "ok":
            self.log.write(f"{desc}: {message}")
        self.app.open_adapter(adapter)
        self.refresh()

    # -- records -----------------------------------------------------------
    def forget_selected(self) -> None:
        row = self.selected_row()
        if row is None:
            return
        if row["serial"] in self.app.adapter_tabs:
            messagebox.showinfo(APP_NAME, "Close the controller's tab first.")
            return
        if not messagebox.askyesno(
                "Confirm Delete",
                f"Delete every recorded instrument for adapter {row['serial']}?"):
            return
        with sqlite3.connect(DB_NAME, timeout=DB_TIMEOUT) as conn:
            conn.execute("DELETE FROM devices WHERE adapter_serial = ?", (row["serial"],))
            conn.commit()
        self.live.pop(row["serial"], None)
        self.refresh()
        self.app.database_tab.refresh()
        self.log.write(f"Forgot records for {row['serial']}.")

    def shutdown(self) -> None:
        super().shutdown()


# ==========================================================================
# Adapter tab - one per connected controller
# ==========================================================================

BUS_COLUMNS = (
    ("addr", "Addr", 55), ("state", "State", 100), ("stb", "Status byte", 150),
    ("idn", "*IDN? response", 460), ("tc", "TC definition", 190),
    ("last_seen", "Last seen", 160),
)

CONFIG_FIELDS = (
    ("++mode", "Mode", ("0 (Device)", "1 (Controller)"), "grp1"),
    ("++auto", "Read-After-Write", ("0 (Disable)", "1 (Enable)"), "grp1"),
    ("++lon", "Listen-Only", ("0 (Disable)", "1 (Enable)"), "grp1"),
    ("++savecfg", "Save Config", ("0 (Disable)", "1 (Enable)"), "grp1"),
    ("++eos", "Terminator", ("0 (CR+LF)", "1 (CR)", "2 (LF)", "3 (None)"), "grp2"),
    ("++eoi", "Assert EOI", ("0 (Disable)", "1 (Enable)"), "grp2"),
    ("++eot_enable", "EOT Enable", ("0 (Disable)", "1 (Enable)"), "grp2"),
    ("++read_tmo_ms", "Timeout (ms)", None, "grp2"),
    ("++eot_char", "EOT Char", None, "grp2"),
)


class AdapterTab(QueuedFrame):
    """Three panes behind one controller: the bus inventory, the controller's
    own configuration, and a raw terminal."""

    def __init__(self, app: "App", adapter: PrologixAdapter):
        super().__init__(app.notebook, padding=PAD)
        self.app = app
        self.adapter = adapter
        self.stop_event = threading.Event()
        self.worker: Optional[threading.Thread] = None
        self.io_lock = threading.Lock()     # one conversation on the bus at a time
        self.history: list[str] = []
        self.history_pos = 0
        self.config_widgets: dict[str, tk.Widget] = {}

        header = ttk.Frame(self)
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(1, weight=1)
        ttk.Label(header, text=f"{adapter.adapter_type} controller at {adapter.port_or_ip}",
                  font=("TkDefaultFont", 11, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(header, text=f"   adapter ID {adapter.serial_num}",
                  foreground=MUTED).grid(row=0, column=1, sticky="w")
        ttk.Button(header, text="Close tab",
                   command=lambda: self.app.close_adapter_tab(adapter.serial_num)).grid(
            row=0, column=2, sticky="e")

        self.inner = ttk.Notebook(self)
        self.inner.grid(row=1, column=0, sticky="nsew", pady=(PAD, 0))
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        self._build_bus_pane()
        self._build_config_pane()
        self._build_terminal_pane()

        self.load_from_db()
        self.read_config()

    # -- bus inventory -----------------------------------------------------
    def _build_bus_pane(self) -> None:
        pane = ttk.Frame(self.inner, padding=PAD)
        self.inner.add(pane, text="Bus inventory")
        pane.columnconfigure(0, weight=1)
        pane.rowconfigure(2, weight=3)
        pane.rowconfigure(4, weight=1)

        controls = ttk.LabelFrame(pane, text="Scan", padding=PAD)
        controls.grid(row=0, column=0, sticky="ew")
        controls.columnconfigure(6, weight=1)

        self.first_var = tk.IntVar(value=0)
        self.last_var = tk.IntVar(value=30)

        ttk.Label(controls, text="Addresses").grid(row=0, column=0, sticky="w")
        ttk.Spinbox(controls, from_=0, to=30, width=4,
                    textvariable=self.first_var).grid(row=0, column=1, padx=(4, 2))
        ttk.Label(controls, text="to").grid(row=0, column=2)
        ttk.Spinbox(controls, from_=0, to=30, width=4,
                    textvariable=self.last_var).grid(row=0, column=3, padx=(2, PAD))

        buttons = ttk.Frame(controls)
        buttons.grid(row=0, column=7, sticky="e")
        self.scan_btn = ttk.Button(buttons, text="Scan GPIB bus", command=self.start_scan)
        self.scan_btn.pack(side="left")
        self.stop_btn = ttk.Button(buttons, text="Stop", command=self.stop_scan,
                                   state="disabled")
        self.stop_btn.pack(side="left", padx=(PAD, 0))
        ttk.Button(buttons, text="Export CSV…",
                   command=lambda: self.app.export_csv(self.adapter.serial_num)).pack(
            side="left", padx=(PAD, 0))
        ttk.Button(buttons, text="Export JSON…",
                   command=lambda: self.app.export_json(self.adapter.serial_num)).pack(
            side="left", padx=(PAD, 0))
        ttk.Label(controls,
                  text="A scan forces Controller / Auto off / EOS none / EOI on for the "
                       "session only - nothing is written to the adapter's EEPROM.",
                  foreground=MUTED).grid(row=1, column=0, columnspan=8, sticky="w",
                                         pady=(4, 0))

        self.progress = ttk.Progressbar(pane, mode="determinate")
        self.progress.grid(row=1, column=0, sticky="ew", pady=(PAD, 0))

        table, self.tree = build_tree(pane, BUS_COLUMNS, stretch=("idn",),
                                      selectmode="extended")
        table.grid(row=2, column=0, sticky="nsew", pady=PAD)
        self.tree.tag_configure("found", foreground=OK_FG)
        self.tree.tag_configure("missing", foreground="#666666")
        self.tree.bind("<Double-1>", lambda _e: self.send_to_terminal())

        actions = ttk.Frame(pane)
        actions.grid(row=3, column=0, sticky="ew")
        ttk.Button(actions, text="Address in terminal",
                   command=self.send_to_terminal).pack(side="left")
        ttk.Button(actions, text="Re-query *IDN?",
                   command=self.requery_selected).pack(side="left", padx=(PAD, 0))
        ttk.Button(actions, text="Delete record(s)",
                   command=self.delete_selected).pack(side="left", padx=(PAD, 0))
        ttk.Label(actions, text="Double-click a row to load its address into the terminal.",
                  foreground=MUTED).pack(side="right")

        self.log = LogPane(pane, height=7)
        self.log.grid(row=4, column=0, sticky="nsew", pady=(PAD, 0))

    # -- controller configuration -----------------------------------------
    def _build_config_pane(self) -> None:
        pane = ttk.Frame(self.inner, padding=PAD)
        self.inner.add(pane, text="Configuration")
        pane.columnconfigure(0, weight=1)
        pane.rowconfigure(4, weight=1)

        groups = {
            "grp1": ttk.LabelFrame(pane, text="Operating mode & general", padding=PAD),
            "grp2": ttk.LabelFrame(pane, text="Formatting & timeouts", padding=PAD),
        }
        groups["grp1"].grid(row=0, column=0, sticky="ew")
        groups["grp2"].grid(row=1, column=0, sticky="ew", pady=(PAD, 0))
        positions = {"grp1": 0, "grp2": 0}

        for cmd, label, values, group in CONFIG_FIELDS:
            parent = groups[group]
            index = positions[group]
            positions[group] += 1
            row, col = divmod(index, 2)
            ttk.Label(parent, text=f"{label} ({cmd}):").grid(
                row=row, column=col * 2, sticky="e", padx=(0, 4), pady=3)
            if values:
                widget = ttk.Combobox(parent, values=list(values), state="readonly", width=16)
            else:
                widget = ttk.Entry(parent, width=19)
            widget.grid(row=row, column=col * 2 + 1, sticky="w", padx=(0, PAD * 2), pady=3)
            self.config_widgets[cmd] = widget

        immediate = ttk.LabelFrame(pane, text="Immediate actions", padding=PAD)
        immediate.grid(row=2, column=0, sticky="ew", pady=(PAD, 0))
        for text, cmd, note in (
                ("Interface Clear (++ifc)", "++ifc", "Interface clear sent"),
                ("Device Clear (++clr)", "++clr", "Device clear sent"),
                ("Local Lockout (++llo)", "++llo", "Local lockout sent"),
                ("Go to Local (++loc)", "++loc", "Go to local sent"),
                ("Reset Adapter (++rst)", "++rst", "Adapter reset sequence started")):
            ttk.Button(immediate, text=text,
                       command=lambda c=cmd, n=note: self.run_action(c, n)).pack(
                side="left", padx=(0, PAD))

        bar = ttk.Frame(pane)
        bar.grid(row=3, column=0, sticky="ew", pady=(PAD, 0))
        ttk.Button(bar, text="Read from adapter", command=self.read_config).pack(side="left")
        ttk.Button(bar, text="Apply configuration", command=self.apply_config).pack(
            side="left", padx=(PAD, 0))
        ttk.Button(bar, text="Set scanner defaults", command=self.set_scanner_defaults).pack(
            side="left", padx=(PAD, 0))
        ttk.Button(bar, text="Get version (++ver)", command=self.get_version).pack(
            side="right")
        self.config_status = ttk.Label(pane, text="Ready", foreground=MUTED)
        self.config_status.grid(row=4, column=0, sticky="nw", pady=(PAD, 0))

    # -- terminal ----------------------------------------------------------
    def _build_terminal_pane(self) -> None:
        pane = ttk.Frame(self.inner, padding=PAD)
        self.inner.add(pane, text="Terminal")
        pane.columnconfigure(0, weight=1)
        pane.rowconfigure(1, weight=1)

        bar = ttk.Frame(pane)
        bar.grid(row=0, column=0, sticky="ew")
        ttk.Label(bar, text="GPIB address").pack(side="left")
        self.term_addr_var = tk.StringVar()
        self.term_addr_box = ttk.Combobox(bar, textvariable=self.term_addr_var, width=6,
                                          values=[str(a) for a in range(31)])
        self.term_addr_box.pack(side="left", padx=(2, PAD))
        ttk.Button(bar, text="Serial poll",
                   command=lambda: self.terminal_action("spoll")).pack(side="left")
        ttk.Button(bar, text="Read",
                   command=lambda: self.terminal_action("read")).pack(side="left",
                                                                      padx=(PAD, 0))
        ttk.Button(bar, text="Clear log", command=lambda: self.term_out.clear()).pack(
            side="left", padx=(PAD, 0))
        ttk.Label(bar, text="Leave the address blank to talk to the controller only.",
                  foreground=MUTED).pack(side="right")

        self.term_out = LogPane(pane, height=18)
        self.term_out.grid(row=1, column=0, sticky="nsew", pady=PAD)

        entry_row = ttk.Frame(pane)
        entry_row.grid(row=2, column=0, sticky="ew")
        entry_row.columnconfigure(0, weight=1)
        self.command_var = tk.StringVar()
        self.command_entry = ttk.Entry(entry_row, textvariable=self.command_var,
                                       font=("TkFixedFont", 10))
        self.command_entry.grid(row=0, column=0, sticky="ew")
        self.command_entry.bind("<Return>", lambda _e: self.terminal_action("auto"))
        self.command_entry.bind("<Up>", self._history_back)
        self.command_entry.bind("<Down>", self._history_forward)
        ttk.Button(entry_row, text="Send",
                   command=lambda: self.terminal_action("auto")).grid(row=0, column=1,
                                                                      padx=(PAD, 0))
        ttk.Label(pane, text="A ++ command is written and read back; a SCPI command "
                             "ending in ? triggers ++read eoi; anything else is "
                             "write-only. Up/Down recall history.",
                  foreground=MUTED).grid(row=3, column=0, sticky="w", pady=(2, 0))

    # -- table -------------------------------------------------------------
    def load_from_db(self) -> None:
        self.tree.delete(*self.tree.get_children(""))
        for row in fetch_all_devices(self.adapter.serial_num):
            (_kind, _where, _serial, addr, idn, status, last_seen,
             tc_config, stb) = row
            self._put_row(addr, status, stb, idn, tc_config, last_seen)

    def _put_row(self, addr, status, stb, idn, tc_config, last_seen) -> None:
        iid = str(addr)
        values = (addr, status or "", decode_status_byte(stb), idn or "",
                  tc_config or "", last_seen or "")
        tag = "found" if (status or "").lower() == "found" else "missing"
        if self.tree.exists(iid):
            self.tree.item(iid, values=values, tags=(tag,))
        else:
            self.tree.insert("", "end", iid=iid, values=values, tags=(tag,))

    def selected_addresses(self) -> list:
        out = []
        for iid in self.tree.selection():
            try:
                out.append(int(self.tree.set(iid, "addr")))
            except (ValueError, tk.TclError):
                continue
        return out

    # -- scanning ----------------------------------------------------------
    def start_scan(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        first, last = self.first_var.get(), self.last_var.get()
        if first > last:
            messagebox.showinfo(APP_NAME, "The first address must not exceed the last.")
            return
        self.stop_event.clear()
        self.scan_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.progress.configure(value=0, maximum=last - first + 1)
        self.log.write("─" * 60)
        self.log.write(f"Scanning addresses {first}–{last} on {self.adapter.port_or_ip}")
        self.app.set_status(f"Scanning {self.adapter.port_or_ip}…")
        self.worker = threading.Thread(target=self._scan_work, args=(first, last),
                                       daemon=True)
        self.worker.start()

    def stop_scan(self) -> None:
        self.stop_event.set()
        self.log.write("Stop requested…")

    def _scan_work(self, first: int, last: int) -> None:
        try:
            with self.io_lock:
                records, present = scan_bus(
                    self.adapter, first, last, self.stop_event,
                    emit=lambda kind, *a: self.q.put((kind, *a)))
            if not self.stop_event.is_set():
                upsert_devices_batch(records)
                mark_missing_devices(self.adapter.serial_num, [r[3] for r in records],
                                     first, last)
            self.q.put(("scan_done", len(records), len(present)))
        except Exception as exc:
            self.q.put(("log", f"! Scan failed: {exc}"))
            self.q.put(("scan_done", 0, 0))

    def on_event(self, kind: str, *args) -> None:
        if kind == "log":
            self.log.write(args[0])
        elif kind == "progress":
            done, total = args
            self.progress.configure(value=done, maximum=total)
        elif kind == "row":
            (_kind, _where, _serial, addr, idn, status, stb) = args[0]
            self._put_row(addr, status, stb, idn, None, now_stamp())
        elif kind == "scan_done":
            found, present = args
            self.scan_btn.configure(state="normal")
            self.stop_btn.configure(state="disabled")
            if self.stop_event.is_set():
                self.log.write("Scan stopped; nothing was written to the database.")
                self.app.set_status("Scan stopped")
            else:
                self.log.write(f"Scan complete: {found} instrument(s) identified "
                               f"at {present} live address(es).")
                self.load_from_db()
                self.app.database_tab.refresh()
                self.app.set_status(f"{self.adapter.port_or_ip}: {found} instrument(s)")
        elif kind == "term":
            self.term_out.write(args[0])
        elif kind == "cfgvalue":
            self._set_widget_value(args[0], args[1])
        elif kind == "cfgstatus":
            text, colour = args
            self.config_status.configure(text=text, foreground=colour)
        elif kind == "info":
            messagebox.showinfo(*args)
        elif kind == "error":
            messagebox.showerror(*args)

    # -- row actions -------------------------------------------------------
    def send_to_terminal(self) -> None:
        addrs = self.selected_addresses()
        if not addrs:
            return
        self.term_addr_var.set(str(addrs[0]))
        self.inner.select(2)
        self.command_entry.focus_set()

    def requery_selected(self) -> None:
        addrs = self.selected_addresses()
        if not addrs:
            messagebox.showinfo(APP_NAME, "Select one or more addresses first.")
            return
        threading.Thread(target=self._requery_work, args=(addrs,), daemon=True).start()

    def _requery_work(self, addrs) -> None:
        records = []
        try:
            with self.io_lock:
                for addr in addrs:
                    self.adapter.flush_input()
                    self.adapter.write(f"++spoll {addr}")
                    reply = (self.adapter.read() or "").strip()
                    stb = int(reply) if reply.isdigit() and 0 <= int(reply) <= 255 else None
                    self.adapter.flush_input()
                    self.adapter.write(f"++addr {addr}")
                    self.adapter.write("*IDN?")
                    self.adapter.write("++read eoi")
                    idn = (self.adapter.read() or "").strip()
                    status = "Found" if (stb is not None or idn) else "NotFound"
                    idn = idn or "(present, no *IDN? response - pre-488.2 device)"
                    record = (self.adapter.adapter_type, self.adapter.port_or_ip,
                              self.adapter.serial_num, addr, idn, status, stb)
                    records.append(record)
                    self.q.put(("log", f"  {addr:>2}  {idn}"))
                    self.q.put(("row", record))
            upsert_devices_batch(records)
        except Exception as exc:
            self.q.put(("log", f"! Re-query failed: {exc}"))

    def delete_selected(self) -> None:
        addrs = self.selected_addresses()
        if not addrs:
            messagebox.showinfo(APP_NAME, "Select one or more records first.")
            return
        if not messagebox.askyesno(
                "Confirm Delete",
                f"Delete {len(addrs)} record{'s' if len(addrs) > 1 else ''} for this adapter?"):
            return
        delete_device_records([(a, self.adapter.serial_num) for a in addrs])
        self.load_from_db()
        self.app.database_tab.refresh()
        self.log.write(f"Deleted {len(addrs)} record(s).")

    # -- configuration -----------------------------------------------------
    def read_config(self) -> None:
        self.config_status.configure(text="Reading from adapter…", foreground=MUTED)
        threading.Thread(target=self._read_config_work, daemon=True).start()

    def _read_config_work(self) -> None:
        try:
            with self.io_lock:
                for cmd in self.config_widgets:
                    self.adapter.write(cmd)
                    time.sleep(0.05)
                    value = (self.adapter.read() or "").strip()
                    if value:
                        self.q.put(("cfgvalue", cmd, value))
            self.q.put(("cfgstatus", "Configuration read successfully", OK_FG))
        except Exception as exc:
            self.q.put(("cfgstatus", f"Read error - {exc}", BAD_FG))

    def _set_widget_value(self, cmd: str, value: str) -> None:
        widget = self.config_widgets.get(cmd)
        if widget is None or not value:
            return
        if isinstance(widget, ttk.Combobox):
            for option in widget["values"]:
                if option.startswith(value):
                    widget.set(option)
                    return
            widget.set(value)
        else:
            widget.delete(0, tk.END)
            widget.insert(0, value)

    def apply_config(self) -> None:
        values = {}
        for cmd, widget in self.config_widgets.items():
            value = widget.get().strip()
            if not value:
                continue
            values[cmd] = value.split(" ")[0] if " (" in value else value
        self.config_status.configure(text="Applying configuration…", foreground=MUTED)
        threading.Thread(target=self._apply_config_work, args=(values,), daemon=True).start()

    def _apply_config_work(self, values: dict) -> None:
        try:
            # Send ++savecfg last: once savecfg is enabled, every subsequent
            # config command triggers an EEPROM write (limited write cycles).
            savecfg = values.pop("++savecfg", None)
            with self.io_lock:
                for cmd, value in values.items():
                    self.adapter.write(f"{cmd} {value}")
                    time.sleep(0.05)
                if savecfg is not None:
                    self.adapter.write(f"++savecfg {savecfg}")
                    time.sleep(0.05)
            self.q.put(("cfgstatus", "Configuration applied successfully", OK_FG))
        except Exception as exc:
            self.q.put(("cfgstatus", f"Apply error - {exc}", BAD_FG))

    def set_scanner_defaults(self) -> None:
        for cmd, value in (("++mode", "1 (Controller)"), ("++auto", "0 (Disable)"),
                           ("++eos", "3 (None)"), ("++eoi", "1 (Enable)"),
                           ("++read_tmo_ms", "200")):
            self._set_widget_value(cmd, value.split(" ")[0] if " (" in value else value)
        self.config_status.configure(text="Scanner defaults loaded - not yet applied.",
                                     foreground=MUTED)

    def run_action(self, cmd: str, note: str) -> None:
        threading.Thread(target=self._action_work, args=(cmd, note), daemon=True).start()

    def _action_work(self, cmd: str, note: str) -> None:
        try:
            with self.io_lock:
                self.adapter.write(cmd)
            self.q.put(("cfgstatus", note, OK_FG))
            self.q.put(("term", f"> {cmd}"))
        except Exception as exc:
            self.q.put(("cfgstatus", f"{cmd} failed - {exc}", BAD_FG))

    def get_version(self) -> None:
        threading.Thread(target=self._version_work, daemon=True).start()

    def _version_work(self) -> None:
        try:
            with self.io_lock:
                self.adapter.flush_input()
                self.adapter.write("++ver")
                time.sleep(0.1)
                reply = (self.adapter.read() or "").strip()
            self.q.put(("info", "Adapter Version", f"Response from adapter:\n\n{reply}"))
            self.q.put(("cfgstatus", reply or "No response to ++ver",
                        OK_FG if reply else BAD_FG))
        except Exception as exc:
            self.q.put(("error", "Error", str(exc)))

    # -- terminal ----------------------------------------------------------
    def terminal_action(self, action: str) -> None:
        addr = self.term_addr_var.get().strip()
        if action == "auto":
            command = self.command_var.get().strip()
            if not command:
                return
            self.history.append(command)
            self.history_pos = len(self.history)
            self.command_var.set("")
        elif action == "spoll":
            if not addr:
                self.term_out.write("! Serial poll needs a GPIB address.")
                return
            command = f"++spoll {addr}"
            addr = ""
        elif action == "read":
            command = "++read eoi"
        else:
            return
        threading.Thread(target=self._terminal_work, args=(command, addr),
                         daemon=True).start()

    def _terminal_work(self, command: str, addr: str) -> None:
        try:
            with self.io_lock:
                self.adapter.flush_input()
                if addr and not command.startswith("++"):
                    self.adapter.write(f"++addr {addr}")
                self.q.put(("term", f"> {command}"))
                self.adapter.write(command)
                if command.startswith("++"):
                    # Controller commands: query forms reply, set forms don't.
                    time.sleep(0.1)
                    reply = (self.adapter.read() or "").strip()
                    self.q.put(("term", f"< {reply}" if reply else "< (no response)"))
                elif "?" in command:
                    # SCPI query: trigger a bus read for the answer.
                    self.adapter.write("++read eoi")
                    time.sleep(0.1)
                    reply = (self.adapter.read() or "").strip()
                    self.q.put(("term", f"< {reply}" if reply
                                else "< (no response / timeout)"))
                # SCPI non-query commands produce no reply; nothing to read.
        except Exception as exc:
            self.q.put(("term", f"! Error: {exc}"))

    def _history_back(self, _event) -> str:
        if self.history and self.history_pos > 0:
            self.history_pos -= 1
            self.command_var.set(self.history[self.history_pos])
        return "break"

    def _history_forward(self, _event) -> str:
        if self.history_pos < len(self.history) - 1:
            self.history_pos += 1
            self.command_var.set(self.history[self.history_pos])
        else:
            self.history_pos = len(self.history)
            self.command_var.set("")
        return "break"

    # -- teardown ----------------------------------------------------------
    def shutdown(self) -> None:
        self.stop_event.set()
        super().shutdown()
        try:
            self.adapter.close()
        except Exception as exc:
            dprint(f"Error closing adapter: {exc}")


# ==========================================================================
# Database tab
# ==========================================================================

DB_COLUMNS = (
    ("kind", "Type", 90), ("where", "Port / address", 170),
    ("serial", "Adapter ID", 190), ("addr", "GPIB", 60),
    ("state", "State", 100), ("stb", "Status byte", 140),
    ("idn", "*IDN? response", 420), ("tc", "TC definition", 190),
    ("last_seen", "Last seen", 160),
)
TC_COLUMNS = ("tc",)

MATCH_COLUMNS = (
    ("id", "ID", 55), ("kind", "Type", 90), ("addr", "GPIB", 60),
    ("idn", "*IDN? response", 430), ("tc", "Matched definition", 260),
)


class DatabaseTab(QueuedFrame):
    """Two panes: the recorded instruments, and the TestController integration
    that annotates them. The integration is optional, so it gets its own
    sub-tab rather than crowding the records with settings most people never
    touch."""

    def __init__(self, app: "App"):
        super().__init__(app.notebook, padding=PAD)
        self.app = app
        self.config = load_config()
        self.pending_updates: list = []
        self.tc_enabled_var = tk.BooleanVar(value=bool(self.config.get("tc_enabled")))
        self.tc_path_var = tk.StringVar(value=self.config.get("tc_path", ""))

        self.inner = ttk.Notebook(self)
        self.inner.grid(row=0, column=0, sticky="nsew")
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        self._build_records_pane()
        self._build_testcontroller_pane()

        self.apply_tc_visibility()
        self.refresh()

    # -- records -----------------------------------------------------------
    def _build_records_pane(self) -> None:
        pane = ttk.Frame(self.inner, padding=PAD)
        self.inner.add(pane, text="Instruments")
        pane.columnconfigure(0, weight=1)
        pane.rowconfigure(1, weight=1)

        bar = ttk.Frame(pane)
        bar.grid(row=0, column=0, sticky="ew")
        ttk.Button(bar, text="Refresh", command=self.refresh).pack(side="left")
        ttk.Button(bar, text="Export CSV…",
                   command=lambda: self.app.export_csv(None)).pack(side="left", padx=(PAD, 0))
        ttk.Button(bar, text="Export JSON…",
                   command=lambda: self.app.export_json(None)).pack(side="left", padx=(PAD, 0))
        ttk.Button(bar, text="Edit label…",
                   command=self.edit_selected).pack(side="left", padx=(PAD, 0))
        ttk.Button(bar, text="Delete record(s)",
                   command=self.delete_selected).pack(side="left", padx=(PAD, 0))
        self.count_label = ttk.Label(bar, text="", foreground=MUTED)
        self.count_label.pack(side="right")

        table, self.tree = build_tree(pane, DB_COLUMNS, stretch=("idn",),
                                      selectmode="extended")
        table.grid(row=1, column=0, sticky="nsew", pady=(PAD, 0))
        self.tree.tag_configure("found", foreground=OK_FG)
        self.tree.tag_configure("missing", foreground="#666666")

    def refresh(self) -> None:
        self.tree.delete(*self.tree.get_children(""))
        rows = fetch_all_devices()
        for index, row in enumerate(rows):
            (kind, where, serial_num, addr, idn, status, last_seen, tc_config, stb) = row
            tag = "found" if (status or "").lower() == "found" else "missing"
            self.tree.insert("", "end", iid=str(index), tags=(tag,),
                             values=(kind or "", where or "", serial_num or "", addr,
                                     status or "", decode_status_byte(stb), idn or "",
                                     tc_config or "", last_seen or ""))
        live = sum(1 for r in rows if (r[5] or "").lower() == "found")
        self.count_label.configure(
            text=f"{len(rows)} record(s), {live} last seen present")

    def _selected_records(self):
        out = []
        for iid in self.tree.selection():
            out.append((self.tree.set(iid, "addr"), self.tree.set(iid, "serial")))
        return out

    def delete_selected(self) -> None:
        records = self._selected_records()
        if not records:
            messagebox.showwarning(APP_NAME, "Select at least one record to delete.")
            return
        count = len(records)
        if not messagebox.askyesno(
                "Confirm Delete",
                f"Delete the {count} selected record{'s' if count > 1 else ''}?"):
            return
        delete_device_records(records)
        self.refresh()
        for tab in self.app.adapter_tabs.values():
            tab.load_from_db()
        self.app.set_status(f"Deleted {count} record(s)")

    def edit_selected(self) -> None:
        """A nickname is stored in the tc_config_file column when the
        TestController feature is off, so a bench label survives without a
        second schema migration."""
        selection = self.tree.selection()
        if len(selection) != 1:
            messagebox.showinfo(APP_NAME, "Select exactly one record to edit.")
            return
        iid = selection[0]
        if self.tc_enabled_var.get():
            messagebox.showinfo(
                APP_NAME,
                "Turn TestController matching off before editing this field by hand - "
                "a definition scan would overwrite it.")
            return
        dialog = FieldDialog(self, "Edit record",
                             {"Label": self.tree.set(iid, "tc")})
        if not dialog.result:
            return
        addr, serial_num = self.tree.set(iid, "addr"), self.tree.set(iid, "serial")
        with sqlite3.connect(DB_NAME, timeout=DB_TIMEOUT) as conn:
            conn.execute("UPDATE devices SET tc_config_file = ? "
                         "WHERE gpib_address = ? AND adapter_serial = ?",
                         (dialog.result["Label"] or None, addr, serial_num))
            conn.commit()
        self.refresh()

    # -- TestController ----------------------------------------------------
    def _build_testcontroller_pane(self) -> None:
        pane = ttk.Frame(self.inner, padding=PAD)
        self.inner.add(pane, text="TestController")
        pane.columnconfigure(0, weight=1)
        pane.rowconfigure(1, weight=1)

        settings = ttk.LabelFrame(pane, text="Device definitions", padding=PAD)
        settings.grid(row=0, column=0, sticky="ew")
        settings.columnconfigure(2, weight=1)
        ttk.Checkbutton(settings, text="Match instruments to device definitions",
                        variable=self.tc_enabled_var,
                        command=self.toggle_testcontroller).grid(row=0, column=0, sticky="w")
        ttk.Label(settings, text="Install folder").grid(row=0, column=1, sticky="w",
                                                        padx=(PAD * 2, 4))
        self.tc_entry = ttk.Entry(settings, textvariable=self.tc_path_var)
        self.tc_entry.grid(row=0, column=2, sticky="ew")
        self.tc_browse_btn = ttk.Button(settings, text="Browse…", command=self.browse_tc_path)
        self.tc_browse_btn.grid(row=0, column=3, padx=(4, 0))
        self.tc_scan_btn = ttk.Button(settings, text="Match devices",
                                      command=self.match_devices)
        self.tc_scan_btn.grid(row=0, column=4, padx=(PAD, 0))
        ttk.Label(settings,
                  text="Nothing is written to the TestController folder. Definitions are "
                       "matched on #idString, which is compared against the *IDN? reply.",
                  foreground=MUTED).grid(row=1, column=0, columnspan=5, sticky="w",
                                         pady=(4, 0))
        self.tc_status = ttk.Label(settings, text="", wraplength=1100, justify="left",
                                   foreground=MUTED)
        self.tc_status.grid(row=2, column=0, columnspan=5, sticky="w", pady=(4, 0))

        table, self.match_tree = build_tree(pane, MATCH_COLUMNS, stretch=("idn",))
        table.grid(row=1, column=0, sticky="nsew", pady=PAD)
        self.match_tree.tag_configure("matched", foreground=OK_FG)
        self.match_tree.tag_configure("unmatched", foreground="#666666")

        actions = ttk.Frame(pane)
        actions.grid(row=2, column=0, sticky="ew")
        self.tc_accept_btn = ttk.Button(actions, text="Accept & update database",
                                        command=self.accept_matches, state="disabled")
        self.tc_accept_btn.pack(side="left")
        ttk.Button(actions, text="Discard preview",
                   command=self.discard_matches).pack(side="left", padx=(PAD, 0))
        ttk.Label(actions, text="Matches are a preview until you accept them.",
                  foreground=MUTED).pack(side="right")

        self.tc_log = LogPane(pane, height=6)
        self.tc_log.grid(row=3, column=0, sticky="nsew", pady=(PAD, 0))

    def apply_tc_visibility(self) -> None:
        """Hide the definition column in the records table when the feature is off."""
        enabled = self.tc_enabled_var.get()
        columns = [c[0] for c in DB_COLUMNS]
        if not enabled:
            columns = [c for c in columns if c not in TC_COLUMNS]
        self.tree.configure(displaycolumns=columns)
        state = "normal" if enabled else "disabled"
        for widget in (self.tc_entry, self.tc_browse_btn, self.tc_scan_btn):
            widget.configure(state=state)
        if not enabled:
            self.tc_accept_btn.configure(state="disabled")

    def toggle_testcontroller(self) -> None:
        enabled = self.tc_enabled_var.get()
        self.config["tc_enabled"] = enabled
        save_config(self.config)
        self.apply_tc_visibility()
        if enabled:
            self.tc_log.write("TestController matching on. Point at the install folder, "
                              "then press Match devices.")
        else:
            self.discard_matches()
            self.tc_status.configure(text="")
            self.tc_log.write("TestController matching off. Existing links stay in the "
                              "database.")
        self.refresh()

    def browse_tc_path(self) -> None:
        directory = filedialog.askdirectory(
            parent=self, title="Select the TestController installation directory")
        if not directory:
            return
        ok, problem = tc_validate_install(directory)
        if not ok:
            messagebox.showerror("Validation Error", problem, parent=self)
            return
        self.tc_path_var.set(directory)
        self.config["tc_path"] = directory
        save_config(self.config)
        self.tc_log.write(f"TestController install: {directory}")

    def match_devices(self) -> None:
        dev_path = tc_devices_path(self.tc_path_var.get())
        if not dev_path:
            messagebox.showerror(
                "Error", "Could not find a 'Devices' folder in the selected "
                         "TestController path.", parent=self)
            return
        self.match_tree.delete(*self.match_tree.get_children(""))
        self.pending_updates = []

        configs = tc_read_definitions(dev_path)
        self.tc_log.write(f"Read {len(configs)} definition file(s) from {dev_path}")

        try:
            devices = fetch_devices_for_matching()
        except sqlite3.Error as exc:
            messagebox.showerror("Database Error", str(exc), parent=self)
            return

        matched = 0
        for dev_id, kind, addr, idn in devices:
            idn = idn or "Unknown"
            filename = tc_match(idn, configs)
            if filename:
                matched += 1
            self.pending_updates.append((filename, dev_id))
            self.match_tree.insert(
                "", "end", iid=str(dev_id),
                tags=("matched" if filename else "unmatched",),
                values=(dev_id, kind or "", addr, idn, filename or "Not found"))

        self.tc_status.configure(
            text=f"{matched} of {len(devices)} record(s) matched a definition file. "
                 f"Nothing is saved until you accept.")
        self.tc_accept_btn.configure(state="normal" if self.pending_updates else "disabled")
        if not devices:
            self.tc_log.write("No instrument records in the database yet - scan a bus first.")

    def accept_matches(self) -> None:
        if not self.pending_updates:
            return
        batch_update_tc_configs(self.pending_updates)
        count = len(self.pending_updates)
        self.discard_matches()
        self.refresh()
        for tab in self.app.adapter_tabs.values():
            tab.load_from_db()
        self.tc_log.write(f"Wrote definition links for {count} record(s).")
        self.app.set_status("TestController links updated")

    def discard_matches(self) -> None:
        self.pending_updates = []
        self.match_tree.delete(*self.match_tree.get_children(""))
        self.tc_accept_btn.configure(state="disabled")

    def focus_testcontroller(self) -> None:
        self.inner.select(1)

    def on_event(self, kind: str, *args) -> None:
        if kind == "log":
            self.tc_log.write(args[0])


# ==========================================================================
# Root window
# ==========================================================================

class App(tk.Tk):
    def __init__(self, db_path: str):
        super().__init__()
        global DB_NAME
        DB_NAME = db_path
        init_db()

        self.title(f"{APP_NAME} - {APP_TITLE} {VERSION}")
        self.geometry("1280x800")
        self.minsize(980, 600)
        self.adapter_tabs: dict[str, AdapterTab] = {}

        try:
            style = ttk.Style(self)
            for preferred in ("clam", "vista", "aqua"):
                if preferred in style.theme_names():
                    style.theme_use(preferred)
                    break
            style.configure("Treeview", rowheight=22)
        except tk.TclError:
            pass

        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=PAD, pady=(PAD, 0))

        self.connections = ConnectionsTab(self)
        self.notebook.add(self.connections, text="Controllers")
        self.database_tab = DatabaseTab(self)
        self.notebook.add(self.database_tab, text="Database")

        self.status = ttk.Label(self, text=f"Database: {DB_NAME}", anchor="w",
                                relief="sunken", padding=(PAD, 2))
        self.status.pack(fill="x", side="bottom")

        self._build_menu()
        self.protocol("WM_DELETE_WINDOW", self.quit_app)
        self.bind("<Control-w>", lambda _e: self.close_current_tab())
        self.bind("<F5>", lambda _e: self.connections.start())

    def _build_menu(self) -> None:
        menu = tk.Menu(self)
        file_menu = tk.Menu(menu, tearoff=0)
        file_menu.add_command(label="Open database…", command=self.open_database)
        file_menu.add_separator()
        file_menu.add_command(label="Export JSON…", command=lambda: self.export_json(None))
        file_menu.add_command(label="Export CSV…", command=lambda: self.export_csv(None))
        file_menu.add_separator()
        file_menu.add_command(label="Quit", command=self.quit_app)
        menu.add_cascade(label="File", menu=file_menu)

        scan_menu = tk.Menu(menu, tearoff=0)
        scan_menu.add_command(label="Find controllers", accelerator="F5",
                              command=lambda: self.connections.start())
        scan_menu.add_command(label="Add controller by address…",
                              command=lambda: self.connections.add_manual())
        scan_menu.add_command(label="Scan the current bus",
                              command=self.scan_current_tab)
        scan_menu.add_separator()
        scan_menu.add_command(label="Close this tab", accelerator="Ctrl+W",
                              command=self.close_current_tab)
        menu.add_cascade(label="Scan", menu=scan_menu)

        tools_menu = tk.Menu(menu, tearoff=0)
        tools_menu.add_command(label="TestController integration",
                               command=self.open_testcontroller)
        menu.add_cascade(label="Tools", menu=tools_menu)

        help_menu = tk.Menu(menu, tearoff=0)
        help_menu.add_command(label="About", command=self.about)
        menu.add_cascade(label="Help", menu=help_menu)
        self.configure(menu=menu)

    # -- tabs --------------------------------------------------------------
    def open_adapter(self, adapter: PrologixAdapter) -> None:
        existing = self.adapter_tabs.get(adapter.serial_num)
        if existing is not None:
            adapter.close()
            self.notebook.select(existing)
            return
        tab = AdapterTab(self, adapter)
        self.adapter_tabs[adapter.serial_num] = tab
        self.notebook.add(tab, text=f"{adapter.adapter_type}: {adapter.port_or_ip}")
        self.notebook.select(tab)
        self.set_status(f"Connected to {adapter.port_or_ip}")

    def focus_adapter_tab(self, serial_num: str) -> None:
        tab = self.adapter_tabs.get(serial_num)
        if tab is not None:
            self.notebook.select(tab)

    def close_adapter_tab(self, serial_num: str) -> None:
        tab = self.adapter_tabs.pop(serial_num, None)
        if tab is None:
            return
        tab.shutdown()
        self.notebook.forget(tab)
        tab.destroy()
        self.connections.refresh()
        self.set_status("Controller closed")

    def close_current_tab(self) -> None:
        try:
            current = self.notebook.nametowidget(self.notebook.select())
        except tk.TclError:
            return
        for serial_num, tab in list(self.adapter_tabs.items()):
            if tab is current:
                self.close_adapter_tab(serial_num)
                return

    def current_adapter_tab(self) -> Optional[AdapterTab]:
        try:
            current = self.notebook.nametowidget(self.notebook.select())
        except tk.TclError:
            return None
        return current if isinstance(current, AdapterTab) else None

    def scan_current_tab(self) -> None:
        tab = self.current_adapter_tab()
        if tab is None:
            messagebox.showinfo(APP_NAME, "Open a controller tab first.")
            return
        tab.start_scan()

    def open_testcontroller(self) -> None:
        self.notebook.select(self.database_tab)
        self.database_tab.focus_testcontroller()

    def set_status(self, text: str) -> None:
        self.status.configure(text=f"{text}   ·   {DB_NAME}")

    # -- files -------------------------------------------------------------
    def open_database(self) -> None:
        global DB_NAME
        path = filedialog.asksaveasfilename(
            title="Open or create a scanner database", defaultextension=".db",
            initialfile="gpib_devices.db", confirmoverwrite=False,
            filetypes=[("SQLite", "*.db *.sqlite3 *.sqlite"), ("All files", "*.*")])
        if not path:
            return
        for serial_num in list(self.adapter_tabs):
            self.close_adapter_tab(serial_num)
        DB_NAME = path
        init_db()
        self.connections.live.clear()
        self.connections.refresh()
        self.database_tab.discard_matches()
        self.database_tab.refresh()
        self.set_status("Database opened")

    def export_csv(self, adapter_serial: Optional[str] = None) -> None:
        rows = fetch_all_devices(adapter_serial)
        if not rows:
            messagebox.showwarning("Export Empty", "No data available to export.")
            return
        path = filedialog.asksaveasfilename(
            title="Export records as CSV", defaultextension=".csv",
            initialfile="gpib-devices.csv",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")])
        if not path:
            return
        try:
            with open(path, "w", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                writer.writerow(["Adapter Type", "Connection Port/IP", "Adapter Serial",
                                 "GPIB Address", "IDN Response", "Status", "Last Seen",
                                 "TC Config File", "Status Byte"])
                writer.writerows(rows)
        except OSError as exc:
            messagebox.showerror("Export Error", f"Could not write {path}:\n{exc}")
            return
        self.set_status(f"Exported {len(rows)} record(s) to {path}")

    def export_json(self, adapter_serial: Optional[str] = None) -> None:
        rows = fetch_all_devices(adapter_serial)
        if not rows:
            messagebox.showwarning("Export Empty", "No data available to export.")
            return
        path = filedialog.asksaveasfilename(
            title="Export records as JSON", defaultextension=".json",
            initialfile="gpib-devices.json",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")])
        if not path:
            return
        keys = ["adapter_type", "connection_port", "adapter_serial", "gpib_address",
                "idn_response", "status", "last_seen", "tc_config_file", "status_byte"]
        data = []
        for row in rows:
            entry = dict(zip(keys, row))
            entry["status_byte_decoded"] = decode_status_byte(row[8])
            data.append(entry)
        try:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
        except OSError as exc:
            messagebox.showerror("Export Error", f"Could not write {path}:\n{exc}")
            return
        self.set_status(f"Exported {len(rows)} record(s) to {path}")

    def about(self) -> None:
        messagebox.showinfo(
            f"About {APP_NAME}",
            f"{APP_NAME} {VERSION} - {APP_TITLE}\n\n"
            "Finds Prologix GPIB-USB and GPIB-ETHERNET controllers, walks the GPIB\n"
            "bus behind each one, and keeps a SQLite record of what was found and\n"
            "when.\n\n"
            "Ethernet discovery uses the NetFinder UDP broadcast. Bus scans\n"
            "serial-poll every address first, then ask the responders for *IDN?,\n"
            "so pre-488.2 instruments are still detected.\n\n"
            "No VISA runtime required.")

    def quit_app(self) -> None:
        for serial_num in list(self.adapter_tabs):
            self.close_adapter_tab(serial_num)
        self.connections.shutdown()
        self.database_tab.shutdown()
        self.destroy()


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pos", description=f"{APP_NAME} - {APP_TITLE}")
    parser.add_argument("--db", default=DEFAULT_DB_PATH,
                        help="SQLite database to open (default: alongside the program)")
    parser.add_argument("--debug", action="store_true", help="verbose console output")
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {VERSION}")
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    global DEBUG_MODE
    DEBUG_MODE = DEBUG_MODE or args.debug

    dprint("Initializing Tkinter root window...")
    app = App(str(Path(args.db).expanduser()))
    dprint("Handing control to Tkinter mainloop.")
    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
