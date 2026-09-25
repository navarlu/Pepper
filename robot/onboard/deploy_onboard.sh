#!/bin/sh
set -eu

PEPPER_HOST="${PEPPER_HOST:-192.168.210.113}"
PEPPER_USER="${PEPPER_USER:-nao}"
REMOTE_HOME="/home/nao"
PACKAGE_ID="safe-startup-onboard"
PACKAGE_FILE="safe-startup-onboard.pkg"

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PACKAGE_DIR="$SCRIPT_DIR/safe_startup_pkg"
BUILD_DIR=$(mktemp -d)
TARGET="$PEPPER_USER@$PEPPER_HOST"

cleanup() {
    rm -rf "$BUILD_DIR"
}
trap cleanup EXIT HUP INT TERM

echo "Preflight: checking Pepper runtime at $TARGET"
ssh "$TARGET" "export PYTHONPATH=/opt/aldebaran/lib/python2.7/site-packages; export LD_LIBRARY_PATH=/opt/aldebaran/lib; /usr/bin/python --version; /usr/bin/python -c 'import qi; print(qi.__file__)'; /opt/aldebaran/bin/qicli --help >/dev/null"

echo "Building $PACKAGE_FILE"
(
    cd "$PACKAGE_DIR"
    zip -j "$BUILD_DIR/$PACKAGE_FILE" manifest.xml safe_startup_onboard.py
)

echo "Copying package to $TARGET"
scp "$BUILD_DIR/$PACKAGE_FILE" "$TARGET:$REMOTE_HOME/$PACKAGE_FILE"

echo "Installing package"
ssh "$TARGET" "export PATH=/opt/aldebaran/bin:\$PATH; qicli call PackageManager.removePkg '$PACKAGE_ID' >/dev/null 2>&1 || true; qicli call PackageManager.install '$REMOTE_HOME/$PACKAGE_FILE'; rm -f '$REMOTE_HOME/$PACKAGE_FILE'; qicli call PackageManager.hasPackage '$PACKAGE_ID'"

echo "Starting smoke run and waiting for its log"
ssh "$TARGET" "export PATH=/opt/aldebaran/bin:\$PATH; qicli call ALServiceManager.startService '$PACKAGE_ID.safestartup' || true; sleep 15; tail -n 40 '$REMOTE_HOME/safe_startup_onboard.log'"

echo "Deployment complete. Stop the RPi fallback before supervised reboot/cold-boot tests."
