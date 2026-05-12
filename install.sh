#!/bin/bash

# Exit on error
set -e

SERVICE_NAME="audio-mover-ui"
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$APP_DIR/.venv"
PYTHON_BIN="$VENV_DIR/bin/python3"
USER_NAME="$(whoami)"

echo "--- Installing $SERVICE_NAME Service ---"

# 1. Check for Python
if ! command -v python3 &> /dev/null; then
    echo "Error: python3 is not installed."
    exit 1
fi

# 2. Create Virtual Environment
if [ ! -d "$VENV_DIR" ]; then
    echo "Creating virtual environment in $VENV_DIR..."
    python3 -m venv "$VENV_DIR"
fi

# 3. Install Dependencies
echo "Installing dependencies..."
"$VENV_DIR/bin/pip" install --upgrade pip
if [ -f "$APP_DIR/requirements.txt" ]; then
    "$VENV_DIR/bin/pip" install -r "$APP_DIR/requirements.txt"
else
    echo "requirements.txt not found, installing base dependencies..."
    "$VENV_DIR/bin/pip" install flask mutagen requests
fi

# 4. Create Systemd Service File
echo "Creating systemd service file..."
sudo bash -c "cat > /etc/systemd/system/$SERVICE_NAME.service <<EOF
[Unit]
Description=Audio Mover UI Service
After=network.target

[Service]
User=$USER_NAME
WorkingDirectory=$APP_DIR
ExecStart=$PYTHON_BIN $APP_DIR/app.py
Restart=always

[Install]
WantedBy=multi-user.target
EOF"

# 5. Start and Enable Service
echo "Reloading systemd and starting service..."
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"
sudo systemctl restart "$SERVICE_NAME"

echo "-------------------------------------------------------"
echo "Installation complete!"
echo "Status: sudo systemctl status $SERVICE_NAME"
echo "Logs: journalctl -u $SERVICE_NAME -f"
echo "-------------------------------------------------------"