import os
import shutil
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import serial
import serial.tools.list_ports
import socket
import threading
import time
import sqlite3
import csv
import datetime
import sys
import json

__version__ = "1.0.0"

# ================= GLOBAL CONSTANTS =================
DB_NAME = "gpib_devices.db"
CONFIG_FILE = "scanner_config.json"
DB_TIMEOUT = 5.0  # Seconds to wait if database is locked by another thread
PROLOGIX_TCP_PORT = 1234
NETFINDER_UDP_PORT = 3040

# Check for command line debug flag
DEBUG_MODE = '--debug' in sys.argv

def dprint(*args, **kwargs):
    """Custom print function that only outputs if --debug flag is passed."""
    if DEBUG_MODE:
        print(*args, **kwargs)

# ================= CONFIGURATION SETUP =================

def load_config():
    """Loads application settings from a JSON file."""
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            dprint(f"Error loading config: {e}")
    
    # Default configuration
    return {
        "tc_enabled": False,
        "tc_path": ""
    }

def save_config(config):
    """Saves application settings to a JSON file."""
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=4)
    except Exception as e:
        dprint(f"Error saving config: {e}")

# ================= DATABASE SETUP =================

def init_db():
    """Initializes the SQLite database schema and handles migrations."""
    with sqlite3.connect(DB_NAME, timeout=DB_TIMEOUT) as conn:
        cursor = conn.cursor()
        cursor.execute('''
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
        ''')
        
        # Migration: Add tc_config_file column if it doesn't exist
        try:
            cursor.execute("ALTER TABLE devices ADD COLUMN tc_config_file TEXT")
        except sqlite3.OperationalError:
            pass # Column already exists
            
        conn.commit()

def upsert_devices_batch(devices_list):
    """Inserts or updates multiple device records in a single database transaction."""
    if not devices_list:
        return

    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    with sqlite3.connect(DB_NAME, timeout=DB_TIMEOUT) as conn:
        cursor = conn.cursor()
        for dev in devices_list:
            adapter_type, port, serial_num, gpib_addr, idn_response, status = dev
            
            cursor.execute('''
                SELECT id FROM devices 
                WHERE adapter_serial = ? AND gpib_address = ?
            ''', (serial_num, gpib_addr))
            row = cursor.fetchone()
            
            if row:
                cursor.execute('''
                    UPDATE devices 
                    SET idn_response = ?, status = ?, last_seen = ?, adapter_type = ?, connection_port = ?
                    WHERE id = ?
                ''', (idn_response, status, now, adapter_type, port, row[0]))
            else:
                cursor.execute('''
                    INSERT INTO devices (adapter_type, connection_port, adapter_serial, gpib_address, idn_response, status, last_seen)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                ''', (adapter_type, port, serial_num, gpib_addr, idn_response, status, now))
        conn.commit()

def mark_missing_devices(serial_num, found_addresses):
    """Marks previously discovered devices as 'NotFound' if they didn't answer this scan."""
    with sqlite3.connect(DB_NAME, timeout=DB_TIMEOUT) as conn:
        cursor = conn.cursor()
        if found_addresses:
            placeholders = ','.join('?' for _ in found_addresses)
            query = f'''
                UPDATE devices 
                SET status = 'NotFound' 
                WHERE adapter_serial = ? AND gpib_address NOT IN ({placeholders})
            '''
            params = [serial_num] + found_addresses
            cursor.execute(query, params)
        else:
            cursor.execute("UPDATE devices SET status = 'NotFound' WHERE adapter_serial = ?", (serial_num,))
        conn.commit()

def fetch_all_devices(adapter_serial=None):
    """Fetches devices, optionally filtered by a specific adapter serial."""
    with sqlite3.connect(DB_NAME, timeout=DB_TIMEOUT) as conn:
        cursor = conn.cursor()
        if adapter_serial:
            cursor.execute("SELECT adapter_type, connection_port, adapter_serial, gpib_address, idn_response, status, last_seen, tc_config_file FROM devices WHERE adapter_serial = ?", (adapter_serial,))
        else:
            cursor.execute("SELECT adapter_type, connection_port, adapter_serial, gpib_address, idn_response, status, last_seen, tc_config_file FROM devices")
        return cursor.fetchall()

def delete_device_records(records):
    """Deletes specific device records from the DB using a batch execution."""
    if not records: 
        return
    with sqlite3.connect(DB_NAME, timeout=DB_TIMEOUT) as conn:
        cursor = conn.cursor()
        cursor.executemany("DELETE FROM devices WHERE gpib_address = ? AND adapter_serial = ?", records)
        conn.commit()

def batch_update_tc_configs(updates):
    """Executes a batch update of TestController matched configs."""
    if not updates: return
    with sqlite3.connect(DB_NAME, timeout=DB_TIMEOUT) as conn:
        cursor = conn.cursor()
        cursor.executemany("UPDATE devices SET tc_config_file = ? WHERE id = ?", updates)
        conn.commit()

# ================= ADAPTER CLASSES =================

class PrologixAdapter:
    def __init__(self, port_or_ip, adapter_type, serial_num):
        self.port_or_ip = port_or_ip
        self.adapter_type = adapter_type
        self.serial_num = serial_num
    def write(self, command): pass
    def read(self): pass
    def close(self): pass

