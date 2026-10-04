#!/usr/bin/env python3
"""Turn on in-app updates: generate a signing key, wire it in, set the secrets.

Updates are switched off in the repository because it ships no signing key, and
`docs/UPDATES.md` explains why a key must be generated on the machine that will
keep it. This script does that one job end to end so it is one command rather
than a checklist:

    python scripts/setup_updates.py

It generates the keypair (the Tauri CLI prompts for the passphrase — this
script never sees it), writes the **public** half into `tauri.conf.json`, and
offers to set the two repository secrets with `gh` if that is available.

The private key is never printed, never copied, and never written anywhere
inside the repository. Whoever holds it can install software on every machine
running Jarvis, so it stays in one place that you chose.

Useful on its own:

    python scripts/setup_updates.py --check                  # report, change nothing
    python scripts/setup_updates.py --public-key <key|path>  # only write the key
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DESKTOP = ROOT / "apps" / "desktop"
CONF = DESKTOP / "src-tauri" / "tauri.conf.json"

#: Where the private key goes unless `--key-path` says otherwise. Outside the
#: repository on purpose: a key inside a working tree is a key one `git add -A`
#: away from being published.
DEFAULT_KEY_PATH = Path.home() / ".tauri" / "jarvis.key"

#: What a decoded minisign public key file contains. Checked so a truncated
#: paste, a private key, or a file of something else entirely is refused here
#: rather than surfacing later as "signature verification failed".
MINISIGN_PUBLIC_MARKER = "minisign public key"
MINISIGN_PRIVATE_MARKER = "minisign encrypted secret key"

SECRET_KEY = "TAURI_SIGNING_PRIVATE_KEY"
SECRET_PASSWORD = "TAURI_SIGNING_PRIVATE_KEY_PASSWORD"


class SetupError(Exception):
    """Something the person running this needs to fix. Printed without a trace."""


def step(message: str) -> None:
    print(f"\n=== {message} ===", flush=True)


# ── the public key ───────────────────────────────────────────────────────────


def normalise_public_key(raw: str) -> str:
    """Return the base64 form `tauri.conf.json` wants, or raise.

    The Tauri generator writes `<key>.pub` as base64 of the minisign public key
    file, and that base64 blob is what the config takes. People also paste the
    decoded file, or the private key by mistake, so both are recognised: the
    first is accepted and encoded, the second is refused outright.
    """
    text = "".join(raw.split())
    if not text:
        raise SetupError("The public key is empty.")

    # A pasted minisign file rather than the base64 blob: encode it.
    if raw.lstrip().startswith("untrusted comment:"):
        if MINISIGN_PRIVATE_MARKER in raw:
            raise SetupError(
                "That is the PRIVATE key. It never goes in tauri.conf.json, which "
                "is committed. Pass the public half — the `.pub` file beside it."
            )
        if MINISIGN_PUBLIC_MARKER not in raw:
            raise SetupError("That does not look like a minisign public key file.")
        return base64.b64encode(raw.encode("utf-8")).decode("ascii")

    try:
        decoded = base64.b64decode(text, validate=True).decode("utf-8", "replace")
    except (binascii.Error, ValueError) as exc:
        raise SetupError(
            f"That is not a base64 public key ({exc}). Pass the contents of the "
            "`.pub` file the generator wrote, or the path to it."
        ) from exc

    if MINISIGN_PRIVATE_MARKER in decoded:
        raise SetupError(
            "That is the PRIVATE key. It never goes in tauri.conf.json, which is "
            "committed. Pass the public half — the `.pub` file beside it."
        )
    if MINISIGN_PUBLIC_MARKER not in decoded:
        raise SetupError(
            "That decodes to something that is not a minisign public key. A "
            "truncated paste is the usual cause."
        )
    return text


def read_public_key(source: str) -> str:
    """Accept either the key itself or a path to the file holding it."""
    candidate = Path(source).expanduser()
    try:
        if candidate.is_file():
            return normalise_public_key(candidate.read_text("utf-8"))
    except OSError as exc:
        raise SetupError(f"Could not read {candidate}: {exc}") from exc

    # Said plainly rather than left to the base64 check, which would report a
    # mistyped path as a malformed key and send the reader after the wrong
    # problem entirely.
    looks_like_a_path = source.endswith((".pub", ".key")) or any(
        sep in source for sep in ("/", "\\")
    )
    if looks_like_a_path:
        raise SetupError(f"There is no file at {candidate}.")
    return normalise_public_key(source)


# ── the config file ──────────────────────────────────────────────────────────

#: Targets the one `"pubkey"` entry without reformatting the file. A JSON
#: round-trip would rewrite every line and bury the real change in the diff.
PUBKEY_RE = re.compile(r'("pubkey"\s*:\s*)"((?:[^"\\]|\\.)*)"')


def current_state() -> tuple[str, list[str]]:
    """The configured key and endpoints, as the file has them now."""
    try:
        conf = json.loads(CONF.read_text("utf-8"))
    except OSError as exc:
        raise SetupError(f"Could not read {CONF}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise SetupError(f"{CONF} is not valid JSON: {exc}") from exc
    updater = (conf.get("plugins") or {}).get("updater") or {}
    key = updater.get("pubkey") or ""
    endpoints = updater.get("endpoints") or []
    return (key if isinstance(key, str) else ""), list(endpoints)


def write_public_key(key: str) -> bool:
    """Put `key` in `plugins.updater.pubkey`. Returns False if already there.

    The edit is verified by re-parsing the result, because a regex that matched
    the wrong thing would otherwise leave a file that looks edited and is not.
    """
    existing, endpoints = current_state()
    # Checked before the "already in place" case below: a key with no endpoint
    # leaves updates switched off, so reporting that one is set would be a
    # success message for a broken configuration.
    if not endpoints:
        raise SetupError(
            f"{CONF} has no update endpoints. A key with nowhere to check is not a "
            "working updater; the repository ships the endpoint filled in, so this "
            "means it was removed."
        )
    if existing == key:
        return False

    text = CONF.read_text("utf-8")
    matches = PUBKEY_RE.findall(text)
    if len(matches) != 1:
        raise SetupError(
            f'Expected exactly one "pubkey" entry in {CONF}, found {len(matches)}. '
            "Edit it by hand rather than letting this guess."
        )

    patched = PUBKEY_RE.sub(lambda m: f'{m.group(1)}"{key}"', text, count=1)
    CONF.write_text(patched, encoding="utf-8")

    # Round-trip, or revert. An unparseable config stops the app from starting.
    try:
        written, _ = current_state()
    except SetupError:
        CONF.write_text(text, encoding="utf-8")
        raise
    if written != key:
        CONF.write_text(text, encoding="utf-8")
        raise SetupError(
            "The edit did not take effect and has been reverted. Set "
            '"pubkey" in tauri.conf.json by hand.'
        )
    return True


# ── generating the keypair ───────────────────────────────────────────────────


def generate_keypair(key_path: Path) -> Path:
    """Run the Tauri signer. Returns the path to the public half.

    The passphrase is prompted for by the Tauri CLI, not by this script: a
    passphrase that passes through another process is one more place it can be
    logged, and there is no reason for this one to know it.
    """
    pub_path = key_path.with_suffix(key_path.suffix + ".pub")
    if key_path.exists():
        if not pub_path.exists():
            raise SetupError(
                f"{key_path} exists but {pub_path} does not, so the public half "
                "cannot be read. Pass --public-key with it, or --key-path to "
                "generate a new pair somewhere else."
            )
        print(f"Using the existing key at {key_path}.")
        print("Delete it first if you meant to generate a new one — but a new key")
        print("invalidates every release signed with the old one.")
        return pub_path

    if shutil.which("npm") is None:
        raise SetupError("npm is not on PATH, and the Tauri CLI runs through it.")

    key_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"Generating a keypair at {key_path}.")
    print("The Tauri CLI will ask for a passphrase. Choose one and keep it:")
    print(f"it becomes the {SECRET_PASSWORD} secret, and without it you cannot")
    print("sign another update.\n")

    # Inherits stdio so the passphrase prompt works. `shell=True` is not used:
    # the path below is interpolated into no shell.
    result = subprocess.run(  # noqa: S603
        ["npm", "run", "tauri", "--", "signer", "generate", "-w", str(key_path)],
        cwd=DESKTOP,
        check=False,
        shell=sys.platform == "win32",  # npm is npm.cmd on Windows
    )
    if result.returncode != 0:
        raise SetupError(
            f"`tauri signer generate` failed with exit code {result.returncode}."
        )
    if not pub_path.is_file():
        raise SetupError(
            f"The generator reported success but wrote no {pub_path}. "
            "Check the output above."
        )
    return pub_path


# ── the repository secrets ───────────────────────────────────────────────────


def set_secrets_with_gh(key_path: Path) -> bool:
    """Offer to set both secrets through `gh`. Returns True if it did.

    The private key is piped to `gh` from the file, never printed and never put
    on a command line where it would reach the process table.
    """
    if shutil.which("gh") is None:
        return False
    if subprocess.run(["gh", "auth", "status"], capture_output=True).returncode != 0:  # noqa: S603, S607
        print("`gh` is installed but not signed in, so the secrets are yours to add.")
        return False

    answer = (
        input(
            f"\nSet {SECRET_KEY} and {SECRET_PASSWORD} as repository secrets "
            "with `gh` now? [y/N] "
        )
        .strip()
        .lower()
    )
    if answer not in {"y", "yes"}:
        return False

    try:
        private = key_path.read_bytes()
    except OSError as exc:
        raise SetupError(f"Could not read {key_path}: {exc}") from exc

    pushed = subprocess.run(  # noqa: S603, S607
        ["gh", "secret", "set", SECRET_KEY],
        input=private,
        cwd=ROOT,
        capture_output=True,
    )
    if pushed.returncode != 0:
        raise SetupError(
            f"Setting {SECRET_KEY} failed: "
            f"{pushed.stderr.decode('utf-8', 'replace').strip()}"
        )
    print(f"{SECRET_KEY} set.")

    print(
        f"\nNow the passphrase, for {SECRET_PASSWORD}. `gh` will read it without "
        "echoing it."
    )
    # `gh secret set` reads from the terminal when stdin is not redirected, so
    # the passphrase never passes through this process.
    if (
        subprocess.run(["gh", "secret", "set", SECRET_PASSWORD], cwd=ROOT).returncode
        != 0
    ):  # noqa: S603, S607
        raise SetupError(
            f"Setting {SECRET_PASSWORD} failed. Add it by hand under "
            "Settings -> Secrets and variables -> Actions."
        )
    print(f"{SECRET_PASSWORD} set.")
    return True


# ── reporting ────────────────────────────────────────────────────────────────


def report() -> int:
    key, endpoints = current_state()
    print(f"Config:    {CONF}")
    print(f"Public key: {'set (' + key[:16] + '...)' if key else 'NOT set'}")
    print(f"Endpoints:  {len(endpoints)}")
    for endpoint in endpoints:
        print(f"  - {endpoint}")
    configured = bool(key) and bool(endpoints)
    print(
        "\nIn-app updates are "
        + ("ON for builds made from this config." if configured else "OFF.")
    )
    if not configured:
        print("Run this script with no arguments to turn them on.")
    return 0


def next_steps(secrets_done: bool) -> None:
    step("What is left")
    if not secrets_done:
        print("1. Add two repository secrets (Settings -> Secrets and variables ->")
        print("   Actions):")
        print(f"     {SECRET_KEY}       the full contents of your private key file")
        print(f"     {SECRET_PASSWORD}  the passphrase you just chose")
        print()
    print("2. Commit the config change and release:")
    print("     git commit -am 'Turn on in-app updates'")
    print("     git push")
    print("   then bump the version and push a tag (v0.2.1, v0.3.0, ...).")
    print()
    print("A key added now does not retro-sign anything. Releases published")
    print("before it stay plain downloads, and copies already installed have no")
    print("public key compiled in, so they will never offer an update. The first")
    print("self-updating build is the first one built after this change.")
    print("\nSee docs/UPDATES.md.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Turn on in-app updates for Jarvis.",
        epilog="With no arguments, runs the whole setup.",
    )
    parser.add_argument(
        "--check", action="store_true", help="report the current state, change nothing"
    )
    parser.add_argument(
        "--public-key",
        metavar="KEY|PATH",
        help="write this public key and stop; generates nothing",
    )
    parser.add_argument(
        "--key-path",
        type=Path,
        default=DEFAULT_KEY_PATH,
        help=f"where the private key lives (default: {DEFAULT_KEY_PATH})",
    )
    parser.add_argument(
        "--no-secrets", action="store_true", help="do not offer to run `gh secret set`"
    )
    args = parser.parse_args(argv)

    if args.check:
        return report()

    if args.public_key is not None:
        key = read_public_key(args.public_key)
        changed = write_public_key(key)
        print("Public key written." if changed else "That key was already in place.")
        next_steps(secrets_done=False)
        return 0

    step("Where things stand")
    report()

    step("The keypair")
    pub_path = generate_keypair(args.key_path.expanduser())
    key = read_public_key(str(pub_path))

    step("Wiring the public key in")
    changed = write_public_key(key)
    print(f"{CONF}: {'updated' if changed else 'already had this key'}.")

    secrets_done = False
    if not args.no_secrets:
        step("Repository secrets")
        secrets_done = set_secrets_with_gh(args.key_path.expanduser())

    next_steps(secrets_done)
    print(f"\nYour private key stays at {args.key_path}. Back it up somewhere only")
    print("you can reach: lose it and you cannot ship another update.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SetupError as error:
        print(f"\nerror: {error}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nStopped. Nothing was changed.", file=sys.stderr)
        sys.exit(130)
