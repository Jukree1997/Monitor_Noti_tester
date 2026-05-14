#!/bin/bash
# Build an AppImage from the PyInstaller one-folder output.
#
# Prereqs:
#   - dist/MNT/ exists (run `pyinstaller --clean --noconfirm MNT.spec` first)
#   - build_tools/appimagetool exists (download via: see README)
#   - build_assets/icon.png exists (256x256 PNG)
#
# Output:
#   - MNT-<version>-x86_64.AppImage  (in repo root)
#
# Single-file deliverable customers can:
#   chmod +x MNT-1.0.0-x86_64.AppImage
#   ./MNT-1.0.0-x86_64.AppImage

set -euo pipefail

# ─── Config ──────────────────────────────────────────────────────────
VERSION=$(python -c "from core.version import __version__; print(__version__)")
APPNAME="MNT"
DISPLAY_NAME="Baksters Notification Runner"
APPDIR="build/${APPNAME}.AppDir"
OUTPUT="${APPNAME}-${VERSION}-x86_64.AppImage"

# ─── Sanity checks ───────────────────────────────────────────────────
if [ ! -d "dist/${APPNAME}" ]; then
    echo "ERROR: dist/${APPNAME}/ not found. Run pyinstaller first:" >&2
    echo "    pyinstaller --clean --noconfirm MNT.spec" >&2
    exit 1
fi
if [ ! -x "build_tools/appimagetool" ]; then
    echo "ERROR: build_tools/appimagetool not found." >&2
    echo "Download from: https://github.com/AppImage/AppImageKit/releases" >&2
    exit 1
fi
if [ ! -f "build_assets/icon.png" ]; then
    echo "ERROR: build_assets/icon.png not found (need 256x256 PNG)." >&2
    exit 1
fi

# ─── Build AppDir layout ─────────────────────────────────────────────
rm -rf "${APPDIR}"
mkdir -p "${APPDIR}/usr/bin"

# Copy the PyInstaller output into usr/bin/MNT/
cp -r "dist/${APPNAME}" "${APPDIR}/usr/bin/${APPNAME}"

# Icon at the AppDir root (appimagetool requires this) AND in
# hicolor/256x256/apps/ for desktop-integrated installs.
cp build_assets/icon.png "${APPDIR}/${APPNAME}.png"
mkdir -p "${APPDIR}/usr/share/icons/hicolor/256x256/apps"
cp build_assets/icon.png "${APPDIR}/usr/share/icons/hicolor/256x256/apps/${APPNAME}.png"

# Desktop entry — appimagetool looks for this at AppDir root.
cat > "${APPDIR}/${APPNAME}.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=${DISPLAY_NAME}
GenericName=CCTV Monitor
Comment=Real-time CCTV monitoring with AI detection
Exec=${APPNAME}
Icon=${APPNAME}
Categories=AudioVideo;Video;Utility;
Terminal=false
StartupWMClass=${APPNAME}
EOF

# AppRun is the launcher that runs inside the AppImage when the user
# executes it. It resolves the bundle's internal MNT executable and
# exec's into it so all our paths-handling code sees the right cwd.
cat > "${APPDIR}/AppRun" <<'EOF'
#!/bin/bash
# AppImage entry point.
HERE="$(dirname "$(readlink -f "${0}")")"
# The PyInstaller bundle lives under usr/bin/MNT/ with its own _internal
# folder; exec straight into the executable.
exec "${HERE}/usr/bin/MNT/MNT" "$@"
EOF
chmod +x "${APPDIR}/AppRun"

# ─── Run appimagetool ────────────────────────────────────────────────
echo "Building AppImage..."
ARCH=x86_64 ./build_tools/appimagetool --no-appstream "${APPDIR}" "${OUTPUT}" 2>&1

echo ""
echo "Done."
echo "  Output: ${OUTPUT}"
echo "  Size:   $(du -h "${OUTPUT}" | cut -f1)"
echo ""
echo "Test it:"
echo "  chmod +x ${OUTPUT}"
echo "  ./${OUTPUT}"
