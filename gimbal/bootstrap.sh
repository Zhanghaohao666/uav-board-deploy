#!/bin/sh
# Pinned role archive. SHA256 is written by the release packager.
set -eu
VERSION=v1.7.2
ARCHIVE_SHA256=6f20bff52020aa1c8c9b18b74a86cff28e194a1133a55952fac4aaf3f3fea42e
URL="https://github.com/Zhanghaohao666/uav-board-deploy/releases/download/$VERSION/uav-gimbal-deploy-$VERSION.tar"
work=$(mktemp -d /tmp/uav-gimbal-download.XXXXXX)
trap 'rm -rf "$work"' EXIT
if command -v curl >/dev/null; then
    curl -fL --proto '=https' --tlsv1.2 --retry 3 "$URL" -o "$work/gimbal.tar"
elif command -v wget >/dev/null; then
    wget -O "$work/gimbal.tar" "$URL"
else echo 'Missing curl/wget; download the offline .tar package on a computer.' >&2;exit 1;fi
echo "$ARCHIVE_SHA256  $work/gimbal.tar" | sha256sum -c -
tar -xf "$work/gimbal.tar" -C "$work"
sh "$work/uav-gimbal-deploy/install.sh" "$@"
