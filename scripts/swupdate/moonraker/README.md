# Offline Moonraker package

This package installs a pinned Git release from Mingda-tech/moonraker and its
complete offline CPython 3.10 / Linux ARM64 dependency set. Unknown historic
inventory versions and same-commit reinstalls are supported. The source remains
a normal Git checkout for subsequent Moonraker online updates.

Installation preserves the installed systemd service, its arguments, the complete
printer configuration and the running Klipper process. It never flashes firmware,
requests a Klipper restart, modifies Klipper service dependencies or installs
startup conditions. Moonraker and its WebSocket/API connections temporarily stop.
Keep the printer idle and do not initiate online updates or prints elsewhere.

Source and environment are staged and checked before stopping Moonraker. While
Moonraker is stopped, the package copies and verifies the complete database into
the root-private persistent task directory. It then atomically exchanges source
and environment directories, reconnects the API, checks previously enabled
components, confirms the same Klipper process is ready, and verifies preserved
configuration hashes. Console-script paths are rewritten for the stable venv path.

The package-owned durable journal and directory identity markers handle power
loss between exchanges. An interrupted task is resumed from the screen using the
original signed package. The offline updater's normal boot check marks the task
as needing recovery; this package adds no core-service startup gate. A detected
failure attempts to restore source, environment and the stopped database together.
The screen still records a failed update, never a successful rollback as an update.
After verifying a rollback, explicit recovery restages and retries the same task;
it does not demand a new task while the supervisor still retains the pending one.

Database backups may contain printer credentials. They stay only in the printer's
private task directory and are not included in the release package or build records.
The release requires the existing libsodium system library and Python venv module;
it does not replace the OS, nginx, Moonraker configuration or arbitrary system libs.

The repository's remote tags currently describe commit 3a9c3cd as
v0.8.0-370-g3a9c3cd. The inspected printer has a local v0.8.1 tag on the same
commit. The Git commit is the release identity; the different tag labels alone
do not imply a source downgrade. A known descendant commit is still rejected.
