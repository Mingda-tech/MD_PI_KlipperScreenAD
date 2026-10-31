-----BEGIN PGP SIGNED MESSAGE-----
Hash: SHA512

Format: 3.0 (quilt)
Source: openssl
Binary: openssl, libssl3, libcrypto3-udeb, libssl3-udeb, libssl-dev, libssl-doc
Architecture: any all
Version: 3.0.2-0ubuntu1.30
Maintainer: Ubuntu Developers <ubuntu-devel-discuss@lists.ubuntu.com>
Uploaders: Christoph Martin <christoph.martin@uni-mainz.de>, Kurt Roeckx <kurt@roeckx.be>, Sebastian Andrzej Siewior <sebastian@breakpoint.cc>
Homepage: https://www.openssl.org/
Standards-Version: 4.5.0
Vcs-Browser: https://salsa.debian.org/debian/openssl
Vcs-Git: https://salsa.debian.org/debian/openssl.git
Testsuite: autopkgtest
Build-Depends: debhelper-compat (= 12), m4, bc, dpkg-dev (>= 1.15.7)
Package-List:
 libcrypto3-udeb udeb debian-installer optional arch=any profile=!noudeb
 libssl-dev deb libdevel optional arch=any
 libssl-doc deb doc optional arch=all
 libssl3 deb libs optional arch=any
 libssl3-udeb udeb debian-installer optional arch=any profile=!noudeb
 openssl deb utils optional arch=any
Checksums-Sha1:
 c97166014243779a4b1b3613e1fce6087f2e17bc 15038141 openssl_3.0.2.orig.tar.gz
 b6dad5730c5e1304b151e66e37967b8c21d505cc 319128 openssl_3.0.2-0ubuntu1.30.debian.tar.xz
Checksums-Sha256:
 98e91ccead4d4756ae3c9cde5e09191a8e586d9f4d50838e7ec09d6411dfdb63 15038141 openssl_3.0.2.orig.tar.gz
 b2f4d75e1eba248397d8e4f4a029195ec3bb0e971b2e975594115a1c79b31c47 319128 openssl_3.0.2-0ubuntu1.30.debian.tar.xz
Files:
 7f9d43bb7a1e742722cf6d6f40531462 15038141 openssl_3.0.2.orig.tar.gz
 8be40b7bb52ce9c384359b606137d326 319128 openssl_3.0.2-0ubuntu1.30.debian.tar.xz
Original-Maintainer: Debian OpenSSL Team <pkg-openssl-devel@alioth-lists.debian.net>

-----BEGIN PGP SIGNATURE-----

iQIzBAEBCgAdFiEEUMSg3c8x5FLOsZtRZWnYVadEvpMFAmqzx1UACgkQZWnYVadE
vpOLwg//ZpeJXAjMAerWlaItLfJsQIJJEzDyLjvymr3Ff0KuTXdC5mBVUotOsrSh
6nwCDEOiEOIXY3ID1g5X4f65v3CHACnA8A4fnmW9+eD1IGAGPkFaIZ4uXYxQQLBG
MddzhC0tIGg9oFN9JImXSKgLdMQ9I5wl9AnzM5/vE3BXtuPz3dbNbVk0fmXGInGO
8RYbNGoEUTeU55mAutRJuyNfd6GueP2IbBvmX6dx8XdSolWHXCvABIjgtf/1rokZ
XKmJM1Lh+cjV8zPAflIqzITKLxs1WTShU/da6NPehj4v4bYs0ZJq4s74REZgF1IJ
zDI0f+AFwMpB/bCYxKffWmJeqBxkETbpOhqk5C2atsL0K5e0o2m7F7TlSqv8zzEN
oM56VejnLx4zIoFEMlDt2059/Kr7UttKY2U68CuRkZozaeMAPwUTSYEEMIogoVeC
cOlKMS1uk6VmnQR5AYJ5ZP3Sieh0+2mKM00zYEjnKepiATxm8hvDnG48Nmt5cmzP
rMqpwah3WaLYvbXPfZH9j0Mw0HcOumHf2Y/fmRs20SA2zXYRVmhJKqsRunphntlx
iAZj+/qKqTYxsCI3JgDKTOheNhtqIpgCySL4XPVLRQROFbaVVJWq2KhXLD1NDHpU
JUYoR3MBr7ph0JhGQh8IuqtvXdZJuxhcFf/H9RPfsyoTdamu2gc=
=mhtF
-----END PGP SIGNATURE-----
