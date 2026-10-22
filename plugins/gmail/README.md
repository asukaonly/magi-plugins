# Gmail

Gmail is a read-only, connection-owned mail source built on `mail_core` and
Magi Plugin SDK 0.2.3. It registers no Gmail-specific model tools. The host's
`memory_query` entry point searches retained evidence and performs bounded source
checks when needed. Provider/account filters select a particular mailbox.

## Setup

1. In a Google Cloud project, enable Gmail API, configure the OAuth consent screen,
   and create an OAuth client with application type **Desktop app**. If the project
   is in testing, allow the Google account that will connect.
2. Install Gmail with its `mail_core` dependency. Grant the declared trusted-process
   network access and create one connection per mailbox.
3. Enter the Desktop client ID and its client secret in the connection settings.
   Select **Authorize Gmail** and grant the `gmail.readonly` permission.
4. Complete authorization in a browser on the machine running the Magi service.
   The callback listens on a random port on `127.0.0.1`, uses PKCE S256 and a
   single-use state, and expires after ten minutes. For remote service connections,
   open the authorization URL on that service host; a browser on a different
   machine cannot reach its loopback callback.
5. Enable the mail source. The initial import defaults to the previous 30 days;
   on-demand search may retrieve older mail. The default sync interval is ten
   minutes. Disable **Allow on-demand mail checks** to keep only scheduled sync.

Credentials, including refresh tokens, use the host's connection-scoped vault.
No credential is placed in settings updates, source payloads, cursors or recall
results. Reauthorization must use the same account; another account requires a
new connection. **Disconnect** deletes the local token and disables the source.
It preserves retained memory and does not revoke the Google-side grant; use Google
account permissions to revoke that separately. Memory forgetting is host-owned.

## Behavior and limits

- Full import checkpoints each bounded page, then catches up from its original
  Gmail history marker. Subsequent sync uses history changes, including label
  updates and deletions. An expired history marker (HTTP 404) restarts full import
  at the original configured time boundary. API failures do not advance a cursor.
- History sync describes changes, not complete retained mailbox coverage. Old
  remembered messages may remain useful historical evidence after remote deletion;
  present-state questions require a source check.
- Queries support literal search terms, message IDs, threads, time bounds and
  label scopes. They include spam/trash so a hidden default folder exclusion never
  masquerades as whole-mailbox coverage. The agent does not construct Gmail syntax.
- Gmail search is best-effort, with no historical `as_of` or snapshot guarantee.
  A final page is not proof of an exact total or global absence, and
  `resultSizeEstimate` is never used as an exact count. The host exposes partial
  results and source-check limitations instead.
- Bodies become plain text; HTML scripts/styles and remote images are never
  executed or loaded. Attachment names are metadata only. Attachments and bodies
  exposed only as external attachment references are not downloaded. Text above
  12,000 characters, missing bodies and decoding loss are marked incomplete.
  Each HTTP response is capped at 4 MiB; each source query page at 50 messages.
- Mail is external evidence stored through the normal L1 ingestion gateway. It
  does not automatically establish user preferences or run L2 extraction. Live
  query results are transient and never silently persisted as memory events.

## Paired host release

Publish the exact Magi host commit pinned in `scripts/registry-requirements.txt`
before publishing this plugin revision. Gmail and `mail_core` require SDK 0.2.3;
older hosts must reject installation. Local tests may use that host checkout
through `PYTHONPATH`; this is not evidence that the commit is available remotely.

## Validation

Package tests use mocked Gmail HTTP responses and a real local OAuth callback.
They cover authorization, token refresh, MIME parsing, pagination, history recovery,
checkpoint behavior and account isolation. They do not certify a live Google
project or real-account authorization. Verify setup with an actual account after
installing the paired host version before treating provider acceptance as complete.

References: [Desktop OAuth](https://developers.google.com/identity/protocols/oauth2/native-app),
[Gmail synchronization](https://developers.google.com/workspace/gmail/api/guides/sync),
[message listing](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/list).
