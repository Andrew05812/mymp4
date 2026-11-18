# Deployment on GMKtec K12

The repository stores source code and deployment templates only. Runtime state remains on the mini PC.

## Runtime dependencies

- Ubuntu Server 24.04
- Python 3.12+
- Docker with the Flowseal `tg-ws-proxy` container listening on `127.0.0.1:1443`
- Telegram API credentials from https://my.telegram.org
- Bot token for Telegram Mini App `initData` verification
- A forum-enabled destination supergroup

## First deployment

Run as the Linux user `adelf`:

```bash
mkdir -p ~/projects
cd ~/projects
git clone https://github.com/Andrew05812/mymp4.git tg-video
cd tg-video

python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

cp .env.example .env
chmod 600 .env
mkdir -p data
chmod 700 data
```

Edit `.env` on the server. Set `API_ID`, `API_HASH`, `BOT_TOKEN`, `OWNER_ID` and `TARGET_CHAT`. The example file contains placeholders only.

## Flowseal MTProxy secret

The application reads the already-installed proxy secret from:

`/home/adelf/.config/tg-ws-proxy-test/proxy.env`

The file is not copied to this repository. Keep its permissions restrictive:

```bash
chmod 700 /home/adelf/.config/tg-ws-proxy-test
chmod 600 /home/adelf/.config/tg-ws-proxy-test/proxy.env
```

The application expects a 32-character hexadecimal secret and automatically prepends the `dd` MTProxy mode prefix. It connects to `127.0.0.1:1443`; this is MTProto, not SOCKS5.

## Create the Telegram user session

From the project directory, run interactively:

```bash
.venv/bin/python login_qr.py
```

Scan the QR code using Telegram → Settings → Devices → Link Desktop Device. If Telegram asks for the two-step verification password, enter it in the local terminal prompt. The session is saved in `data/session.session`. Do not copy it to GitHub or share it.

## Foreground verification

```bash
.venv/bin/python app.py
```

The FastAPI app binds only to `127.0.0.1:8080`. In another shell, check:

```bash
curl -fsS http://127.0.0.1:8080/healthz
```

Stop the foreground process with Ctrl+C after confirming it starts and reports the Telegram user session as authorized.

## Enable background service

The template `deploy/tg-video.service` assumes this deployment path and account:

- User: `adelf`
- Project: `/home/adelf/projects/tg-video`
- Local API: `127.0.0.1:8080`

Install and start only after the foreground test succeeds:

```bash
sudo install -m 0644 deploy/tg-video.service /etc/systemd/system/tg-video.service
sudo systemctl daemon-reload
sudo systemctl enable --now tg-video.service
sudo systemctl status tg-video.service
journalctl -u tg-video.service -f
```

## Mini App public URL

The app intentionally binds to loopback. If the Mini App must be reachable from Telegram, configure a dedicated hostname and route it through the existing Cloudflare Tunnel to `http://127.0.0.1:8080`. Do not edit the existing `cloudflared` service or its ingress configuration until the current tunnel configuration and hostname ownership are checked. Set the corresponding HTTPS URL as the bot's Mini App URL in BotFather.

## Runtime data policy

Keep these files only on the mini PC:

- `.env` — bot token and Telegram API credentials
- `data/session.session` — Telegram user authorization
- `data/data.db` and SQLite WAL files — categories, channel assignments and deduplication records
- temporary QR image during login

The repository's `.gitignore` excludes these paths. Back them up only to a private encrypted destination. GitHub contains source code and deployment documentation, not user data.
