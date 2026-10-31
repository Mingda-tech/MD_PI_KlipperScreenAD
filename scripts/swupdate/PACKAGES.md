# Native SWUpdate package contract

The update engine is upstream SWUpdate2026.05. The screen supervisor supplies
local-file selection, an internal task lock and persistent task status; it
does not contain per-program installers. A signed package carries its own
installation and verification logic.

## Container and signature

Generate native CPIO packages with `scripts/build-swu.py`:

```bash
python3 scripts/build-swu.py scripts/swupdate/examples/demo/recipe.json \
  --key /secure/release-rsa-private.pem --output /output/demo.mdu
```

The private key is RSA2048–8192, signed with SHA-256 / PKCS#1v1.5 using SWUpdate's
native `SIGALG_RAWRSA` mode. Private keys never enter the package.
The device must have the matching public PEM installed before accepting releases.

The project uses the `.mdu` suffix for update packages. The generated file remains native SWUpdate `newc` CPIO; the suffix does not change its format or engine. All archive names are flat:

```text
demo.mdu
  sw-description          first, JSON accepted by SWUpdate's JSON parser
  sw-description.sig      second, RSA/SHA256 signature of sw-description
  mingda-meta.json        signed-description-bound metadata for screen preview
  update.sh               signed-description-bound shellscript
  installer.py            example package-owned implementation
  demo.txt                 example payload
  rescue.sh               only for declared screen self-update rescue
```

Every script, metadata file and payload has its SHA-256 in `sw-description`.
The screen's structural reader is not an authentication mechanism: it first
rejects unsafe names or executable parser hooks, then the real pinned SWUpdate
binary must verify the archive with `-c -i FILE -k PUBLIC_KEY -e stable,main`.
No preview is treated as trusted before that check succeeds. Check-only mode
never runs `preinst`: a prepared preview confirms signature, file integrity and
script protocol, not actual system or board compatibility.

The description enables only native `dummy` image handlers and a native
`shellscript` callback. `dummy` means SWUpdate verifies/stages the payload; the
signed package script decides how to install it. No custom handler is compiled
into the screen. `installed-directly`, embedded Lua, arbitrary parser hooks,
partition/bootloader changes and description-level output paths are not accepted
by this screen integration. New installation actions belong in the package's
script rather than in a new screen release.

## Recipe and metadata

The recipe has `metadata`, `script`, `sources`, and optionally `rescueScript`.
`sources` maps each flat archive name to a workstation path relative to recipe.
The recipe and the signing key are not packed. Script files must have LF line
endings and begin with `#!/bin/sh`.

Metadata is `schemaVersion:2`, `scriptApi:1`. It declares `releaseId`, `version`
and the component release identities used by the screen's inventory.
Components need `id`, `version`, positive `sequence`; they do not need a fixed
`git/tree/file` kind. `configChanges` contains only path/op/description summaries,
never private configuration values. `touchesFirmware` and `touchesScreen` are
required booleans. `recoveryMode` is `replay` or `none`.

A missing component in the local inventory means its previous version is
unknown. It does not prevent package preparation or installation, and does not
require an `installIfAbsent` declaration. The screen displays Unknown while the
signed package retains its target, backup and recovery checks. Known registered
sequences still prevent downgrade and conflicting release identities. Inventory
is updated only after confirmed success; malformed state is not treated as an
unknown version. Package preinst must not reject merely unparseable old-version
text, but must still validate actual hardware and installation compatibility.

The native compatibility revision identifies this package script protocol:

```text
mdu-script-api-1
```

The supervisor passes `-H MINGDA:mdu-script-api-1`. Printer model, SKU, mainboard
name, serial prefix, distribution and OS version do not restrict package
acceptance. Runtime initialization chooses by CPU architecture and actually runs
the engine's `--version/--help`; the supplied runtime is ARM64 only. Its binary
and dependencies must still run on the target system. Package scripts own the
requirements of the particular update.
Rebuild and sign early test packages that used SKU- or OS-based native revisions
with the current builder. Renaming an archive does not change its signed list.

Firmware packages still declare firmware release identities for the inventory.
`hardware` and `boardProfiles` are optional descriptive metadata, not admission
tables; neither screen nor builder requires a formal MCU parameter table. Each
package supplies its firmware, tools, parameters, installation checks and recovery
logic. Signing does not make guessed board data valid or guarantee flashing success.

The supervisor does not query Moonraker or Updater update-coordination APIs and
does not coordinate external online updates. Do not run an online update or
start a print from another interface during an offline installation. Local
printer-state and temperature checks, the internal task lock and recovery records
remain in place. These checks run before installation; the updater does not
install Klipper/Linux MCU startup conditions or make core services depend on its
recovery service. Its maintenance record only restricts later offline updates.

## Native callback and workspace contract

The supervisor passes a private `TMPDIR` and durable `MINGDA_JOB_DIR`.
SWUpdate2026.05 stages ordinary payloads in `$TMPDIR/<archive-name>` and copies
scripts into `$TMPDIR/scripts/`. Payloads remain available through `postinst`;
SWUpdate removes transient files afterwards. Persistent backups/checkpoints must
therefore go below `MINGDA_JOB_DIR`, never just below `TMPDIR`.

