"""
Input domains for the property generator, derived from *usage* in the service.

No production data exists for this service (see ../README.md), so the input
space is reconstructed from how each field is actually consumed. Every domain
below cites the line in `src/ingest.php` that motivates its values, so a reader
can check that the generator explores the branches that exist rather than a
space someone imagined.

This module is data and pure functions only. It performs no I/O.
"""

# Fleet identifiers present in the real seed (sql/schema.sql). Kept in sync by
# lib/stack.py, which reads the fleet from the database rather than trusting
# this list; these are the values the generator *draws* from.
SEED_SINGLE_CONNECTOR = ["CP-0001", "CP-0003", "CP-0010", "CP-0011", "CP-0012"]
SEED_MULTI_CONNECTOR = ["CP-0002"]          # model_code 7
SEED_LEGACY = ["CP-0004"]                   # model_code 253, takes the simple path
CONNECTOR_IDENTS = ["CP-0002-1", "CP-0002-2", "CP-0002-3"]
UNKNOWN_IDENTS = ["CP-9999", "CP-0000", "NOT-A-CP", "CP-0002-9"]

# `inbox.src`: only the literal 'gw' is special (ingest.php:85, :316).
SRC_VALUES = ["cp", "gw", "ocpp16", "GW", ""]

# msg_type is compared against these exact values (ingest.php:151, :159, :162).
# Everything else falls through to the ordinary telemetry path.
MSG_TYPE_DISPATCHED = [3, 9, 11, 14, 17]
MSG_TYPE_ORDINARY = [1, 2, 5, 8, 20, 127]

# wh is written to meter_events.wh (INT NULL) and charge_points.wh (INT).
# ingest.php:240 defaults it to 0 when absent.
WH_VALUES = [0, 1, 7, 1234, 2147483647, "500"]

# `la` is the charger's local wall clock. ingest.php:109 guards on !empty,
# :110 constructs a DateTime from it -- an unparseable value therefore THROWS
# and strands the frame (known issue 5). ':133' also makes it the dt source.
# Values in 2021 are safely in the past; 2099 trips the >now+2d rejection.
LA_PARSEABLE_PAST = [
    "2021-03-04 12:00:00",
    "2021-07-19 23:59:59",
    "2021-01-01 00:00:00",
    "2021-11-07 01:30:00",   # inside the US DST fall-back overlap
]
LA_FUTURE_REJECTED = ["2099-01-01 00:00:00", "2088-06-15 08:00:00"]
# Curated because PHP's DateTime parser is permissive in surprising ways. Each
# of these is verified to throw by tests/property/README.md's calibration step.
LA_UNPARSEABLE = ["banana", "not-a-date", "13/45/9999"]

# rd + rh build dt as "{rd} {rh}:00:00" (ingest.php:132). rh defaults to 0.
RD_VALUES = ["2021-03-03", "2021-06-30", "2021-12-31"]
RH_VALUES = [0, 7, 14, 23, None]

# `dt` supplied directly in the body SURVIVES when neither rd nor la is set,
# because ingest.php:130/:133 only overwrite it in those cases, and :138 then
# consumes whatever the caller sent. This input path is undocumented.
DT_DIRECT = ["2021-05-05 05:05:05", "2021-09-09 09:09:09"]

# Fault notes. ingest.php:166 uses strpos(...) !== false for OVERHEAT, but :168
# omits the !== false for the other two markers, so a marker at offset 0 is
# falsy and missed (known issue 10). The generator draws from both positions so
# that predicted HTTP call counts exercise the quirk.
NT_MARKER_AT_ZERO = ["GROUND FAULT on phase A", "CONNECTOR LOCK FAULT latched"]
NT_MARKER_OFFSET = [
    "ALERT: GROUND FAULT on phase A",
    "fault: CONNECTOR LOCK FAULT latched",
    "site 12 GROUND FAULT",
]
NT_OVERHEAT = ["OVERHEAT detected", "ALERT: OVERHEAT and GROUND FAULT"]
NT_BENIGN = ["OK", "door open", ""]

# fl == '1' gates diagnostic synthesis (ingest.php:180); sv/hv are compared
# against 6 and 3 (:183); dbg is scanned for these three markers (:186-188).
FL_VALUES = ["1", "0", 1, None]
SV_VALUES = [0, 5, 6, 9, None]
HV_VALUES = [0, 2, 3, 7, None]
DBG_VALUES = [
    "x;A1;y", "x;B1;y", "x;C1;y", "a;A1;b;B1;c;C1;d", "nothing here", "",
]

# `as` is split on ':' and indexed by connector_no - 1 (ingest.php:200-204).
AS_VALUES = ["100:200:300", "11:22", "5", "1:2:3:4:5", "", "a:b:c"]

# m.* metadata (ingest.php:101-106).
# m.zd only has to be *truthy* to suppress fan-out; a JSON null does not.
ZD_VALUES = [None, "Z1", "", 0, "descriptor"]
# m.la / m.lo are interpolated straight into the lookup URL path (:14) with a
# `?? 0` default at the call site (:173).
LAT_VALUES = [41.88, -87.63, 0, "41.88", 90, -0.5]
LON_VALUES = [-87.63, 0, "-87.63", 180, 12.5]
FIRMWARE_VALUES = ["v7.2-beta", "v2-beta", "3", "v10-x-y", "", "8.1"]
SW_VALUES = [0, 1, None]

# connector_count is copied onto charge_points when it differs (:317).
CONNECTOR_COUNT_VALUES = [1, 2, 3, 0, None]

# `nl` is unset on link-state frames (:158) and otherwise ignored.
NL_VALUES = ["x", None]

# Real IANA zones only. charge_points.tz feeds DateTimeZone (:139); an
# unresolvable zone throws and strands the frame, which characterization case 20
# already pins, so the generator keeps zones valid to explore other behavior.
TZ_VALUES = [
    "America/Chicago", "America/Denver", "UTC",
    "Europe/Berlin", "Asia/Tokyo", "Australia/Adelaide",  # +10:30/+9:30 offsets
    "Pacific/Kiritimati",                                  # +14, the extreme
]

# Pre-scenario charge_points state the generator may set. tariff being non-NULL
# is what allows the outbound lookup to happen at all (:170).
TARIFF_VALUES = [None, "PEAK", "OFFPEAK", "T1"]
FAULT_NOTE_VALUES = [None, "OK", "ok", "previous fault"]
SETTLED_VALUES = [0, 1]

# Fields deliberately NOT generated, and why. Each would require the oracle to
# re-implement a branch of the service rather than predict an outcome from the
# input, which is how property tests turn into a second copy of the bug.
EXCLUDED_FIELDS = {
    "md": "rewrites charge_points.model_code mid-scenario, which is the branch "
          "discriminator for every later frame in the same batch",
    "rt": "rewrites charge_points.tariff mid-scenario, which changes whether "
          "later frames in the same batch perform an outbound lookup",
    "numeric \"1\"": "a numeric identifier makes MySQL coerce the VARCHAR "
                     "cp_ident column to a number, so 0 matches every row; "
                     "this deserves its own characterization case, not random "
                     "exploration",
    "empty-string la": "PHP parses \"\" as 'now', making utc_event_at "
                       "clock-derived and unpredictable",
}
