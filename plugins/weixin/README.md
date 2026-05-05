# Weixin

Text-only Weixin channel for Magi, based on Tencent's iLink bot gateway protocol used by `openclaw-weixin`.

## Scope

- QR login helper that stores bot credentials locally.
- Direct-message text ingestion through `getupdates` long polling.
- Text replies through `sendmessage`.
- Context token persistence for replies.
- Optional typing indicator through `getconfig` and `sendtyping`.

Media messages are intentionally out of scope for the first version.

## Settings QR Login

When the installed Magi host supports plugin settings actions, open Settings -> Channels, select Weixin, and run the Weixin QR Login action. The host renders the QR code generically; this plugin owns the iLink login protocol and stores credentials locally.

On success, the plugin saves credentials under the configured state directory and returns safe settings updates (`account_id`, `credentials_path`, `state_dir`, and `base_url`) for Magi to persist.

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

You can also configure `bot_token` and `account_id` directly in the extension settings, or point `credentials_path` at a JSON file:

```json
{
  "account_id": "example@im.bot",
  "token": "...",
  "base_url": "https://ilinkai.weixin.qq.com",
  "user_id": "optional-user-id"
}
```