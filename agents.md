# Magi Plugins — Agent Handbook

## Scope

This document defines mandatory implementation and delivery rules for coding agents working in the **magi-plugins** repository.

This is a companion repository to the [Magi main repo](https://github.com/asukaonly/magi). The main repo owns the plugin runtime, contracts, and host infrastructure. This repo owns plugin source code and the registry index.

---

## Quick Rules (Do / Don't)

**Do**
- Follow the plugin contracts and base classes defined in the main Magi repo.
- Keep each plugin self-contained in its own directory under `plugins/`.
- **After editing any tracked file or executable bit inside
  `plugins/<name>/`**, bump that plugin's version, stage the complete package
  change, run
  `bash scripts/refresh.sh <name>`, and commit the regenerated
  `requirements.lock`, `registry.json`, and `version-history.json` with the
  package change. `build-registry.py` hashes every regular file exactly as
  staged in the Git index, so changing code, assets, tests, or metadata without
  a version bump is rejected. Unstaged package changes make generation fail.
  - **Strongly recommended:** wire up the in-repo pre-commit hook once per clone so the artifacts re-generate automatically when you stage plugin package files:
    `git config core.hooksPath scripts/hooks`
    The hook is idempotent — a no-op when no package file is staged. Bypass for emergencies with `git commit --no-verify` (CI will still catch drift).
  - `lock-deps.py` needs `uv` (`pip install uv`). Lockfiles target the bundled **Python 3.13 runtime**, not your host Python — the locks are identical regardless of the contributor's local interpreter.
  - Registry generation needs the host contract selected by
    `scripts/registry-requirements.txt`. Install it with
    `python -m pip install -r scripts/registry-requirements.txt`; CI uses the
    same file.
  - To adopt newer dependency versions, bump `EXCLUDE_NEWER` in `scripts/lock-deps.py` and re-run `bash scripts/refresh.sh`. The pinned uv version in CI (`0.11.17`) is a coupled determinism input — bump it deliberately too.
- To mark a plugin official, add its `plugin_id` to `official-plugins.json` (maintainer-gated via CODEOWNERS), then regenerate the registry.
- Declare what a plugin accesses with `[[plugin.permissions.capabilities]]` (capability from the known set: screen_recording, accessibility, calendar, photos, contacts, system_media, filesystem_read, filesystem_write, network, subprocess; optional `scope`, `optional`, `reason_i18n`). Users see these at install for consent; reviewers use them as a checklist.
- Use Conventional Commits with clear English subjects.
- Use English for comments, docstrings, logs, and error messages.
- Run SDK-only conformance and focused behavior tests before publishing.

**Don't**
- Don't modify the plugin runtime or contracts here — those live in the main repo.
- Don't mirror SDK manifest, registry, identifier, version, or dependency-graph
  rules here. Import the authoritative `magi-plugin-sdk` models and keep local
  validation limited to repository publication policy.
- Don't batch unrelated plugin changes in one commit.
- Don't include `cursor` / `claude` / `chatgpt` / `copilot` in commit text.
- Don't add AI identity signatures (e.g. `Co-authored-by: AI Agent`).
- Don't put non-plugin code in this repo.
- Don't manually edit `registry.json` — always regenerate it via the script.
- Don't manually edit or remove entries from `version-history.json`. Published
  `plugin_id@version` identities are append-only.
- Don't commit links, special files, caches, dependency directories, or build
  output inside a plugin package.
- Don't hand-edit `requirements.lock` files — always regenerate via `scripts/lock-deps.py`.
- Don't set `official = true` in a plugin's `plugin.toml` expecting a badge — the `official` flag is derived solely from `official-plugins.json` (maintainer-controlled). Self-declared values are ignored.
- Don't declare a capability outside the known set — `build-registry.py` will fail the build. Adding a new capability requires updating the SDK + frontend too.

---

## 1) Relationship to Main Repo

| Aspect | Main Repo (magi) | This Repo (magi-plugins) |
|--------|-------------------|--------------------------|
| **URL** | `github.com/asukaonly/magi` | `github.com/asukaonly/magi-plugins` |
| **Owns** | Plugin runtime, contracts, manager, API, frontend | Plugin source code, registry index |
| **Core plugins** | `plugins/core-tools` (bundled in app) | — |
| **Optional plugins** | — | Marketplace plugins and hidden shared libraries |
| **Registry** | Backend fetches `registry.json` from this repo | Hosts and maintains `registry.json` |
| **Docs** | `docs/plugin-development-guide.md`, `docs/plugin-extension-architecture.md` | This file (`agents.md`) |

Key public contracts live under `sdk/src/magi_plugin_sdk/` in the main repo:
- `base.py` and `context.py`: connection-bound Plugin and host-owned state/resources/credentials.
- `runtime.py`: invocation identity, source changes, operations, readiness, and resource references.
- `sensors.py`, `channels.py`, `history_imports.py`: source, channel, and bounded archive authoring.
- `contracts.py`: strict manifest, settings, extraction and registry declarations.

All packages explicitly declare `protocol_version = 2`, `min_sdk_version = "0.2.0"`,
`execution_mode`, and `projection_sources`. Existing native/filesystem/network
packages require `trusted_process` and explicit host trust. A process boundary
alone is not a sandbox. Never add a legacy alias, adapter, or data migration.

The host calls `configure(manifest=..., connection=..., context=...)`. Use the
bound `self.connection` identity and `self.context` paths. Every instance, cache,
source cursor and credential belongs to that connection. Credential access uses
synchronous scoped `get(key)`, `set(key, value)`, `delete(key)` calls, never a
caller-chosen connection or credential file. Shared libraries are stateless with
respect to accounts; caller-owned objects hold mutable state.

Sensors return `SourceChangeBatch` and explicit stable `SourceChange` revisions.
Payloads must be JSON values; serialize parser timestamps before crossing the
SDK boundary. Keep source categories independent of connection IDs. Projections
must declare actual semantic source selectors; the host limits them to authorized
connection data and versions projection rules by package version.

Declare all settings, including internal controls and secret keys, under
`[[plugin.settings_fields]]`. The host validates this schema before importing
plugin code. Export and review declarations with `scripts/export-settings-fields.py`;
contribution field methods remain descriptive views. Vault keys must match the
secret field key exactly, including any dotted source prefix.

### Declarative setup catalog

Export `settings_fields`, `activation_flow`, `settings_actions`, `settings_resources`,
and `settings_ui_blocks` with `scripts/export-settings-fields.py` against the exact
SDK under development. The first registered sensor activation flow is primary;
Local Documents and Obsidian Vault use their knowledge tier. Export only public
schemas. Never resolve a settings resource or start an action during export or
discovery. The host authorizes explicit pre-enable setup actions and resource reads only
when their reviewed schema declares `requires_enabled = false`.

Settings fields use `minimum` and `maximum`. Strict SDK models reject unknown
fields; do not silently discard obsolete extraction or manifest options.

---

## 2) Repository Structure

```text
magi-plugins/
├── registry.json                  # Auto-generated plugin index
├── version-history.json           # Append-only hash + executable metadata
├── agents.md                      # This file
├── README.md
├── plugins/
│   ├── calendar_plugin/           # Sensor: Calendar events (macOS/iOS)
│   ├── chrome-history/            # Sensor: Chrome browsing history
│   ├── claude-code/               # Sensor: Claude Code transcripts
│   ├── codex/                     # Sensor: Codex transcripts
│   ├── git_activity/              # Sensor: Git repository activity
│   ├── netease_music/             # Sensor: NetEase Cloud Music history
│   ├── apple-photos/              # Sensor: Apple Photos (macOS)
│   ├── local-photos/              # Sensor: User-selected local photo folders
│   ├── agent_history_core/        # Hidden shared transcript library
│   ├── photo_library_core/        # Hidden shared photo library
│   ├── screen_time/               # Sensor: App usage tracking (macOS)
│   ├── system_media/              # Sensor: Media playback tracking
│   └── terminal_history/          # Sensor: Terminal command history (macOS)
└── scripts/
    ├── build-registry.py          # Writes registry + version history
    ├── registry-requirements.txt  # Authoritative host-contract dependency
    └── package_identity.py        # Canonical tracked-package SHA-256
```

---

## 3) Plugin Structure

Every plugin is a directory under `plugins/` with at minimum:

```text
plugins/<plugin_name>/
├── assets/              # Packaged brand/static assets (optional)
│   └── icon.svg
├── plugin.toml          # Manifest — declares id, version, contribution types
├── plugin.py            # Entry class inheriting magi_plugin_sdk.Plugin
├── sensor.py            # Sensor implementation (optional, for timeline sensors)
├── normalizers.py       # Data normalizers (optional)
├── reader.py            # Data source readers (optional)
├── i18n/                # Localisation files (optional)
│   ├── en.json
│   └── zh-CN.json
└── ...
```

### plugin.toml fields

| Field | Required | Description |
|-------|----------|-------------|
| `id` | Yes | Unique identifier accepted by the SDK |
| `protocol_version` | Yes | Explicit `2`; no old manifest fallback |
| `min_sdk_version` | Yes | Explicit `"0.2.0"` |
| `execution_mode` | Yes | Existing packages declare `trusted_process`; host trust is required |
| `projection_sources` | Yes | Semantic source selectors; grants remain host-owned |
| `settings_fields` | Yes | Complete declarative schema, including hidden controls and secret keys |
| `activation_flow` | When needed | Initial setup form; first registered sensor flow is primary |
| `settings_actions` / `settings_resources` / `settings_ui_blocks` | Yes | Public setup schemas; empty arrays when unused |
| `name` | Yes | Display name |
| `version` | Yes | Semver version string |
| `description` | Yes | One-line description |
| `author` | Yes | Author name |
| `icon` | No | `asset:assets/icon.svg` for packaged brand art, or `lucide:<name>` for a generic host icon |
| `entry_module` | Yes | Python module name (usually `plugin`) |
| `entry_class` | Yes | Class name in entry module |
| `official` | No | Marketplace status comes from `official-plugins.json`, never self-assertion |
| `contribution_types` | Yes | SDK contribution values, such as `sensor`, `tool`, `operation`, `channel`, or `history_importer` |
| `platforms` | No | Array: `["windows", "macos", "linux", "ios"]` |
| `dependencies` | No | SDK-validated Python dependencies resolved into the package lockfile |
| `depends_on` | No | IDs of separately installed shared libraries |

---

## 4) Coding Standards

### Python
- Python 3.10+ required.
- Classes: `PascalCase`, functions/variables: `snake_case`, constants: `UPPER_SNAKE_CASE`.
- Public methods must include type hints.
- I/O should be async (`async/await`).
- Use specific exceptions; avoid bare `except`.
- Prefer Google-style docstrings for non-trivial public methods.
- Comments, docstrings, logs, and error messages must be in English.

### Plugin-Specific Rules
- Each plugin must be fully self-contained — no cross-imports between plugins.
- Brand icons must live inside the plugin package and use an `asset:` path.
- Generic icons may use any icon from the host's Lucide library through a `lucide:` value.
- Packaged icons must be SVG, PNG, or WebP, no larger than 64 KiB, and pass the registry's safety validation.
- Use relative imports within a plugin (`from .reader import ...`).
- Never import from the `magi` backend, including re-export paths. Only `magi_plugin_sdk` is public.
- If a plugin needs third-party packages, declare them in `plugin.toml` `dependencies`. They will be pip-installed into the plugin's `.deps/` directory at install time.
- Platform-specific code must be guarded. Use `platforms` in `plugin.toml` to declare supported platforms, and use runtime checks for platform-specific imports.

### i18n
- Plugin display names and descriptions shown in the UI should use `i18n/` locale files when available.
- Keep `en.json` and `zh-CN.json` aligned.

---

## 5) Registry Management

`registry.json` is the index file fetched by the Magi backend to populate the
marketplace. It is auto-generated — never edit it manually. Every entry has a
`package_sha256` covering all regular files staged in the Git index for that
plugin directory, including code, assets, tests, the manifest, and dependency
locks. The generator freezes one Git tree before reading metadata or package
bytes, so the working tree is never a second content source. Source identity
covers normalized paths and file bytes so identity remains stable across
operating systems. Git executable modes are collected from that same frozen
tree as separate publication metadata.

`version-history.json` permanently binds every published
`plugin_id@version` to one package SHA-256 plus its sorted canonical executable
paths. A package content or executable-permission change must use a new version.
Executable paths are not part of `package_sha256` and do not create another
runtime identity. Versions must use canonical `MAJOR.MINOR.PATCH` form and
contain no more than 32 characters. Historical entries may be appended but
never changed or deleted. The current registry entry must use that plugin's
highest published version; downgrades and lower-version insertions are rejected.

The registry generator directly uses the `PluginManifest`,
`PluginRegistryIndex`, plugin identifier, and version contracts installed from
`magi-plugin-sdk`; it does not maintain a local field or dependency-graph
schema. `scripts/registry-requirements.txt` selects the SDK source used locally
and in CI and must be pinned to an exact Magi commit before release. The local
adapter only adds repository publication policy, including the capability
allowlist. Invalid manifests fail generation instead of making the complete
marketplace index unreadable at runtime.

### Regenerate after changes

```bash
python -m pip install -r scripts/registry-requirements.txt
python scripts/build-registry.py
```

The script scans all staged `plugins/*/plugin.toml` entries from one frozen Git
snapshot, extracts metadata, and writes `registry.json` with the following
structure (schema arrays abbreviated here; generated entries contain their full declarations):

```json
{
  "registry_version": "4",
  "repo_url": "https://github.com/asukaonly/magi-plugins.git",
  "plugins": [
    {
      "plugin_id": "chrome-history",
      "name": "Chrome History",
      "version": "0.2.0",
      "protocol_version": 2,
      "min_sdk_version": "0.2.0",
      "execution_mode": "trusted_process",
      "projection_sources": ["chrome_history"],
      "settings_fields": [],
      "settings_actions": [],
      "settings_resources": [],
      "settings_ui_blocks": [],
      "package_sha256": "64 lowercase hexadecimal characters",
      "path": "plugins/chrome-history",
      "description": "...",
      "author": "Magi Team",
      "official": true,
      "contribution_types": ["sensor"],
      "platforms": ["windows", "macos", "linux"]
    }
  ]
}
```

### Important: always commit generated identity files with plugin changes

If you add, remove, or update any tracked package file:

1. bump the package version
2. stage the complete package change so it becomes the Git package snapshot
3. regenerate and commit `registry.json` and `version-history.json` with the
   package change

The generator rejects links, special files, generated runtime products,
non-portable paths, and portable filename collisions. Ignored local caches are
never included.

---

## 6) Task Execution Rules

A task is the smallest independently verifiable and reversible change unit.

A task is complete only when:
1. Plugin code is implemented.
2. `plugin.toml` is correct and complete.
3. `registry.json` and `version-history.json` are regenerated.
4. SDK-only conformance and focused behavioral validation pass.

Rules:
- Keep each commit atomic — one plugin change per commit when practical.
- Do not mix unrelated plugin changes.

---

## 7) Testing & Validation

Plugins run in connection-bound workers launched by Magi. To validate:

1. Add a development root containing the plugin package to Magi's
   `plugins.scan_paths`. Keep it outside the managed `~/.magi/plugins/`
   install directory.
2. Start Magi and rescan plugins from Settings → Extensions.
3. Create a connection, complete its declared setup, grant trusted access, and enable it.

Do not manually copy or symlink a package into the managed install directory.
If validation uses the product's **Install from local directory** action, Magi
copies the package into that directory and records the installation.

For sensor plugins, verify:
- The sensor appears in Settings → Sensors.
- A manual sync produces timeline entries (or logs the expected behavior).

```bash
python -m pip install -r scripts/conformance-requirements.txt
python -m pytest --import-mode=importlib scripts/test_sdk_conformance.py plugins -q
```

---

## 8) Git Commit Policy

### Commit Format
Use Conventional Commits:

```text
<type>: <subject>

<body>

<footer>
```

Recommended types: `feat`, `fix`, `refactor`, `docs`, `chore`

### Commit Quality Rules
- Subject: concise, English, <= 50 chars recommended.
- Body: explain why/scope/impact for non-trivial changes.
- Keep each commit atomic.

### Prohibited Content
Commit text must not contain:
- `cursor`, `claude`, `chatgpt`, `copilot`
- `ai-generated`, `generated by ai`
- `Co-authored-by: Cursor`, `Co-authored-by: Claude`, or similar

---

## 9) Development Workflow

1. Create or modify plugin code under `plugins/<plugin_name>/`.
2. Bump the version in `plugin.toml`.
3. Stage the complete package change.
4. Run `bash scripts/refresh.sh <plugin_name>`.
5. Run SDK-only conformance and focused package tests.
6. Commit plugin changes + updated `registry.json` +
   `version-history.json`.
7. Push.

---

## 10) Branching

- `main`: stable branch, generated registry and version history reflect current state
- `feat/<plugin-name>`: new plugin development
- `fix/<plugin-name>`: bug fixes for existing plugins

---

## 11) Review Checklist

- [ ] Plugin is self-contained (no cross-plugin imports)
- [ ] `plugin.toml` has all required fields
- [ ] `plugin.py` entry class inherits `Plugin`
- [ ] `platforms` declared if platform-specific
- [ ] package version bumped for every tracked content change
- [ ] `registry.json` and `version-history.json` regenerated and committed
- [ ] Code follows naming/type/async conventions
- [ ] Commit message follows policy
- [ ] Commit message contains no agent/model identity markers

---

## 12) References

- [Magi Main Repo](https://github.com/asukaonly/magi)
- [Plugin Development Guide](https://github.com/asukaonly/magi/blob/main/docs/plugin-development-guide.md)
- [Plugin Extension Architecture](https://github.com/asukaonly/magi/blob/main/docs/plugin-extension-architecture.md)
- [Magi Agent Handbook](https://github.com/asukaonly/magi/blob/main/agents.md)

---

**Last Updated**: 2026-09-05
**Maintainer**: Magi Development Team
