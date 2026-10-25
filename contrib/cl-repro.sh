#!/bin/sh
# This script creates base images for jammy, noble, and resolute. Then it builds the
# cl-repro-jammy, cl-repro-noble, and cl-repro-resolute builder images. The base images are created using
# debootstrap, and the cl-repro images are created using the Dockerfiles in
# contrib/reprobuild. These builder images will finally be used to build the
# reproducible binaries.
# It automates the process described here:
# https://docs.corelightning.org/docs/repro#build-environment-setup
# https://docs.corelightning.org/docs/repro#builder-image-setup
#
# ROOTLESS=1 builds without root, for rootless Docker.  PLATFORM=linux/arm64
# builds the arm64 images on an amd64 host, under qemu-user emulation.

set -e

# Assuming script will be used from the root of lightning directory
LIGHTNING_DIR=$PWD
# Remove '/contrib' from the end if it exists
LIGHTNING_DIR=$(echo "$LIGHTNING_DIR" | sed 's|/contrib$||')
echo "Lightning Directory: $LIGHTNING_DIR"
# The base image steps delete $LIGHTNING_DIR/<dist> as root.
if [ ! -f "$LIGHTNING_DIR"/contrib/cl-repro.sh ]; then
  echo "Run this from the root of the lightning repository" >&2
  exit 1
fi

SUDO=sudo
NODEV=""
if [ -n "$ROOTLESS" ]; then
  SUDO=""
  # A rootless container cannot create device nodes, so skip debootstrap's
  # device setup: docker provides /dev when the image runs.  Packages are
  # still checked against the signed Release file.
  NODEV="sed -i -e 's/^check_sane_mount () {/check_sane_mount () { return 0;/' -e 's/^setup_devices () {/setup_devices () { return 0;/' /usr/share/debootstrap/functions &&"
fi
PLAT=""
case "$PLATFORM" in
  "") ;;
  linux/amd64|linux/arm64) PLAT="--platform $PLATFORM" ;;
  *) echo "Unsupported PLATFORM $PLATFORM: use linux/amd64 or linux/arm64" >&2; exit 1 ;;
esac

# arm64 images are suffixed so both architectures can coexist, and so the
# bin-Ubuntu-<dist>-arm64 targets resolve.  amd64 keeps its bare name.
SUFFIX=""
case "${PLATFORM:-$(uname -m)}" in
  linux/arm64|aarch64|arm64) SUFFIX="-arm64" ;;
esac

ROOTFS=$(mktemp -d)
for v in jammy noble resolute; do
  echo "Building base image for $v"
  # shellcheck disable=SC2086
  $SUDO docker run --rm $PLAT -v "$LIGHTNING_DIR":/build ubuntu:$v \
	bash -c "rm -rf /build/$v && apt-get update && apt-get install -y debootstrap && $NODEV debootstrap --keyring=/usr/share/keyrings/ubuntu-archive-keyring.gpg $v /build/$v"
  # shellcheck disable=SC2086
  $SUDO docker run --rm $PLAT -v "$LIGHTNING_DIR":/build -v "$ROOTFS":/out ubuntu:$v tar -C /build/$v -cf /out/$v.tar .
  # shellcheck disable=SC2086
  $SUDO docker import $PLAT "$ROOTFS/$v.tar" $v
  $SUDO rm -f "$ROOTFS/$v.tar"
  echo "$v release:"
  $SUDO docker run --rm $v cat /etc/lsb-release
  echo "Building CL repro $v:"
  # shellcheck disable=SC2024,SC2086
  $SUDO docker build $PLAT --no-cache -t cl-repro-$v$SUFFIX - < "$LIGHTNING_DIR"/contrib/reprobuild/Dockerfile.$v
done
rmdir "$ROOTFS"
