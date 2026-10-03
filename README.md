# alarm-route-scan

What a Niagara station does to an alarm between the source and the recipient -
one queue, one thread, a coalesce rule that drops duplicates, and a success()
that returns true for an alarm nobody delivered. Read out of `alarm-rt.jar` and
`baja.jar` rather than out of the documentation.

One Python file, standard library only.

```
./alarm-route-scan.py [NIAGARA_HOME]
```

## Why this exists

Alarm routing looks like a fan-out: a point goes off-normal, the alarm class
routes it, recipients are notified. In the bytecode it is a single
`CoalesceQueue` drained by a single worker thread, and three of its properties
behave in ways that matter when a site is busy or when something has gone wrong.

The one worth knowing first: when two routes of the same record are in that
queue at the same time, one is coalesced away - and the invocation that lost
the collision is marked finished with no throwable, so its `IFuture.success()`
returns `true`. Code that branches on `success()` cannot tell a delivered alarm
from a discarded one. Neither can the caller, because both `post` methods drop
the boolean `enqueue` returns.

The second: `coalesceAlarms` defaults to `true`, and in that default the
coalesce key is the record UUID, the alarm class and the action - **not** the
source state. Setting the property to `false` is what switches in the class
that also compares source state. The property name reads like it turns
coalescing on; what it really selects is which of two coalescing rules applies.

The third: inside `doRouteAlarm` the alarm database write happens before
`fireAlarm`. A failed write is logged SEVERE and rethrown, so no recipient is
notified at all. Persistence first, people second.

## What it reads, and where from

- `javax.baja.alarm.BAlarmService` and `BAlarmClass` - where the queue lives,
  how it is handed out, how many workers `serviceStarted()` builds, and the
  order of operations inside `doRouteAlarm` by bytecode offset.
- `com.tridium.alarm.AlarmClassRouteAlarmInvocation` and
  `CoalesceUuidOnlyInvocation` - the two `equals`/`hashCode` pairs that decide
  the coalesce key, and what `coalesce()` does to the invocation it drops.
- `javax.baja.util.Queue` and `CoalesceQueue` - the real `maxSize`, the
  condition `enqueue` throws on, and the hash table it sizes from it.
- `javax.baja.sys.Flags` - so the flag numbers printed for properties, actions
  and topics are decoded from the shipped constants rather than from a table in
  a README.
- The escalation properties and their `min` facets, the topics each level fires,
  and `ackRequired`'s default bits, all read from the static initialiser.

## Read first: what it does and does not touch

**It never connects to a station.** It unzips two jars out of a Niagara
installation and runs `javap`. Nothing is installed, patched, written to a
station or sent anywhere. No station needs to be running, and it does not care
whether one is.

It needs a Niagara installation to read and a `javap` from a JDK 8. It looks for
`javap` in `$JAVAP`, then on `PATH`, then under `$JAVA_HOME` and the Niagara
install, then in Debian's default location. The install to read comes from the
first argument or `$NIAGARA_HOME`.

**The output below was measured against Niagara 4.15.5.22.** Another version may
differ, and that is the point - run it against yours rather than trusting this
page. A reference counted here is not a fault; it is where to look.

## Running it