class USBAdapter(PrologixAdapter):
    def __init__(self, port, serial_num):
        super().__init__(port, "USB", serial_num)
        self.ser = serial.Serial(port, baudrate=9600, timeout=0.5)
        
    def write(self, command):
        try:
            self.ser.write((command + '\n').encode('ascii'))
        except serial.SerialException as e:
            dprint(f"USB Write Error: {e}")
        
    def read(self):
        try:
            return self.ser.readline().decode('ascii').strip()
        except serial.SerialException:
            return ""
        
    def close(self):
        try:
            if self.ser and self.ser.is_open:
                self.ser.close()
                dprint(f"Closed USB port {self.port_or_ip}")
        except Exception as e: 
            dprint(f"Error closing USB port {self.port_or_ip}: {e}")

class EthernetAdapter(PrologixAdapter):
    def __init__(self, ip, mac_address):
        super().__init__(ip, "Ethernet", mac_address)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(0.5)
        self.sock.connect((ip, PROLOGIX_TCP_PORT))
        
    def write(self, command):
        try:
            self.sock.sendall((command + '\n').encode('ascii'))
        except (socket.timeout, TimeoutError, OSError):
            pass
            
    def read(self):
        try:
            data = b""
            while True:
                chunk = self.sock.recv(1024)
                if not chunk:
                    break
                data += chunk
                if b'\n' in data or b'\r' in data:
                    break
            return data.decode('ascii', errors='ignore').strip()
            
        except (socket.timeout, TimeoutError, OSError):
            return ""
            
    def close(self):
        try:
            if self.sock:
                self.sock.close()
                dprint(f"Closed socket for {self.port_or_ip}")
        except Exception as e: 
            dprint(f"Error closing socket {self.port_or_ip}: {e}")

# ================= TESTCONTROLLER INTEGRATION =================

