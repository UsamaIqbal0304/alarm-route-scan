#!/usr/bin/env python3
"""What a Niagara station does to an alarm between the source and the recipient.

Reads javax.baja.alarm and com.tridium.alarm out of alarm-rt.jar, and
javax.baja.util.Queue / CoalesceQueue plus javax.baja.sys.Flags out of
baja.jar, with javap - and reports, from the bytecode rather than the docs:

  - that every alarm route in the station goes through one CoalesceQueue
    drained by one Worker thread, and that the queue's maxSize is
    Integer.MAX_VALUE, so QueueFullException is unreachable in practice
  - the coalesce key: coalesceAlarms defaults to true, which keys on the
    record UUID alone; setting it false is what adds the source state
  - that the coalesced-away invocation is marked finished with no
    throwable, so its IFuture.success() returns true
  - the order inside doRouteAlarm: the alarm database write happens before
    fireAlarm, and a failed write throws instead of notifying anyone
  - the escalation defaults (all three levels off) and the facet string the
    escalated topics are selected by
  - ackRequired's default bits, and the one place the value is actually used

Usage: alarm-route-scan.py [NIAGARA_HOME]
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile

def _find_javap(niagara_home=None):
    """javap from $JAVAP, then PATH, then the JDK Niagara ships, then Debian's."""
    cand = [os.environ.get("JAVAP"), shutil.which("javap")]
    for base in (os.environ.get("JAVA_HOME"), niagara_home):
        if base:
            cand += [os.path.join(base, "bin", "javap"),
                     os.path.join(base, "jre", "bin", "javap")]
    cand.append("/usr/lib/jvm/java-8-openjdk-amd64/bin/javap")
    for c in cand:
        if c and os.path.exists(c):
            return c
    return None


HOME = sys.argv[1] if len(sys.argv) > 1 else os.environ.get(
    "NIAGARA_HOME", "/opt/Niagara/Niagara-4.15.5.22")
JAVAP = _find_javap(str(HOME))


def abort(why):
    sys.exit("ABORT %s" % why)


if not os.path.isdir(HOME):
    abort("no Niagara install at %s" % HOME)
if not os.path.exists(JAVAP):
    abort("no javap found - set $JAVAP or put a JDK 8 javap on PATH")

TMP = tempfile.mkdtemp(prefix="alarmscan-")
WANT = {
    "alarm-rt.jar": ("javax/baja/alarm/", "com/tridium/alarm/"),
    "baja.jar": ("javax/baja/util/", "javax/baja/sys/Flags"),
}
for jarname, prefixes in WANT.items():
    jar = os.path.join(HOME, "modules", jarname)
    if not os.path.exists(jar):
        abort("no %s under %s" % (jarname, HOME))
    with zipfile.ZipFile(jar) as z:
        names = [n for n in z.namelist()
                 if n.endswith(".class") and n.startswith(prefixes)]
        if not names:
            abort("%s has no classes under %s" % (jarname, prefixes))
        z.extractall(TMP, names)


def dis(cls):
    p = subprocess.run([JAVAP, "-p", "-c", "-constants", cls + ".class"],
                       cwd=TMP, capture_output=True, text=True)
    if p.returncode != 0 or "Compiled from" not in p.stdout:
        abort("javap failed on %s: %s" % (cls, p.stderr.strip()[:200]))
    return p.stdout


def method(text, sig, what):
    """Slice one member out of a javap dump, by signature.

    javap puts the exception table inside the member with no blank line
    before it, and exactly one blank line before the next member, so the
    first blank line is the right end marker.
    """
    i = text.find("  " + sig + ";\n")
    if i < 0:
        abort("no member %r in %s" % (sig, what))
    j = text.find("\n\n", i)
    return text[i:j if j > 0 else len(text)]


def one(hay, pat, what):
    m = re.findall(pat, hay)
    if len(m) != 1:
        abort("%d matches for %s (%r)" % (len(m), what, pat))
    return m[0]


def atleast(hay, pat, n, what):
    m = re.findall(pat, hay)
    if len(m) < n:
        abort("%d matches for %s, wanted at least %d" % (len(m), what, n))
    return m


def nomore(hay, pat, n, what):
    m = re.findall(pat, hay)
    if len(m) > n:
        abort("%d matches for %s, wanted at most %d" % (len(m), what, n))
    return m


AC = dis("javax/baja/alarm/BAlarmClass")
AS = dis("javax/baja/alarm/BAlarmService")
SUP = dis("javax/baja/alarm/AlarmSupport")
INV = dis("com/tridium/alarm/AlarmClassRouteAlarmInvocation")
UON = dis("com/tridium/alarm/CoalesceUuidOnlyInvocation")
CQ = dis("javax/baja/util/CoalesceQueue")
Q = dis("javax/baja/util/Queue")
FL = dis("javax/baja/sys/Flags")
TB = dis("javax/baja/alarm/BAlarmTransitionBits")

