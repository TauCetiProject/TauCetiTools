The bundled `bubblewrap_0.9.0-1build1_amd64.deb` is the unchanged Ubuntu 24.04
release-pocket package, downloaded from:

https://archive.ubuntu.com/ubuntu/pool/main/b/bubblewrap/bubblewrap_0.9.0-1build1_amd64.deb

Package SHA-256:
`dde30d1f24da50446d647ed504ec8dc5a714f171974d054c70a589b48ba38b48`

Installed `/usr/bin/bwrap` SHA-256:
`3e33f8255e1411ada346db27028c25ef5ba4fa3327754a2321ea4c8732a3bc10`

Bundling removes Ubuntu DNS and mirror availability from sandbox startup. The
workflow still verifies both hashes, loads the binary-specific AppArmor profile
when required, and runs the existing isolation self-test before candidate code.
The existing candidate `.lake` normalization remains required for this version.

When updating the pin, obtain the official release-pocket package, verify its
contents and binary hash, replace the bundled file, and update the version and
both hashes in every installer together. Keep the package's copyright/license
notices, provided under `/usr/share/doc/bubblewrap` in the archive. Never replace
a checksum failure with a network fallback or an unverified package.
