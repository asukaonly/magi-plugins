# magi-plugins

Official plugin registry for [Magi](https://github.com/asukaonly/magi).

## Structure

```
magi-plugins/
├── registry.json              # Plugin index (auto-generated)
├── plugins/
│   ├── calendar_plugin/
│   ├── chrome-history/
│   ├── git_activity/
│   ├── netease_music/
│   ├── photo-library/
│   ├── screen_time/
│   ├── system_media/
│   └── terminal_history/
└── scripts/
    └── build-registry.py      # Regenerate registry.json from plugin.toml files
```

## Usage

Plugins are installed automatically from the Magi settings page (Settings → Extensions → Marketplace).

## Regenerating registry.json

```bash
python scripts/build-registry.py
```
