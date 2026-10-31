SWUpdate ARM64 runtime, source commit 602edf8e57f940791034096c9e24206097b4b7a6.

Build type: cross-validated. Host compiler: aarch64-linux-gnu-gcc13 on Ubuntu24.04 x86_64. All target headers/startup/link libraries come from verified Ubuntu22.04 ARM64 packages; no target GLIBC requirement exceeds2.35. The main executable and OpenSSL require GLIBC2.34. See build-info.json, build-inputs.json and abi-validation.json.

Runtime: swupdate plus five non-glibc private shared libraries. System libc/ld-linux are supplied by Ubuntu22.04; no private glibc, loader, pthread or dl is shipped. libubootenv0.3.5 is built from the supplied unmodified upstream source using the recorded build flags; SWUpdate uses its in-memory NONE bootloader interface and does not write the device bootloader environment.

Validation: six real ARM64 engine check-only runs under QEMU8.2.2, using the new release RSA4096 public key and root-created signed smoke package. Both M1D_STANDARD and M1D_ULTRA pass; payload/signature changes, absent signature and wrong hardware fail. Check-only did not execute package scripts. Physical printer, CAN flashing and power-loss acceptance have not been run.

Licenses/source: SWUpdate COPYING and complete upstream source are included. dependency-sources contains upstream source + Ubuntu packaging patches matching the private Ubuntu binary libraries; dependency-licenses contains copyright notices and license texts. No private signing keys, test keys or emulator executables are in this release directory.
