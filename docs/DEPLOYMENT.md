# Deployment: 24/7 on a Hetzner VPS (prepared, NOT executed)

Nothing has been purchased or deployed. These files let you do it yourself once local testing succeeds.

## Recommended design

```
 your phone / laptop ──(Tailscale, encrypted, private)──► VPS
                                                         ├─ gim-dashboard.service   Streamlit on 127.0.0.1:8501
                                                         ├─ gim-collect.timer       every 30 min (sources keep their own intervals)
                                                         ├─ gim-backup.timer        daily 03:17 UTC
                                                         └─ gim-daily-brief.timer   06:43 UTC (enable when AI + e-mail are set)
 No public port except SSH. AI = DeepSeek API (or any OpenAI-compatible API); budget-capped.
```

- **Server:** the smallest Hetzner Cloud shared-vCPU instance (2 vCPU / 4 GB class), Ubuntu 24.04. Check current prices: expect roughly €4–8/month including IPv4 and backups.
- **Why Tailscale:** the dashboard is reachable only from *your* devices. There is no login page on the internet to attack, and the HTTPS certificate is handled for you. The free personal plan is enough.
- **AI on the server:** use the DeepSeek API, not Ollama. A small VPS has no GPU. Local inference on your PC can't serve a 24/7 server while the PC is off.

## Steps

1. Create the VPS in the Hetzner Cloud console with your **SSH key** (no password login).
2. SSH in as root and run the setup script. Read it first; it only installs packages, configures the firewall (SSH only), creates the `gim` user and installs the systemd units:

```bash
curl -fsSL https://raw.githubusercontent.com/nickjbowdark/ihor/claude/lucid-thompson-ssnima/deploy/hetzner/setup_server.sh -o setup_server.sh
```

```bash
less setup_server.sh
```

```bash
bash setup_server.sh https://github.com/nickjbowdark/ihor.git claude/lucid-thompson-ssnima
```

   For a private repository, clone with a deploy key or token instead of the public URL.

3. Edit the configuration (permissions are set to 600):

```bash
nano /opt/gim/.env
```

   Set at least `GIM_USER_AGENT`, the AI section, `GIM_LLM_MONTHLY_BUDGET_USD`, and a dashboard password hash (`python -m gim.ui.auth` prints the hash).

4. Install Tailscale and publish the dashboard **inside your tailnet only**:

```bash
curl -fsSL https://tailscale.com/install.sh | sh
```

```bash
tailscale up
```

```bash
tailscale serve --bg 8501
```

   Install the Tailscale app on your phone and laptop and sign in with the same account. The dashboard is then at `https://<server-name>.<tailnet>.ts.net`.

5. Validate the sources from the server, where network conditions differ from your PC:

```bash
sudo -u gim /opt/gim/.venv/bin/python -m gim.validate
```

6. When the AI provider and e-mail are configured, enable the daily brief:

```bash
systemctl enable --now gim-daily-brief.timer
```

## Alternative: public HTTPS with Caddy

If you want a normal domain instead of Tailscale, use `deploy/hetzner/Caddyfile` (automatic HTTPS + basic auth) and open ports 80/443 in `ufw`. **Keep the dashboard password enabled as a second layer.** This exposes a login page to the internet, so Tailscale remains the recommendation.

## Operations

| Task | Command |
|---|---|
| Status of all timers | `systemctl list-timers 'gim-*'` |
| Dashboard logs | `journalctl -u gim-dashboard -f` |
| Collection log | `journalctl -u gim-collect -n 100` |
| Source health | `sudo -u gim /opt/gim/.venv/bin/python -m gim.collect --status` |
| Update | `cd /opt/gim && sudo -u gim git pull && sudo -u gim .venv/bin/pip install -e . && systemctl restart gim-dashboard` |
| Off-site backup | copy `/opt/gim/data/backups/` regularly to your PC (`scp`) or enable Hetzner snapshots |

## Security checklist

- [ ] SSH key login only (`PasswordAuthentication no` in `/etc/ssh/sshd_config`)
- [ ] `ufw status` shows only OpenSSH (plus 80/443 only with Caddy)
- [ ] `/opt/gim/.env` has mode 600 and owner `gim`
- [ ] Dashboard reachable only via Tailscale (or Caddy + auth); port 8501 is never opened
- [ ] `GIM_DASHBOARD_PASSWORD_SHA256` set
- [ ] AI budget cap set; check monthly spend in Settings & System
- [ ] Unattended security upgrades enabled (installed by the setup script)
