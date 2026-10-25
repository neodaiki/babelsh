# ush rules — schema & release manifest (draft 0)

Status: draft 1, 2026-10-26. Nothing here is implemented yet.

Scope tags: **[v1]** = build first. **[later]** = designed, but deliberately not built until
there is a real need (external users, many rules, name drift). Untagged text applies to both.
Rule of thumb: security boundaries are [v1]; convenience and ceremony are [later].

## 0. Principles

1. **Rules are data, never code.** No shell snippets, no eval, no interpolation language.
   Every executable thing is an `argv` template built from a closed vocabulary.
2. **Hub model.** Everything maps through a canonical id (`concept` for names, `intent` for
   command semantics). Adding a new OS family = adding a column of forms, not N² pairs.
3. **Every rule says how faithful it is** (`fidelity`) and how dangerous it is (`risk`).
4. **Provenance is part of the rule** (`source`, `verified`). CI, not trust, promotes rules.
5. **The client only needs the stdlib.** Release payload is canonical JSON; YAML exists only in
   the rules repo.

## 1. Vocabulary

| Term | Values |
|---|---|
| `family` | `debian`, `fedora`, `macos` (extensible: `arch`, `suse`, `alpine` ...) |
| `os_id` (for verification) | `debian-12`, `ubuntu-24.04`, `fedora-40`, `rhel-9`, `macos-14` |
| `fidelity` | `exact` / `approximate` / `lossy` / `unsupported` |
| `risk` | `read` / `modify` / `destructive` |
| `source` | `seed` / `human` / `ai` / `repology` / `user` |

`fidelity` semantics:
- `exact`: same effect and same output shape.
- `approximate`: same effect, different details (flags dropped, output format differs).
- `lossy`: only part of the effect can be reproduced; user must read the note.
- `unsupported`: no equivalent; client prints the note and does NOT run anything.

## 2. Host profile (what `when:` selectors match against)

Computed once per shell session, cached in `USH_HOST` (never recomputed per command).

```json
{
  "family": "macos",
  "os_id": "macos-14",
  "arch": "arm64",
  "brew_prefix": "/opt/homebrew",
  "dialect": { "sed": "bsd", "grep": "bsd", "date": "bsd", "xargs": "bsd" },
  "has": { "brew": true, "apt": false, "systemctl": false, "sudo": true },
  "is_root": false
}
```

`dialect.*` is **probed from the real binary** (e.g. `sed --version` succeeds => `gnu`),
not inferred from the OS. `has.*` drives the pass-through rule: if the typed binary exists
on the host and the intent is not dialect-sensitive, ush does not intercept.

## 3. File: `packages.yaml` (names → concepts)

```yaml
schema: 1
concepts:
  - id: openssl_dev              # [a-z0-9_]+, unique, immutable once released
    kind: package                # package | cask   (services live in services.yaml)
    source: seed
    verified: 2026-10-26         # [v1] date CI confirmed all listed targets exist; absent = unverified
                                 # [later] per-os_id: { debian: [debian-12], ... }
    targets:                     # one list per family; entries tried in order
      debian: [ { pkg: libssl-dev } ]
      fedora: [ { pkg: openssl-devel } ]
      macos:  [ { pkg: "openssl@3" } ]
    fidelity: { macos: approximate }      # per target family; missing family = exact
    note:                                 # per target family; shown only when that family is the host
      macos:
        en: "set LDFLAGS/CPPFLAGS to $(brew --prefix openssl@3)."
        ko: "LDFLAGS/CPPFLAGS를 $(brew --prefix openssl@3)로 지정해야 합니다."
```

**Scoping rule [v1].** `fidelity` and `note` on a concept are maps keyed by the *target*
family (the host the command will run on). A translation to Fedora never shows a macOS note.
A plan's fidelity is the worst fidelity among its steps, looked up for the host family.
`unsupported` is implied by a `"!"`/`{unsupported: true}` target and needs no explicit fidelity.

### 3.0 Shorthand [v1] (what contributors actually write)

The common contribution is "add one package". It must be one line:

```yaml
schema: 1
defaults: { source: human }
packages:
  openssl_dev: { debian: libssl-dev, fedora: openssl-devel, macos: "openssl@3" }
  build_toolchain:
    debian: build-essential
    fedora: [gcc, gcc-c++, make]          # list = several packages
    macos: { action: xcode_clt }          # object = full target entry (see below)
  gnu_sed: { debian: ~, fedora: ~, macos: gnu-sed }   # ~ = drop
  openssh_server: { debian: openssh-server, fedora: openssh-server, macos: "!" }  # "!" = unsupported
```