# ---- the flag and transition vocabularies, read not recalled ------------
FLAGS = {}
for name, val in re.findall(
        r"public static final int ([A-Z_0-9]+) = (-?\d+);", FL):
    FLAGS[name] = int(val)
for need in ("READONLY", "TRANSIENT", "HIDDEN", "SUMMARY", "ASYNC",
             "DEFAULT_ON_CLONE", "NO_AUDIT"):
    if need not in FLAGS:
        abort("javax.baja.sys.Flags has no %s" % need)

TRANS = {}
for name, val in re.findall(
        r"public static final int (TO_[A-Z]+) = (\d+);", TB):
    TRANS[name] = int(val)
if sorted(TRANS.values()) != [1, 2, 4, 8]:
    abort("BAlarmTransitionBits is not four single bits: %r" % TRANS)


def decode(bits, table):
    """Name a flag word, and refuse to report one with a bit we cannot name."""
    out, left = [], int(bits)
    for name, val in sorted(table.items(), key=lambda kv: kv[1]):
        if val > 0 and left & val:
            out.append(name)
            left &= ~val
    if left:
        abort("flag word %d has unnamed bit %d left over" % (bits, left))
    return "|".join(out) if out else "none"


# ---- BAlarmClass: the defaults an engineer never sees changed -----------
ACINIT = method(AC, "static {}", "BAlarmClass")

def boolprop(init, name, what):
    """(flags, default) of a boolean property, from the newProperty call."""
    blk = one(init,
              r"(iconst_\d+|bipush\s+-?\d+|sipush\s+-?\d+)\n"
              r"\s+\d+: (iconst_[01])\n"
              r"\s+\d+: aconst_null\n"
              r"\s+\d+: invokestatic\s+#\d+\s+// Method newProperty:\(IZ"
              r"(?:(?!putstatic)[\s\S]){0,900}?"
              r"putstatic\s+#\d+\s+// Field %s:" % name,
              what)
    return insn_int(blk[0]), 1 if blk[1] == "iconst_1" else 0


def insn_int(insn):
    insn = insn.strip()
    if insn.startswith("iconst_"):
        return int(insn[len("iconst_"):])
    m = re.match(r"(?:bipush|sipush|ldc\w*)\s+(-?\d+)$", insn)
    if not m:
        abort("cannot read an int out of the instruction %r" % insn)
    return int(m.group(1))


ACK = int(one(ACINIT,
              r"(?:iconst_(?:\d)|bipush\s+(?:\d+))\n"
              r"\s+\d+: bipush\s+(\d+)\n"
              r"\s+\d+: invokestatic\s+#\d+\s+// Method "
              r"javax/baja/alarm/BAlarmTransitionBits\.make:",
              "the ackRequired default bits"))
ACKFLAGS = insn_int(one(ACINIT,
                        r"(iconst_\d+|bipush\s+\d+|sipush\s+\d+)\n"
                        r"\s+\d+: bipush\s+\d+\n"
                        r"\s+\d+: invokestatic\s+#\d+\s+// Method "
                        r"javax/baja/alarm/BAlarmTransitionBits\.make:"
                        r"(?:(?!putstatic)[\s\S]){0,900}?"
                        r"putstatic\s+#\d+\s+// Field ackRequired:",
                        "the ackRequired flags"))


ESC = []
for n in (1, 2, 3):
    efl, edef = boolprop(ACINIT, "escalationLevel%dEnabled" % n,
                         "escalationLevel%dEnabled" % n)
    delay, minf = one(ACINIT,
                      r"ldc2_w\s+#\d+\s+// long (\d+)l\n"
                      r"(?:(?!putstatic)[\s\S]){0,900}?"
                      r"// String min\n"
                      r"\s+\d+: ldc2_w\s+#\d+\s+// long (\d+)l\n"
                      r"(?:(?!putstatic)[\s\S]){0,900}?"
                      r"putstatic\s+#\d+\s+// Field escalationLevel%dDelay:"
                      % n,
                      "escalationLevel%dDelay" % n)
    ESC.append((n, efl, edef, int(delay), int(minf)))

if [e[2] for e in ESC] != [0, 0, 0]:
    abort("an escalation level is enabled by default: %r"
          % [(e[0], e[2]) for e in ESC])
if [e[3] for e in ESC] != sorted(e[3] for e in ESC):
    abort("the escalation delays do not ascend: %r" % [e[3] for e in ESC])

COUNTS = []
for p in ("totalAlarmCount", "openAlarmCount", "inAlarmCount",
          "unackedAlarmCount"):
    f = one(ACINIT,
            r"(iconst_\d+|bipush\s+\d+|sipush\s+\d+)\n"
            r"\s+\d+: iconst_0\n"
            r"\s+\d+: aconst_null\n"
            r"\s+\d+: invokestatic\s+#\d+\s+// Method newProperty:\(II"
            r"(?:(?!putstatic)[\s\S]){0,900}?"
            r"putstatic\s+#\d+\s+// Field %s:" % p,
            "the %s flags" % p)
    COUNTS.append((p, insn_int(f)))
