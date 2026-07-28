# Prologix OpenGPIB Scanner

Built and tested on Windows. Supports Prologix USB and Ethernet adapters.
A simple tool that scans for COM ports, lets you select one, and then scans for
devices attached to the GPIB bus. It enumerates them and creates a SQLite database,
with export to CSV/JSON. This is the tool I use to find my devices and keep track
of my used addresses across more than one GPIB bus.

There are also TestController integration functions that check whether a device
configuration exists for the instruments found on your buses.

Uses a Tkinter GUI, serial ports, network discovery, SQLite storage, and CSV/JSON
exports. All files are stored in the same directory as the program.

## Features

- Tkinter desktop UI
- Serial port and Ethernet Prologix enumeration
- Prologix configuration panels
- Network broadcast to discover your Prologix controllers in case of DHCP
- SQLite persistence for scan sessions and device data
- CSV and JSON export utilities

## Run

From source (requires Python 3.10+ and pyserial):

    pip install pyserial
    python pos.py

Or download a prebuilt binary from the
[Releases page](https://github.com/Cyclotronic/pos/releases):

- **Windows:** `pos-windows.exe`
- **macOS, Apple Silicon** (M1 or later, 2020+): `pos-macos-applesilicon.zip`
- **macOS, Intel** (pre-2020): `pos-macos-intel.zip`

Unzip the macOS download and launch the app inside.

### macOS note
The app is not code-signed. On first launch macOS will block it — go to
System Settings → Privacy & Security and click "Open Anyway"
(on older macOS versions: right-click the app → Open). This is only
needed once.

## License

MIT