# Updating Jarvis

The app can update itself: it checks a manifest, verifies the download against
a public key compiled into the build, and installs on restart. **It is switched
off until you generate a signing key**, because the repository ships no key and
a plausible-looking one nobody generated would be worse than none — the same
reason the model catalogue leaves an unverified checksum empty.

With it off, Settings → Updates says so, and the app makes no network request
looking for updates at all.

> **Nothing here has been run end to end.** The code compiles, its
> configuration check is unit-tested, and the workflow's YAML is valid — but no
> release has been published, so no update has ever been downloaded, verified
> or installed. Publishing the first one is what proves it. The failure modes
> most likely to bite are listed at the end.

---

## The key is yours, and it matters

Whoever holds the private key can install software on every machine running
Jarvis. The updater's whole security model is that only you can produce an
artifact it will accept.

So: generate it yourself, on your own machine. Don't let anyone generate it for
you and hand it over — including me. A key that has passed through someone
else's computer is a key they have.

```powershell
cd apps/desktop
npm run tauri signer generate -- -w "$env:USERPROFILE\.tauri\jarvis.key"
```

It asks for a passphrase, then prints the **public** key and writes the
**private** key to that path.

- **Private key** (`jarvis.key`) and its passphrase: back up somewhere only you
  can reach. Lose them and you cannot ship another update — every installed
  copy will reject anything signed with a new key, and the only way forward is
  asking people to reinstall by hand.
- **Public key**: goes in the repository. It is not a secret.

---

## Wiring it up

**1. Put the public key and the endpoint in the config.**
`apps/desktop/src-tauri/tauri.conf.json`:

```json
"plugins": {
  "updater": {
    "pubkey": "<the public key the generator printed>",
    "endpoints": [
      "https://github.com/FawadHomaan2/AI-Assistant/releases/latest/download/latest.json"
    ],
    "requireSignedVersion": true,
    "allowDowngrades": false
  }
}
```

Both halves are required. The app treats a key with no endpoint, or an endpoint
with no key, as not configured — an endpoint alone would mean downloading
something it cannot verify.

**2. Add two repository secrets** under Settings → Secrets and variables →
Actions:

| Secret | Value |
|---|---|
| `TAURI_SIGNING_PRIVATE_KEY` | the full contents of `jarvis.key` |
| `TAURI_SIGNING_PRIVATE_KEY_PASSWORD` | the passphrase you chose |

**3. Release.** The version in `tauri.conf.json` and the tag have to agree; the
workflow refuses otherwise, because a mismatch either offers an update that
installs the same version or hides one that exists.

```powershell
# bump "version" in apps/desktop/src-tauri/tauri.conf.json first
git commit -am "Jarvis 0.2.0"
git tag v0.2.0
git push origin main --tags
```

`.github/workflows/release.yml` then builds the core, builds a signed
installer, writes `latest.json`, and publishes a GitHub Release with all three.
Installed copies see it on the next check.

---

## What the two security flags do

**`requireSignedVersion: true`** closes a downgrade attack. The manifest is
fetched over TLS but is not itself signed, and the signature only covers the
installer. Without this flag, anyone able to serve a crafted response can pair
an inflated `version` with the URL and signature of an *older* release — and
that older artifact has a genuinely valid signature, so it installs. The result
is a forced downgrade to a real but outdated build.

It is on from the start deliberately. Turning it on later rejects every release
signed before the Tauri CLI began recording the version in the signature, so
there is no cheap moment to add it after the fact.

**`allowDowngrades: false`** keeps the version check at "must be newer" rather
than "must be different".

---

## Signing the update is not code signing

Two different signatures, often confused:

| | What it proves | Who checks it |
|---|---|---|
| **Update signature** (this document) | the update came from whoever holds the key | Jarvis, before installing |
| **Authenticode** (a bought certificate) | the publisher's identity | Windows, SmartScreen, antivirus |

Setting up updates does nothing for SmartScreen. Downloads will still warn
about an unknown publisher until there is a real code-signing certificate — see
`docs/PACKAGING.md`. Conversely, a code-signing certificate does nothing for
the updater.

---

## What a reinstall keeps, either way

Updating in place and reinstalling by hand both leave your data alone. Only the
program is replaced.

Kept in `%LOCALAPPDATA%\jarvis`: conversation history, learned memory, the audit
log, granted permissions, installed plugins, and every downloaded model — the
wake word (~4 MB), the Piper voice (~62 MB) and Whisper (~74 MB). You do not
re-download those.

The uninstaller is the only thing that offers to delete them, and it asks once
and defaults to keeping.

---

## If the first release does not work

In rough order of likelihood:

- **The app says "not set up" after you filled in the config.** Both `pubkey`
  and a non-empty `endpoints` are required. Check you rebuilt — the key is
  compiled in, so editing the config without rebuilding changes nothing.
- **"Signature verification failed".** The published artifact was signed with a
  different key than the one in `pubkey`. Most often the secret was pasted with
  a missing line or trailing whitespace.
- **The update is rejected and the log mentions a signed version.** The Tauri
  CLI that built it did not record the version in the signature, which
  `requireSignedVersion` demands. Update the CLI (`npm i -D @tauri-apps/cli@2`)
  and re-release.
- **The check 404s.** `latest.json` is not on the release, or the release is a
  draft — a draft's assets are not public. The workflow publishes
  non-draft for this reason.
- **Nothing is offered although a release exists.** The installed build's
  version is not lower than the release's. The workflow's tag check prevents
  the usual cause.
