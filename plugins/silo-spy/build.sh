#!/bin/sh
# Builds the spike plugin for Linux (amd64 and arm64) inside Docker.
# Usage: ./build.sh http://<mediawrangler-host>:8791
set -e
LISTENER="${1:?listener URL, e.g. http://<mediawrangler-host>:8791}"
cd "$(dirname "$0")"
mkdir -p dist
docker run --rm -u "$(id -u):$(id -g)" -v "$PWD:/src" -v mw-gomod:/go -e HOME=/tmp -e GOFLAGS=-buildvcs=false -w /src golang:1.26 sh -c "
  go mod tidy &&
  for arch in amd64 arm64; do
    CGO_ENABLED=0 GOOS=linux GOARCH=\$arch go build -trimpath -ldflags '-s -w -X main.listener=$LISTENER' -o dist/mw-spy-linux-\$arch .
  done"
ls -la dist
