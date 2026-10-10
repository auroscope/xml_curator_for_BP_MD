# WSL2 Ubuntu Sandbox Provisioning Script

`create_wsl2_ubuntu_sandbox.ps1` builds a new, hardened Ubuntu instance under WSL2 and exposes it over SSH on the host's network using WSL mirrored networking. It is interactive, opt-in for anything destructive, and designed so existing WSL instances are never modified unless you explicitly choose to delete them.

It contains no application. To install xml_curator_for_BP_MD in the new instance afterwards, open a shell in it as the user (`wsl -d <name>`) and run [`setup_xml_curator.sh`](setup_xml_curator.sh); or use `create_xml_curator_wsl2_vm.ps1`, which does both in one go.

## Contents

1. [What it does](#what-it-does)
2. [Requirements](#requirements)
3. [Running it](#running-it)
4. [Prompts and defaults](#prompts-and-defaults)
5. [Execution flow](#execution-flow)
6. [Resulting configuration](#resulting-configuration)
7. [Connecting](#connecting)
8. [Security posture](#security-posture)
9. [Troubleshooting](#troubleshooting)
10. [Manual cleanup and rollback](#manual-cleanup-and-rollback)
11. [Known limitations](#known-limitations)

## What it does

- Lists existing WSL instances and optionally deletes selected ones.
- Installs a **new** Ubuntu instance under a name you choose (default `ubuntu_sandbox`).
- Ensures `networkingMode=mirrored` in `%USERPROFILE%\.wslconfig`.
- Creates a non-root sudo user, locks the root password.
- Enables systemd, installs OpenSSH server and fail2ban, applies an sshd hardening drop-in.
- Optionally installs Microsoft ODBC Driver 18 for SQL Server plus `sqlcmd`/`bcp`.
- Creates scoped Windows Defender Firewall and Hyper-V firewall rules for the chosen SSH port.

## Requirements

| Item | Requirement |
|---|---|
| OS | Windows 11. The Hyper-V firewall step needs 22H2 or later and is skipped with a warning otherwise. |
| WSL | WSL 2 version 2.4.4 or later (`wsl --version`); `wsl --install --name` needs it. On a PC with no WSL, or an older one, the script's preflight check offers to install or update it (`wsl --install --no-distribution` / `wsl --update`). **A reboot may be required**; if so the script stops and asks you to restart Windows and run it again. |
| Shell | Windows PowerShell 5.1, **elevated** (the script throws if not Administrator). |
| Network | Internet access for the Ubuntu image, apt, and (optionally) packages.microsoft.com. |
| Virtualisation | Enabled in firmware and Windows (WSL2 working). |
| Optional | An SSH public key at `%USERPROFILE%\.ssh\id_ed25519.pub` (or another path you supply). |

## Running it

The script can live in any folder. From an elevated PowerShell, change to that folder and run:

```powershell
powershell.exe -NoExit -ExecutionPolicy Bypass -File .\create_wsl2_ubuntu_sandbox.ps1
```

Or use the full path to the script:

```powershell
powershell.exe -NoExit -ExecutionPolicy Bypass -File "<path-to-script>\create_wsl2_ubuntu_sandbox.ps1"
```

The same command works as a desktop shortcut target. In that case use the full path, and set the shortcut to run as administrator (Properties > Shortcut > Advanced).

`-ExecutionPolicy Bypass` applies to that one process only and does not change the machine's policy. If the script was downloaded or copied from another machine, you may also need `Unblock-File .\create_wsl2_ubuntu_sandbox.ps1`.

## Prompts and defaults

Prompts appear in this order. Pressing Enter accepts the default.

| Prompt | Default | Notes |
|---|---|---|
| Numbers to DELETE | none | Only shown if instances exist. Comma-separated list. Each deletion needs the exact instance name typed to confirm. |
| Name for the NEW WSL instance | `ubuntu_sandbox` | Letters, digits, `_`, `-`, `.`. Rejected if the name already exists. |
| SSH port | `22`, or `2222` if something is already listening on TCP 22 | 1-65535. Rejected if the port is already in use on the host. |
| Linux username | `defaultuser` (set by `$DefaultUser`) | Must match `^[a-z_][a-z0-9_-]{0,31}$` and cannot be `root`. |
| Password | blank = generate | Entered as a secure string and confirmed. A generated 24-character password is shown once at the end. Warns below 12 characters. |
| Public key file | `%USERPROFILE%\.ssh\id_ed25519.pub` | If missing, key login is skipped and only password login is configured. |
| Install ODBC Driver 18 | Yes | Declining skips step 6b entirely. |
| Install/update WSL now | Yes | Only asked if WSL is missing or older than 2.4.4. Declining lets you continue anyway, but the install step will probably fail. |
| Continue anyway (Windows build older than 22H2) | No | Only asked on builds below 22621, where mirrored networking and the Hyper-V firewall are unsupported. |
| Run `wsl --shutdown` now | Yes | Only asked if `.wslconfig` actually changed. |

The defaults live at the top of the script (`$BaseDistro`, `$DefaultName`, `$DefaultUser`, `$PubKeyDefault`).

## Execution flow

| Step | Action |
|---|---|
| Preflight | Checks the Windows build (22H2 or later) and the WSL version. Offers to install or update WSL; if that needs a reboot, the script stops and asks you to restart Windows and run it again. Nothing else has been changed at that point. |
| 0 | Interactive setup: list and optionally delete instances, then collect name, port, user, password, key and ODBC choice. |
| 1 | Prints the plan. |
| 2 | Ensures `networkingMode=mirrored` under `[wsl2]` in `.wslconfig` while preserving other settings. If a change is made, offers `wsl --shutdown` (this stops all running distros, but does not modify them). |
| 3 | `wsl --install -d Ubuntu --name <name> --no-launch`. Verifies the instance is registered afterwards. |
| 4 | Creates the user (if absent), sets the password via `chpasswd` over stdin, adds the user to `sudo`, and locks root with `passwd -l root`. |
| 5 | Writes `/etc/wsl.conf` (default user, `systemd=true`), then terminates the instance so systemd starts on the next command. |
| 6 | `apt-get update`, then installs `git net-tools curl openssh-server fail2ban`. |
| 6b | Optional. Adds the Microsoft repo for the detected Ubuntu version and installs `msodbcsql18 mssql-tools18 unixodbc-dev`. |
| 7 | Installs the authorised key, writes the sshd drop-in, runs `ssh-keygen -A` and `sshd -t`, switches from `ssh.socket` to `ssh.service`, then configures and starts fail2ban. |
| 8 | Creates the Windows Firewall and Hyper-V firewall rules. |
| 9 | Prints `systemctl is-active` and the effective sshd values, terminates the instance, and prints a summary. |

Any failing WSL command aborts the script (`$ErrorActionPreference = 'Stop'` plus exit-code checks). Secrets and keys are piped via stdin with carriage returns stripped, so they do not appear on a command line and Windows line endings cannot corrupt files.

## Resulting configuration

### Windows host

| Item | Detail |
|---|---|
| `%USERPROFILE%\.wslconfig` | `[wsl2]` section gains `networkingMode=mirrored`. Other settings are preserved. Written as UTF-8 without BOM. |
| Windows Firewall rule | `WSL2 <name> SSH Server (TCP <port>)`: inbound, allow, TCP, profiles **Private and Domain only**. |
| Hyper-V firewall rule | `WSL2 <name> SSH Server (TCP <port>, Hyper-V)`: inbound, TCP, scoped to the WSL VM creator ID `{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}`. |

Rule names include the instance and port, so re-running the script only replaces its own rules.

### Inside the new instance

| Path | Purpose |
|---|---|
| `/etc/wsl.conf` | `[user] default=<user>`, `[boot] systemd=true`. |
| `/etc/ssh/sshd_config.d/99-hardening.conf` | sshd hardening drop-in (below). |
| `/home/<user>/.ssh/authorized_keys` | Your public key (only if one was supplied). Mode 600. |
| `/etc/fail2ban/jail.local` | sshd jail: ban 1h, find 10m, max retry 4, systemd backend. |
| `/etc/profile.d/mssql-tools18.sh` | Adds `/opt/mssql-tools18/bin` to PATH (only if ODBC was installed). |
| Microsoft apt repo | Added by `packages-microsoft-prod.deb` (only if ODBC was installed). |

### sshd drop-in

```
Port <port>
PermitRootLogin no
AllowUsers <user>
PasswordAuthentication yes
PubkeyAuthentication yes
PermitEmptyPasswords no
KbdInteractiveAuthentication no
MaxAuthTries 3
LoginGraceTime 30
X11Forwarding no
AllowAgentForwarding no
AllowTcpForwarding no
ClientAliveInterval 300
ClientAliveCountMax 2
```

Stock Ubuntu includes `sshd_config.d/*.conf` at the top of `sshd_config`, so these values take precedence. The script adds an `Include` line if it is missing. Ubuntu 24.04's `ssh.socket` ignores `Port`, so the script disables the socket and runs `ssh.service` instead.

## Connecting

With mirrored networking the instance shares the host's network stack.

| From | Command |
|---|---|
| The Windows host itself | `ssh -p <port> <user>@127.0.0.1` |
| Another machine on the LAN | `ssh -p <port> <user>@<host LAN IP>` |

**Important:** the host cannot reach the instance using its own LAN IP. Windows treats a connection to its own address as local and nothing on Windows listens on that port. Use `127.0.0.1` from the host. This is expected behaviour, not a fault.

Convenience entry for `%USERPROFILE%\.ssh\config` on the host:

```
Host wsl
    HostName 127.0.0.1
    Port <port>
    User <user>
```

## Security posture

| Control | State |
|---|---|
| Root SSH login | Disabled (`PermitRootLogin no`). |
| Root password | Locked. Use `sudo`. |
| SSH access | Restricted to the one created user (`AllowUsers`). |
| Authentication | Public key **and** password both enabled (a deliberate choice). Key-only is stronger; set `PasswordAuthentication no` in the drop-in once your key works. |
| Brute-force protection | fail2ban plus `MaxAuthTries 3` and a 30 second login grace time. |
| Forwarding | TCP, agent and X11 forwarding disabled. |
| Firewall scope | Inbound allowed on Private and Domain profiles only; Public networks are blocked. |
| Credentials in the script | None stored. The password is prompted for, or generated and shown once. |

The password is held as a plain string in the PowerShell process while the script runs and is cleared at the end.

## Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| Script exits immediately with "must be run as Administrator" | Relaunch PowerShell elevated. |
| Script stopped after the preflight and asked you to restart Windows | WSL was just installed or updated and is not usable until a reboot. Restart Windows and run the script again. |
| `wsl --install --name failed` | WSL is too old. Run `wsl --update` and retry. Nothing existing has been modified at that point. |
| "not registered after install" | Check `wsl --list --verbose`. If the instance exists under a different name, re-run with that name or remove it. |
| SSH to the host's LAN IP fails from the host | Expected; use `127.0.0.1` (see [Connecting](#connecting)). |
| SSH works locally but not from the LAN | Check the network profile is Private or Domain (`Get-NetConnectionProfile`). Public is blocked by design. Confirm the Windows and Hyper-V rules exist. Check any hardware or router firewall. |
| Hyper-V firewall rule was skipped | The cmdlets need Windows 11 22H2 or later. As a fallback, see `Set-NetFirewallHyperVVMSetting` for the WSL VM creator ID, but prefer scoped rules. |
| Port in use | Mirrored mode shares one network between the host and all distros, so only one process can bind a port. Pick another port. If an old instance runs sshd on 22, use 2222 or stop that instance. |
| Mirrored mode not active | `.wslconfig` takes effect only after `wsl --shutdown`. Verify the file contains `networkingMode=mirrored` under `[wsl2]`. |
| Locked out after 3 failed attempts or by fail2ban | `wsl -d <name> -u root fail2ban-client set sshd unbanip <ip>` or wait out the 1 hour ban. |
| ODBC connections fail with certificate errors | Driver 18 encrypts by default. Use `Encrypt=yes` with a trusted certificate, or `TrustServerCertificate=yes` for self-signed servers. |
| `sqlcmd` not found | `/etc/profile.d/mssql-tools18.sh` applies at login. Start a new login shell or `source` it. |
| Unexpected `^M` or line-ending errors when editing scripts in WSL | Windows line endings. Run `sed -i 's/\r$//' file`. |

Useful checks:

```powershell
wsl --list --verbose
wsl -d <name> -u root systemctl is-active ssh fail2ban
wsl -d <name> -u root sshd -T
Get-NetFirewallRule -DisplayName "WSL2 <name>*"
Get-NetTCPConnection -LocalPort <port> -State Listen
```

## Manual cleanup and rollback

Remove an instance and its rules (replace `<name>` and `<port>`):

```powershell
wsl --terminate <name>
wsl --unregister <name>        # permanently deletes all data in the instance
Remove-NetFirewallRule -DisplayName "WSL2 <name> SSH Server (TCP <port>)"
Remove-NetFirewallHyperVRule -Name "WSL2 <name> SSH Server (TCP <port>, Hyper-V)"
```

To revert mirrored networking, remove `networkingMode=mirrored` from `%USERPROFILE%\.wslconfig` and run `wsl --shutdown`. Be aware that other tooling or instances may now rely on it.

## Known limitations

- Only one instance can use a given port at a time, and all instances share the host's network in mirrored mode.
- `wsl --shutdown` (offered only when `.wslconfig` changes) stops every running distro, including ones you did not create, though it does not modify them.
- Deleted instances cannot be recovered. The script does not take backups.
- The script targets Ubuntu images. The ODBC step checks `/etc/os-release` and aborts on unsupported releases (supported list: Ubuntu 18.04, 20.04, 22.04, 24.04, 26.04).
- The delete-name regex that tags "previous sandbox" instances is cosmetic only (`^ubuntu_sandbox\d*$`); deletion applies to anything you select.
