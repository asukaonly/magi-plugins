# MySQL Storage

Stores Magi chat, L1 memory events, and channel/outreach state in a dedicated MySQL database. Requires Magi Plugin SDK 0.2.7 or later and a MySQL server supporting the packaged migration chain (InnoDB, utf8mb4, JSON_TABLE, triggers and enforced constraints).

Install through Magi Extensions, review its permissions, and create a connection with the database host, port, database, user and password. Credentials belong to that connection's secret store. Remote hosts require verified TLS; an optional CA certificate can be supplied. The database account needs schema migration privileges, including triggers. The database must belong to one Magi center; populated unowned tables are rejected.

Copy the connection ID shown in Extensions. With the service stopped, select it in `config/agent.yaml`:

```yaml
structured_storage:
  chat:
    connection_id: conn_0123456789abcdef0123456789abcdef
    provider_id: mysql
  memory_events:
    connection_id: conn_0123456789abcdef0123456789abcdef
    provider_id: mysql
  channels:
    connection_id: conn_0123456789abcdef0123456789abcdef
    provider_id: mysql
```

Replace the example ID with the actual connection ID. Omitted domains retain the bundled SQLite provider. Selection does not move existing data. For every existing domain being moved, use the server's Python environment while the service is stopped:

```sh
python -m magi.bootstrap.storage_transfer --root /absolute/magi-root --domain chat
python -m magi.bootstrap.storage_transfer --root /absolute/magi-root --domain memory_events
python -m magi.bootstrap.storage_transfer --root /absolute/magi-root --domain channels
```

A nonempty destination requires the explicit `--replace-target` option. Transfers preserve the source and activate the destination only after checksum verification. Interrupted transfers block startup and resume with the same command. Keep both connections for a remote-to-remote move. To return to SQLite, select `{}` for that domain and use the same transfer command.

Pre-plugin databases with version-1 bindings require `--adopt-current-binding` for the same domain and physical database before normal startup. The command verifies ownership without initializing or migrating a replacement database.

The plugin owns database drivers, native schema migrations, and repository transactions. Agent policy, attachments, workspace files, trace data, vectors and other Magi databases remain owned by their existing host services. Selected connections cannot be changed or removed online. A worker or connection failure never silently selects a local database or retries an uncertain write.

Dependencies are installed from `requirements.lock` into the plugin's private dependency directory. Host imports are prohibited. SDK conformance and settings declaration checks run from the companion repository. Real MySQL transaction, round-trip, interruption, restore, and process-boundary tests live in Magi's shared conformance suite; select this package with `MAGI_TEST_MYSQL_PLUGIN_ROOT` and supply a disposable `magi_test_` database. Do not run those tests against a production database.