The compiler expands this into the canonical `concepts:` form below; `note`, `fidelity`
and `aliases` can still be added per concept by using the long form. The canonical form is
what is released and what the client reads.

### Target entry forms (exactly one of)

| Form | Meaning |
|---|---|
| `{ pkg: NAME, when?: SEL }` | install this package with the family's package manager |
| `{ cask: NAME, when?: SEL }` | macOS only; Homebrew cask |
| `{ action: ENUM, when?: SEL }` | built-in action from the closed list below |
| `{ drop: true }` | concept is implicit/irrelevant on this family; omit it, show note |
| `{ unsupported: true }` | no equivalent; whole plan becomes `unsupported` |

`ENUM` (closed, implemented in the client, not in the data):
`xcode_clt` (`xcode-select --install`), `brew_tap` (requires `arg`), `noop`.
New actions require a client release; rules cannot invent them.

[later] `when` selector (v1 rules match on `family` only): `{ os_id: "debian-12" }`, `{ version: ">=12" }`, `{ arch: arm64 }`.
Comparison operators: `>= <= > < ==`; versions compared numerically per dotted component.

### Reverse index

The compiler builds `(family, name) → concept_id` from `targets`. A single `(family, name)`
mapping to two concepts is a **build error**. Extra lookup-only names go in:

```yaml
    aliases: { debian: [libssl1.1-dev], fedora: [openssl-devel.x86_64] }
```

### Validation (client enforces at load; invalid rules are dropped individually)

- `pkg`/`cask` match `^[A-Za-z0-9][A-Za-z0-9@+._:-]{0,127}$` (no `/`, spaces, `$`, `;`, quotes).
- `id` matches `^[a-z0-9_]{1,64}$`.
- Strings in `note` are display-only, ≤ 240 chars, control chars stripped.
- Unknown keys are rejected (no silent extension).

### 3.1 File: `services.yaml` [v1]

Service (unit/agent) names are a different namespace from package names
(`ssh` vs `sshd` vs `openssh-server`), so they get their own file:

```yaml
schema: 1
defaults: { source: human }
services:
  nginx:                                  # service concept id
    debian: nginx
    fedora: nginx
    macos: nginx                          # brew formula used by `brew services`
  ssh_daemon:
    debian: ssh
    fedora: sshd
    macos: "!"                            # unsupported via brew; note explains System Settings
    pkg: openssh_server                   # optional link to a packages.yaml concept
    note:
      macos:
        en: "macOS ships sshd; enable via System Settings > General > Sharing > Remote Login."
```

Same name validation as packages. Reverse index is `(family, unit) → service id`.

## 4. File: `commands.yaml` (command semantics → intents)

An **intent** is a canonical operation. Each family has a *parse form* (how a user of that
family types it) and a *render form* (how to run it natively). Same data drives both
directions.

```yaml
schema: 1
intents:
  - id: pkg.install
    risk: modify
    args:
      pkgs: { type: package_list, min: 1 }    # resolved through packages.yaml
    flags:                                     # canonical flag names
      assume_yes: { type: bool }
    forms:
      - when: { family: debian }
        match: { cmd: [apt, apt-get], sub: install }
        flag_map: { "-y": assume_yes, "--yes": assume_yes }
        render: [ apt, install, { if: assume_yes, then: ["-y"] }, { splat: pkgs } ]
        root: required
      - when: { family: fedora }
        match: { cmd: [dnf, yum], sub: install }
        flag_map: { "-y": assume_yes, "--assumeyes": assume_yes }
        render: [ dnf, install, { if: assume_yes, then: ["-y"] }, { splat: pkgs } ]
        root: required
      - when: { family: macos }
        match: { cmd: [brew], sub: install }
        flag_map: {}
        render: [ brew, install, { splat: pkgs } ]
        root: forbidden            # client strips sudo; see §4.2
        implicit: [ assume_yes ]   # brew never prompts; flag intentionally not rendered
```

### 4.1 Template language (complete; nothing else is allowed)

A `render` is a list whose items are one of:
- a literal string token (matches `^[A-Za-z0-9._/@:=+-]+$`, or a bare flag like `-y`)
- `{ ref: ARG }` — one value
- `{ splat: ARG }` — a list argument expanded into separate argv items
- `{ if: FLAG, then: [tokens...] , else?: [tokens...] }`

Output is an **argv list**. The client never joins it into a string for execution
(display only, via `shlex.join`). No pipes, `&&`, `;`, redirects or subshells in rules.
Compound user input is split into segments by the client's parser *before* rule lookup.

### 4.2 Root handling

