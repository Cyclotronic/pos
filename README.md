# Prologix OpenGPIB Scanner

A starter Python app for scanning and managing OpenGPIB devices using a Tkinter GUI, serial ports, network requests, SQLite storage, and CSV/JSON exports.

## Features

- Tkinter desktop UI
- Serial port enumeration and OpenGPIB command support
- Network request helper for remote queries
- SQLite persistence for scan sessions and device data
- CSV and JSON export utilities

## Install

```bash
python -m pip install -e .
```

## Run

```bash
python -m pogpib_scanner
```

## Package name

The package is installed as `pogpib_scanner` and provides the entrypoint script `pogpib-scanner`.