if len({f for _, f in COUNTS}) != 1:
    abort("the four alarm counts do not share one flag word: %r" % COUNTS)

ROUTEFLAGS = insn_int(one(ACINIT,
                          r"(iconst_\d+|bipush\s+\d+|sipush\s+\d+)\n"
                          r"\s+\d+: new\s+#\d+\s+// class "
                          r"javax/baja/alarm/BAlarmRecord\n"
                          r"(?:(?!putstatic)[\s\S]){0,900}?"
                          r"putstatic\s+#\d+\s+// Field routeAlarm:",
                          "the BAlarmClass routeAlarm action flags"))
if not (ROUTEFLAGS & FLAGS["ASYNC"]):
    abort("BAlarmClass.routeAlarm is no longer ASYNC, so it no longer "
          "goes through the queue: flags %d" % ROUTEFLAGS)

TOPICS = []
for t in ("alarm", "escalatedAlarm1", "escalatedAlarm2", "escalatedAlarm3"):
    f = one(ACINIT,
            r"(iconst_\d+|bipush\s+\d+|sipush\s+\d+)\n"
            r"\s+\d+: aconst_null\n"
            r"\s+\d+: invokestatic\s+#\d+\s+// Method newTopic:"
            r"(?:(?!putstatic)[\s\S]){0,900}?"
            r"putstatic\s+#\d+\s+// Field %s:" % t,
            "the %s topic flags" % t)
    TOPICS.append((t, insn_int(f)))

LOGGER = one(ACINIT,
             r"ldc\s+#\d+\s+// String (\S+)\n"
             r"\s+\d+: invokestatic\s+#\d+\s+// Method "
             r"java/util/logging/Logger\.getLogger:",
             "the BAlarmClass logger name")

# ---- changed(): what enabling an escalation level actually does ---------
CHANGED = method(AC, "public void changed(javax.baja.sys.Property, "
                     "javax.baja.sys.Context)", "BAlarmClass")
SUMBIT = FLAGS["SUMMARY"]
CLEARMASK = -1 - SUMBIT
if not re.search(r"bipush\s+%d\n\s+\d+: ior" % SUMBIT, CHANGED):
    abort("changed() no longer ors flag %d into an escalated topic" % SUMBIT)
if not re.search(r"bipush\s+%d\n\s+\d+: iand" % CLEARMASK, CHANGED):
    abort("changed() no longer ands flag mask %d out" % CLEARMASK)

# which topic does each enable branch read, and which does it write?
BRANCH = []
for n in (1, 2, 3):
    blk = one(CHANGED,
              r"invokevirtual\s+#\d+\s+// Method getEscalationLevel%dEnabled"
              r":\(\)Z\n\s+\d+: ifeq\s+\d+\n"
              r"\s+\d+: aload_0\n"
              r"\s+\d+: getstatic\s+#\d+\s+// Field (escalatedAlarm\d):"
              r"[\s\S]{0,400}?"
              r"\s+\d+: getstatic\s+#\d+\s+// Field (escalatedAlarm\d):"
              r"[^\n]*\n\s+\d+: iload_3\n"
              r"\s+\d+: invokevirtual\s+#\d+\s+// Method setFlags:" % n,
              "the escalation level %d enable branch" % n)
    BRANCH.append((n, blk[0], blk[1]))
MISMATCH = [b for b in BRANCH if b[1] != b[2]]

# ---- BAlarmService: the one queue, the one thread -----------------------
ASINIT = method(AS, "static {}", "BAlarmService")
COAL_FLAGS, COAL_DEF = boolprop(ASINIT, "coalesceAlarms", "coalesceAlarms")

TRIGMIN = int(one(ASINIT,
                  r"iconst_(\d)\n"
                  r"\s+\d+: invokestatic\s+#\d+\s+// Method "
                  r"javax/baja/sys/BRelTime\.makeMinutes:\(I\)",
                  "the escalation trigger interval"))
TRIGFLAGS = insn_int(one(ASINIT,
                         r"(iconst_\d+|bipush\s+\d+|sipush\s+\d+)\n"
                         r"\s+\d+: new\s+#\d+\s+// class "
                         r"javax/baja/control/trigger/BTimeTrigger\n"
                         r"(?:(?!putstatic)[\s\S]){0,900}?"
                         r"putstatic\s+#\d+\s+// Field escalationTimeTrigger:",
                         "the escalationTimeTrigger flags"))
ESCACTFLAGS = insn_int(one(ASINIT,
                           r"(iconst_\d+|bipush\s+\d+|sipush\s+\d+)\n"
                           r"\s+\d+: aconst_null\n"
                           r"\s+\d+: invokestatic\s+#\d+\s+// Method "
                           r"newAction:\(ILjavax/baja/sys/BFacets;\)"
                           r"(?:(?!putstatic)[\s\S]){0,900}?"
                           r"putstatic\s+#\d+\s+// Field escalateAlarms:",
                           "the escalateAlarms action flags"))