`root: required | forbidden | optional`. The client decides sudo from this plus host
(`is_root`, `has.sudo`):
- `required` + not root + has sudo → prefix `sudo`; root or no sudo → run bare, note.
- `forbidden` → remove a leading `sudo` from user input, show one-line notice.
- `optional` → keep whatever the user typed.
This replaces the blanket "strip sudo on macOS" rule: it is per-intent.

### 4.3 Unknown / unmapped flags

[v1] One global policy: **any flag not listed in a form's `flag_map` makes the translation
fail**; ush prints "no safe translation" and does not touch the command (it is then run
as typed, or not at all, depending on pass-through rules). Never pass through an unmapped
flag silently. Consequence: `apt install --no-install-recommends x` is refused until that
flag is added to the rule. That is intended; rule coverage grows by adding flags.

#### 4.3.1 Canonical flag coverage [v1, compile-time]

Each intent declares canonical `flags`. For **every form** and **every canonical flag**, the
compiler requires exactly one of:
- the flag is used in `render` (via `if: FLAG`), or
- `implicit: [FLAG]`: the form achieves the flag's semantics by default; no fidelity penalty, or
- `unsupported_flags: [FLAG]`: if the user supplies it, translation is refused (`unmapped_flag`).

Anything else is a build error, so a flag can never be dropped silently by omission.

[later] per-form `ignore_flags` / `drops` and a `warn_drop` policy (drop the flag, downgrade
fidelity to `approximate`) for flags with no equivalent but no semantic risk.

### 4.4 Dialect-sensitive intents (coreutils)

Same structure, but selectors use the probed dialect instead of the family:

```yaml
  - id: text.sed_inplace
    risk: modify
    args: { script: { type: string }, files: { type: path_list, min: 1 } }
    forms:
      - when: { dialect: { sed: gnu } }
        match: { cmd: [sed], flags_any: ["-i", "--in-place"] }
        render: [ sed, -i, { ref: script }, { splat: files } ]
      - when: { dialect: { sed: bsd } }
        match:
          cmd: [sed]
          flags_any: ["-i"]
          evidence: [ { after_flag: "-i", token: "" } ]   # `sed -i '' ...` only makes sense for BSD
        render: [ sed, -i, "", { ref: script }, { splat: files } ]
    # NOTE: "" (empty string) is the one allowed empty literal; rendered as an empty argv item.
    # `takes_value: ["-i"]` on the BSD form: the next argument is the backup suffix.
    # Attached `-i.bak` works on both GNU and BSD, so ush leaves it alone (no form matches it).
    # [later] translating a separate suffix (`-i .bak`, BSD) to GNU `-i.bak`: in v1 a non-empty
    # suffix is refused (`backup_suffix`) when it would have to be translated.
```

### 4.5 Service intents

```yaml
  - id: service.start
    risk: modify
    args: { svc: { type: service } }           # resolved via services.yaml
    forms:
      - when: { family: debian }
        match: { cmd: [systemctl], sub: start }
        render: [ systemctl, start, { ref: svc } ]
        root: required
      - when: { family: macos }
        match: { cmd: [brew], sub: services, subsub: start }
        render: [ brew, services, start, { ref: svc } ]
        root: optional
        fidelity: approximate
        note: { en: "brew services = per-user launchd agent, not a system unit." }
```

### 4.6 Required metadata on every intent/form

`risk`, and either `fidelity` (default `exact`) or a reason it is lower (`note`).
`risk: destructive` forms (`remove`, `purge`, `autoremove`, `rm -rf` class) force the
strongest confirmation in the client regardless of source.

### 4.7 Resolution: parse vs render [v1]

`when` selects the **render** form for the host. `match` selects the **parse** form for the
input. They are separate lookups:

1. **Parse.** Split input into segments (`&&`, `||`, `;`, `|`). For each segment, try the
   `match` of *every* form of every intent (indexed by `cmd` and `sub`), regardless of `when`.
   A form parses successfully only if its `flag_map` covers every flag and its `args`
   constraints (e.g. `min`) hold. Dynamic arguments (`$(...)`, backticks, unquoted `$VAR`
   in a name position) → refused `dynamic_argument`.
2. **Disambiguate** if more than one (intent, form) parses:
   a. prefer the form whose `match.evidence` is present in the input;
   b. else prefer the host's own form (the input is then native → passthrough);
   c. else refuse `ambiguous`.
3. **Pass-through.** If the parsed form *is* the host's render form, the segment is native and
   is left untouched (root fix-ups are open question 5 in §8).
4. **Render.** Pick the form whose `when` matches the host profile, resolve names through
   packages/services, apply `root`, expand the template to argv.
