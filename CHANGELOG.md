# Changelog

## [1.3.0] - 2026-08-06

### Changed
- Reworked the window on the same framework as LGI: `App` is the `tk.Tk`
  subclass, tabs are self-contained `QueuedFrame` classes, and every worker
  posts events to a queue the main loop drains instead of touching widgets
- Native ttk theme (clam/vista/aqua) replaces the hard-coded Segoe UI palette,
  so the app looks native on macOS and Linux rather than Windows-shaped
- Grid layout on a single `PAD` unit throughout; panes give their space to the
  table and log instead of to padding
- Connections tab merges live serial ports, NetFinder replies and remembered
  adapters into one sortable table; double-click connects
- Adapter validation and connection moved off the main thread - the window no
  longer freezes for the ~1 s probe
- TestController integration moved from a separate `Toplevel` into a sub-tab of
  the Database tab; its column hides when the feature is off
- Scan address range is settable and interruptible, with a live log pane

### Added
- Menu bar (File / Scan / Tools / Help), status bar, `Ctrl+W`, `F5`
- `--db` and `--debug` command line options, `--version`
- Click any column heading to sort
- Re-query *IDN?* and delete records from within a controller tab
- Shared `QueuedFrame`, `LogPane`, `FieldDialog` and `sortable` helpers, matching
  LGI so the two programs stay in step

### Fixed
- A partial-range scan no longer marks in-range-only addresses `NotFound`
  outside the range that was actually walked
- Bus access is serialised per controller, so a terminal command issued during
  a scan can no longer interleave on the wire

## [1.2.0] - 2026-07-28

### Added
- Serial-poll status byte capture, storage, and decoded SPoll column (SRQ/ESB/MAV)
- Interactive Terminal tab per adapter: raw ++/SCPI commands, auto read-back
  for queries, command history, target-address field
- JSON export (Database Manager and per-adapter); Status Byte column in CSV

### Changed
- Two-phase scan: ++spoll presence detection first, *IDN? only to responders -
  faster and safe for pre-488.2 instruments
- Scan preconditions set automatically per session
- Adapter validation rejects streaming serial devices (silence + consistency
  checks); compatible clones (AR488) connect via confirmation prompt
- ++savecfg sent last when applying configuration (EEPROM wear)
- Database/config files stored next to the program regardless of launch directory

### Fixed
- Phantom/duplicated scan results from GPIB reply desynchronization
- TC Integration Accept/Ignore buttons clipped off-screen by long device lists
- Latent crash in adapter config thread error reporting

## [1.0.0] - 2026-07-27

- Initial release: Prologix USB/Ethernet adapter enumeration, GPIB bus
  scanning, SQLite device database, CSV export, NetFinder discovery,
  TestController config matching
  
### Packaging
- macOS builds now ship as proper .app bundles in zip archives, for both
  Apple Silicon (pos-macos-applesilicon.zip) and Intel (pos-macos-intel.zip)
- Windows binary renamed to pos-windows.exe for clarity alongside macOS assets