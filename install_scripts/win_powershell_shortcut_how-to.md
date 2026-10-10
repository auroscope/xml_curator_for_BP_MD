# How to run the install scripts as Administrator on Windows

The PowerShell install scripts in this folder change system settings (WSL, firewall rules, scheduled tasks), so Windows requires them to run **as Administrator**. If you just double-click one, or right-click and choose **Run with PowerShell**, it runs without administrator rights and stops with *"This script must be run as Administrator."*

This guide shows three ways to launch a script properly. **Method 1 (a desktop shortcut)** is the one most people should use.

## Before you start

1. **Save the script somewhere permanent**, for example `C:\Scripts\`. Avoid the Downloads folder or any path that changes. The examples below use `C:\Scripts\create_xml_curator_wsl2_vm.ps1`; replace it with your own path and script name.
2. **Unblock the file if you downloaded it.** Windows marks downloaded files as coming from the internet. Open PowerShell, run the following (with your own path), and press Enter. You only need to do this once per file.

```powershell
Unblock-File "C:\Scripts\create_xml_curator_wsl2_vm.ps1"
```

3. **Read the script first.** It installs software and changes firewall rules, so only run scripts you have looked at and trust.

## Method 1: a desktop shortcut that always runs as Administrator (recommended)

1. Right-click an empty space on the Desktop and choose **New > Shortcut**.
2. In **Type the location of the item**, paste the following line, changing the path to your script, then click **Next**:

```
powershell.exe -NoExit -ExecutionPolicy Bypass -File "C:\Scripts\create_xml_curator_wsl2_vm.ps1"
```

3. Give the shortcut a name, for example `Create xml_curator VM`, and click **Finish**.
4. Right-click the new shortcut and choose **Properties**. On the **Shortcut** tab, click **Advanced...**, tick **Run as administrator**, then click **OK** twice.
5. Double-click the shortcut. When Windows asks *"Do you want to allow this app to make changes to your device?"*, click **Yes**. A blue PowerShell window opens and the script starts asking its questions.

What the options mean:

| Part | Meaning |
|---|---|
| `-NoExit` | Keeps the window open when the script finishes, so you can read the results. |
| `-ExecutionPolicy Bypass` | Lets this one script run without changing your computer's script policy. It applies to that window only. |
| `-File "..."` | The script to run. Keep the quotes, especially if the path contains spaces. |

You can make one shortcut per script. Change only the path at the end of the line.

## Method 2: open an Administrator terminal and run the script by hand

Use this for a one-off run, or if you prefer to see exactly what you are typing.

1. Right-click the **Start** button and choose **Terminal (Admin)** (or **Windows PowerShell (Admin)**). Click **Yes** at the prompt.
2. Change to the folder holding the script, then run it. Adjust the folder and script name:

```powershell
cd C:\Scripts
```

```powershell
powershell.exe -NoExit -ExecutionPolicy Bypass -File .\create_xml_curator_wsl2_vm.ps1
```

If the window title does not start with **Administrator**, it is not elevated. Close it and open it again from the Start menu as shown.

## Method 3 (advanced): add "Run with PowerShell (Admin)" to the right-click menu

This edits the Windows registry, so only use it if you are comfortable with that. It adds an entry for **every** `.ps1` file on the PC, and scripts started this way ignore the PowerShell execution policy.

1. Open **Notepad** and paste the following, then save it as `ps1-admin.reg` (set **Save as type** to *All files* so it is not saved as `.txt`):

```
Windows Registry Editor Version 5.00

[HKEY_CLASSES_ROOT\Microsoft.PowerShellScript.1\Shell\runas]
@="Run with PowerShell (Admin)"

[HKEY_CLASSES_ROOT\Microsoft.PowerShellScript.1\Shell\runas\command]
@="\"C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe\" -NoExit -ExecutionPolicy Bypass -File \"%1\""
```

2. Double-click `ps1-admin.reg` and approve the prompts.
3. Right-click a `.ps1` file. On Windows 11, choose **Show more options** (or press **Shift+F10**) to reach the classic menu, where **Run with PowerShell (Admin)** now appears. Windows asks you to approve each run.

To remove it again, open **Terminal (Admin)** and run:

```powershell
Remove-Item "Registry::HKEY_CLASSES_ROOT\Microsoft.PowerShellScript.1\Shell\runas" -Recurse
```

## Troubleshooting

| Problem | What to do |
|---|---|
| "This script must be run as Administrator." | The window is not elevated. Use Method 1 and check **Run as administrator** is ticked, or Method 2 and confirm the title starts with **Administrator**. |
| The window flashes and closes | The shortcut is missing `-NoExit`, or the path in it is wrong. Check **Properties > Shortcut > Target**. |
| "The argument ... to the -File parameter does not exist" | The path in the shortcut does not match where the script is. Fix the path, and keep the quotes. If you renamed or moved the script, edit the shortcut to match. |
| "File ... cannot be loaded. The file is not digitally signed" or a "Security warning" | The file is blocked because it was downloaded. Run `Unblock-File` on it (see **Before you start**). |
| "Running scripts is disabled on this system" | Run the script with `-ExecutionPolicy Bypass` as in the examples above. Do not change the machine-wide policy for this. |
| You cannot edit the shortcut's **Target** box | Make sure you opened Properties on the **shortcut**, not on PowerShell itself, and that you are on the **Shortcut** tab. If it is still locked, delete the shortcut and create a new one with Method 1. |
| No UAC prompt appears and nothing happens | Check the shortcut still has **Run as administrator** ticked. If your account is not an administrator, ask the person who manages your PC to run the script. |
| The script says WSL needs a restart | Restart Windows, then run the same shortcut again. The script picks up where it left off. |

## Safety notes

- Only run scripts from a source you trust, and read them first. A script running as Administrator can change almost anything on your PC.
- The install scripts create new things and do not modify your existing WSL instances. Deleting an old instance is optional and needs you to type its exact name to confirm.
- `-ExecutionPolicy Bypass` affects only the window you launch it from. It does not weaken your PC's settings afterwards.

See the other guides in this folder for what each script does:

- `readme_create_xml_curator_wsl2_vm.md`: the script that builds a Linux VM and installs xml_curator_for_BP_MD.
- `readme_wsl2_ubuntu_sandbox.md`: the script that builds a hardened Linux VM with SSH, without the application.