ASCTOR = method(AS, "public javax.baja.alarm.BAlarmService()", "BAlarmService")
QCLASS = one(ASCTOR,
             r"new\s+#\d+\s+// class javax/baja/util/(\w+)\n"
             r"(?:(?!putfield)[\s\S]){0,600}?"
             r"putfield\s+#\d+\s+// Field alarmQueue:",
             "the class of the alarm queue")
if QCLASS != "CoalesceQueue":
    abort("the alarm queue is no longer a CoalesceQueue but a %s" % QCLASS)
if not re.search(r"new\s+#\d+\s+// class javax/baja/util/CoalesceQueue\n"
                 r"\s+\d+: dup\n"
                 r"\s+\d+: invokespecial\s+#\d+\s+// Method "
                 r"javax/baja/util/CoalesceQueue\.\"<init>\":\(\)V", ASCTOR):
    abort("the alarm queue is no longer built with the no-argument "
          "CoalesceQueue constructor, so its maxSize is not the default")

STARTED = method(AS, "public void serviceStarted()", "BAlarmService")
WNAME = one(STARTED,
            r"ldc\s+#\d+\s+// String (\S+)\n"
            r"\s+\d+: invokevirtual\s+#\d+\s+// Method "
            r"javax/baja/util/Worker\.start:",
            "the alarm worker thread name")
NWORKERS = len(atleast(STARTED,
                       r"new\s+#\d+\s+// class javax/baja/util/Worker\b", 1,
                       "the alarm worker"))
nomore(STARTED, r"new\s+#\d+\s+// class javax/baja/util/Worker\b", 1,
       "the alarm worker")
if not re.search(r"getfield\s+#\d+\s+// Field alarmQueue:[\s\S]{0,80}?"
                 r"invokespecial\s+#\d+\s+// Method javax/baja/util/Worker\."
                 r"\"<init>\":\(Ljavax/baja/util/Worker\$ITodo;\)V", STARTED):
    abort("the single Worker is no longer fed from alarmQueue")

FW = method(AS, "public final java.lang.Object fw(int, java.lang.Object, "
                "java.lang.Object, java.lang.Object, java.lang.Object)",
            "BAlarmService")
# the switch case that returns the queue, found by its target offset rather
# than by a constant written here
FWTARGET = one(FW,
               r"\s+(\d+): aload_0\n"
               r"\s+\d+: getfield\s+#\d+\s+// Field alarmQueue:[^\n]*\n"
               r"\s+\d+: areturn",
               "the fw branch that returns the alarm queue")
FWCODE = int(one(FW,
                 r"\s+(\d+): %s\n" % FWTARGET,
                 "the switch case jumping to offset %s" % FWTARGET))
if "lookupswitch" not in FW and "tableswitch" not in FW:
    abort("BAlarmService.fw no longer dispatches on a switch")

SPY = method(AS, "public void spy(javax.baja.spy.SpyWriter) throws "
                 "java.lang.Exception", "BAlarmService")
SPYPROP = one(SPY,
              r"ldc\w*\s+#\d+\s+// String (work\w+)\n"
              r"(?:(?!SpyWriter\.prop)[\s\S]){0,300}?"
              r"invokevirtual\s+#\d+\s+// Method javax/baja/util/Queue\.size:",
              "the spy page's queue-depth property")

# ---- the two posts, and the boolean both of them throw away ------------
ACPOST = method(AC, "public javax.baja.util.IFuture post(javax.baja.sys."
                    "Action, javax.baja.sys.BValue, javax.baja.sys.Context)",
                "BAlarmClass")
if not re.search(r"sipush\s+%d\n\s+\d+: invokevirtual\s+#\d+\s+// Method "
                 r"javax/baja/sys/BComponent\.fw:\(I\)" % FWCODE, ACPOST):
    abort("BAlarmClass.post no longer reaches the service queue through "
          "fw(%d)" % FWCODE)
INVNAME = one(ACPOST,
              r"invokestatic\s+#\d+\s+// Method com/tridium/alarm/(\w+)\.make:",
              "the invocation BAlarmClass.post builds")
one(ACPOST,
    r"invokevirtual\s+#\d+\s+// Method javax/baja/util/Queue\.enqueue:"
    r"\(Ljava/lang/Object;\)Z\n\s+\d+: (pop)",
    "the enqueue result BAlarmClass.post discards")

ASPOST = method(AS, "public javax.baja.util.IFuture post(javax.baja.sys."
                    "Action, javax.baja.sys.BValue, javax.baja.sys.Context)",
                "BAlarmService")
one(ASPOST,
    r"invokevirtual\s+#\d+\s+// Method javax/baja/util/Queue\.enqueue:"
    r"\(Ljava/lang/Object;\)Z\n\s+\d+: (pop)",
    "the enqueue result BAlarmService.post discards")