Package scripts can publish `script-progress.json` in `MINGDA_JOB_DIR` with
`phase`, integer `progress` (0–99), `component`, and `message`. Optional bounded
fields are `stage`, `completed`/`total`, `unit`, `elapsedSeconds`, and `heartbeat`.
Include counts and elapsed time in `message` for existing screen clients.
The updated runtime reserves total progress 20–35 for native payload handling and
35–94 for package work. Once script progress begins, late native frames cannot
overwrite its stage or text. The task high-water mark survives recovery; only
verified outer completion reaches 100. Package progress and local health success
are not evidence that inventory and final commit records have been persisted.

`update.sh` receives native SWUpdate stages as `$1`:

| Stage | Package responsibility |
| --- | --- |
| `preinst` | Read-only checks of all system/application/board targets; no service stop, bootloader transition or firmware write |
| `postinst` | Install or recover, then verify; nonzero exit is failure |
| `postfailure` | Record/repair failure as supported; never claim generic rollback |

`MINGDA_UPDATE_ACTION` distinguishes installation from explicit recovery. The
demo accepts `install`/`start`, and `recover`. The runtime's native progress socket
provides engine progress; scripts must cooperate if finer-grained internal
progress is needed. Printing a final “success” line is not completion evidence.

Scripts are trusted publisher code executed after signature verification. They
must be noninteractive, offline and idempotent, package all required dependencies,
and preserve calibration/identity data. A timeout or power loss can happen inside
any stage; script authors own recovery of their side effects. `preinst` must
check every target before any device modification. If a check requires entering
the bootloader, `postinst` must first persist the board identity and current step,
enter the bootloader, and recheck actual board/firmware compatibility before any
write. Persist per-board state before transitions and writes, then persist
verification and recovery results. Unknown side effects remain recovery-required;
an absent success record never proves that the device was unchanged.

## Screen self-update and rescue

An operator may explicitly cancel a failed task once its worker has stopped.
This closes the task without replaying package code, rolling back installed
changes or committing inventory. If writes may have occurred, the original
package, frozen runner and recovery evidence remain available for service; boot
recovery will not resume a cancelled task. A pending final commit cannot be
cancelled: recovery must finish its bookkeeping. Package authors must never
treat cancellation as proof that their side effects were undone.

After the outer durable commit, an independent cleanup task waits for the
installer to exit, then deletes all successful-task caches and backups. It keeps
JSON task/checkpoint records and records the cleanup result. The shipped
installers' external backups are bound to their task-specific names and state;
live releases and materials needed by an unfinished or cancelled-after-write
task must survive. SDK-specific retention finishes before general cleanup.
Cleanup retries independently and cannot turn a committed installation into a
failure. Pending tasks and tasks cancelled after possible writes retain their
recovery materials. Before that commit, keep backups outside `native-tmp`,
`native-run` and `runner`, which SWUpdate may remove during a normal run.

Packages that replace the screen declare all three:

```json
{"touchesScreen": true, "bootRescue": true, "recoveryMode": "replay"}
```

They must provide `rescueScript`. The builder emits native software selections
`stable,main` and `stable,rescue`, both binding the same verified payloads.
The rescue selection must ONLY restore an already-saved screen baseline and
must be safe to repeat. It must not flash MCU firmware or start a fresh install.
`rescue.template.sh` documents the interface; it is not a complete screen
installer and intentionally requires the package to implement `installer.py rescue`.

The supervisor sets `MINGDA_BOOT_RECOVERY=1` when rescue is invoked by the boot
oneshot, and `0` for an ordinary runtime task. During boot rescue, restore files
only: do not start, stop or restart Klipper, Linux MCU or Moonraker. The oneshot
may be ordered before KlipperScreen only; core printer services must not depend
on it. Do not synchronously wait for KlipperScreen from inside that oneshot.
Runtime rescue may restart the restored screen. Package authors must honor this
environment contract in their rescue implementation.

`build-klipperscreen-mdu.py --full-bundle` includes the complete release tree and
its reachable Git history in `screen.bundle`, with no prerequisite Git objects.
It includes the screen's bundled offline runtime, public key and libraries. The
default remains a delta bundle against the verified baseline. Both modes keep
the same baseline, local-change, backup and recovery checks; a full bundle does
not authorize overwriting arbitrary site modifications. The build receipt records
`bundleMode` as `full` or `delta`. Verify a full bundle in a newly initialized empty
Git repository and check the release file inventory after fetching it.

The demo package never modifies a printer or system application. It writes only
`demo-installed.txt` and its checkpoint inside the current task directory. It is
for native engine acceptance tests, not a production1000D firmware release.

## Verified upstream references

- [Shell scripts](https://sbabic.github.io/swupdate/sw-description.html#scripts)
- [Native signature format](https://sbabic.github.io/swupdate/signed_images.html)
- [Progress API](https://sbabic.github.io/swupdate/progress.html)
- [2026.05 local-file installation](https://github.com/sbabic/swupdate/blob/2026.05/core/install_from_file.c)
- [2026.05 script and payload staging](https://github.com/sbabic/swupdate/blob/2026.05/core/installer.c)
- [2026.05 shell callback implementation](https://github.com/sbabic/swupdate/blob/2026.05/handlers/shell_scripthandler.c)
