# Magi Plugins — Agent Handbook

## Scope

This document defines mandatory implementation and delivery rules for coding agents working in the **magi-plugins** repository.

This is a companion repository to the [Magi main repo](https://github.com/asukaonly/magi). The main repo owns the plugin runtime, contracts, and host infrastructure. This repo owns plugin source code and the registry index.

---

## Quick Rules (Do / Don't)

**Do**
- Follow the plugin contracts and base classes defined in the main Magi repo.
- Keep each plugin self-contained in its own directory under `plugins/`.
- Run `python scripts/build-registry.py` after adding or modifying a plugin.
- Commit the updated `registry.json` together with plugin changes.
- Use Conventional Commits with clear English subjects.
- Use English for comments, docstrings, logs, and error messages.
- Test plugin functionality against the Magi backend before pushing.

**Don't**
- Don't modify the plugin runtime or contracts here — those live in the main repo.
- Don't batch unrelated plugin changes in one commit.
- Don't include `cursor` / `claude` / `chatgpt` / `copilot` in commit text.
- Don't add AI identity signatures (e.g. `Co-authored-by: AI Agent`).
- Don't put non-plugin code in this repo.
- Don't manually edit `registry.json` — always regenerate it via the script.

---

## 1) Relationship to Main Repo

| Aspect | Main Repo (magi) | This Repo (magi-plugins) |
|--------|-------------------|--------------------------|
| **URL** | `github.com/asukaonly/magi` | `github.com/asukaonly/magi-plugins` |
| **Owns** | Plugin runtime, contracts, manager, API, frontend | Plugin source code, registry index |
| **Core plugins** | `plugins/core-tools`, `plugins/core-actions` (bundled in app) | — |
| **Optional plugins** | — | All 8 optional plugins (installed via marketplace) |
| **Registry** | Backend fetches `registry.json` from this repo | Hosts and maintains `registry.json` |
| **Docs** | `docs/plugin-development-guide.md`, `docs/plugin-extension-architecture.md` | This file (`agents.md`) |

Key contracts defined in the main repo:
- `backend/src/magi/plugins/base.py` — `Plugin` base class
- `backend/src/magi/plugins/contracts.py` — `PluginManifest`, `PluginContribution`, `ExtensionFieldSpec`, etc.
- `backend/src/magi/awareness/sensor.py` — `Sensor` base class for timeline sensors
- `backend/src/magi/plugins/actions.py` — `BaseAction` for outbound actions

---

## 2) Repository Structure

```text
magi-plugins/
├── registry.json                  # Auto-generated plugin index
├── agents.md                      # This file
├── README.md
├── plugins/
│   ├── calendar_plugin/           # Sensor: Calendar events (macOS/iOS)
│   ├── chrome-history/            # Sensor: Chrome browsing history
│   ├── git_activity/              # Sensor: Git repository activity
│   ├── netease_music/             # Sensor: NetEase Cloud Music history
│   ├── photo-library/             # Sensor: Local photo library
│   ├── screen_time/               # Sensor: App usage tracking (macOS)
│   ├── system_media/              # Sensor: Media playback tracking
│   └── terminal_history/          # Sensor: Terminal command history (macOS)
└── scripts/
    └── build-registry.py          # Scans plugin.toml files → writes registry.json
```

---

## 3) Plugin Structure

Every plugin is a directory under `plugins/` with at minimum:

```text
plugins/<plugin_name>/
├── plugin.toml          # Manifest — declares id, version, contribution types
├── plugin.py            # Entry class inheriting magi.plugins.Plugin
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
| `id` | Yes | Unique plugin identifier (kebab-case) |
| `name` | Yes | Display name |
| `version` | Yes | Semver version string |
| `description` | Yes | One-line description |
| `author` | Yes | Author name |
| `entry_module` | Yes | Python module name (usually `plugin`) |
| `entry_class` | Yes | Class name in entry module |
| `official` | No | `true` for Magi Team plugins |
| `contribution_types` | Yes | Array: `["sensor"]`, `["tool"]`, `["action"]`, or combinations |
| `platforms` | No | Array: `["windows", "macos", "linux", "ios"]` |
| `dependencies` | No | Array of pip package names for auto-install |

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
- Use relative imports within a plugin (`from .reader import ...`).
- Do not import from `magi.plugins` internals beyond the public contracts (`Plugin`, `Sensor`, `BaseAction`, field specs).
- If a plugin needs third-party packages, declare them in `plugin.toml` `dependencies`. They will be pip-installed into the plugin's `.deps/` directory at install time.
- Platform-specific code must be guarded. Use `platforms` in `plugin.toml` to declare supported platforms, and use runtime checks for platform-specific imports.

### i18n
- Plugin display names and descriptions shown in the UI should use `i18n/` locale files when available.
- Keep `en.json` and `zh-CN.json` aligned.

---

## 5) Registry Management

`registry.json` is the index file fetched by the Magi backend to populate the marketplace. It is auto-generated — never edit it manually.

### Regenerate after changes

```bash
python scripts/build-registry.py
```

The script scans all `plugins/*/plugin.toml`, extracts metadata, and writes `registry.json` with the following structure:

```json
{
  "registry_version": "1",
  "repo_url": "https://github.com/asukaonly/magi-plugins.git",
  "plugins": [
    {
      "plugin_id": "chrome-history",
      "name": "Chrome History",
      "version": "0.1.0",
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

### Important: always commit registry.json with plugin changes

If you add, remove, or update a plugin, regenerate and commit `registry.json` in the same commit or an immediately following one.

---

## 6) Task Execution Rules

A task is the smallest independently verifiable and reversible change unit.

A task is complete only when:
1. Plugin code is implemented.
2. `plugin.toml` is correct and complete.
3. `registry.json` is regenerated.
4. Basic validation is done (import test, or manual test against Magi backend).

Rules:
- Keep each commit atomic — one plugin change per commit when practical.
- Do not mix unrelated plugin changes.

---

## 7) Testing & Validation

Plugins run inside the Magi backend. To validate:

1. Copy (or symlink) the plugin to `~/.magi/plugins/<plugin_id>/`.
2. Start the Magi backend.
3. Rescan plugins from Settings → Extensions.
4. Enable the plugin and verify it loads without errors.

For sensor plugins, verify:
- The sensor appears in Settings → Sensors.
- A manual sync produces timeline entries (or logs the expected behavior).

```bash
# Quick import validation (from the main magi repo with venv activated)
cd <magi-repo>
python -c "import importlib.util; spec = importlib.util.spec_from_file_location('plugin', '<path>/plugin.py'); mod = importlib.util.module_from_spec(spec)"
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
2. Update `plugin.toml` if metadata changed.
3. Run `python scripts/build-registry.py`.
4. Test against the Magi backend.
5. Commit plugin changes + updated `registry.json`.
6. Push.

---

## 10) Branching

- `main`: stable branch, registry.json always reflects current state
- `feat/<plugin-name>`: new plugin development
- `fix/<plugin-name>`: bug fixes for existing plugins

---

## 11) Review Checklist

- [ ] Plugin is self-contained (no cross-plugin imports)
- [ ] `plugin.toml` has all required fields
- [ ] `plugin.py` entry class inherits `Plugin`
- [ ] `platforms` declared if platform-specific
- [ ] `registry.json` regenerated and committed
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

**Last Updated**: 2026-04-19
**Maintainer**: Magi Development Team
