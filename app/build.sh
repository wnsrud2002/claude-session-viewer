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
# 아이콘은 이미지 파일을 저장소에 두지 않고 매번 코드로 그려 크기별로 줄인 뒤 .icns 로 묶음
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
swift app/make_icon.swift "$TMP/icon.png"
mkdir "$TMP/AppIcon.iconset"
for s in 16 32 128 256 512; do
  sips -z $s $s "$TMP/icon.png" --out "$TMP/AppIcon.iconset/icon_${s}x${s}.png" >/dev/null
  sips -z $((s * 2)) $((s * 2)) "$TMP/icon.png" --out "$TMP/AppIcon.iconset/icon_${s}x${s}@2x.png" >/dev/null
done
iconutil -c icns "$TMP/AppIcon.iconset" -o "$APP/Contents/Resources/AppIcon.icns"
codesign --force --sign - "$APP"
touch "$APP"  # Finder·Dock 이 아이콘 캐시를 새로 읽도록
echo "$APP"
