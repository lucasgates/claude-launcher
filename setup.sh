#!/usr/bin/env bash
# Local setup for claude-launcher.
#
# Generates the secret + config, renders and installs the user systemd unit for
# the current user and repo location, and starts the listener. It then prints the
# Cloudflare steps you run yourself (they need your account and a browser).
#
# Portable across users/distros: nothing here assumes a specific username or path.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/claude-launcher"
CONFIG_FILE="$CONFIG_DIR/config.env"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
PORT="${LAUNCHER_PORT:-8787}"

bold() { printf '\033[1m%s\033[0m\n' "$1"; }
have() { command -v "$1" >/dev/null 2>&1; }

bold "==> claude-launcher setup"

# 1. Resolve dependencies ----------------------------------------------------
CLAUDE_BIN="$(command -v claude || true)"
PYTHON_BIN="$(command -v python3 || true)"
[ -n "$CLAUDE_BIN" ]  || { echo "ERROR: 'claude' not found on PATH";  exit 1; }
[ -n "$PYTHON_BIN" ]  || { echo "ERROR: 'python3' not found on PATH"; exit 1; }
have screen      || { echo "ERROR: 'screen' not found";      exit 1; }
have cloudflared || echo "WARNING: 'cloudflared' not found (needed for the tunnel; listener will still run)"
echo "claude:      $CLAUDE_BIN"
echo "python3:     $PYTHON_BIN"
echo "screen:      $(command -v screen)"
echo "project dir: $PROJECT_DIR"

# 2. Config + secret (never overwrite an existing secret) --------------------
mkdir -p "$CONFIG_DIR"
if [ -f "$CONFIG_FILE" ]; then
  echo "Config already exists at $CONFIG_FILE — leaving it untouched."
else
  if have openssl; then SECRET="$(openssl rand -hex 32)"
  else SECRET="$("$PYTHON_BIN" -c 'import secrets;print(secrets.token_hex(32))')"; fi
  umask 077
  cat > "$CONFIG_FILE" <<EOF
LAUNCHER_SECRET=$SECRET
LAUNCHER_HOST=127.0.0.1
LAUNCHER_PORT=$PORT
LAUNCH_WORKDIR=$HOME
CLAUDE_BIN=$CLAUDE_BIN
EOF
  chmod 600 "$CONFIG_FILE"
  echo "Wrote $CONFIG_FILE (secret generated, mode 600)."
  echo "NOTE: set LAUNCH_WORKDIR to a folder you have already trusted in Claude Code."
fi

# 3. Render + install the systemd user unit ----------------------------------
mkdir -p "$UNIT_DIR"
sed -e "s|__PYTHON__|$PYTHON_BIN|g" \
    -e "s|__PROJECT_DIR__|$PROJECT_DIR|g" \
    "$PROJECT_DIR/systemd/claude-launcher.service" > "$UNIT_DIR/claude-launcher.service"
systemctl --user daemon-reload
echo "Installed user unit into $UNIT_DIR."

# 4. Keep user services running without an active login ----------------------
if ! loginctl show-user "$USER" 2>/dev/null | grep -q 'Linger=yes'; then
  echo
  bold "ACTION: enable lingering so the service survives logout/reboot:"
  echo "    sudo loginctl enable-linger $USER"
fi

# 5. Start the listener (needs no Cloudflare setup) --------------------------
systemctl --user enable --now claude-launcher.service
sleep 1
if curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
  echo "Listener is up on 127.0.0.1:$PORT (/health OK)."
else
  echo "WARNING: health check failed — see: journalctl --user -u claude-launcher -e"
fi

# 6. Cloudflare — interactive, do these yourself -----------------------------
cat <<STEPS

================================================================================
Cloudflare tunnel (choose ONE method) — run these yourself, then route your
chosen hostname to  http://127.0.0.1:$PORT
================================================================================

A) Dashboard / token (simplest; tunnel config lives in the Cloudflare dashboard)
   - In Zero Trust > Networks > Tunnels, create a tunnel, add a Public Hostname
     pointing to  http://localhost:$PORT , then install the connector it shows:
       sudo cloudflared service install <TOKEN>

B) Locally-managed (config file on this box)
     cloudflared tunnel login
     cloudflared tunnel create claude-launcher          # prints <UUID> + creds JSON
     cloudflared tunnel route dns claude-launcher launch.YOURDOMAIN.com
     # write ~/.cloudflared/config.yml:
     #   tunnel: <UUID>
     #   credentials-file: $HOME/.cloudflared/<UUID>.json
     #   ingress:
     #     - hostname: launch.YOURDOMAIN.com
     #       service: http://127.0.0.1:$PORT
     #     - service: http_status:404
     sudo cloudflared service install        # or: cloudflared tunnel run claude-launcher

Then test (secret is in $CONFIG_FILE):
     curl -X POST https://launch.YOURDOMAIN.com/launch \\
          -H "Authorization: Bearer <SECRET>"
================================================================================
STEPS

bold "==> Local setup done."
