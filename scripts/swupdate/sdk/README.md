# Managed SDK and AI offline update

This signed MDU installs SDK 1.6.41 and the matching six-model AI runtime from
the supplied X527 NoSHA archive. Existing managed SDK and AI services, current
links, device identity, configuration and a healthy rollback baseline are required.
The package selects the existing device profile's runtime variant. It never calls
the source platform/bootstrap installer, updates Updater/Janus/lock integrations,
or changes Klipper, Moonraker, KlipperScreen or their startup configuration.

The builder records the original tar digest, then packages SDK, one common Runtime
and six small variant overlays. Installed release bytes, modes, timestamps and
internal hardlink equivalence remain unchanged. The MDU uses the normal
publisher signature and payload SHA256 checks. Internal model, cache and component
file checksum gates remain disabled as requested by the original NoSHA release.
Path, type, executable/ABI, dependency, six-model layout and pairing checks remain.
After validating archive headers against the signed layout, native GNU tar extracts
only SDK, common Runtime and the selected overlay directly into private stages.
There is no complete source-tree extraction or second release copy. Bulk syncfs
persists each staging filesystem before any service stop or program replacement.
Payload counters and elapsed-time heartbeats provide continuous stage information.

Every attempt stages both components, including same-version reinstalls. The
installer saves a durable task journal, stops SDK/AI only after independent
read-only Klipper idle checks, exchanges any existing canonical releases, and
records each current-link transition. Original releases remain available for
rollback until the outer task commits. A failed attempt restores both components; explicit recovery retries
the same task after confirming the original baseline is healthy.
After local health succeeds, a transient cleanup task waits for the outer durable
COMPLETE record before pruning obsolete SDK/AI releases and terminal task scratch
copies. Only the current installed SDK/AI set is retained. Current package, task
records, logs, settings, keys and database backups are protected. No rollback
baseline is pruned while installation or recovery remains pending.
Both Ready and Idle timeout states are accepted only when the separate print,
pause, probe, heater and SDK-inspection checks also permit the operation.

While SDK is stopped, the package saves a fixed private baseline of its SQLite
state and configured AI/command journals, including WAL/SHM sidecars. It restores
that baseline before restarting the old SDK after failure, so a schema migration
does not remain behind after a program rollback. These backups stay only in the
printer's private task directory. Device-lock databases and application evidence,
output, models and cache directories are not part of this database rollback.

The two component switches are separately journaled. An interrupted upgrade
requires the original screen task to resume; this package adds no boot ordering
or startup gate. Keep the printer idle and do not start prints or online updates
from another interface during the operation. Complete local SDK/AI health checks
precede success; cloud readiness stays DEFERRED while offline and is not fabricated.

Preserved configuration includes device identity, endpoint/TLS settings, keys,
application environments and persistent application state. No example backend
URL or packaged site credentials overwrite the printer's installed configuration.
Existing inspection support must match the new SDK's requirements; this package
checks required support rather than installing core printer patches.
Managed configuration must retain its factory root ownership and must not be
writable by group or other users. The installed SDK version comes from its code
and version.json; the site configuration's version label is left unchanged.
