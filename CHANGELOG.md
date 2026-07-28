# Changelog

## [1.2.0] - 2026-07-28

### Added
- Serial-poll status byte capture, storage, and decoded SPoll column (SRQ/ESB/MAV)
- Interactive Terminal tab per adapter: raw ++/SCPI commands, auto read-back
  for queries, command history, target-address field
- JSON export (Database Manager and per-adapter); Status Byte column in CSV

### Changed
- Two-phase scan: ++spoll presence detection first, *IDN? only to responders —
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