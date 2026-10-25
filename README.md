# ush — Universal Shell Hub (work in progress)

Type the commands you know (`apt`, `dnf`, `systemctl`, `sed -i`, `ss`); ush runs the native
equivalent on the host (Debian family, Fedora family, macOS). Rules are data
(`spec/examples/*.yaml` → `rules.json`); there is no LLM in the client. Design: `spec/SPEC.md`.

## Try it (no install)

```sh
bin/ush hostinfo                       # detected host profile
bin/ush explain dnf install -y htop    # what would run here; runs nothing
python3 compiler/compile_rules.py spec/examples -o ush/data/rules.json   # rebuild bundled rules
```

## Shell hook (bash; zsh is generated but untested)

```sh
bin/ush init bash > ~/.config/ush/hook.bash
echo 'source ~/.config/ush/hook.bash' >> ~/.bashrc
```

Regenerate the hook whenever `rules.json` gains new commands. What it installs:

| Piece | Why |
|---|---|
| `command_not_found_handle` | `apt` on macOS etc.; the shell has already split/expanded the line, so `&&`, pipes and redirects keep working |
| `sudo` wrapper | `sudo apt ...` never reaches the handler; also fixes `sudo brew` |
| `sed` wrapper | only calls ush when a dialect flag (`-i`) is present; otherwise `command sed` directly |

Interactive shells only. Existing `command_not_found_handle` (e.g. Ubuntu's suggestions) is chained.

Environment: `USH_DISABLE=1` bypass · `USH_DRY_RUN=1` print instead of run ·
`USH_ASSUME_YES=1` skip prompts for non-destructive commands when there is no terminal ·
`USH_HOST='{json}'` pin the host profile · `USH_RULES=path` · `USH_LANG=ko|en`.

Confirmation: read-only → Enter runs, `n` cancels · modifying → `y` required ·
destructive (`remove`, ...) → type `yes`. Prompts read `/dev/tty`; with no terminal nothing runs
(exit 125). Exit 127 = not translated, 1 = no equivalent on this host.

## Signed rule updates

```sh
ush update --dry-run URL     # verify and show what would change (URL = release directory)
ush update URL               # install atomically; USH_RULES_URL can supply the URL
ush rules status | rollback
```

A release is `manifest.json` + `manifest.json.release.minisig` + `rules.json`, signed with
[minisign](https://jedisct1.github.io/minisign/). The client pins the public key in
`ush/data/trusted_keys.json` (ships **empty**; or `USH_TRUSTED_KEYS=path`) and refuses
anything not signed by it, not newer than what it has installed, or not matching the signed
sizes/hashes. Publisher side:

```sh
python3 compiler/release.py build spec/examples OUT_DIR --minisign-key ~/.minisign/ush.key   # production
python3 compiler/release.py keygen --out-dir keys/                                           # DEV key only
python3 compiler/release.py build spec/examples OUT_DIR --dev-key keys/release.key
```

The built-in signer is pure Python and not constant-time: use it for tests and local trials,
never for the production key. Compatibility with the real `minisign` binary is implemented
from the format description and has not been run against it (the binary is not installed here).

## Tests

```sh
python3 -m unittest discover -s tests   # tokenizer, validator, host probe, plan execution, real bash hook
python3 tests/run_golden.py             # spec/tests/golden.yaml
```

Client code is stdlib-only and meant for Python 3.8+ (only 3.10 has been run so far).
`compiler/` needs PyYAML.