class TestControllerIntegration(tk.Toplevel):
    def __init__(self, parent, parent_app=None):
        super().__init__(parent)
        self.title("TestController Integration")
        self.geometry("950x650")
        self.parent_app = parent_app
        
        # Muted background
        self.configure(bg="#f4f6f9")
        
        self.config = load_config()
        self.tc_path = tk.StringVar(value=self.config.get("tc_path", ""))
        self.integration_enabled = tk.BooleanVar(value=self.config.get("tc_enabled", False))
        self.pending_updates = [] 
        
        self.build_ui()
        self.apply_ui_state()

    def build_ui(self):
        settings_frame = ttk.LabelFrame(self, text="Integration Settings", padding=15)
        settings_frame.pack(fill=tk.X, padx=20, pady=20)

        ttk.Checkbutton(
            settings_frame, 
            text="Enable TestController Integration", 
            variable=self.integration_enabled,
            command=self.toggle_integration
        ).grid(row=0, column=0, sticky=tk.W, columnspan=3, pady=5)

        ttk.Label(settings_frame, text="TestController Path:").grid(row=1, column=0, sticky=tk.W, pady=5)
        self.path_entry = ttk.Entry(settings_frame, textvariable=self.tc_path, width=65, state='disabled')
        self.path_entry.grid(row=1, column=1, padx=10, pady=5)
        
        self.browse_btn = ttk.Button(settings_frame, text="Browse...", command=self.browse_path, state='disabled')
        self.browse_btn.grid(row=1, column=2, pady=5)

        self.scan_btn = ttk.Button(settings_frame, text="Match Discovered Devices", command=self.match_devices, state='disabled')
        self.scan_btn.grid(row=2, column=0, columnspan=3, pady=15)

        list_frame = ttk.LabelFrame(self, text="Discovered Devices & Matching TC Configs (Preview)", padding=15)
        list_frame.pack(fill=tk.BOTH, expand=True, padx=20, pady=5)

        columns = ("ID", "Adapter", "GPIB Addr", "IDN Response", "TC Config File")
        self.tree = ttk.Treeview(list_frame, columns=columns, show="headings")
        
        # Setup Zebra Striping with softer contrast
        self.tree.tag_configure('evenrow', background='#ebedf0')
        self.tree.tag_configure('oddrow', background='#f4f6f9')
        
        self.tree.heading("ID", text="ID")
        self.tree.heading("Adapter", text="Adapter")
        self.tree.heading("GPIB Addr", text="GPIB Addr")
        self.tree.heading("IDN Response", text="IDN Response")
        self.tree.heading("TC Config File", text="TC Config File")

        self.tree.column("ID", width=50, anchor=tk.CENTER)
        self.tree.column("Adapter", width=120)
        self.tree.column("GPIB Addr", width=100, anchor=tk.CENTER)
        self.tree.column("IDN Response", width=300)
        self.tree.column("TC Config File", width=250)

        scrollbar = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscroll=scrollbar.set)
        
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        
        action_frame = ttk.Frame(self, padding=20)
        action_frame.pack(fill=tk.X)
        
        self.accept_btn = ttk.Button(action_frame, text="Accept & Update DB", command=self.accept_matches, state='disabled')
        self.accept_btn.pack(side=tk.RIGHT, padx=5)
        
        self.cancel_btn = ttk.Button(action_frame, text="Ignore & Close", command=self.cancel_matches)
        self.cancel_btn.pack(side=tk.RIGHT, padx=5)

    def apply_ui_state(self):
        state = 'normal' if self.integration_enabled.get() else 'disabled'
        self.path_entry.config(state=state)
        self.browse_btn.config(state=state)
        self.scan_btn.config(state=state)
        if not self.integration_enabled.get():
            self.accept_btn.config(state='disabled')

    def toggle_integration(self):
        self.apply_ui_state()
        self.config["tc_enabled"] = self.integration_enabled.get()
        save_config(self.config)

    def browse_path(self):
        directory = filedialog.askdirectory(parent=self, title="Select TestController Installation Directory")
        if directory:
            if not os.path.isdir(directory):
                messagebox.showerror("Validation Error", "The selected path is not a valid directory.", parent=self)
                return

            has_jar = any(f.lower() == "testcontroller.jar" for f in os.listdir(directory))
            has_devices = os.path.isdir(os.path.join(directory, "Devices")) or os.path.isdir(os.path.join(directory, "devices"))

            if has_jar and has_devices:
                self.tc_path.set(directory)
                self.config["tc_path"] = directory
                save_config(self.config)
            else:
                err_msg = "Selected directory is missing required files:\n\n"
                if not has_jar: err_msg += "- TestController.jar not found\n"
                if not has_devices: err_msg += "- 'Devices' folder not found"
                messagebox.showerror("Validation Error", err_msg, parent=self)

    def get_tc_devices_path(self):
        base_path = self.tc_path.get()
        if not base_path:
            return None
        for folder_name in ["Devices", "devices"]:
            dev_path = os.path.join(base_path, folder_name)
            if os.path.isdir(dev_path):
                return dev_path
        return None

    def match_devices(self):
        dev_path = self.get_tc_devices_path()
        if not dev_path:
            messagebox.showerror("Error", "Could not find 'Devices' folder in the selected TestController path.", parent=self)
            return

        for item in self.tree.get_children():
            self.tree.delete(item)

        tc_configs = {}
        for filename in os.listdir(dev_path):
            if filename.endswith(".txt"):
                filepath = os.path.join(dev_path, filename)
                tc_configs[filename] = [] 
                try:
                    with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                        for line in f:
                            line_stripped = line.strip()
                            if line_stripped.lower().startswith("#idstring"):
                                id_string = line_stripped[9:].strip().strip(",")
                                tc_configs[filename].append(id_string)
                except Exception:
                    continue

        try:
            with sqlite3.connect(DB_NAME, timeout=DB_TIMEOUT) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT id, adapter_type, gpib_address, idn_response FROM devices")
                devices = cursor.fetchall()
            
            self.pending_updates = []
            for i, dev in enumerate(devices):
                dev_id, adapter, gpib, idn = dev
                idn = idn if idn else "Unknown"
                matched_file = "Not Found"
                
                for filename, id_strings in tc_configs.items():
                    file_matched = False
                    for id_string in id_strings:
                        id_parts = [part.strip().lower() for part in id_string.split(",") if part.strip()]
                        idn_lower = idn.lower()
                        if id_parts and all(part in idn_lower for part in id_parts):
                            matched_file = filename
                            file_matched = True
                            break
                    if file_matched: break 

                self.pending_updates.append((matched_file if matched_file != "Not Found" else None, dev_id))
                
                # Apply Zebra Striping tags
                tag = 'evenrow' if i % 2 == 0 else 'oddrow'
                self.tree.insert("", tk.END, values=(dev_id, adapter, gpib, idn, matched_file), tags=(tag,))
            
            if self.pending_updates:
                self.accept_btn.config(state='normal')
                messagebox.showinfo("Scan Complete", "Devices matched. Please review the list and click 'Accept & Update DB' to save changes.", parent=self)
            else:
                messagebox.showinfo("Scan Complete", "No devices found in the database to match.", parent=self)
            
        except sqlite3.Error as e:
            messagebox.showerror("Database Error", str(e), parent=self)

    def accept_matches(self):
        if self.pending_updates:
            batch_update_tc_configs(self.pending_updates)
            if self.parent_app:
                self.parent_app.refresh_db_view()
            messagebox.showinfo("Success", "Database updated successfully with matched configuration files.", parent=self)
        self.destroy()

    def cancel_matches(self):
        self.destroy()


# ================= MAIN GUI APPLICATION =================

class PrologixMultiScannerApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Prologix OpenGPIB Scanner")
        self.root.geometry("1150x800") 
        
        self.apply_modern_flat_styling()
        
        self.active_adapters = []
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)
        
        self.usb_serials = {}
        self.eth_macs = {}
        
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=10, pady=10)
        
        init_db()
        self.setup_connection_tab()
        self.setup_db_manager_tab()
        self.scan_com_ports()

    def apply_modern_flat_styling(self):
        """Applies a clean, modern Flat UI aesthetic with muted, low-contrast colors."""
        self.style = ttk.Style(self.root)
        
        if 'clam' in self.style.theme_names():
            self.style.theme_use('clam')
            
        # --- Lower-Contrast Color Palette ---
        bg_color = "#f4f6f9"         # Muted off-white background
        surface_color = "#e2e6ea"    # Soft grey for headers/stripes
        border_color = "#cbd3da"     # Muted grey for borders
        primary = "#5b7c99"          # Muted slate/steel blue
        primary_hover = "#4a6882"    # Darker slate for active states
        text_dark = "#343a40"        # Softer dark-gray text (less harsh than black)
        
        self.root.configure(bg=bg_color)
        
        app_font = ("Segoe UI", 11)
        bold_font = ("Segoe UI", 11, "bold")
        title_font = ("Segoe UI", 12, "bold")
        
        # Base configuration
        self.style.configure(".", font=app_font, background=bg_color, foreground=text_dark)
        
        # Notebook & Tabs
        self.style.configure("TNotebook", background=bg_color, borderwidth=0)
        self.style.configure("TNotebook.Tab", font=bold_font, padding=[20, 10], 
                             background=surface_color, borderwidth=1, bordercolor=border_color, 
                             foreground="#6c757d")
        self.style.map("TNotebook.Tab", 
                       background=[("selected", primary)], 
                       foreground=[("selected", "#ffffff")])
                       
        # Containers (LabelFrames)
        self.style.configure("TLabelframe", background=bg_color, borderwidth=1, bordercolor=border_color, relief="solid")
        self.style.configure("TLabelframe.Label", font=title_font, background=bg_color, foreground=primary, padding=[10, 0])
        
        # Flat Action Buttons
        self.style.configure("TButton", font=bold_font, padding=[12, 8], 
                             background=primary, foreground="white", 
                             lightcolor=primary, darkcolor=primary, bordercolor=primary_hover, borderwidth=1)
        self.style.map("TButton", 
                       background=[("active", primary_hover), ("disabled", "#d1d5db")],
                       lightcolor=[("active", primary_hover), ("disabled", "#d1d5db")],
                       darkcolor=[("active", primary_hover), ("disabled", "#d1d5db")],
                       bordercolor=[("active", primary_hover), ("disabled", border_color)],
                       foreground=[("disabled", "#8a949e")])
                       
        # Treeview (Data Tables)
        self.style.configure("Treeview", font=app_font, rowheight=35, borderwidth=1, bordercolor=border_color)
        self.style.configure("Treeview.Heading", font=bold_font, background=surface_color, foreground=text_dark, padding=8, borderwidth=1, bordercolor=border_color)
        self.style.map("Treeview", background=[("selected", primary)], foreground=[("selected", "white")])
        
        # Inputs
        self.style.configure("TCombobox", padding=6)
        self.style.configure("TEntry", padding=6)

    def on_closing(self):
        dprint("Shutting down... releasing resources.")
        for adapter in self.active_adapters:
            adapter.close()
        self.root.destroy()

    def setup_connection_tab(self):
        conn_frame = ttk.Frame(self.notebook)
        self.notebook.add(conn_frame, text="Connections Manager")
        
        lbl_frame = ttk.LabelFrame(conn_frame, text="1. Discover & Connect", padding=20)
        lbl_frame.pack(fill="x", padx=20, pady=20)

        # USB
        ttk.Label(lbl_frame, text="USB COM Ports:").grid(row=0, column=0, padx=10, pady=15, sticky="e")
        self.com_combo = ttk.Combobox(lbl_frame, state="readonly", width=45)
        self.com_combo.grid(row=0, column=1, padx=10, pady=15)
        
        ttk.Button(lbl_frame, text="Refresh Ports", command=self.scan_com_ports).grid(row=0, column=2, padx=10, pady=15)
        ttk.Button(lbl_frame, text="Connect & Create Tab", command=self.connect_usb).grid(row=0, column=3, padx=10, pady=15)

        # Ethernet
        ttk.Label(lbl_frame, text="Ethernet IP:").grid(row=1, column=0, padx=10, pady=15, sticky="e")
        self.eth_combo = ttk.Combobox(lbl_frame, width=45)
        self.eth_combo.grid(row=1, column=1, padx=10, pady=15)
        
        self.btn_discover_eth = ttk.Button(lbl_frame, text="Discover (NetFinder)", command=self.start_netfinder_discovery)
        self.btn_discover_eth.grid(row=1, column=2, padx=10, pady=15)
        self.btn_connect_eth = ttk.Button(lbl_frame, text="Connect & Create Tab", command=self.connect_eth)
        self.btn_connect_eth.grid(row=1, column=3, padx=10, pady=15)

    def setup_db_manager_tab(self):
        self.db_frame = ttk.Frame(self.notebook)
        self.notebook.add(self.db_frame, text="Database Manager")
        
        btn_frame = ttk.Frame(self.db_frame, padding=10)
        btn_frame.pack(fill="x", padx=10, pady=10)
        
        ttk.Button(btn_frame, text="Refresh View", command=self.refresh_db_view).pack(side="left", padx=5)
        ttk.Button(btn_frame, text="Export All to CSV", command=lambda: self.export_csv(None)).pack(side="left", padx=5)
        
        # Updated to reflect multi-select functionality
        ttk.Button(btn_frame, text="Delete Selected Record(s)", command=self.delete_db_record).pack(side="left", padx=5)
        ttk.Button(btn_frame, text="TestController Integration", command=self.open_tc_integration).pack(side="left", padx=5)
        
        columns = ("Type", "Port/IP", "Adapter Serial", "GPIB", "IDN Response", "Status", "Last Seen", "TC Config")
        self.db_tree = ttk.Treeview(self.db_frame, columns=columns, show="headings")
        
        # Apply softer Zebra Striping Tags
        self.db_tree.tag_configure('evenrow', background='#ebedf0')
        self.db_tree.tag_configure('oddrow', background='#f4f6f9')
        
        for col in columns:
            self.db_tree.heading(col, text=col)
            self.db_tree.column(col, width=120)
            
        self.db_tree.column("IDN Response", width=250)
        self.db_tree.column("TC Config", width=200)
        self.db_tree.column("GPIB", width=60, anchor="center")
        self.db_tree.pack(fill="both", expand=True, padx=20, pady=10)
        
        self.refresh_db_view()

    def open_tc_integration(self):
        TestControllerIntegration(self.root, parent_app=self)

    def refresh_db_view(self):
        for item in self.db_tree.get_children():
            self.db_tree.delete(item)
        rows = fetch_all_devices()
        for i, row in enumerate(rows):
            tag = 'evenrow' if i % 2 == 0 else 'oddrow'
            self.db_tree.insert("", tk.END, values=row, tags=(tag,))

    def delete_db_record(self):
        """Allows deletion of one or multiple selected records from the database view."""
        selected_items = self.db_tree.selection()
        
        if not selected_items:
            messagebox.showwarning("Warning", "Please select at least one record to delete.")
            return
            
        count = len(selected_items)
        prompt_msg = f"Are you sure you want to delete the {count} selected record{'s' if count > 1 else ''}?"
        
        if messagebox.askyesno("Confirm Delete", prompt_msg):
            records_to_delete = []
            for item_id in selected_items:
                values = self.db_tree.item(item_id)['values']
                # values[3] is GPIB Address, values[2] is Adapter Serial
                records_to_delete.append((values[3], values[2]))
                
            delete_device_records(records_to_delete)
            self.refresh_db_view()

    # --- Discovery Methods ---

    def scan_com_ports(self):
        ports = serial.tools.list_ports.comports()
        port_list = []
        self.usb_serials.clear()
        
        for port in ports:
            desc = f"{port.device} - {port.description}"
            port_list.append(desc)
            serial_num = port.serial_number if port.serial_number else f"Unknown_USB_{port.device}"
            self.usb_serials[port.device] = serial_num
            
        self.com_combo['values'] = port_list
        if port_list:
            self.com_combo.current(0)
        else:
            self.com_combo.set("No COM ports found")

    def start_netfinder_discovery(self):
        self.eth_combo.set("Discovering... (Wait 5s)")
        self.btn_discover_eth.config(state="disabled")
        self.btn_connect_eth.config(state="disabled")
        threading.Thread(target=self.udp_discover_thread, daemon=True).start()

    def udp_discover_thread(self):
        dprint("\n--- STARTING NETFINDER UDP DISCOVERY ---")
        discovered_options = []
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.settimeout(5.0)
            
            discovery_packet = bytes.fromhex("5A 00 5A 9E FF FF FF FF FF FF 00 00")
            
            try: sock.sendto(discovery_packet, ('255.255.255.255', NETFINDER_UDP_PORT))
            except: pass
            
            try:
                hostname = socket.gethostname()
                _, _, ips = socket.gethostbyname_ex(hostname)
                for ip in ips:
                    parts = ip.split('.')
                    if len(parts) == 4:
                        subnet_bcast = f"{parts[0]}.{parts[1]}.{parts[2]}.255"
                        try: sock.sendto(discovery_packet, (subnet_bcast, NETFINDER_UDP_PORT))
                        except: pass
            except: pass

            while True:
                try:
                    data, addr = sock.recvfrom(1024)
                    if len(data) >= 24 and data.startswith(b"\x5a\x01\x5a\x9e"):
                        mac_bytes = data[4:10]
                        mac_str = ':'.join(f'{b:02X}' for b in mac_bytes)
                        ip_bytes = data[20:24]
                        ip_str = socket.inet_ntoa(ip_bytes)
                        
                        self.eth_macs[ip_str] = mac_str
                        display_str = f"{ip_str} (MAC: {mac_str})"
                        
                        if display_str not in discovered_options:
                            discovered_options.append(display_str)
                except socket.timeout: break 
                except: break
        finally:
            sock.close()
            
        if self.root.winfo_exists():
            self.root.after(0, lambda: self.finish_netfinder_discovery(discovered_options))

    def finish_netfinder_discovery(self, options):
        self.btn_discover_eth.config(state="normal")
        self.btn_connect_eth.config(state="normal")
        if options:
            self.eth_combo['values'] = options
            self.eth_combo.current(0)
            messagebox.showinfo("NetFinder", f"Found {len(options)} adapter(s).")
        else:
            self.eth_combo['values'] = []
            self.eth_combo.set("No Prologix adapters found.")

    # --- Connection & Tab Creation Methods ---

    def connect_usb(self):
        selection = self.com_combo.get()
        if not selection or "No COM ports" in selection:
            messagebox.showerror("Error", "Please select a valid COM port.")
            return
        port = selection.split(" - ")[0]
        serial_num = self.usb_serials.get(port, f"Unknown_{port}")
        
        try:
            # Instantiate adapter silently
            adapter = USBAdapter(port, serial_num)
            
            # --- Hardware Validation Step ---
            # Send the ++ver command to check if it's genuinely a Prologix controller
            adapter.write("++ver")
            time.sleep(0.2)
            version_response = adapter.read()
            
            if "Prologix" not in version_response:
                adapter.close()
                messagebox.showerror(
                    "Hardware Validation Failed", 
                    f"The device on {port} did not respond as a Prologix controller.\n\n"
                    f"Expected 'Prologix' in response.\nReceived: '{version_response}'"
                )
                return
            # --- End Validation ---

            self.active_adapters.append(adapter)
            self.create_adapter_tab(adapter)
            
        except Exception as e:
            messagebox.showerror("Connection Error", str(e))

    def connect_eth(self):
        selection = self.eth_combo.get().strip()
        if not selection or "Discovering" in selection or "No Prologix" in selection:
            messagebox.showerror("Error", "Please enter or select a valid IP address.")
            return
        ip = selection.split(" ")[0]
        mac_addr = self.eth_macs.get(ip, f"Manual_{ip}")
        
        try:
            adapter = EthernetAdapter(ip, mac_addr)
            
            # --- Hardware Validation Step ---
            adapter.write("++ver")
            time.sleep(0.2)
            version_response = adapter.read()
            
            if "Prologix" not in version_response:
                adapter.close()
                messagebox.showerror(
                    "Hardware Validation Failed", 
                    f"The device at {ip} did not respond as a Prologix controller.\n\n"
                    f"Expected 'Prologix' in response.\nReceived: '{version_response}'"
                )
                return
            # --- End Validation ---

            self.active_adapters.append(adapter)
            self.create_adapter_tab(adapter)
            
        except Exception as e:
            messagebox.showerror("Connection Error", f"Failed to connect to {ip}:{PROLOGIX_TCP_PORT}\n{str(e)}")

    def create_adapter_tab(self, adapter):
        tab_name = f"{adapter.adapter_type}: {adapter.port_or_ip}"
        tab_frame = ttk.Frame(self.notebook)
        self.notebook.add(tab_frame, text=tab_name)
        self.notebook.select(tab_frame)
        
        header = ttk.Frame(tab_frame)
        header.pack(fill="x", padx=15, pady=15)
        ttk.Label(header, text=f"Type: {adapter.adapter_type}   |   Address: {adapter.port_or_ip}   |   ID: {adapter.serial_num}", font=("Segoe UI", 12, "bold"), foreground="#5b7c99").pack(side="left")

        inner_notebook = ttk.Notebook(tab_frame)
        inner_notebook.pack(fill="both", expand=True, padx=10, pady=10)

        scan_frame = ttk.Frame(inner_notebook)
        config_frame = ttk.Frame(inner_notebook)
        inner_notebook.add(scan_frame, text="Device Scanner")
        inner_notebook.add(config_frame, text="Adapter Configuration (Optional)")

        # ================= SCANNER UI =================
        ttk.Label(scan_frame, text="Note: Valid scans require the adapter to be configured as Mode: Controller, Auto: Disable, EOS: None.", foreground="#a85c5c").pack(pady=10)
        
        ctrl_frame = ttk.Frame(scan_frame)
        ctrl_frame.pack(fill="x", padx=20, pady=10)
        
        btn_scan = ttk.Button(ctrl_frame, text="Scan GPIB Bus")
        btn_scan.pack(side="left", padx=5)
        
        ttk.Button(ctrl_frame, text="Export CSV", command=lambda a=adapter: self.export_csv(a.serial_num)).pack(side="left", padx=5)
        progress = ttk.Progressbar(ctrl_frame, orient="horizontal", length=400, mode="determinate")
        progress.pack(side="left", padx=25, pady=5)
        
        columns = ("Type", "Port", "Serial", "GPIB", "IDN Response", "Status", "Last Seen")
        tree = ttk.Treeview(scan_frame, columns=columns, show="headings")
        
        # Setup Zebra Striping
        tree.tag_configure('evenrow', background='#ebedf0')
        tree.tag_configure('oddrow', background='#f4f6f9')
        
        for col in columns: tree.heading(col, text=col); tree.column(col, width=120)
        tree.column("IDN Response", width=250); tree.column("GPIB", width=60, anchor="center")
        tree.pack(fill="both", expand=True, padx=20, pady=10)
        
        self.populate_adapter_tree(tree, adapter.serial_num)

        # ================= CONFIGURATION UI =================
        grp1 = ttk.LabelFrame(config_frame, text="Operating Mode & General", padding=15)
        grp1.pack(fill="x", padx=20, pady=10)
        
        ttk.Label(grp1, text="Mode (++mode):").grid(row=0, column=0, padx=10, pady=8, sticky="e")
        cb_mode = ttk.Combobox(grp1, values=["0 (Device)", "1 (Controller)"], state="readonly", width=18)
        cb_mode.grid(row=0, column=1, padx=10, pady=8, sticky="w")
        
        ttk.Label(grp1, text="Read-After-Write (++auto):").grid(row=0, column=2, padx=10, pady=8, sticky="e")
        cb_auto = ttk.Combobox(grp1, values=["0 (Disable)", "1 (Enable)"], state="readonly", width=18)
        cb_auto.grid(row=0, column=3, padx=10, pady=8, sticky="w")
        
        ttk.Label(grp1, text="Listen-Only (++lon):").grid(row=1, column=0, padx=10, pady=8, sticky="e")
        cb_lon = ttk.Combobox(grp1, values=["0 (Disable)", "1 (Enable)"], state="readonly", width=18)
        cb_lon.grid(row=1, column=1, padx=10, pady=8, sticky="w")
        
        ttk.Label(grp1, text="Save Config (++savecfg):").grid(row=1, column=2, padx=10, pady=8, sticky="e")
        cb_savecfg = ttk.Combobox(grp1, values=["0 (Disable)", "1 (Enable)"], state="readonly", width=18)
        cb_savecfg.grid(row=1, column=3, padx=10, pady=8, sticky="w")

        grp2 = ttk.LabelFrame(config_frame, text="Formatting & Timeouts", padding=15)
        grp2.pack(fill="x", padx=20, pady=10)
        
        ttk.Label(grp2, text="Terminator (++eos):").grid(row=0, column=0, padx=10, pady=8, sticky="e")
        cb_eos = ttk.Combobox(grp2, values=["0 (CR+LF)", "1 (CR)", "2 (LF)", "3 (None)"], state="readonly", width=18)
        cb_eos.grid(row=0, column=1, padx=10, pady=8, sticky="w")
        
        ttk.Label(grp2, text="Assert EOI (++eoi):").grid(row=0, column=2, padx=10, pady=8, sticky="e")
        cb_eoi = ttk.Combobox(grp2, values=["0 (Disable)", "1 (Enable)"], state="readonly", width=18)
        cb_eoi.grid(row=0, column=3, padx=10, pady=8, sticky="w")
        
        ttk.Label(grp2, text="Timeout ms (++read_tmo_ms):").grid(row=1, column=0, padx=10, pady=8, sticky="e")
        ent_tmo = ttk.Entry(grp2, width=21)
        ent_tmo.grid(row=1, column=1, padx=10, pady=8, sticky="w")
        
        ttk.Label(grp2, text="EOT Enable (++eot_enable):").grid(row=2, column=0, padx=10, pady=8, sticky="e")
        cb_eoten = ttk.Combobox(grp2, values=["0 (Disable)", "1 (Enable)"], state="readonly", width=18)
        cb_eoten.grid(row=2, column=1, padx=10, pady=8, sticky="w")
        
        ttk.Label(grp2, text="EOT Char (++eot_char):").grid(row=2, column=2, padx=10, pady=8, sticky="e")
        ent_eotchar = ttk.Entry(grp2, width=21)
        ent_eotchar.grid(row=2, column=3, padx=10, pady=8, sticky="w")

        grp3 = ttk.LabelFrame(config_frame, text="Advanced Actions (Immediate)", padding=15)
        grp3.pack(fill="x", padx=20, pady=10)
        ttk.Button(grp3, text="Interface Clear (++ifc)", command=lambda a=adapter: self.run_action_cmd(a, "++ifc", "Interface Clear Sent")).grid(row=0, column=0, padx=10, pady=5)
        ttk.Button(grp3, text="Device Clear (++clr)", command=lambda a=adapter: self.run_action_cmd(a, "++clr", "Device Clear Sent")).grid(row=0, column=1, padx=10, pady=5)
        ttk.Button(grp3, text="Local Lockout (++llo)", command=lambda a=adapter: self.run_action_cmd(a, "++llo", "Local Lockout Sent")).grid(row=0, column=2, padx=10, pady=5)
        ttk.Button(grp3, text="Go to Local (++loc)", command=lambda a=adapter: self.run_action_cmd(a, "++loc", "Go to Local Sent")).grid(row=0, column=3, padx=10, pady=5)

        grp_ctrl = ttk.Frame(config_frame, padding=10)
        grp_ctrl.pack(fill="x", padx=10, pady=15)

        lbl_cfg_status = ttk.Label(grp_ctrl, text="Status: Ready", font=("Segoe UI", 10, "italic"), foreground="#8a949e")
        lbl_cfg_status.pack(side="bottom", pady=15)

        config_widgets = {
            "++mode": cb_mode, "++auto": cb_auto, "++eos": cb_eos, "++eoi": cb_eoi,
            "++eot_enable": cb_eoten, "++eot_char": ent_eotchar, "++read_tmo_ms": ent_tmo,
            "++lon": cb_lon, "++savecfg": cb_savecfg
        }

        btn_scan.config(command=lambda a=adapter, cw=config_widgets: self.run_bus_scan(a, tree, progress, btn_scan))

        ttk.Button(grp_ctrl, text="Read from Adapter", command=lambda a=adapter, cw=config_widgets, sl=lbl_cfg_status: self.run_read_config(a, cw, sl)).pack(side="left", padx=5)
        ttk.Button(grp_ctrl, text="Apply Configuration", command=lambda a=adapter, cw=config_widgets, sl=lbl_cfg_status: self.run_apply_config(a, cw, sl)).pack(side="left", padx=5)
        ttk.Button(grp_ctrl, text="Set Scanner Defaults", command=lambda cw=config_widgets: self.set_scanner_defaults(cw)).pack(side="left", padx=5)
        
        ttk.Button(grp_ctrl, text="Reset Adapter (++rst)", command=lambda a=adapter: self.run_action_cmd(a, "++rst", "Adapter Reset Sequence Initiated")).pack(side="right", padx=5)
        ttk.Button(grp_ctrl, text="Get Version (++ver)", command=lambda a=adapter: self.get_version(a)).pack(side="right", padx=5)

        self.run_read_config(adapter, config_widgets, lbl_cfg_status)

    # --- Configuration Methods ---

    def run_read_config(self, adapter, config_widgets, status_lbl):
        status_lbl.config(text="Status: Reading from adapter...", foreground="#5b7c99")
        threading.Thread(target=self.read_config_thread, args=(adapter, config_widgets, status_lbl), daemon=True).start()

    def read_config_thread(self, adapter, config_widgets, status_lbl):
        try:
            for cmd, widget in config_widgets.items():
                adapter.write(cmd)
                time.sleep(0.05)
                val = adapter.read()
                if self.root.winfo_exists():
                    self.root.after(0, self.update_widget_value, widget, val)
            if self.root.winfo_exists():
                self.root.after(0, lambda: status_lbl.config(text="Status: Configuration Read Successfully", foreground="#5ca86c"))
        except Exception as e:
            msg = str(e)
            if self.root.winfo_exists():
                self.root.after(0, lambda m=msg: status_lbl.config(text=f"Status: Read Error - {m}", foreground="#a85c5c"))

    def update_widget_value(self, widget, val):
        if not val: return
        if isinstance(widget, ttk.Combobox):
            for option in widget['values']:
                if option.startswith(val):
                    widget.set(option)
                    break
        elif isinstance(widget, ttk.Entry):
            widget.delete(0, tk.END)
            widget.insert(0, val)

    def run_apply_config(self, adapter, config_values, status_lbl):
        config_values_dict = {}
        for cmd, widget in config_values.items():
            val = widget.get()
            if " (" in val:
                val = val.split(" ")[0]
            config_values_dict[cmd] = val
        status_lbl.config(text="Status: Applying configuration...", foreground="#5b7c99")
        threading.Thread(target=self.apply_config_thread, args=(adapter, config_values_dict, status_lbl), daemon=True).start()

    def apply_config_thread(self, adapter, config_values, status_lbl):
        try:
            for cmd, val in config_values.items():
                adapter.write(f"{cmd} {val}")
                time.sleep(0.05)
            if self.root.winfo_exists():
                self.root.after(0, lambda: status_lbl.config(text="Status: Configuration Applied Successfully", foreground="#5ca86c"))
        except Exception as e:
            msg = str(e)
            if self.root.winfo_exists():
                self.root.after(0, lambda m=msg: status_lbl.config(text=f"Status: Apply Error - {m}", foreground="#a85c5c"))

    def set_scanner_defaults(self, config_widgets):
        config_widgets['++mode'].set("1 (Controller)")
        config_widgets['++auto'].set("0 (Disable)")
        config_widgets['++eos'].set("3 (None)")
        config_widgets['++eoi'].set("1 (Enable)")
        config_widgets['++read_tmo_ms'].delete(0, tk.END)
        config_widgets['++read_tmo_ms'].insert(0, "200")

    def run_action_cmd(self, adapter, cmd, success_msg):
        try:
            adapter.write(cmd)
            messagebox.showinfo("Command Sent", success_msg)
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def get_version(self, adapter):
        try:
            adapter.write("++ver")
            time.sleep(0.1)
            msg = adapter.read()
            messagebox.showinfo("Adapter Version", f"Response from Adapter:\n\n{msg}")
        except Exception as e:
            messagebox.showerror("Error", str(e))

    # --- Scanning & Export Methods ---

    def populate_adapter_tree(self, tree, serial_num):
        for item in tree.get_children():
            tree.delete(item)
        rows = fetch_all_devices(serial_num)
        for i, row in enumerate(rows):
            tag = 'evenrow' if i % 2 == 0 else 'oddrow'
            tree.insert("", tk.END, values=row[:7], tags=(tag,))

    def run_bus_scan(self, adapter, tree, progress, btn):
        btn.config(state="disabled")
        progress["value"] = 0
        progress["maximum"] = 30
        threading.Thread(target=self.scan_thread, args=(adapter, tree, progress, btn), daemon=True).start()

    def scan_thread(self, adapter, tree, progress, btn):
        found_devices = []
        found_gpib_addrs = []
        
        for addr in range(31):
            if not self.root.winfo_exists():
                return
            
            adapter.write(f"++addr {addr}") 
            adapter.write("*IDN?")
            adapter.write("++read eoi")
            
            response = adapter.read()
            
            if response:
                found_gpib_addrs.append(addr)
                found_devices.append((adapter.adapter_type, adapter.port_or_ip, adapter.serial_num, addr, response, "Found"))
            
            if self.root.winfo_exists():
                self.root.after(0, lambda val=addr: progress.configure(value=val))
            
        upsert_devices_batch(found_devices)
        mark_missing_devices(adapter.serial_num, found_gpib_addrs)
        
        if self.root.winfo_exists():
            self.root.after(0, lambda: self.finish_scan(adapter.serial_num, tree, progress, btn))

    def finish_scan(self, serial_num, tree, progress, btn):
        progress["value"] = 30
        btn.config(state="normal")
        self.populate_adapter_tree(tree, serial_num)
        self.refresh_db_view()
        messagebox.showinfo("Scan Complete", f"Scan finished for adapter {serial_num}. Database updated.")

    def export_csv(self, adapter_serial=None):
        rows = fetch_all_devices(adapter_serial)
        if not rows:
            messagebox.showwarning("Export Empty", "No data available to export.")
            return
            
        filepath = filedialog.asksaveasfilename(
            defaultextension=".csv", filetypes=[("CSV files", "*.csv"), ("All files", "*.*")], title="Save Export as CSV"
        )
        if filepath:
            try:
                with open(filepath, mode='w', newline='', encoding='utf-8') as file:
                    writer = csv.writer(file)
                    writer.writerow(["Adapter Type", "Connection Port/IP", "Adapter Serial", "GPIB Address", "IDN Response", "Status", "Last Seen", "TC Config File"])
                    writer.writerows(rows)
                messagebox.showinfo("Success", f"Data exported successfully to {filepath}")
            except Exception as e:
                messagebox.showerror("Export Error", f"Failed to save CSV:\n{e}")

dprint("Classes loaded. Launching application...")

def main():
    dprint("Initializing Tkinter root window...")
    root = tk.Tk()
    app = PrologixMultiScannerApp(root)
    dprint("Handing control to Tkinter mainloop. The GUI should now be visible on your screen.")
    root.mainloop()

if __name__ == "__main__":
    main()