ASPOSTACT = one(ASPOST,
                r"getstatic\s+#\d+\s+// Field (\w+):Ljavax/baja/sys/Action;",
                "the one action BAlarmService.post queues itself")

# ---- the queue's own arithmetic -----------------------------------------
CQCTOR = method(CQ, "public javax.baja.util.CoalesceQueue()", "CoalesceQueue")
MAXSIZE = int(one(CQCTOR,
                  r"ldc\w*\s+#\d+\s+// int (\d+)\n"
                  r"\s+\d+: invokespecial\s+#\d+\s+// Method "
                  r"\"<init>\":\(I\)V",
                  "the default CoalesceQueue maxSize"))
CQINT = method(CQ, "public javax.baja.util.CoalesceQueue(int)", "CoalesceQueue")
LOAD = float(one(CQINT, r"ldc\s+#\d+\s+// float ([\d.]+)f", "the load factor"))
FLOOR, DIV, CAP = one(CQINT,
                      r"bipush\s+(\d+)\n"
                      r"\s+\d+: iload_1\n"
                      r"\s+\d+: iconst_(\d)\n"
                      r"\s+\d+: idiv\n"
                      r"\s+\d+: bipush\s+(\d+)\n"
                      r"\s+\d+: invokestatic\s+#\d+\s+// Method "
                      r"java/lang/Math\.min:\(II\)I\n"
                      r"\s+\d+: invokestatic\s+#\d+\s+// Method "
                      r"java/lang/Math\.max:\(II\)I",
                      "the hash table sizing")


