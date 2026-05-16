# OnionShare — Home Assistant Add-on

Share files and folders, receive uploads, host static websites, and run anonymous
chat rooms — all over Tor hidden services. Built around the official
[`onionshare-cli`](https://github.com/onionshare/onionshare) tool.

> ⚠️ Tor traffic leaves your network. Make sure that's what you want before you
> use this. The add-on does not log who downloads what, but the Tor network is
> not magic — read the OnionShare docs first if you're new to it.

## Features

- 📤 **Share mode** — upload files/folders, get a `.onion` URL
- 📥 **Receive mode** — anyone with the link can drop files into
  `/share/onionshare-uploads`
- 🌐 **Website mode** — serve a static-site folder anonymously
- 💬 **Chat mode** — ephemeral anonymous chat room
- 🗂️ Browse `/share`, `/media` (incl. EasyNas mounts), `/backup`, `/config`
- 🔒 Persistent onion addresses (optional) — keys live in `/data/onionshare/`
- 🌉 Tor bridge support (for use behind censorship)
- 🌙 Dark themed Flask UI behind HA Ingress

## Configuration

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `log_level` | enum | `info` | `trace`/`debug`/`info`/`notice`/`warning`/`error`/`fatal` |
| `persistent_onions` | bool | `true` | Keep onion addresses across restarts |
| `bridges_enabled` | bool | `false` | Use Tor bridges |
| `bridges` | string | `""` | One bridge line per row (paste from [bridges.torproject.org](https://bridges.torproject.org)) |

## Known quirks & decisions

A few notes for future me when something breaks:

### Process management

Each session is one `onionshare-cli` subprocess managed by `SessionManager`. We
**parse stdout** to extract the `.onion` URL and the auth key. If the
onionshare-cli output format changes (it has before), the regexes in
`server.py` (`RE_ONION_URL`, `RE_AUTH_KEY`) need updating.

We start each subprocess in its own process group (`os.setsid`) so we can
`SIGTERM` the whole tree on stop — otherwise leftover Tor connections hang
around.

### Tor & onionshare integration

We run a **single Tor daemon** in the container with ControlPort on 9051 and
SocksPort on 9050. `onionshare-cli` auto-detects a running Tor and uses it
instead of spinning up its own.

Cookie auth file at `/var/lib/tor/control_auth_cookie` must be group-readable
(`CookieAuthFileGroupReadable 1`) so onionshare-cli (running as root) can
read it.

### Persistent onion addresses

When `persistent_onions: true`, each unique (mode, paths) combination gets a
JSON file in `/data/onionshare/persist-<mode>-<hash>.json`. Delete that file
to get a fresh address. `/data` is preserved across addon updates; `/config`
is preserved across HA OS updates.

### HA Ingress

Flask sits behind HA's Ingress proxy. The `ReverseProxied` middleware reads
`X-Ingress-Path` and rewrites `SCRIPT_NAME` so url_for and redirects work.
The `<base href="./">` tag in `index.html` makes static assets resolve
relative to the Ingress path.

### File access

The add-on maps `/share` and `/media` read-write, `/backup` read-only, and
`/config` read-write. The browse API verifies every path stays inside one of
these roots via `Path.relative_to()` — paths escaping the root return 400.

### Modes that don't take files

`chat` and `receive` mode hide the file picker in the UI. The backend
validates: share/website **must** have at least one path; chat/receive
ignore paths entirely.

### Restart behavior

If the addon restarts:
- With `persistent_onions: true`: addresses come back, but you need to
  re-create sessions through the UI (no auto-resume yet — TODO)
- With `persistent_onions: false`: brand new addresses every time

### What this is NOT

- Not a Tor relay or exit node — `ClientOnly 1` in torrc
- Not an HTTP proxy — onionshare-cli does its own thing
- Not a long-running daemon you can leave for months — Tor circuits and
  hidden service descriptors need occasional refresh; restart weekly

## Install

1. Settings → Add-ons → Add-on Store → ⋮ → Repositories
2. Add: `https://github.com/gregorwolf1973/onionshare-addon`
3. Install OnionShare, then Start.

The first start can take **60–120 seconds** while Tor bootstraps. Watch the
"Tor" pill in the header — it goes green when SOCKS is up.

## Development notes

```
onionshare/
├── config.yaml            # Addon manifest
├── build.yaml             # Multi-arch build config
├── Dockerfile             # Alpine + tor + onionshare-cli + Flask
├── app/
│   ├── server.py          # Flask app + SessionManager
│   ├── templates/
│   │   └── index.html
│   └── static/
│       ├── app.js
│       └── style.css
└── rootfs/
    └── etc/
        ├── cont-init.d/
        │   └── 10-torrc.sh    # Generate torrc from options
        └── services.d/
            ├── tor/run        # Start tor daemon
            └── flask/run      # Start Flask UI
```

## TODOs

- [ ] Resume sessions after restart (read `/data/onionshare/persist-*.json`)
- [ ] Show Tor bootstrap progress %, not just up/down
- [ ] Auto-stop sessions after N hours (configurable)
- [ ] Direct upload-to-share for small files (don't require browse)
- [x] ~~Optional Buy Me a Coffee link in footer~~ ☕

## Support

If this add-on saves you time, consider buying me a coffee:

[!["Buy Me A Coffee"](https://www.buymeacoffee.com/assets/img/custom_images/orange_img.png)](https://buymeacoffee.com/gregorwolf1973)