```
$ ./alarm-route-scan.py
alarm-route-scan - what a station does to an alarm on its way out
Niagara home: /opt/Niagara/Niagara-4.15.5.22
read from: modules/alarm-rt.jar, modules/baja.jar (javap, no station running)

1. one queue, one thread, and no ceiling

   BAlarmService holds the alarm queue in a field and hands it out through
   fw(601); BAlarmClass.post(routeAlarm, record) asks for it that way and
   enqueues a AlarmClassRouteAlarmInvocation. So every alarm route in the
   station, from every driver and every control point, goes through one
   CoalesceQueue drained by one Worker thread named Alarm:ServiceWorker.
   Workers constructed in serviceStarted(): 1.

   The queue is built with the no-argument constructor, which passes
   maxSize = 2147483647. Queue.enqueue throws QueueFullException only
   when size >= maxSize, so on this build that exception is unreachable
   in practice: a burst the worker cannot keep up with is not refused,
   it is accumulated. Hash table: max(16, min(maxSize/3, 101)) = 101 buckets
   at load factor 0.75.

   The depth is published in exactly one place: the service's spy page,
   as workInAlarmQueue = Queue.size(). It is not a property, so it cannot
   be linked, trended or alarmed on.

2. the coalesce key, and the property name points the other way

   coalesceAlarms: default true, flags 0 (none)

   make() reads it and picks the class:
     coalesceAlarms true  -> AlarmClassRouteAlarmInvocation
     coalesceAlarms false -> CoalesceUuidOnlyInvocation

   Both equals methods compare the alarm class instance, the action and
   the record UUID. Only CoalesceUuidOnlyInvocation also compares the
   record's source state, in equals and in hashCode alike.

   So with the default - coalesceAlarms true - two routes of one record
   that are in the queue at the same time collapse into one whatever
   their states are. Turning the property off is what keeps them apart.

3. the invocation that loses the collision says it succeeded

   CoalesceQueue.enqueue finds the equal entry, calls
   existing.coalesce(incoming), stores the result back into that entry's
   slot - so the survivor inherits the earlier arrival's queue position -
   and returns false.

   coalesce() sets finished = true on the invocation being dropped and
   returns the incoming one. throwable is left null. success() is
   finished and no throwable, so on the dropped invocation it returns
   true: the IFuture says the alarm was routed, and doRouteAlarm never
   ran for it.

   Neither caller can see it either way. BAlarmClass.post and
   BAlarmService.post both pop the boolean enqueue returns.
   (BAlarmService.post only queues escalateAlarms itself; everything
   else goes to the superclass.)

4. the database write comes before the people

   doRouteAlarm, by offset: AlarmDbConnection.append at 169 or .update at
   178, then fireAlarm at 367. An Exception out of that write is
   logged SEVERE 'Cannot write alarm.' and rethrown as an AlarmException, so
   fireAlarm is never reached and no recipient is notified.
   Persistence first, people second.

   A ServiceNotFoundException handler covers the method to offset 503 of
   519 - effectively all of it - and its handler logs and returns. Upstream,
   AlarmSupport logs SEVERE 'Unable to route new alert for' and returns null
   when there is no alarm service at all.

5. escalation is off, and it is cumulative

   escalationLevel<n>Enabled / escalationLevel<n>Delay, by level:

   level 1  enabled false  flags 0  delay  300000 ms  min facet  60000 ms
   level 2  enabled false  flags 0  delay  900000 ms  min facet 120000 ms
   level 3  enabled false  flags 0  delay 1800000 ms  min facet 180000 ms

   escalationTimeTrigger: interval 1 minute(s), flags 4 (HIDDEN)
   escalateAlarms action: flags 2068 (HIDDEN|ASYNC|NO_AUDIT)
   - ASYNC, so the once-a-minute escalation scan is queued onto the same
     single worker thread that delivers alarms.

   Which topic fires is decided by a string facet on the record. doRouteAlarm
   adds 'escalated' with the empty string when it is absent, so a first
   route fires no escalated topic. The tests are cumulative:
     fireEscalatedAlarm1 on level1, level2, level3
     fireEscalatedAlarm2 on level2, level3
     fireEscalatedAlarm3 on level3

   Topic flags as declared:
     alarm            8 (SUMMARY)
     escalatedAlarm1  0 (none)
     escalatedAlarm2  0 (none)
     escalatedAlarm3  0 (none)
   changed() ors SUMMARY into the matching escalated topic when a level is
   enabled and masks it out again when it is disabled.

   Worth a look: the level 2 enable branch reads the flags of
   escalatedAlarm1 and writes them to escalatedAlarm2.
   Levels whose branches agree: 1, 3.

6. ackRequired, and where it is really used

   ackRequired: default 7 = TO_OFFNORMAL|TO_FAULT|TO_NORMAL, flags 0 (none)
   not set by default: TO_ALERT

   doRouteAlarm calls getAckRequired() 1 time(s).
   Its includes(sourceState) result is stored in a local that the
   method never loads again - a dead store. The live consumer is
   AlarmSupport.isAckRequired, called when the record is created.

   AlarmSupport.isAckRequired returns false outright when the alarm
   class reference is null, so a record whose alarm class name does not
   resolve is created with ackRequired false rather than defaulting to
   true.

   BAlarmClass.routeAlarm action flags 20 (HIDDEN|ASYNC)
   the four alarm counts share flags 67 (READONLY|TRANSIENT|DEFAULT_ON_CLONE)
   logger name: alarm

three checks one station settles in an afternoon

   1. Open Services/AlarmService and read coalesceAlarms. If it is true,
      ask whether any driver on the station re-routes one record.
   2. Open the AlarmService spy page and watch workInAlarmQueue while the
      busiest panel or device is made to report a burst. A number that
      does not return to zero is the worker falling behind.
   3. Grep your own alarm code for the IFuture that post(routeAlarm)
      returns. If anything branches on success(), it cannot tell a
      delivered alarm from a coalesced-away one.

Nothing above was measured against a running station: it is read out of
the shipped jars. A reference counted here is not a fault - it is where
to look.
```

## The same finding, written up

The queue, the single worker thread, the coalesce rule that decides which duplicate survives, and the `success()` that returns `true` for an alarm nobody delivered are also written up as a page: <https://plantroomlabs.com/tools/alarm-route-scan/>. It carries a captured run of this program, the download with its byte count and SHA-256, the Niagara version the bytecode was read on beside the version of the JACE it was checked against, and the note on alarm routing that explains why the defaults are shaped the way they are.

## Licence

MIT. Written by Usama Iqbal at [Plantroom Labs](https://plantroomlabs.com).
