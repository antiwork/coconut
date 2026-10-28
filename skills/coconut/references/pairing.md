# Pair an iPhone with this Mac

The iPhone and the Mac pair once. After that, the iPhone reaches the Mac through the relay, end-to-end encrypted. Normal updates do not need a new pairing.

## Pair with a code from the iPhone

Onboarding on the iPhone shows one message for Hermes. The person sends it to you, for example in Telegram:

```
curl -fsSL https://coconut-updates.gumroad.workers.dev/install | bash -s <code>
```

Run the message with `terminal`. The script does these steps:

1. It installs the newest notarized Coconut.app, if this Mac has no usable one. It checks the download, the code signature, the team ID, and Gatekeeper.
2. It opens the app, which sets up Coconut. The setup can take a few minutes.
3. It waits for the service and the relay.
4. It turns on automatic updates.
5. It runs `coconut pair <code>`. The iPhone then pairs by itself.
6. It prints a note for Hermes: how to turn automatic updates off, and how to update Coconut.

Tell the person the note. Ask whether to keep automatic updates on.

The script is safe to run again. It keeps an installed Coconut.app that can do the setup.

If Coconut is already installed, run only the pair step:

```
/Applications/Coconut.app/Contents/Helpers/coconut pair <code>
```

The command prints `Sending the setup to the iPhone.`, then `Waiting for the iPhone to pair.`, then `The iPhone is paired with this Mac.`

## Rules for the code

- The code has 26 letters and digits. A different text is not a Coconut code.
- The code works once, for ten minutes, and only for the iPhone that made it.
- Telegram does not encrypt bot chats end to end. After the iPhone pairs, the code is useless.
- If the code expired, the command says so. Ask the person to make a new code on the iPhone.

## Pair with a setup link or a QR code

Use this when Hermes cannot run commands, or when the person asks for a link:

1. Run `coconut pair` with no code. It prints a one-time setup link. The link works for ten minutes.
2. Send the link only to the person who pairs. The person opens it on the iPhone.

The person can also do it without Hermes. On the iPhone, **Need help?** shows the download link of Coconut.app. The person installs the app, chooses **Pair iPhone…** in its menu bar menu, and scans the QR code in Coconut on the iPhone.

## Pair again

- A new installation of the iPhone app opens onboarding, which shows a new message.
- On an iPhone that is already paired, the person opens **Settings** in Coconut and taps **Pair a new Mac**. That opens the same screen.
- If the iPhone already has a pairing, Coconut asks before a new link replaces it.

## Pitfalls

- `coconut pair` needs a running service and a relay route. If `coconut status` shows `Relay: not installed`, the setup is not complete. See `references/troubleshooting.md`.
- Do not store a code or a setup link in memory or in a file.
