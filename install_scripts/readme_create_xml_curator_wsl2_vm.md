# xml_curator_for_BP_MD on WSL2: Provisioning Script

`create_xml_curator_wsl2_vm.ps1` builds a new, hardened Ubuntu instance under WSL2 and deploys [xml_curator_for_BP_MD](https://github.com/auroscope/xml_curator_for_BP_MD) (a Flask application) in it, served by gunicorn and reachable from the host and the local network through WSL mirrored networking. It is based on `create_wsl2_ubuntu_sandbox.ps1` (see `readme_wsl2_ubuntu_sandbox.md` for the shared SSH and hardening details) and adds the application deployment, its firewall rules and an optional keep-alive task. The application itself is installed by the repository's `install.sh`, the same installer used on any Debian/Ubuntu system.

The script is interactive. It creates a **new** instance and never modifies an existing one; deleting old instances is opt-in and needs a typed confirmation.

## Contents

1. [What it does](#what-it-does)
2. [Requirements](#requirements)
3. [Running it](#running-it)
4. [Prompts and defaults](#prompts-and-defaults)
5. [Execution flow](#execution-flow)
6. [Resulting configuration](#resulting-configuration)
7. [Accessing the application](#accessing-the-application)
8. [Keeping the instance running](#keeping-the-instance-running)
9. [Operating the application](#operating-the-application)
10. [Security notes](#security-notes)
11. [Troubleshooting](#troubleshooting)
12. [Cleanup and rollback](#cleanup-and-rollback)
13. [Known limitations](#known-limitations)

## What it does

- Lists existing WSL instances and optionally deletes selected ones.
- Installs a new Ubuntu instance under a name you choose (default `ubuntu_xml_curator`).
- Ensures `networkingMode=mirrored` in `%USERPROFILE%\.wslconfig`.
- Creates a non-root sudo user and locks the root password.
- Enables systemd, installs and hardens OpenSSH, and configures fail2ban.
- Clones xml_curator_for_BP_MD to `~/xml_curator_for_BP_MD` and runs its `install.sh`, which installs the system packages the app uses (Ghostscript, poppler-utils, Tesseract OCR with English, DejaVu fonts and, optionally, LibreOffice Writer), creates a `.venv` with `requirements.txt`, checks the installation and sets up the service.
- Runs the app under gunicorn as the systemd service `xml-curator`, as the new user, started at instance boot.
- Creates Windows Defender Firewall and Hyper-V firewall rules for both SSH and the application port.
- Optionally creates a scheduled task to keep the instance running.

## Requirements

| Item | Requirement |
|---|---|
| OS | Windows 11. The Hyper-V firewall step needs 22H2 or later and is skipped with a warning otherwise. |
| WSL | WSL 2 version 2.4.4 or later (`wsl --version`); `wsl --install --name` needs it. On a PC with no WSL, or an older one, the script's preflight check offers to install or update it (`wsl --install --no-distribution` / `wsl --update`). **A reboot may be required**; if so the script stops and asks you to restart Windows and run it again. |
| Shell | Windows PowerShell 5.1, **elevated** (the script throws if not Administrator). |
| Network | Internet access to the Ubuntu image, apt, GitHub and PyPI. The repository must remain public, as the script has no credential handling. |
| Disk | About 2 GB for the instance with LibreOffice (about 1.4 GB without), plus the XML files you upload. |
| Virtualisation | Enabled in firmware and Windows. |
| Optional | An SSH public key at `%USERPROFILE%\.ssh\id_ed25519.pub` (or another path you supply). |

## Running it

The script can live in any folder. From an elevated PowerShell:

```powershell
powershell.exe -NoExit -ExecutionPolicy Bypass -File .\create_xml_curator_wsl2_vm.ps1
```

Or with the full path:

```powershell
powershell.exe -NoExit -ExecutionPolicy Bypass -File "<path-to-script>\create_xml_curator_wsl2_vm.ps1"
```

As a shortcut target, use the full path and set the shortcut to run as administrator. `-ExecutionPolicy Bypass` applies to that one process only. If the script was copied from another machine, run `Unblock-File` on it first.

## Prompts and defaults

Press Enter to accept a default. Prompts appear in this order.

| Prompt | Default | Notes |
|---|---|---|
| Numbers to DELETE | none | Only shown if instances exist. Each deletion needs the exact instance name typed to confirm. |
| Name for the NEW WSL instance | `ubuntu_xml_curator` | Letters, digits, `_`, `-`, `.`. Rejected if the name already exists. |
| SSH port | `22`, or `2222` if something already listens on TCP 22 | Rejected if in use on the host. |
| Port for xml_curator_for_BP_MD (gunicorn) | `5008` | 1024-65535. Rejected if equal to the SSH port or already in use on the host. |
| Allow xml_curator_for_BP_MD from | `1` Local subnet only | `2` = Anywhere. Applies to the Windows Defender rule (Private and Domain profiles in both cases). |
| Linux username | `defaultuser` | Set by `$DefaultUser`. Cannot be `root`. |
| Password | blank = generate | Confirmed when typed. A generated 24-character password is shown once at the end. |
| Public key file | `%USERPROFILE%\.ssh\id_ed25519.pub` | If missing, SSH is password-only. |
| Install LibreOffice Writer | **Yes** | Renders RTF letters with their pictures and layout, and is needed for RTF previews when shrinking. Without it RTFs are shown as plain text. A few hundred MB. |
| Create keep-alive scheduled task | **No** | See [Keeping the instance running](#keeping-the-instance-running). |
| Install/update WSL now | Yes | Only asked if WSL is missing or older than 2.4.4. Declining lets you continue anyway, but the install step will probably fail. |
| Continue anyway (Windows build older than 22H2) | No | Only asked on builds below 22621, where mirrored networking and the Hyper-V firewall are unsupported. |
| Run `wsl --shutdown` now | Yes | Only asked if `.wslconfig` actually changed. |

The defaults (`$BaseDistro`, `$DefaultName`, `$RepoUrl`, `$Branch`, `$DefaultAppPort`, `$DefaultUser`, `$PubKeyDefault`) are at the top of the script.

## Execution flow

| Step | Action |
|---|---|
| Preflight | Checks the Windows build (22H2 or later) and the WSL version. Offers to install or update WSL; if that needs a reboot, the script stops and asks you to restart Windows and run it again. Nothing else has been changed at that point. |
| 0 | Interactive setup (above). |
| 1 | Prints the plan. |
| 2 | Ensures `networkingMode=mirrored` in `.wslconfig`, preserving other settings. Offers `wsl --shutdown` only if the file changed (stops all running distros without modifying them). |
| 3 | `wsl --install -d Ubuntu --name <name> --no-launch`, then verifies the instance is registered. |
| 4 | Creates the user, sets the password over stdin, adds to `sudo`, locks root. |
| 5 | Writes `/etc/wsl.conf` (default user, `systemd=true`) and restarts the instance so systemd is active. |
| 6 | Installs `git net-tools curl openssh-server fail2ban`. |
| 6a | Clones the app as the new user, then runs its `install.sh --yes --service --port <app-port> --user <user> --name xml-curator` as root: apt packages (Ghostscript, poppler-utils, Tesseract + English, DejaVu fonts, Python venv/pip, LibreOffice Writer unless declined), the `.venv` created as the user with `requirements.txt`, a self-check, and the `xml-curator` systemd service. Then waits up to 30 seconds for an HTTP response; on failure it prints the service journal and stops. |
| 7 | SSH hardening drop-in, host keys, `sshd -t`, service switch from `ssh.socket`, fail2ban jail. |
| 8 | Windows Firewall and Hyper-V firewall rules for SSH. |
| 8b | Windows Firewall and Hyper-V firewall rules for the application port. |
| 8c | Optional: the keep-alive scheduled task. |
| 9 | Verification (service states, effective sshd values, app HTTP status) and summary. The instance is left running. |

Any failing step aborts the script. Passwords and files are passed over stdin with carriage returns stripped, and the embedded deploy script is written to a temporary file in the instance, so tools cannot consume the script from stdin.

## Resulting configuration

### Windows host

| Item | Detail |
|---|---|
| `%USERPROFILE%\.wslconfig` | `[wsl2]` gains `networkingMode=mirrored` (other settings preserved). |
| SSH firewall rule | `WSL2 <name> SSH Server (TCP <ssh-port>)`: inbound, Private and Domain. |
| SSH Hyper-V rule | `WSL2 <name> SSH Server (TCP <ssh-port>, Hyper-V)`. |
| App firewall rule | `WSL2 <name> xml_curator_for_BP_MD (TCP <app-port>)`: inbound, Private and Domain, remote address `LocalSubnet` or `Any` as chosen. |
| App Hyper-V rule | `WSL2 <name> xml_curator_for_BP_MD (TCP <app-port>, Hyper-V)`. It cannot be limited to the subnet; the Defender rule does the scoping. |
| Scheduled task (optional) | `WSL2 <name> keep-alive`. |

### Inside the instance

| Path or item | Purpose |
|---|---|
| `/home/<user>/xml_curator_for_BP_MD` | The cloned repository. |
| `/home/<user>/xml_curator_for_BP_MD/.venv` | Python virtual environment with the app's dependencies. |
| `/home/<user>/xml_curator_for_BP_MD/data` | Uploaded originals (read-only), curated copies, saved copies, change logs and caches. Untracked by git. |
| `/etc/systemd/system/xml-curator.service` | Runs gunicorn as `<user>`: `--workers 3 --threads 3 --timeout 300 --bind 0.0.0.0:<app-port> app:app`, `Restart=on-failure`, enabled at boot. |
| `/etc/wsl.conf` | Default user and `systemd=true`. |
| `/etc/ssh/sshd_config.d/99-hardening.conf` | sshd hardening (no root login, `AllowUsers`, 3 attempts, forwarding off, and so on). |
| `/etc/fail2ban/jail.local` | sshd jail (1h ban, 4 failures in 10 minutes). |

The service runs gunicorn rather than Flask's development server (`python app.py`), which is meant for development only.

## Accessing the application

| From | URL |
|---|---|
| The Windows host | `http://localhost:<app-port>/` |
| Another machine on the LAN | `http://<host-LAN-IP>:<app-port>/` |
| SSH from the host | `ssh -p <ssh-port> <user>@127.0.0.1` |
| SSH from the LAN | `ssh -p <ssh-port> <user>@<host-LAN-IP>` |

The host cannot reach the instance through its own LAN IP. Windows treats a connection to its own address as local and nothing on Windows listens there. Use `localhost` or `127.0.0.1` on the host. From other machines the Windows and Hyper-V rules must allow the port, and the network must be a Private or Domain profile.

## Keeping the instance running

WSL stops an instance once nothing is attached to it, and systemd services do not count as attached. Without something keeping it alive, the app and SSH go offline after you close your last terminal, and nothing starts the instance after a reboot.

The optional keep-alive task addresses this:

- Name: `WSL2 <name> keep-alive`.
- Trigger: at logon of the user who ran the script.
- Action: a hidden `powershell.exe` running `wsl.exe -d <name> --exec /bin/sleep infinity`.
- Runs as that user (interactive, limited), with no time limit, restarting up to 3 times on failure. It is started immediately when created.

Limitation: it runs only while that user is logged in. After a reboot, the app stays down until that user logs in. For an always-on server, consider configuring Windows automatic logon, or a different approach suited to your environment. Remove the task with `Unregister-ScheduledTask -TaskName "WSL2 <name> keep-alive" -Confirm:$false`.

## Operating the application

Run these from the Windows host (replace `<name>` and `<user>`).

```powershell
# Status and recent logs
wsl -d <name> -u root systemctl status xml-curator --no-pager
wsl -d <name> -u root journalctl -u xml-curator -n 50 --no-pager

# Restart / stop / start
wsl -d <name> -u root systemctl restart xml-curator

# Update the app from GitHub, refresh dependencies and the service, restart
wsl -d <name> -u <user> -- bash -c "cd ~/xml_curator_for_BP_MD && git pull --ff-only"
wsl -d <name> -u root /home/<user>/xml_curator_for_BP_MD/install.sh --yes --service --port <app-port> --user <user> --name xml-curator
```

A restart interrupts requests in progress, including background preparation of shrink previews and OCR (it resumes when the page is opened again). Uploaded files are stored in the repository's `data/` folder, which is untracked by git, so `git pull` and restarts do not remove them. A `git pull` can still fail if local changes conflict.

To open a shell in the instance as the user: `wsl -d <name> -u <user>`.

## Security notes

- Network exposure: the app listens on `0.0.0.0` inside the shared network. Reach is controlled by the Windows Firewall rule: **Local subnet only** (default) or **Anywhere**, always limited to Private and Domain profiles. Choose the narrowest scope that works.
- The application handles patient health record data. Treat the host, instance and network accordingly.
- The app has no login of its own: assume anyone who can reach the port can use it and see the patient files uploaded to it. Its session key is generated per installation (`data/.secret_key`).
- gunicorn is used instead of Flask's debug server (no Werkzeug debugger exposure).
- SSH posture is as in `readme_wsl2_ubuntu_sandbox.md`: no root login, `AllowUsers`, key and password auth, 3 attempts, forwarding disabled, fail2ban, Private and Domain only. Password login is on by design; consider key-only once your key works.
- Passwords are not stored in the script. They are prompted for, or generated and shown once.

## Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| Script ends with "did not respond on port ..." | Read the journal it prints, or `journalctl -u xml-curator`. Common causes: a dependency failure, or a port clash inside the instance. |
| `install.sh` stops with "Python 3.11 or newer is needed" | The Ubuntu image is too old. Use a current Ubuntu (24.04 has Python 3.12). |
| `apt-get update reported errors` | Usually a third-party repository; `install.sh` carries on, since its packages come from Ubuntu's own repositories. |
| Script stopped after the preflight and asked you to restart Windows | WSL was just installed or updated and is not usable until a reboot. Restart Windows and run the script again. |
| `wsl --install --name failed` | WSL is too old. Run `wsl --update` and retry. Nothing existing has been modified. |
| `git clone` fails | No internet or GitHub access from the instance, a proxy, or the repository is no longer public. |
| `pip install` fails | PyPI unreachable, or a package needs a newer Python. Check the pip output. |
| App works on the host (`localhost`) but not from the LAN | Check the network profile (`Get-NetConnectionProfile`) is Private or Domain. Confirm the Windows and Hyper-V rules exist. Check the app firewall scope (`LocalSubnet` excludes other subnets). Check upstream firewalls. |
| Hyper-V rule skipped | The cmdlets need Windows 11 22H2 or later. |
| Port already in use | Mirrored mode shares one network between the host and all instances. Pick another port. |
| App and SSH stop after you close your terminal | WSL stopped the idle instance. See [Keeping the instance running](#keeping-the-instance-running). |
| Mirrored mode not active | `.wslconfig` applies only after `wsl --shutdown`. Check it contains `networkingMode=mirrored` under `[wsl2]`. |
| Browser shows a connection error on `<host-LAN-IP>` from the host itself | Expected; use `localhost` on the host. |
| Locked out of SSH by fail2ban | `wsl -d <name> -u root fail2ban-client set sshd unbanip <ip>`, or wait out the 1 hour ban. |

Useful checks:

```powershell
wsl --list --verbose
Get-NetFirewallRule -DisplayName "WSL2 <name>*"
Get-NetFirewallHyperVRule -Name "WSL2 <name>*"
Get-NetTCPConnection -LocalPort <app-port> -State Listen
Get-ScheduledTask -TaskName "WSL2 <name> keep-alive"
```

## Cleanup and rollback

Remove an instance and everything the script created for it (replace `<name>`, `<ssh-port>` and `<app-port>`):

```powershell
wsl --terminate <name>
wsl --unregister <name>        # permanently deletes the instance and all its data, including uploaded files
Remove-NetFirewallRule -DisplayName "WSL2 <name> SSH Server (TCP <ssh-port>)"
Remove-NetFirewallRule -DisplayName "WSL2 <name> xml_curator_for_BP_MD (TCP <app-port>)"
Remove-NetFirewallHyperVRule -Name "WSL2 <name> SSH Server (TCP <ssh-port>, Hyper-V)"
Remove-NetFirewallHyperVRule -Name "WSL2 <name> xml_curator_for_BP_MD (TCP <app-port>, Hyper-V)"
Unregister-ScheduledTask -TaskName "WSL2 <name> keep-alive" -Confirm:$false
```

To revert mirrored networking, remove `networkingMode=mirrored` from `%USERPROFILE%\.wslconfig` and run `wsl --shutdown`. Other tooling or instances may rely on it.

## Known limitations

- The script creates new instances only. To change settings later (ports, scope), edit the rules and service manually or build a new instance and delete the old one.
- One process can bind a given port at a time; all instances and the host share the network in mirrored mode.
- `wsl --shutdown` (offered only when `.wslconfig` changes) stops every running distro, including ones you did not create.
- Deleted instances cannot be recovered; the script does not take backups.
- The keep-alive task only works while the user is logged in.
- The app needs Python 3.11 or newer (Ubuntu 24.04 has 3.12).
- The script has no support for private repositories or a different application. Change `$RepoUrl`, `$AppName` and `$ServiceName` at the top to reuse it for another Flask project, and review the deploy step for app-specific assumptions.
