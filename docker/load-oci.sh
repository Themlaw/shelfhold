#!/usr/bin/env bash
# Load an OCI archive into the local Docker daemon as shelfhold:ci, whatever its image store:
# `docker load` only reads OCI archives with the containerd store, skopeo reads them anywhere.
# Usage: load-oci.sh <archive.tar>
set -euo pipefail

archive="${1:?usage: load-oci.sh <archive.tar>}"
command -v skopeo >/dev/null || sudo apt-get install -y --no-install-recommends skopeo
skopeo copy "oci-archive:${archive}" "docker-daemon:shelfhold:ci"
