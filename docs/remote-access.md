# Remote iPhone access

Coconut must work over cellular and other Wi-Fi networks. Local execution and storage refer to the Mac, not the phone's network.

Use HTTPS over Tailscale. The phone connects to Coconut's API on port 8960. SSH supports host administration only.

Both devices need internet access and permission to communicate within Tailscale. The Mac must remain awake.

For installation from this development Mac, use [the installation and update workflow](install-and-update.md).
Its QR pairing avoids manual entry of the address, certificate fingerprint, and code.

Tailscale tries direct connections and uses encrypted relays when required. This setup needs no router port forwarding or public endpoint.

Sources: [Tailscale connections](https://tailscale.com/docs/reference/connection-types) and [iOS VPN On Demand](https://tailscale.com/docs/features/client/ios-vpn-on-demand).

## Prepare the devices

1. Connect the Mac mini through Ethernet when available.
2. Enable **Prevent automatic sleeping when the display is off** in macOS Energy settings.
3. Install the standalone Tailscale app on the Mac mini.
4. Install Tailscale on the iPhone.
5. Sign both devices into the same private Tailscale network.
6. Enable Tailscale **VPN On Demand** for Wi-Fi and cellular on the iPhone.
7. Copy the Mac mini's full DNS name from Tailscale.

Tailscale recommends its [standalone Mac app](https://tailscale.com/docs/concepts/macos-variants). Apple documents the [sleep setting](https://support.apple.com/en-gb/guide/mac-help/mchle41a6ccd/mac).

For an unattended Mac, consider disabling Tailscale key expiry for that device. Keep access limited to your devices and account.

Expired device keys prevent connections until reauthentication. See [Tailscale key expiry](https://tailscale.com/docs/features/access-control/key-expiry).

## Configure Coconut

Install Coconut and its managed Hermes runtime in the Mac mini's final folder. Use the installation steps in the README.

Replace `coconut.example.ts.net` below with the full DNS name copied from Tailscale. Run these commands from the Coconut folder.

```sh
python3 scripts/host.py setup --address https://coconut.example.ts.net:8960
python3 scripts/host.py start
```

Setup saves the address under `.runtime/host`. Subsequent host commands reuse it. The generated certificate includes that exact DNS name.

Enter the displayed address, certificate fingerprint, and pairing code in Coconut Settings. The code expires after five minutes.

If the code expires, run `python3 scripts/host.py pair`. The phone verifies the certificate before sending the code.

For an existing certificate that lacks the new address, stop Coconut before replacing the certificate:

```sh
python3 scripts/host.py stop
python3 scripts/host.py setup --address https://coconut.example.ts.net:8960 --replace-certificate
python3 scripts/host.py start
```

Replacement preserves the old certificate and key in a backup. It preserves conversations, decisions, receipts, and artifacts. Pair each phone again afterward.

`host.py stop` also unloads this installation's LaunchAgent when it runs.
After certificate replacement, restore an installed LaunchAgent with `launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/local.coconut.agent.plist"`.
The command preserves the saved service configuration.

The certificate lasts 365 days. Renewal requires the same explicit replacement and phone pairing process.

Do not put Cloudflare Tunnel or Tailscale Serve in front of this pinned TLS connection without changing certificate handling.

## Keep the Mac available

The normal launcher starts a background process. It survives terminal closure. It does not restart the service after a Mac restart.

After installing Coconut in its final folder, stop the manual service before installing the optional service:

```sh
python3 scripts/host.py stop
python3 scripts/host.py install-service
```

The LaunchAgent restarts the process after a crash. It starts after account login. It does not provide service before login.

Test login, FileVault recovery, and Tailscale reconnection on the actual Mac before relying on unattended restarts. Keep the Mac screen locked during normal operation.

The cleanup script removes Coconut's LaunchAgent, saved address, certificates, and local data. It does not uninstall Tailscale or Hermes.

## Verify remote use

1. Turn off Wi-Fi on the iPhone.
2. Confirm that Tailscale connects over cellular.
3. Open Coconut and confirm a fresh synchronization time.
4. Send a short task to one bot.
5. Close Coconut while that bot works.
6. Reopen Coconut and inspect the result.
7. Request context on a pending decision without answering it.
8. Reconnect after a backend restart and inspect the same decision.
9. Test switching between cellular and Wi-Fi during a conversation.
10. Test a full Mac restart separately.

These physical device checks remain required. Local TLS tests do not prove cellular access or Mac mini restart recovery.

## Notifications

Tailscale supplies connectivity. It does not keep Coconut running while iOS suspends the app.

Mac tasks continue while the phone app is closed. Reopening Coconut restores stored events and decisions.

The mini sends encrypted previews through Coconut's shared push service and APNs. See [notification setup](notifications.md).
