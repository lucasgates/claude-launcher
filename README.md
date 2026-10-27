```
                               /\
                              /  \
                              |  |
                              |C |
                              |  |
                             /|  |\
                            / |  | \
                           /  |  |  \
                              |  |
                             (|  |)
                              |  |
                              \__/
                             '/  \'
                             '    '

       =[ claude-launcher v1.0.0                               ]
+ -- --=[ launch Claude Code from your phone, behind NAT       ]
+ -- --=[ 1 webhook  -  0 inbound ports  -  via cloudflare     ]
+ -- --=[ bearer auth  -  screen  -  claude --remote-control   ]
```

![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)
![Python 3.8+](https://img.shields.io/badge/python-3.8%2B-blue.svg)
![Dependencies: none](https://img.shields.io/badge/dependencies-none-brightgreen.svg)
![Platform: Linux](https://img.shields.io/badge/platform-linux-lightgrey.svg)

> **Launch a [Claude Code](https://claude.com/claude-code) session on a machine
> from your phone, over the internet, from behind NAT — with no inbound ports.**

A tiny webhook listener spawns `claude --remote-control` in a `screen` session;
the session then shows up under Remote Control in the Claude app, where you drive
it from your phone.

```
Phone (Shortcuts / any HTTP client)
   │  HTTPS POST /launch  +  Authorization: Bearer <secret>
   ▼
Cloudflare edge  ──(tunnel: outbound-only connection from the box)──►  cloudflared
   │                                                                      │
   └── answers only for your hostname                                     ▼
                                                   launcher.py  (127.0.0.1:8787)
                                                                          │ checks secret
                                                                          ▼
                                          screen -dmS <name> claude --remote-control <name>
                                                                          │
                                   drive it from the Claude app (Remote Control)
```

Because `cloudflared` makes an **outbound** connection to Cloudflare, nothing is
exposed on your NAT/router. The listener binds to `127.0.0.1`, so it is reachable
*only* through the tunnel — never from the LAN or the public internet directly.

The webhook is a pure "start a session" button: it takes **no commands and no
caller-supplied parameters**. The session name is generated server-side and the
child is spawned with an argument list (never a shell), so there is nothing for a
request to inject. All real work happens later, through the Claude app.

## Quick start

```bash
git clone https://github.com/lucasgates/claude-launcher.git
cd claude-launcher && ./setup.sh      # generate secret, install + start the listener
sudo loginctl enable-linger "$USER"   # keep it running across reboots
```

Then point a Cloudflare tunnel at `http://127.0.0.1:8787` (see
[Cloudflare tunnel](#cloudflare-tunnel)) and add a phone shortcut that POSTs to
`/launch` with your secret (see [Phone trigger](#phone-trigger-ios-shortcuts)).
Tap it → a fresh Claude session appears in the app under Remote Control.

## Requirements

- Linux with **user systemd** (`systemctl --user`) — or run `launcher.py` under
  any init/supervisor you like; it's a dependency-free Python 3 script.
- [`claude`](https://claude.com/claude-code) installed **and already logged in**
  on the box (the launcher starts sessions as that user).
- `screen`, `python3`, and (for the tunnel) `cloudflared`.

## Install

```bash
git clone https://github.com/lucasgates/claude-launcher.git
cd claude-launcher
./setup.sh
```

`setup.sh` does the local half:

- generates a 256-bit secret into `~/.config/claude-launcher/config.env` (mode 600),
- renders and installs the user systemd unit for **your** user and repo path,
- starts the listener and health-checks it.

It then prints the Cloudflare steps below (run those yourself — they need your
account and a browser). Enable lingering so the service survives logout/reboot:

```bash
sudo loginctl enable-linger "$USER"
```

Read the secret back any time:

```bash
grep '^LAUNCHER_SECRET=' ~/.config/claude-launcher/config.env | cut -d= -f2
```

## Folder trust (important)

Claude Code shows a one-time *"Do you trust this folder?"* prompt the first time
it runs in a directory. A launched session would **hang on that prompt** before
Remote Control attaches. So `LAUNCH_WORKDIR` must be a folder you've **already
trusted**. To trust one, run `claude` in it once by hand and accept the prompt;
after that the launcher starts cleanly there. Trust is stored per-folder in
`~/.claude.json` as `hasTrustDialogAccepted`.

## Cloudflare tunnel

Route a hostname to `http://127.0.0.1:8787`. Either method works:

**A) Dashboard / token (simplest).** In Cloudflare Zero Trust → Networks →
Tunnels, create a tunnel, add a **Public Hostname** → `http://localhost:8787`,
then install the connector it gives you as a system service:

```bash
sudo cloudflared service install <TOKEN>
```

**B) Locally-managed (config file on the box).**

```bash
cloudflared tunnel login
cloudflared tunnel create claude-launcher          # prints <UUID> + creds JSON
cloudflared tunnel route dns claude-launcher launch.example.com
# ~/.cloudflared/config.yml:
#   tunnel: <UUID>
#   credentials-file: /home/you/.cloudflared/<UUID>.json
#   ingress:
#     - hostname: launch.example.com
#       service: http://127.0.0.1:8787
#     - service: http_status:404
sudo cloudflared service install
```

## Phone trigger (iOS Shortcuts)

1. Shortcuts → **+** → **Get Contents of URL**.
2. URL: `https://launch.example.com/launch`
3. Method: **POST**.
4. Header: `Authorization` = `Bearer <your-secret>` (one space after `Bearer`,
   no trailing whitespace).
5. Name it, then **Add to Home Screen** for a one-tap icon.

Any HTTP client works — it's just an authenticated `POST`.

## HTTP API

| Method | Path | Auth | Result |
|--------|------|------|--------|
| GET  | `/health`   | none   | `{"status":"ok"}` |
| POST | `/launch`   | Bearer | spawns a session, returns its name |
| GET  | `/sessions` | Bearer | lists live launcher sessions |

Each launch is a uniquely-named session (`<prefix>-<UTC timestamp>`); repeat taps
accumulate sessions. End one with `screen -S <name> -X quit` or from the app.

## Configuration

`~/.config/claude-launcher/config.env` (see `config.example.env`):

| Key | Default | Meaning |
|-----|---------|---------|
| `LAUNCHER_SECRET` | — (required) | bearer secret the client must send |
| `LAUNCHER_HOST` | `127.0.0.1` | bind address (keep localhost) |
| `LAUNCHER_PORT` | `8787` | bind port |
| `LAUNCH_WORKDIR` | `$HOME` | cwd for the session (must be trusted) |
| `SESSION_PREFIX` | hostname | prefix for generated session names |
| `CLAUDE_BIN` | from `PATH` | absolute path to the `claude` binary |

After editing, apply with `systemctl --user restart claude-launcher`.

## Verify / operate

```bash
curl -s http://127.0.0.1:8787/health
curl -s -X POST https://launch.example.com/launch -H "Authorization: Bearer <secret>"
curl -s https://launch.example.com/sessions -H "Authorization: Bearer <secret>"
systemctl --user status claude-launcher
journalctl --user -u claude-launcher -e
```

## Security notes

- The secret is the only gate on `/launch`. Treat it like a password; it lives
  only in `config.env` (mode 600) and is git-ignored. Rotate by editing the file
  and running `systemctl --user restart claude-launcher`.
- A launched session can do anything `claude` can on the box. Anyone with the
  secret can start one.
- Hardening options: put **Cloudflare Access** (service token) in front of the
  hostname so unauthenticated traffic never reaches the box; or run the trigger
  over a private overlay network (e.g. Tailscale) instead of a public tunnel.

## License

MIT — see [LICENSE](LICENSE).
