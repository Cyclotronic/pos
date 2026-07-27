# Prologix OpenGPIB Scanner

Built and tested on Windows. Supports Prologix USB and Ethernet Adatpers.
Its a simple script that will scan for com ports, let you select one and then scan for devices attacked to the GPIB bus. It enumerates them an creates a sqlite DB allowing you to also export to CSV. This is script I use to find my devices and keep track of my used addresses on more than one GPIB.

There is also some TestController functions that allow me to see if there's a device configuration for the devices I have on my GPIBes.

It's simple, uses Tkinter GUI, serial ports, network requests, SQLite storage, and CSV/JSON exports.
All files are stored in same directory and you can run it as a python script or I've compiled it into a single file binary for Windows.

## Features

- Tkinter desktop UI
- Serial port and Ethernet Prologix enumeration
- Prologix configuration panels
- Network broadcast to discover your Prologix Controllers in case of DHCP
- SQLite persistence for scan sessions and device data
- CSV and JSON export utilities

## Run

```bash
python pos.py

or

pos.exe
```

## Package name
pos.py
