# Weixin

Weixin channel for Magi, based on Tencent's iLink bot gateway protocol used by `openclaw-weixin`.

## Scope

- QR login helper that stores bot credentials locally.
- Direct-message text ingestion through `getupdates` long polling.
- Text replies through `sendmessage`.
- Inbound image, file, voice, and video attachments through Weixin CDN download/decryption.
- Context token persistence for replies.
- Optional typing indicator through `getconfig` and `sendtyping`.
- Maintenance actions for connection validation, cursor reset, dedupe reset, and logout.

Inbound media is stored as Magi chat attachments. Images and files are available to Magi's normal attachment pipeline. Voice and video are preserved as attachments, but the plugin does not transcribe audio or analyze video frames by itself; when Weixin provides `voice_item.text`, that transcript is included in the message text.

Outbound media upload is not implemented yet. Magi replies are sent as text and split into multiple Weixin messages when they exceed the configured maximum message length.

## Settings QR Login

When the installed Magi host supports plugin settings actions, open Settings -> Channels, select Weixin, and run the Weixin QR Login action. The host renders the QR code generically; this plugin owns the iLink login protocol and stores credentials locally.

On success, the plugin saves credentials under the configured state directory and returns safe settings updates (`account_id`, `credentials_path`, `state_dir`, and `base_url`) for Magi to persist. The bot token remains in the credentials file and is not copied into the manual `bot_token` setting.

## CLI QR Login Helper

Run the helper from this repository or from an installed plugin copy:

```bash
python plugins/weixin/login.py
```

The helper prints a Weixin QR-code link and stores credentials under:

```text
~/.magi/weixin/accounts/<account_id>.json
```

If exactly one account is saved in the state directory, the channel can load it automatically. If multiple accounts exist, set `account_id` in the extension settings.

## Manual Credentials

You can also configure the manual `bot_token` and `account_id` directly in the extension settings, or point `credentials_path` at a JSON file:

```json
{
  "account_id": "example@im.bot",
  "token": "...",
  "base_url": "https://ilinkai.weixin.qq.com",
  "user_id": "optional-user-id"
}
```