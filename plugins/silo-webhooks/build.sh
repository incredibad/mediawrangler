#!/bin/sh
# Builds the plugin for Linux (amd64 and arm64) in Docker, and regenerates docs/REFERENCE.md.
# Usage: ./build.sh [version]
set -e
VERSION="${1:-0.1.0-beta.8}"
cd "$(dirname "$0")"
mkdir -p dist
docker run --rm -u "$(id -u):$(id -g)" -v "$PWD:/src" -v mw-gomod:/go -e HOME=/tmp -e GOFLAGS=-buildvcs=false -w /src golang:1.26 sh -c "
  go mod tidy &&
  go run . docs > docs/REFERENCE.md &&
  go vet ./... &&
  go test ./... &&
  for arch in amd64 arm64; do
    CGO_ENABLED=0 GOOS=linux GOARCH=\$arch go build -trimpath -ldflags '-s -w -X main.version=$VERSION' -o dist/event-webhooks-linux-\$arch .
  done"
ls -la dist
