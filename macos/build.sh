#!/bin/sh
# Builds "Brain Dump.app" and an installer disk image in macos/dist/.
#
# The app carries its own Python with the server and its packages, so nothing else needs
# installing apart from Ollama. Needs an Apple Silicon Mac with the Xcode command line
# tools (xcode-select --install). Run: ./macos/build.sh
set -eu

VERSION="1.0.0"
PY_RELEASE="20261021"
PY_FILE="cpython-3.12.15+${PY_RELEASE}-aarch64-apple-darwin-install_only_stripped.tar.gz"
PY_SHA256="7b8d00b751db21cea87dc551a8f8165147c5fbc5251d5537d7e5497dde00a219"

[ "$(uname -m)" = "arm64" ] || { echo "Brain Dump builds on Apple Silicon Macs only." >&2; exit 1; }

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$HERE")"
CACHE="$HERE/build/cache"
DIST="$HERE/dist"
APP="$DIST/Brain Dump.app"
RES="$APP/Contents/Resources"

mkdir -p "$CACHE"
rm -rf "$APP" "$DIST/Brain Dump.dmg"
mkdir -p "$APP/Contents/MacOS" "$RES/server"

echo "==> Python"
if [ ! -f "$CACHE/$PY_FILE" ]; then
  curl -fsSL --retry 3 -o "$CACHE/$PY_FILE.part" \
    "https://github.com/astral-sh/python-build-standalone/releases/download/$PY_RELEASE/$(echo "$PY_FILE" | sed 's/+/%2B/')"
  mv "$CACHE/$PY_FILE.part" "$CACHE/$PY_FILE"
fi
echo "$PY_SHA256  $CACHE/$PY_FILE" | shasum -a 256 -c - >/dev/null || {
  echo "Python download doesn't match its checksum; delete $CACHE and try again." >&2; exit 1; }
tar -xzf "$CACHE/$PY_FILE" -C "$RES"   # unpacks to $RES/python
PY="$RES/python/bin/python3"

echo "==> Server packages"
"$PY" -m pip install --quiet --disable-pip-version-check --no-warn-script-location \
  --cache-dir "$CACHE/pip" -r "$ROOT/requirements.txt"
"$PY" -m pip uninstall --quiet -y pip
rm -rf "$RES/python/lib/python3.12/test" "$RES/python/lib/python3.12/idlelib" \
  "$RES/python/lib/python3.12/tkinter" "$RES/python/lib/python3.12/turtledemo"

echo "==> App code"
rsync -a --exclude __pycache__ "$ROOT/app" "$RES/server/"
cp "$HERE/launcher.py" "$RES/server/"
# Compile ahead of time: the app never writes into its own (signed) bundle.
"$PY" -m compileall -q -j 0 "$RES/python/lib" "$RES/server" >/dev/null || true

echo "==> Mac app"
swiftc -O -target arm64-apple-macos14.0 -o "$APP/Contents/MacOS/BrainDump" "$HERE/BrainDump.swift"
sed "s/VERSION/$VERSION/g" "$HERE/Info.plist" > "$APP/Contents/Info.plist"

ICONSET="$HERE/build/AppIcon.iconset"
rm -rf "$ICONSET" && mkdir -p "$ICONSET"
# Mac icons sit inside a margin on their canvas: scale the artwork to 824 of 1024 px.
sips -z 824 824 "$ROOT/app/static/icon-512.png" --out "$HERE/build/icon-824.png" >/dev/null
sips --padToHeightWidth 1024 1024 "$HERE/build/icon-824.png" --out "$HERE/build/icon-1024.png" >/dev/null
for size in 16 32 128 256 512; do
  sips -z $size $size "$HERE/build/icon-1024.png" --out "$ICONSET/icon_${size}x${size}.png" >/dev/null
  sips -z $((size * 2)) $((size * 2)) "$HERE/build/icon-1024.png" --out "$ICONSET/icon_${size}x${size}@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o "$RES/AppIcon.icns"

echo "==> Signing (for this Mac only)"
codesign --force --sign - --identifier com.stevej763.braindump "$APP"

echo "==> Disk image"
STAGE="$HERE/build/dmg"
rm -rf "$STAGE" && mkdir -p "$STAGE"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
hdiutil create -quiet -volname "Brain Dump" -srcfolder "$STAGE" -ov -format UDZO "$DIST/Brain Dump.dmg"
rm -rf "$STAGE"

echo
echo "Built $APP ($(du -sh "$APP" | cut -f1))"
echo "  and $DIST/Brain Dump.dmg ($(du -sh "$DIST/Brain Dump.dmg" | cut -f1))"