def buckets(maxsize):
    """Exactly what the int constructor computes, in its own order."""
    return max(int(FLOOR), min(maxsize // int(DIV), int(CAP)))


QENQ = method(Q, "public synchronized boolean enqueue(java.lang.Object) "
                 "throws javax.baja.util.QueueFullException", "Queue")
if not re.search(r"getfield\s+#\d+\s+// Field size:I\n"
                 r"\s+\d+: aload_0\n"
                 r"\s+\d+: getfield\s+#\d+\s+// Field maxSize:I\n"
                 r"\s+\d+: if_icmplt", QENQ):
    abort("Queue.enqueue no longer compares size against maxSize")
if "QueueFullException" not in QENQ:
    abort("Queue.enqueue no longer throws QueueFullException when full")

CQENQ = method(CQ, "public synchronized boolean enqueue(java.lang.Object) "
                   "throws javax.baja.util.QueueFullException",
               "CoalesceQueue")
if not re.search(r"invokeinterface\s+#\d+,\s+\d+\s+// InterfaceMethod "
                 r"javax/baja/util/ICoalesceable\.coalesce:[\s\S]{0,120}?"
                 r"putfield\s+#\d+\s+// Field javax/baja/util/CoalesceQueue"
                 r"\$HashEntry\.value:Ljava/lang/Object;\n"
                 r"\s+\d+: iconst_0\n"
                 r"\s+\d+: ireturn", CQENQ):
    abort("CoalesceQueue.enqueue no longer stores the coalesce result into "
          "the existing entry and returns false")

# ---- the coalesce key, and which way the property points ----------------
MAKE = method(INV, "public static com.tridium.alarm."
                   "AlarmClassRouteAlarmInvocation make(javax.baja.sys."
                   "BComponent, javax.baja.sys.Action, javax.baja.alarm."
                   "BAlarmRecord, javax.baja.sys.Context)",
              "AlarmClassRouteAlarmInvocation")
WHENTRUE, WHENFALSE = one(MAKE,
                          r"invokevirtual\s+#\d+\s+// Method javax/baja/alarm/"
                          r"BAlarmService\.getCoalesceAlarms:\(\)Z\n"
                          r"\s+\d+: ifeq\s+\d+\n"
                          r"\s+\d+: new\s+#\d+\s+// class "
                          r"com/tridium/alarm/(\w+)\n"
                          r"[\s\S]{0,400}?"
                          r"\s+\d+: new\s+#\d+\s+// class "
                          r"com/tridium/alarm/(\w+)\n",
                          "the two invocation classes make() chooses between")
if WHENTRUE == WHENFALSE:
    abort("make() no longer chooses between two classes on coalesceAlarms")

INVEQ = method(INV, "public boolean equals(java.lang.Object)",
               "AlarmClassRouteAlarmInvocation")
UONEQ = method(UON, "public boolean equals(java.lang.Object)",
               "CoalesceUuidOnlyInvocation")
for name, body in (("the UUID-only equals", INVEQ),
                   ("the UUID-and-state equals", UONEQ)):
    for field in ("instance", "action"):
        if not re.search(r"getfield\s+#\d+\s+// Field %s:" % field, body):
            abort("%s no longer compares %s" % (name, field))
    if "BAlarmRecord.getUuid" not in body:
        abort("%s no longer compares the record UUID" % name)
INVSTATE = "BAlarmRecord.getSourceState" in INVEQ
UONSTATE = "BAlarmRecord.getSourceState" in UONEQ
if INVSTATE or not UONSTATE:
    abort("the two equals methods no longer differ by the source state: "
          "%s has it %r, %s has it %r"
          % (WHENTRUE, INVSTATE, WHENFALSE, UONSTATE))
if "BAlarmRecord.getSourceState" not in method(
        UON, "public com.tridium.alarm.CoalesceUuidOnlyInvocation("
             "javax.baja.sys.BComponent, javax.baja.sys.Action, javax.baja."
             "alarm.BAlarmRecord, javax.baja.sys.Context)",
        "CoalesceUuidOnlyInvocation"):
    abort("CoalesceUuidOnlyInvocation no longer mixes the source state into "
          "its hashCode, so equals and hashCode disagree")

# ---- what the dropped invocation reports --------------------------------
COALESCE = method(INV, "public javax.baja.util.ICoalesceable coalesce("
                       "javax.baja.util.ICoalesceable)",
                  "AlarmClassRouteAlarmInvocation")
if not re.search(r"iconst_1\n\s+\d+: putfield\s+#\d+\s+// Field finished:Z\n"
                 r"\s+\d+: aload_1\n"
                 r"\s+\d+: areturn", COALESCE):
    abort("coalesce() no longer marks itself finished and returns the "
          "incoming invocation: %r" % COALESCE)
SUCCESS = method(INV, "public boolean success() throws java.lang.Exception",
                 "AlarmClassRouteAlarmInvocation")
if not re.search(r"getfield\s+#\d+\s+// Field finished:Z\n"
                 r"\s+\d+: ifne[\s\S]{0,500}?"
                 r"getfield\s+#\d+\s+// Field throwable:Ljava/lang/Throwable;\n"
                 r"\s+\d+: ifnonnull\s+\d+\n"
                 r"\s+\d+: iconst_1", SUCCESS):
    abort("success() is no longer finished-and-no-throwable, so the "
          "coalesced-away invocation's verdict may have changed")

# ---- doRouteAlarm: the order, and what a failed write costs -------------
DRA = method(AC, "public void doRouteAlarm(javax.baja.alarm.BAlarmRecord)",
             "BAlarmClass")
POS = {}
for label, pat in (
        ("db append", r"(\d+): invokevirtual\s+#\d+\s+// Method javax/baja/"
                      r"alarm/AlarmDbConnection\.append:"),
        ("db update", r"(\d+): invokevirtual\s+#\d+\s+// Method javax/baja/"
                      r"alarm/AlarmDbConnection\.update:"),
        ("fireAlarm", r"(\d+): invokevirtual\s+#\d+\s+// Method fireAlarm:"),
):
    POS[label] = int(one(DRA, pat, "the %s call in doRouteAlarm" % label))
if not (POS["db append"] < POS["fireAlarm"]
        and POS["db update"] < POS["fireAlarm"]):
    abort("doRouteAlarm no longer writes the database before fireAlarm: %r"
          % POS)
WRITEFAIL = one(DRA,
                r"getstatic\s+#\d+\s+// Field java/util/logging/Level\.SEVERE"
                r":[\s\S]{0,120}?ldc\s+#\d+\s+// String (Cannot write [^\n]*)",
                "the failed-write log message")
if "AlarmException" not in DRA:
    abort("doRouteAlarm no longer rethrows a failed write as an "
          "AlarmException, so it may now notify anyway")
# the pattern proves the default is the empty string rather than taking it
# on trust: javap prints an empty literal as "// String" with nothing after
FACETDEF = one(DRA,
               r"ldc\s+#\d+\s+// String (escalated)\n"
               r"\s+\d+: ldc\s+#\d+\s+// String\n"
               r"\s+\d+: invokestatic\s+#\d+\s+// Method "
               r"javax/baja/sys/BString\.make:[^\n]*\n"
               r"\s+\d+: invokevirtual\s+#\d+\s+// Method javax/baja/alarm/"
               r"BAlarmRecord\.addAlarmFacet:",
               "the escalated facet doRouteAlarm defaults to an empty string")
LEVELS = {}
for n in (1, 2, 3):
    blk = one(DRA,
              r"invokevirtual\s+#\d+\s+// Method getEscalationLevel%dEnabled:"
              r"\(\)Z\n"
              r"(?:(?!fireEscalatedAlarm)[\s\S]){0,1400}?"
              r"invokevirtual\s+#\d+\s+// Method fireEscalatedAlarm%d:" % (n, n),
              "the level %d escalation test" % n)
    LEVELS[n] = sorted(set(re.findall(r"// String (level\d)\n", blk)))
if LEVELS[1] != ["level1", "level2", "level3"] or LEVELS[3] != ["level3"]:
    abort("the escalated topics are no longer cumulative: %r" % LEVELS)
SNF = atleast(DRA,
              r"\s+(\d+)\s+(\d+)\s+(\d+)\s+Class "
              r"javax/baja/sys/ServiceNotFoundException", 1,
              "the ServiceNotFoundException handlers")
SNFSPAN = max(int(b) for _, b, _ in SNF)
LASTOFF = int(re.findall(r"\n\s+(\d+): \w", DRA)[-1])

ACKUSE = atleast(DRA,
                 r"invokevirtual\s+#\d+\s+// Method getAckRequired:", 1,
                 "the getAckRequired call in doRouteAlarm")
DEADSTORE = (re.search(r"invokevirtual\s+#\d+\s+// Method "
                       r"javax/baja/alarm/BAlarmTransitionBits\.includes:"
                       r"\(Ljavax/baja/alarm/BSourceState;\)Z\n"
                       r"\s+\d+: istore_(\d)", DRA) is not None
             and not re.search(r"\n\s+\d+: iload_3\b", DRA))

ISACK = method(SUP, "public boolean isAckRequired(javax.baja.alarm."
                    "BSourceState)", "AlarmSupport")
if not re.search(r"getfield\s+#\d+\s+// Field alarmClass:[^\n]*"
                 r"\n\s+\d+: ifnull\s+\d+", ISACK):
    abort("AlarmSupport.isAckRequired no longer short-circuits on a null "
          "alarm class")
NOSVC = one(SUP,
            r"ldc\s+#\d+\s+// String (Unable to route [^\n]*)",
            "the no-alarm-service message in AlarmSupport")

# ------------------------------------------------------------------------
W = []


def say(s=""):
    W.append(s)


say("alarm-route-scan - what a station does to an alarm on its way out")
say("Niagara home: %s" % HOME)
say("read from: modules/alarm-rt.jar, modules/baja.jar (javap, no station "
    "running)")
say()
say("1. one queue, one thread, and no ceiling")
say()
say("   BAlarmService holds the alarm queue in a field and hands it out "
    "through")
say("   fw(%d); BAlarmClass.post(routeAlarm, record) asks for it that way "
    "and" % FWCODE)
say("   enqueues a %s. So every alarm route in the" % INVNAME)
say("   station, from every driver and every control point, goes through one")
say("   %s drained by one Worker thread named %s." % (QCLASS, WNAME))
say("   Workers constructed in serviceStarted(): %d." % NWORKERS)
say()
say("   The queue is built with the no-argument constructor, which passes")
say("   maxSize = %d. Queue.enqueue throws QueueFullException only" % MAXSIZE)
say("   when size >= maxSize, so on this build that exception is "
    "unreachable")
say("   in practice: a burst the worker cannot keep up with is not refused,")
say("   it is accumulated. Hash table: max(%s, min(maxSize/%s, %s)) = %d "
    "buckets" % (FLOOR, DIV, CAP, buckets(MAXSIZE)))
say("   at load factor %s." % LOAD)
say()
say("   The depth is published in exactly one place: the service's spy page,")
say("   as %s = Queue.size(). It is not a property, so it cannot" % SPYPROP)
say("   be linked, trended or alarmed on.")
say()
say("2. the coalesce key, and the property name points the other way")
say()
say("   coalesceAlarms: default %s, flags %d (%s)"
    % ("true" if COAL_DEF else "false", COAL_FLAGS, decode(COAL_FLAGS, FLAGS)))
say()
say("   make() reads it and picks the class:")
say("     coalesceAlarms true  -> %s" % WHENTRUE)
say("     coalesceAlarms false -> %s" % WHENFALSE)
say()
say("   Both equals methods compare the alarm class instance, the action and")
say("   the record UUID. Only %s also compares the" % WHENFALSE)
say("   record's source state, in equals and in hashCode alike.")
say()
say("   So with the default - coalesceAlarms true - two routes of one record")
say("   that are in the queue at the same time collapse into one whatever")
say("   their states are. Turning the property off is what keeps them apart.")
say()
say("3. the invocation that loses the collision says it succeeded")
say()
say("   CoalesceQueue.enqueue finds the equal entry, calls")
say("   existing.coalesce(incoming), stores the result back into that "
    "entry's")
say("   slot - so the survivor inherits the earlier arrival's queue position "
    "-")
say("   and returns false.")
say()
say("   coalesce() sets finished = true on the invocation being dropped and")
say("   returns the incoming one. throwable is left null. success() is")
say("   finished and no throwable, so on the dropped invocation it returns")
say("   true: the IFuture says the alarm was routed, and doRouteAlarm never")
say("   ran for it.")
say()
say("   Neither caller can see it either way. BAlarmClass.post and")
say("   BAlarmService.post both pop the boolean enqueue returns.")
say("   (BAlarmService.post only queues %s itself; everything" % ASPOSTACT)
say("   else goes to the superclass.)")
say()
say("4. the database write comes before the people")
say()
say("   doRouteAlarm, by offset: AlarmDbConnection.append at %d or .update at"
    % POS["db append"])
say("   %d, then fireAlarm at %d. An Exception out of that write is"
    % (POS["db update"], POS["fireAlarm"]))
say("   logged SEVERE %r and rethrown as an AlarmException, so" % WRITEFAIL)
say("   fireAlarm is never reached and no recipient is notified.")
say("   Persistence first, people second.")
say()
say("   A ServiceNotFoundException handler covers the method to offset %d of"
    % SNFSPAN)
say("   %d - effectively all of it - and its handler logs and returns. "
    "Upstream," % LASTOFF)
say("   AlarmSupport logs SEVERE %r and returns null" % NOSVC)
say("   when there is no alarm service at all.")
say()
say("5. escalation is off, and it is cumulative")
say()
say("   escalationLevel<n>Enabled / escalationLevel<n>Delay, by level:")
say()
for n, efl, edef, delay, minf in ESC:
    say("   level %d  enabled %-5s  flags %d  delay %7d ms  min facet %6d ms"
        % (n, "true" if edef else "false", efl, delay, minf))
say()
say("   escalationTimeTrigger: interval %d minute(s), flags %d (%s)"
    % (TRIGMIN, TRIGFLAGS, decode(TRIGFLAGS, FLAGS)))
say("   escalateAlarms action: flags %d (%s)"
    % (ESCACTFLAGS, decode(ESCACTFLAGS, FLAGS)))
say("   - ASYNC, so the once-a-minute escalation scan is queued onto the "
    "same")
say("     single worker thread that delivers alarms.")
say()
say("   Which topic fires is decided by a string facet on the record. "
    "doRouteAlarm")
say("   adds %r with the empty string when it is absent, so a first" % FACETDEF)
say("   route fires no escalated topic. The tests are cumulative:")
for n in (1, 2, 3):
    say("     fireEscalatedAlarm%d on %s" % (n, ", ".join(LEVELS[n])))
say()
say("   Topic flags as declared:")
for t, f in TOPICS:
    say("     %-16s %d (%s)" % (t, f, decode(f, FLAGS)))
say("   changed() ors %s into the matching escalated topic when a level is"
    % decode(SUMBIT, FLAGS))
say("   enabled and masks it out again when it is disabled.")
if MISMATCH:
    say()
    for n, got, put in MISMATCH:
        say("   Worth a look: the level %d enable branch reads the flags of" % n)
        say("   %s and writes them to %s." % (got, put))
        say("   Levels whose branches agree: %s."
            % ", ".join(str(b[0]) for b in BRANCH if b[1] == b[2]))
else:
    say("   All three enable branches read and write their own topic.")
say()
say("6. ackRequired, and where it is really used")
say()
say("   ackRequired: default %d = %s, flags %d (%s)"
    % (ACK, decode(ACK, TRANS), ACKFLAGS, decode(ACKFLAGS, FLAGS)))
missing = [n for n, v in sorted(TRANS.items(), key=lambda kv: kv[1])
           if not ACK & v]
say("   not set by default: %s" % (", ".join(missing) if missing else "none"))
say()
say("   doRouteAlarm calls getAckRequired() %d time(s)." % len(ACKUSE))
if DEADSTORE:
    say("   Its includes(sourceState) result is stored in a local that the")
    say("   method never loads again - a dead store. The live consumer is")
    say("   AlarmSupport.isAckRequired, called when the record is created.")
else:
    say("   The result is used in the method.")
say()
say("   AlarmSupport.isAckRequired returns false outright when the alarm")
say("   class reference is null, so a record whose alarm class name does not")
say("   resolve is created with ackRequired false rather than defaulting to")
say("   true.")
say()
say("   BAlarmClass.routeAlarm action flags %d (%s)"
    % (ROUTEFLAGS, decode(ROUTEFLAGS, FLAGS)))
say("   the four alarm counts share flags %d (%s)"
    % (COUNTS[0][1], decode(COUNTS[0][1], FLAGS)))
say("   logger name: %s" % LOGGER)
say()
say("three checks one station settles in an afternoon")
say()
say("   1. Open Services/AlarmService and read coalesceAlarms. If it is "
    "true,")
say("      ask whether any driver on the station re-routes one record.")
say("   2. Open the AlarmService spy page and watch %s while the" % SPYPROP)
say("      busiest panel or device is made to report a burst. A number that")
say("      does not return to zero is the worker falling behind.")
say("   3. Grep your own alarm code for the IFuture that post(routeAlarm)")
say("      returns. If anything branches on success(), it cannot tell a")
say("      delivered alarm from a coalesced-away one.")
say()
say("Nothing above was measured against a running station: it is read out of")
say("the shipped jars. A reference counted here is not a fault - it is where")
say("to look.")

OUT = "\n".join(W) + "\n"
for line in OUT.splitlines():
    if len(line) > 78:
        abort("output line is %d chars: %r" % (len(line), line))
sys.stdout.write(OUT)
