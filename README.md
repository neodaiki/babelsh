# babelsh — the Babel fish for your shell

[![CI](https://github.com/neodaiki/babelsh/actions/workflows/ci.yml/badge.svg)](https://github.com/neodaiki/babelsh/actions/workflows/ci.yml)

**Type the commands you already know. Run them on whatever OS you're on.**

*The project is **babelsh**; the command you type is **`ush`** (Universal SHell).*

You learned `apt` on Ubuntu, `brew` on a Mac, `dnf` at work, and `sed -i ''` the hard way.
Then you SSH into the other kind of machine and your fingers betray you. `ush` sits in your
shell, notices when a command doesn't belong on this host, shows you the native equivalent,
and — only after you say yes — runs it.

```console
# on Ubuntu, with Fedora and macOS muscle memory
$ dnf install -y htop
ush: dnf install -y htop  ->  sudo apt install -y htop   [exact, modify]
Run? [y/N] y

$ brew install htop
ush: brew install htop  ->  sudo apt install htop   [exact, modify]
Run? [y/N] n
ush: cancelled

$ ush explain sed -i '' 's/foo/bar/' app.conf      # BSD sed syntax on a GNU box
status:   translated
command:  sed -i s/foo/bar/ app.conf
fidelity: exact
```

No cloud call. No model in the loop. The same input always gives the same answer, and when `ush`
doesn't know something it says so instead of guessing.

> **Status: alpha (v0.0.1).** Tested on Ubuntu 22.04 with bash and zsh. On macOS, CI on a GitHub
> runner (macOS 26, arm64, zsh 5.9) confirms host detection, translation planning and the zsh hook
> — but nothing has been *executed* on a Mac yet, and nobody has used it on a laptop. See
> [Status](#status--what-is-and-isnt-verified). Rule coverage is deliberately tiny so far.

## Why this exists

| You type | Where it breaks | What ush does |
|---|---|---|
| `sudo apt install nginx` | macOS has no `apt` | `brew install nginx` — and drops the `sudo`, because Homebrew refuses to run as root |
| `apt install libssl-dev build-essential` | one command, two very different things on a Mac | `brew install openssl@3 && xcode-select --install`, with a note about `LDFLAGS` |
| `systemctl start nginx` | no systemd on macOS | `brew services start nginx`, labelled *approximate*: it's a per-user launchd agent, not a system unit |
| `sed -i 's/a/b/' f` | BSD sed wants `-i ''` | `sed -i '' 's/a/b/' f` (and the reverse) |
| `systemctl start ssh` | nothing to translate to | says *unsupported* and tells you where the setting lives; runs nothing |

*(The macOS rows are what the rules specify, checked in simulation — see Status.)*

## What makes it different

- **Rules, not a language model.** Translations come from small, readable YAML rules compiled
  to JSON. Deterministic, offline, auditable. A translation takes tens of milliseconds, and the
  hook only calls `ush` for commands that might need it — ordinary commands (`ls`, `apt` on
  Debian, `sed` without `-i`) never start Python.
- **It tells you how faithful the translation is.** Every result is labelled `exact`,
  `approximate`, `lossy` or `unsupported`, plus risk (`read` / `modify` / `destructive`). A
  destructive command makes you type `yes`.
- **It refuses instead of guessing.** Unknown package, unmapped flag, `$(computed)` argument,
  ambiguous dialect: nothing runs, and you're told why.
- **It never gets in the way of native commands.** If the host already has the command
  (`apt` on Debian), `ush` stays out of it. The only thing it ever fixes on a native command is
  `sudo brew`.
- **Rules are signed data.** `ush update` downloads a new rule release only if it's signed by a
  key the client has pinned, is newer than anything installed, and matches the signed hashes.
  A replayed old release can't roll you back.
- **Built so AI can help write rules without being in your shell.** (Roadmap — see below.)
  Rules are plain data, so contributions, human or machine-assisted, arrive as reviewable diffs.

## Try it

Needs Python 3.8+ (standard library only). There is no PyPI package yet.

```sh
git clone <this repo> && cd babelsh
bin/ush hostinfo                          # what ush detected about this machine
bin/ush explain dnf install -y htop       # show the translation; run nothing
```

Hook it into your shell (bash 4+ or zsh; both pass the same hook tests on Linux):

```sh
mkdir -p ~/.config/ush
bin/ush init bash > ~/.config/ush/hook.bash && echo 'source ~/.config/ush/hook.bash' >> ~/.bashrc
# or, for zsh:
bin/ush init zsh  > ~/.config/ush/hook.zsh  && echo 'source ~/.config/ush/hook.zsh'  >> ~/.zshrc
```

Start with `export USH_DRY_RUN=1` if you'd like to see what it would do before it does anything.

## Status — what is and isn't verified

Honest list, because a tool that touches your package manager should be.

**Verified (automated, 121 tests + 30 golden translation cases, run on Ubuntu 22.04):**
tokenizer and shell-syntax handling, rule validation, translation logic, host probing on
Linux, the bash 5.1 and zsh 5.8 hooks (command-not-found handler, `sudo` and `sed` wrappers,
chaining an existing handler), confirmation prompts, signed-update verification (tamper, wrong key,
rollback, oversize, path traversal, crash safety), Ed25519 against RFC 8032 and an independent
library.

**Verified on a real macOS CI runner** (GitHub `macos-latest`: macOS 26, arm64, Python 3.12,
zsh 5.9, preinstalled Homebrew): all 121 tests pass (14 skipped, see below), including the 14
zsh-hook scenarios; real host detection reports `family: macos`, `brew_prefix: /opt/homebrew`,
BSD `sed`; and `ush explain` produces the expected plans there, e.g. `apt install -y nginx` →
`brew install nginx`, `ss -tulnp` → `lsof -i -P -n`, `sed -i` → `sed -i ''`. Latest results:
the CI badge above. Caveat: that is a CI virtual machine, and everything is dry-run
planning — no package was ever installed through `ush` on a Mac.

**Not verified yet:**
- **Executing translations on macOS** and what they do for a person: `sudo brew services start`
  semantics, the real output of `lsof -i -P -n` versus `ss -tulnp`, the interactive prompts in a
  real terminal, a user's actual dotfiles and PATH.
- **macOS system bash 3.2** has no `command_not_found_handle`, so the bash hook can't work
  there (CI skips those 14 tests on purpose). Use zsh, or Homebrew's bash 5.
- **Real `minisign`** — signature checking is implemented from the format spec and tested
  against our own signer; it has not been run against the official binary.

**Coverage today (this is v0.0.1, and small on purpose):** 6 package concepts, 2 services,
6 operations — `install`, `remove`, `update` of the package index, `service start`,
`sed -i`, and "what's listening" (`ss` ↔ `lsof`). Missing: `upgrade`, `search`, casks,
`service stop/enable/status`, `journalctl`, `ip`, `free`, flags beyond the basics. If a flag
isn't in a rule, `ush` refuses rather than dropping it silently.

**Known limits:** `sudo` with options (`-u`, `-E`) isn't translated; redirects are carried over
only for single-step translations; backup-suffix variants of `sed -i` aren't translated;
Debian/Fedora/macOS only; no Arch, SUSE, Alpine or BSD yet.

### Help wanted

1. **A real Mac, used by a person.** CI proves the plans; it can't tell us whether they feel
   right. Clone, hook it into zsh, use it for a day with `USH_DRY_RUN=1`, then without, and open
   an issue with anything surprising. This is the single most valuable contribution right now.
2. **Rules.** Adding a package is one line — see below.
3. **Fedora** users: the Fedora side has the least real-world mileage.

## Add a package mapping in one line

`spec/examples/packages.yaml`:

```yaml
packages:
  openssl_dev: { debian: libssl-dev, fedora: openssl-devel, macos: "openssl@3" }
  gnu_sed:     { debian: ~, fedora: ~, macos: gnu-sed }          # ~  = nothing to install there
  openssh_server: { debian: openssh-server, fedora: openssh-server, macos: "!" }   # "!" = no equivalent
```

Then `python3 compiler/compile_rules.py spec/examples -o babelsh/data/rules.json` and
`python3 -m unittest discover -s tests`. The compiler validates names, forbids shell
metacharacters, and checks that every flag of every operation is handled or explicitly
declared (so a flag can't be dropped by omission).

## Roadmap

- Validate on real macOS; run CI on Linux **and** macOS runners
- zsh hook verified on macOS; a documented answer for macOS bash 3.2 (use zsh, or Homebrew bash 5)
- Much wider rule coverage (the 20 commands people actually type)
- Per-entry rule validation instead of all-or-nothing
- CI that checks a proposed package really exists on each OS (`apt-cache`, `dnf info`, `brew info`)
- Two-signature releases (CI key + offline maintainer key) and key rotation
- Tooling that lets AI *propose* rules offline, gated by that CI — never at runtime

## Reference

<details><summary>How it works</summary>

Every command maps through a canonical hub — a *concept* for names (`openssl_dev`), an *intent*
for operations (`pkg.install`) — so adding a new OS family means adding a form, not N² pairs.
Input is parsed against the forms of the family it *looks* like; output is rendered with the
form of the host. Ambiguous parses (`sed -i ''` fits both dialects) are resolved by explicit
evidence, then by "assume the host's own dialect", and refused otherwise. Output is an argv
list executed without a shell, so nothing in a rule or a package name is ever interpreted by one.
Details: [`spec/SPEC.md`](spec/SPEC.md).
</details>

<details><summary>Shell hook details</summary>

| Piece | Why |
|---|---|
| `command_not_found_handle` | `apt` on macOS etc.; the shell has already split and expanded the line, so `&&`, pipes and redirects keep working |
| `sudo` wrapper | `sudo apt …` never reaches the handler; also fixes `sudo brew` |
| `sed` wrapper | calls ush only when a dialect flag (`-i`) is present; otherwise straight to `command sed` |

Interactive shells only. An existing `command_not_found_handle` (e.g. Ubuntu's) is chained.
Environment: `USH_DISABLE=1` bypass · `USH_DRY_RUN=1` print instead of run ·
`USH_ASSUME_YES=1` skip prompts for non-destructive commands without a terminal ·
`USH_HOST='{json}'` pin the host profile · `USH_RULES=path` · `USH_LANG=ko|en`.
Prompts read `/dev/tty`; with no terminal nothing runs (exit 125). Exit 127 = not translated,
1 = no equivalent here.
</details>

<details><summary>Signed rule updates</summary>

```sh
ush update --dry-run URL     # verify and show what would change (URL = release directory)
ush update URL               # install atomically; USH_RULES_URL can supply the URL
ush rules status | rollback
```

A release is `manifest.json` + `manifest.json.release.minisig` + `rules.json`, signed with
[minisign](https://jedisct1.github.io/minisign/). The client pins the public key in
`babelsh/data/trusted_keys.json` (**ships empty** until a publisher key is pinned; or
`USH_TRUSTED_KEYS=path`). Publisher side:

```sh
python3 compiler/release.py build spec/examples OUT_DIR --minisign-key ~/.minisign/ush.key
python3 compiler/release.py keygen --out-dir keys/            # DEV key only
python3 compiler/release.py build spec/examples OUT_DIR --dev-key keys/release.key
```

The built-in signer is pure Python and not constant-time: tests and local trials only.
</details>

<details><summary>Tests</summary>

```sh
python3 -m unittest discover -s tests   # tokenizer, validator, host probe, plan execution, real bash hook, updates
python3 tests/run_golden.py             # spec/tests/golden.yaml
```
Client code is stdlib-only (Python 3.8+ intended; only 3.10 has been run). `compiler/` needs PyYAML.
</details>

## License

[MIT](LICENSE). `babelsh/ed25519.py` is adapted from the RFC 8032 reference code (IETF Trust
Revised BSD terms; see the file header).
