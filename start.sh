#!/bin/bash
# Torrchive — Linux/macOS launcher
# https://github.com/Versveth/torrchive

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON=""

# Find Python 3.10+
for cmd in python3 python3.14 python3.13 python3.12 python3.11 python3.10; do
    if command -v "$cmd" &>/dev/null; then
        VERSION=$("$cmd" -c "import sys; print(sys.version_info >= (3,10))" 2>/dev/null)
        if [ "$VERSION" = "True" ]; then
            PYTHON="$cmd"
            break
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    echo ""
    echo "Python 3.10 or higher is required but was not found."
    echo ""
    echo "Install it:"
    echo "  Ubuntu/Debian : sudo apt install python3"
    echo "  Fedora/RHEL   : sudo dnf install python3"
    echo "  macOS         : brew install python3"
    echo "  Or download   : https://www.python.org/downloads/"
    echo ""
    read -p "Press Enter to exit..."
    exit 1
fi

echo "Python found: $($PYTHON --version)"

# Check/install dependencies
MISSING=""
for pkg in yaml requests; do
    $PYTHON -c "import $pkg" 2>/dev/null || MISSING="$MISSING $pkg"
done

if [ -n "$MISSING" ]; then
    echo "Installing missing dependencies:$MISSING"
    $PYTHON -m pip install pyyaml requests rich --quiet
fi

# Check ffmpeg
if ! command -v ffmpeg &>/dev/null; then
    echo ""
    echo "ffmpeg is required but was not found."
    echo ""
    echo "Install it:"
    echo "  Ubuntu/Debian : sudo apt install ffmpeg"
    echo "  Fedora/RHEL   : sudo dnf install ffmpeg"
    echo "  macOS         : brew install ffmpeg"
    echo ""
    read -p "Press Enter to exit..."
    exit 1
fi

# Launch wizard if no config, otherwise launch normally
if [ ! -f "$SCRIPT_DIR/config.yaml" ]; then
    $PYTHON "$SCRIPT_DIR/torrchive.py" setup --config "$SCRIPT_DIR/config.yaml"
else
    $PYTHON "$SCRIPT_DIR/torrchive.py" "$@" --config "$SCRIPT_DIR/config.yaml"
fi