5. If an intent has no form for the host, or a name has no target for the host family:
   refuse `no_rule` / `unknown_name`.

**Native commands are never blocked** (implemented). A command is *native* on the host if it is
the `cmd` of a form whose `when` matches the host, or `host.has[cmd]` is true, excluding
dialect-sensitive commands. For native commands every failure (unmapped flag, unknown name,
no rule, dynamic argument, shell syntax) means pass-through; ush only ever refuses commands the
host does not have. The one thing done to a native command is the `root: forbidden` fix-up
(`sudo brew install` → `brew install`, with a notice). Dialect-sensitive commands (`sed`) are
parsed but any failure also means pass-through.

Other implemented details: `package_action: install` on an intent is what lets built-in
`action` targets (e.g. `xcode_clt`) run; other intents skip them (so `apt remove build-essential`
never runs `xcode-select --install`). Untouched segments keep their original text (`raw`).
`sudo` with any option (`-u`, `-E` ...) leaves the segment untouched in v1. Refusal reasons:
`unmapped_flag unknown_name no_rule bad_arguments dynamic_argument shell_syntax ambiguous
backup_suffix`.

Worked example (`sed`): `sed -i 's/a/b/' f` parses as GNU only (BSD would take `s/a/b/` as
the suffix and be left with no files) → on a BSD host, render BSD. `sed -i '' 's/a/b/' f`
parses as both, but only BSD has the evidence (`-i` followed by `""`) → BSD, so on a GNU host
it renders as `sed -i 's/a/b/' f`.

## 5. Overlay (user rules)

`~/.config/ush/overlay.yaml` uses the same schema, always `source: user`.
Precedence: `overlay > release rules > bundled seed`. Updates never modify the overlay;
a conflicting release rule produces a notice only.

## 6. Release artifact

```
rules-<version>/
  manifest.json
  manifest.json.release.minisig      # one detached signature per role in manifest.signing.roles
                                     # [v1] roles = ["release"]   (single minisign key)
                                     # [later] roles = ["ci","maintainer"]  (2-of-2)
  rules.json                         # compiled, canonical JSON of packages+commands
  rules.json.sigstore.bundle         # optional, not required by client
```

Design decision: **ship canonical JSON, not a prebuilt SQLite file.** The client schema-validates
`rules.json` and builds its own local SQLite index (`~/.cache/ush/index-<version>.db`). The client
never opens a SQLite database authored by someone else.

### 6.1 manifest.json

See `examples/manifest.json`. Fields:

| Field | Meaning |
|---|---|
| `format` | constant `ush-rules-release` |
| `rules_schema` | integer; client skips releases newer than it supports |
| `version` | monotonically increasing integer (`YYYYMMDDNN`); client rejects `<=` current |
| `released_at`, `expires` | RFC 3339 UTC. Expired = warn "stale", keep using (offline-safe) |
| `min_client` | semver; older clients skip the release |
| `signing` | `{scheme: "minisign", roles: [...]}`. Client requires a valid signature for every listed role and must hold a pinned key for each. Lets 2-of-2 arrive later with no format change. A manifest naming a role the client has no key for is rejected |
| `source_commit` | git commit of the rules repo that produced it |
| `files[]` | `{path, sha256, size}`; client verifies every listed file |
| `stats` | counts, informational |
| `key_rotation?` | optional announcement of new keys (see 6.3) |

### 6.2 Verification order (client, `ush update`)

1. Download `manifest.json` + both `.minisig` (size caps: manifest ≤ 256 KiB, rules ≤ 16 MiB).
2. Verify a signature for every role in `signing.roles` against the pinned key for that role. Fail → abort, keep current rules.
3. Parse manifest; check `format`, `rules_schema`, `min_client`, `version > current`.
4. Download listed files; check size and SHA-256 against manifest.
5. Schema-validate `rules.json`; drop invalid entries individually, abort if > 5% invalid.
6. Build index to a temp file, then atomically rename into place and write `current` pointer.
7. Keep the previous release for `ush rules rollback`.

Pure-Python Ed25519 verification runs only here, never on the command path.

### 6.2.1 Implemented behaviour of `ush update` [v1]

- The client fetches one `manifest.json.<role>.minisig` per role in its own pinned key file
  (roles with at least one key). It does **not** use the unverified manifest to decide what to
  require; after verification the manifest's `signing.roles` must equal the client's set.
