# Weixin

Weixin channel for Magi using Tencent's iLink bot gateway.

## Connection and trust

This package requires plugin protocol 2 and SDK 0.2.0. Each configured connection
owns its account, cursors, message mappings, and host-scoped credentials. Existing
credential files are not imported. The plugin requires explicit trusted-process
activation because it accesses the network and local attachment files.

## Connect

Open the connection's settings and run **Weixin QR Login**. The host displays the
QR code; the plugin handles iLink authorization and stores the returned account
through `self.context.credentials`. Successful settings updates contain only
`account_id` and `base_url`. Tokens and host storage paths are never returned as
settings or action results.

A manual `bot_token` may also be entered in the connection's secret field. The
host stores it in that connection's credential port. Add another connection to
use another account. There is no standalone credential-file login command.

## Messaging

The adapter supports direct-message text and inbound image, file, voice, and video
attachments. Provider-supplied voice transcripts are included; the plugin does
not transcribe audio itself. Outbound text and image upload are supported. Host
attachments must include a resolved `storage_path`; the plugin never guesses
host storage layout from a relative path.

Connection validation, cursor reset, dedupe reset, and logout run through the
host's typed settings-operation boundary. Clearing conversation content preserves
credentials and source progress. Logging out deletes this connection's credentials.
