# SDK platform offline update

The command-line builder defaults to `--install-mode platform-offline`. This
mode preserves the complete publisher `.tar.gz` byte for byte and invokes the
same offline entry point used during a successful manual installation:

```bash
bash install.sh --offline --verify-only
bash install.sh --offline
```

Build the package on the release workstation, supplying the actual Updater
version shipped in that original archive:

```bash
python3 scripts/build-sdk-mdu.py \
  --install-mode platform-offline \
  --archive /release/MINGDA-Platform-OneClick-X527-Ultra-Algorithms-SDK1.6.43-NoSHA-JanusDeps-20261030-r3.tar.gz \
  --version 2026.10.31.5 --sequence 2026103105 --updater-version 1.1.9 \
  --key /secure/release-private.pem --output /release/MINGDA-SDK-1.6.43-offline.mdu
```

The signed MDU contains `platform.tar.gz`, `platform-release.json`,
`platform-audit.json`, a thin `platform_installer.py` wrapper, the archive reader,
and the shell stage entry. The original archive bytes remain unchanged.

The release workstation performs the complete path, member type, parent,
symlink/hardlink, size, metadata and installer-entry audit. It publishes an audit
certificate under the fixed `sdk-platform-audit-v2` policy, bound to the source
archive SHA-256, byte size, root, entry count, uncompressed tar byte count and SDK
identity, plus whether every explicit directory already has public read/execute
bits. The certificate and archive are both authenticated by the outer MDU
signature; the schema-2 release also pins the certificate's digest and size.
Packages containing screen patches or forward hardlinks are rejected from this
published fast path. A changed or malformed certificate fails closed rather than
falling back to an expensive device-side Python scan.

On the printer, the wrapper verifies the signed certificate reference and its
binding to the actual source archive, then runs GNU tar directly without a
Python metadata scan or per-member verbose output. Native checkpoint records
report actual processed uncompressed tar bytes. The final byte count is reported
only after native success and post-extraction verification. Existing screens can
show byte counts, local stage percentage and elapsed time in the message without
a UI update. Legacy schema-1 packages retain their previous one-audit extraction
path for compatibility; newly built packages use schema 2.

The source installer runs in a root-owned, protected directory below
`/opt/mingda/.offline-platform-updates/<jobId>`, allowing its mingda dependency
probes to traverse public release material. Checkpoints and command logs remain
in the private persistent task directory. Staging stays `0700` until the source
is safely extracted and permissions are protected. GNU tar uses
`--no-same-permissions` with a controlled child umask of `022`, removing group and
other write bits during creation while preserving private file read permissions.
When the signed audit proves every explicit directory already contains `0555`,
these are the complete required source permissions and no second whole-tree
find/chmod pass runs. Archives with restricted directories, legacy packages or
an absent proof retain the conservative directory-permission pass. Private
`0600` files are never made world-readable. The wrapper does not reuse a
user-writable `/home/mingda` release tree.

The original platform's dependency, identity, model, service, conditional
inspection and offline acceptance checks remain in effect. The MDU adds no old
SDK full-health requirement on port 7126. It does not remove checks made by the
original `install.sh --offline` workflow itself.

Progress is disposable observation: `script-progress.json` uses atomic replacement
without fsync, and byte counters, elapsed time and heartbeat are jointly throttled
to at least five seconds. Stage transitions and final progress publish immediately.
Transaction checkpoints, backups and final completion records keep their durable
fsync semantics. This limits repeated flash journal commits while tar is creating
files and hardlinks. The private `sdk-platform-state.json` records phase timings
for publication verification, extraction, permission protection, source preflight,
installation and source cleanup. Overall progress remains below 100 until the
outer executor commits the task. Original source-script execution reports stage
and elapsed time instead of inventing internal completion counts.

This is a platform update, not just an SDK binary replacement. Its original
installer can install or migrate SDK, AI Runtime, Updater, Janus, device-lock and
inspection support, update their configuration/service integration, and restart
Klipper or Moonraker where that original workflow requires it. These effects
are declared in the MDU configuration summary and build receipt. Packages that
also carry `lock-screen/manifest.json` are rejected by this builder because they
need a dedicated screen update and rescue contract.

Successful completion follows the original installer's local acceptance in
`--offline` mode. Cloud readiness is deferred, not fabricated as READY. Keep the
printer idle and powered throughout the operation and do not issue concurrent
prints or online updates from another interface.

The original platform installer can roll back failures it catches, but its
temporary `/run` backup does not guarantee recovery after power loss or a forced
kill. This MDU therefore declares `recoveryMode=none`. A failed/interrupted task
retains its evidence for manual recovery; it does not offer an automatic replay
of unknown side effects. `postfailure` records the failure instead of invoking a
second, different rollback implementation. Use a new release sequence and a new
signed package identity when changing the wrapper or source archive.

## Legacy managed SDK/AI mode

Select `--install-mode managed` explicitly to retain the previous SDK/AI-only
transaction described below. Programmatic builder callers that do not supply an
`install_mode` attribute also retain that legacy behavior for compatibility.

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
