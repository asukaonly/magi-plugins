# Apple Health

The service processes portable observations from Magi iOS. HealthKit code and
entitlements ship in the iOS app; this package contains no device reader and does
not request host filesystem, network, or model execution permissions.

Enable a dedicated connection for each iPhone after consenting to upload and
processing by the service and its configured models. Device permission alone is
not upload consent. The first release supports manual, foreground synchronization
of selected steps, sleep and workouts. It does not promise background delivery.

Steps are HealthKit daily statistics, which account for overlapping device sources.
Sleep stages and workouts retain their original record IDs and source attribution;
clients must not add overlapping sleep stages to produce a medical interpretation.
Changes use `source.change.v1` and `apple_health.record` schema version 1. Source
object deletions and user-requested forgetting remain different operations. A
permission denial is not proof of an empty Health store. No records are deleted
merely because a query returned no data.

Pausing stops collection while retaining service memory. Clearing the connection's
content invokes the host's governed clear and rotates its observation generation;
iOS must discard old pending observations and start a new explicit selection.