- State lives in `$XDG_CONFIG_HOME/ush`: `releases/<version>/` (manifest, signatures,
  rules.json), `current` and `previous` symlinks (swapped atomically), `high_water` (highest
  version ever installed). The floor is `max(high_water, current.version)`: deleting `current`
  does not re-open old releases. `ush rules rollback` swaps current/previous and leaves the
  floor alone, so the release you rolled back from is not offered again as "new".
- Version <= floor → "up to date" (older → warning), no files downloaded. Newer: check
  `rules_schema`, `min_client`, expiry (expired = warning only), then download each file with a
  cap equal to its signed size, verify size and SHA-256, run the client's own `Rules`
  validation (strict in v1: any invalid entry aborts; per-entry dropping is [later]).
- Redirects from https to http are refused. `file://` and plain `http://` are allowed (mirrors;
  integrity comes from the signature, not the transport).
- `trusted_keys.json` ships **empty** in the package. Until a publisher key is pinned (or
  `USH_TRUSTED_KEYS` points to one) `ush update` refuses to run.
- Exit codes: 0 ok / up to date, 1 update failed (nothing changed), 2 usage.
- Not verified at runtime: files already in `current/` are trusted (a local attacker who can
  write them already owns the user's shell).

### 6.3 Pinned keys & rotation

[v1] The client pins one `release` public key; rotation = new client release.
[later] Everything below: separate `ci` / `maintainer` roles, `next` keys, `key_rotation`.

Client ships `trusted_keys.json`:
```json
{ "ci": ["<pubkey-current>", "<pubkey-next>"], "maintainer": ["<pubkey-current>", "<pubkey-next>"] }
```
A valid signature from any listed key of each role passes. To rotate, a release signed by
the *current* keys may carry `key_rotation: {ci: [...], maintainer: [...]}`; the client
persists the new set. Emergency (both keys lost) requires a client release.

### 6.4 Update behaviour

- Never on the command path or shell startup. `ush update` is explicit; optional daily
  background job uses conditional GET (ETag), no identifiers sent.
- `USH_RULES_URL` points to a mirror; pinned keys still apply (`USH_TRUSTED_KEYS` for corporate keys).
- `ush rules diff <old> <new>` prints changed concepts/intents before activation (`--dry-run`).

## 7. CI gates in the rules repo (for AI- and human-authored PRs)

[v1] gates: **1 (schema + reverse index) and 3 (golden tests)** only, with every PR
reviewed by a human. Gates 2, 4, 5, 6 are [later]: add 2 (existence checks) when AI starts
generating rules in bulk, 4–5 when review volume hurts, and the dual-sign step of 6 together
with 2-of-2 signing.

1. Schema validation + reverse-index uniqueness.
2. Existence check per family: `apt-cache policy`, `dnf info`, `brew info` in containers / macOS runner. Passing cells get `verified` stamped by CI, not by the author.
3. Golden test table (`tests/golden.yaml`): input, source family, host profile → expected plan. Any regression blocks merge.
4. Auto-merge allowed only for: new `read`-risk or `exact` package mappings that pass 2 and 3.
5. Human review required for: `risk != read`, `fidelity` of `lossy`/`unsupported`, any `root:` change, any new `intent`.
6. Release job signs with the CI key; maintainer signs the same manifest offline before publishing.

## 8. Open questions

- [later] Package name drift across versions (python@3.12 vs python@3.13): model with `when`, or a `track: latest|pinned` field? Decide when the first real case appears.
- Should `cask` vs formula be inferred (GUI apps) or always explicit in rules?
- Where do `pip/npm/cargo` fit — out of scope for v1.
- Gaps surfaced by `tests/golden.yaml` (cases marked `# GAP`), each needs a decision before the parser:
  1. RESOLVED (§3 scoping rule): `note`/`fidelity` are per target family.
  2. RESOLVED (§4.3.1): every canonical flag must be rendered, `implicit`, or `unsupported_flags`.
  3. RESOLVED (§4.7): separate parse (`match`) and render (`when`) lookups; `evidence` breaks
     ties; backup-suffix variants of `sed -i` are [later] and refused in v1.
  4. Short-flag clustering (`-tulnp`) and `--long=value` normalisation belong in the parser.
  5. Native command with wrong sudo (`sudo brew install` on macOS): fix up with a notice, or
     pass through untouched? Leaning: fix up, because it is a common failure and `root: forbidden` already encodes it.
  6. `confirm` levels (`none/enter/yn/typed`) are derived from `risk` + `fidelity`; the exact
     mapping table still has to be written in §4.6.
  7. `apt update` vs `dnf makecache` / `brew update`: decide if "close enough" is `exact`.
- Golden-table format needs a shared fixture of host profiles (`tests/hosts/*.json`).
