#!/bin/sh
# 사용법: app/build.sh [설치 폴더]   (기본 ~/Applications)
set -eu
cd "$(dirname "$0")/.."
DEST="${1:-$HOME/Applications}"
APP="$DEST/Session Viewer.app"
mkdir -p "$DEST"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
swiftc -O -swift-version 5 -o "$APP/Contents/MacOS/SessionViewer" app/main.swift
cp app/Info.plist "$APP/Contents/"
cp sessions.py index.html "$APP/Contents/Resources/"
codesign --force --sign - "$APP"
echo "$APP"
