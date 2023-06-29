#!/usr/bin/env python3
#
# Copyright 2024 Cisco Systems, Inc. and its affiliates
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# SPDX-License-Identifier: Apache-2.0

# This module parses the output of "perf script" (with particular parameters -
# see below).
#
# You might wonder, why? The `perf` executable already has a python scripting
# interface. However, this omits events we require (in particular, the
# PERF_RECORD_COMM events providing process/thread hierarchy and names).

import logging
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Union
from unittest import TestCase

# All possible records
Record = Union["SchedRecord", "CommRecord", "ForkRecord", "ExitRecord"]


# Main export of this module: parse a line into a structured object if possible.
# Return whether or not this is a perf record (vs something totally different),
# and also, the parsed object if possible.
def parse(line: str) -> Optional[Record]:
    # Try most common case first: a sched record
    msched = SCHED_RE.match(line)
    if msched is not None:
        args = [msched.group(i) for i in range(1, 8)]
        return SchedRecord.parse(*args)

    # If that fails, then try a PERF_RECORD_* match
    mperf = PERF_RECORD_RE.match(line)
    if mperf is None:
        logging.warning("Ignoring unknown record: %s", line)
        return None

    # Parse out the common bits, and then switch to a specific parser for this
    # particular PERF_RECORD_* type.
    opid, otid, cpu, secs, nsecs = [int(mperf.group(n)) for n in range(1, 6)]
    ts = int(secs) * 1_000_000_000 + int(nsecs)  # use ns internally
    event_type = mperf.group(6)
    rest_of_line = mperf.group(7)
    match event_type:
        case "COMM":
            return CommRecord.parse(rest_of_line)
        case "FORK":
            # Ignore the timestamp zero PERF_RECORD_FORK entries, since these
            # give less information than the PERF_RECORD_COMM ones.
            if ts > 0:
                return ForkRecord.parse(rest_of_line, opid, otid, cpu, ts)
        case "EXIT":
            return ExitRecord.parse(rest_of_line, opid, otid, cpu, ts)
        case _:
            logging.warning("Ignoring unknown PERF_RECORD_* entry: %s", line)
    return None


# Regexp for a sched record, consuming the entire line. eg:
#
# 1234/1234  [002] 3376096.441959680:  sched:sched_waking: comm=kworker/2:1 ...
# ^^^^ ^^^^   ^^^  ^^^^^^^ ^^^^^^^^^         ^^^^^^^^^^^^ ^^^^^^^^^^^^^^^^^^^^^
# opid otid   cpu  ts secs  ts nsecs             type          other stuff
#
# where opid/otid are the pid/tid as seen outside the namespace, cpu is the core
# number (small integer), and ts is the timestamp.
SCHED_RE = re.compile(
    r" *([\d-]+)/([\d-]+) +\[0*(\d+)\] +(\d+)\.(\d+): +sched:(\w+): (.*)$"
)


# Base regexp for any PERF_RECORD_*, consuming the entire line. eg:
#
#  6802/6802  [004] 926991.760617747: PERF_RECORD_COMM exec: ifconfig:6802/6802
#  ^^^^ ^^^^   ^^^  ^^^^^^ ^^^^^^^^^              ^^^^ ^^^^^^^^^^^^^^^^^^^^^^^^
#  opid otid   cpu  ts sec  ts nsec               type         other stuff
#
# where opid/otid are the pid/tid as seen outside the namespace, cpu is the core
# number (small integer), and ts is the timestamp.
PERF_RECORD_RE = re.compile(
    r" *([\d-]+)/([\d-]+) +\[0*(\d+)\] +(\d+)\.(\d+): PERF_RECORD_([A-Z]+)(.*)$"
)


#
# SCHED records (scheduling event)
#
@dataclass
class SchedRecord:
    rec_type: str
    opid: int
    otid: int
    cpu: int
    ts: int
    args: Dict[str, str]

    @classmethod
    def parse(
        cls,
        opid: str,
        otid: str,
        cpu: str,
        secs: str,
        nsecs: str,
        rec_type: str,
        raw_args: str,
    ) -> "SchedRecord":
        # Convert timestamp to integer nanoseconds
        ts = int(secs) * 1_000_000_000 + int(nsecs)

        # Convert the args (eg "key1=val1 key2=val2 [ns]") into a dict
        args = {}
        for t in map(lambda x: x.split("="), raw_args.split()):
            match t:
                case k, v:  # skip things like "==>" between "key=value" items
                    args[k] = v

        # A "normal" sched_switch or sched_wakup output here is something like:
        #
        # sched:sched_switch: prev_comm=evm_signal_thre prev_pid=41014 prev_prio=120 prev_state=R ==> next_comm=xrosPoolWorker next_pid=41047 next_prio=120
        # sched:sched_wakeup: comm=kworker/u8:0 pid=1369725 prio=120 target_cpu=001
        #
        # that are handled by the generic key=val code above. But for at least
        # one version of perf (that doesn't output a version number) we instead get:
        #
        # sched:sched_switch: dev 0 ts:6450 [120] S ==> swapper/3:0 [120]
        # sched:sched_wakeup: db_writer:3736 [120] success=1 CPU:003
        #
        # so handle those case too.
        #
        # It's ok to be slower for these cases, since this shouldn't really
        # happen, and arguably somebody deserves some consequences for using
        # a weird perf binary.
        if rec_type == "sched_switch" and "prev_comm" not in args:
            m = WEIRD_SCHED_SWITCH_RE.match(raw_args)
            if m is not None:
                args = {
                    "prev_comm": m.group(1),
                    "prev_pid": m.group(2),
                    "prev_prio": m.group(3),
                    "prev_state": m.group(4),
                    "next_comm": m.group(5),
                    "next_pid": m.group(6),
                    "next_prio": m.group(7),
                }
        elif rec_type == "sched_wakeup" and "pid" not in args:
            m = WEIRD_SCHED_WAKEUP_RE.match(raw_args)
            if m is not None:
                args = {
                    "comm": m.group(1),
                    "pid": m.group(2),
                    "prio": m.group(3),
                    "target_cpu": m.group(4),
                }

        return SchedRecord(rec_type, int(opid), int(otid), int(cpu), ts, args)


# Unusual output for sched:sched_switch:
#
# sched:sched_switch: dev 0 ts:6450 [120] S ==> swapper/3:0 [120]
#                     ^^^^^^^^ ^^^^  ^^^  ^     ^^^^^^^^^ ^  ^^^
#                     prev_comm pid  prio state next_comm pid prio
#
# You can get state being things like "D|W" sometimes.
WEIRD_SCHED_SWITCH_RE = re.compile(
    r"(.*?):(\d+) \[(\d+)\] (\S+) ==> (.*):(\d+) \[(\d+)\]$"
)

# Unusual output for sched:sched_wakeup:
#
# sched:sched_wakeup: db_writer:3736 [120] success=1 CPU:003
#                     ^^^^^^^^^ ^^^^  ^^^                ^^^
#                      comm     pid   prio               cpu
WEIRD_SCHED_WAKEUP_RE = re.compile(r"(.*?):(\d+) \[(\d+)\] .*? CPU:(\d+)$")


#
# COMM records (process/thread info at start)
#
@dataclass
class CommRecord:
    """
    A process/thread record, indicating those process/threads running at the
    start of the recording.
    """

    name: str  # thread name
    pid: int  # process id
    tid: int  # thread id

    @classmethod
    def parse(cls, rest_of_line: str) -> Optional["CommRecord"]:
        mcomm = PERF_RECORD_COMM_RE.match(rest_of_line)
        if mcomm is None:
            logging.error("PERF_RECORD_COMM failed to match: %s", rest_of_line)
            return None
        name = mcomm.group(1)
        pid = int(mcomm.group(2))
        tid = int(mcomm.group(3))
        return CommRecord(name, pid, tid)


# Parse the specifics of a PERF_RECORD_COMM, eg:
#                                                          input is this bit
#                                                      |----------------------|
#  6802/6802  [004] 926991.760617747: PERF_RECORD_COMM exec: ifconfig:6802/6802
#                                                            ^^^^^^^^ ^^^^ ^^^^
#                                                 process/thread name  pid  tid
PERF_RECORD_COMM_RE = re.compile(r"(?: exec)?: (.*):(\d+)/(\d+)$")


#
# FORK records (process/thread created during recording)
#
@dataclass
class ForkRecord:
    """
    A fork record, indicaitng a process or thread creation during the recording.
    """

    pid: int  # pid
    tid: int  # tid
    ppid: int  # parent pid
    ptid: int  # parent tid
    opid: int  # pid as seen from outside the namespace
    otid: int  # tid as seen from outside the namespace
    cpu: int  # cpu number
    ts: int  # timestamp in ns

    @classmethod
    def parse(
        cls, rest_of_line: str, opid: int, otid: int, cpu: int, ts: int
    ) -> Optional["ForkRecord"]:
        mfork = FORK_EXIT_RE.match(rest_of_line)
        if mfork is None:
            logging.error("PERF_RECORD_FORK failed to match: %s", rest_of_line)
            return None
        pid, tid, ppid, ptid = [int(mfork.group(x)) for x in range(1, 5)]
        return ForkRecord(pid, tid, ppid, ptid, opid, otid, cpu, ts)


# Parse the specifics of a PERF_RECORD_FORK, eg:
#                                                        input is this bit
#                                                     |---------------------|
#  6780/6780  [004] 926991.719359812: PERF_RECORD_FORK(6780:6781):(6780:6780)
#                                                      ^^^^ ^^^^   ^^^^ ^^^^
#                                                      pid  tid    ppid ptid
FORK_EXIT_RE = re.compile(r"\((\d+):(\d+)\):\((\d+):(\d+)\)")


#
# EXIT records (process/thread ending during recording)
#
@dataclass
class ExitRecord:
    """
    An exit record, indicating an exiting thead or process.
    """

    pid: int  # pid
    tid: int  # tid
    opid: int  # pid as seen from outside the namespace
    otid: int  # tid as seen from outside the namespace
    cpu: int  # cpu number
    ts: int  # timestamp in ns

    @classmethod
    def parse(
        cls, rest_of_line: str, opid: int, otid: int, cpu: int, ts: int
    ) -> Optional["ExitRecord"]:
        # Almost identical to a FORK record
        mexit = FORK_EXIT_RE.match(rest_of_line)
        if mexit is None:
            logging.error("PERF_RECORD_EXIT failed to match: %s", rest_of_line)
            return None
        pid, tid = [int(mexit.group(x)) for x in range(1, 3)]
        return ExitRecord(pid, tid, opid, otid, cpu, ts)


##############################################################################
#
# Tests
#
##############################################################################


# Simple equality assertion for inline tests below
class ParseTestCase(TestCase):
    lines: List[str] = []
    expected: List[object] = []

    def test(self) -> None:
        for line, expected in zip(self.lines, self.expected):
            self.assertEqual(parse(line), expected)


class CommRecordTest(ParseTestCase):
    lines = [
        "    0/0     [000]     0.000000000: PERF_RECORD_COMM: invmgr:1059/1097",
        "    0/0     [000]     0.000000000: PERF_RECORD_COMM: SysDB EDM Threa:1059/1104",
        " 6802/6802  [004] 926991.760617747: PERF_RECORD_COMM exec: ifconfig:6802/6802",
        " 6802/6803  [004] 926991.760617747: PERF_RECORD_COMM exec: ifconfig:6804/6805",
    ]

    expected = [
        CommRecord("invmgr", 1059, 1097),
        CommRecord("SysDB EDM Threa", 1059, 1104),
        CommRecord("ifconfig", 6802, 6802),
        CommRecord("ifconfig", 6804, 6805),
    ]


class ForkRecordTest(ParseTestCase):
    lines = [
        " 6780/6780  [004] 926991.719359812: PERF_RECORD_FORK(6780:6781):(6780:6780)"
        " 6784/6785  [004] 926991.719359812: PERF_RECORD_FORK(6780:6781):(6782:6783)"
    ]
    expected = [
        ForkRecord(6780, 6781, 6780, 6780, 6780, 6780, 4, 926991719359812),
        ForkRecord(6780, 6781, 6782, 6783, 6784, 6785, 4, 926991719359812),
    ]


class ExitRecordTest(ParseTestCase):
    lines = [
        " 6782/6782  [004] 926991.722004488: PERF_RECORD_EXIT(6782:6783):(5911:5911)",
        " 6782/6783  [004] 926991.722004488: PERF_RECORD_EXIT(6784:6785):(5911:5912)",
    ]
    expected = [
        ExitRecord(6782, 6783, 6782, 6782, 4, 926991722004488),
        ExitRecord(6784, 6785, 6782, 6783, 4, 926991722004488),
    ]


class SchedRecordTest(ParseTestCase):
    lines = [
        "1372378/1372378 [000] 3376096.592194640:               sched:sched_migrate_task: comm=kworker/u8:0 pid=1369725 prio=120 orig_cpu=0 dest_cpu=1",
        "1372378/1372379 [004] 3376096.592207960:                     sched:sched_wakeup: comm=kworker/u8:0 pid=1369725 prio=120 target_cpu=001",
        "1372378/1372379 [006] 3376096.592216000:               sched:sched_stat_runtime: comm=sshd pid=1372378 runtime=129400 [ns] vruntime=26130216 [ns]",
        "1372378/1372379 [000] 3376096.592218600:                     sched:sched_switch: prev_comm=sshd prev_pid=1372378 prev_prio=120 prev_state=S ==> next_comm=swapper/0 next_pid=0 next_prio=120",
    ]

    expected = [
        SchedRecord(
            "sched_migrate_task",
            1372378,
            1372378,
            0,
            3376096592194640,
            {
                "comm": "kworker/u8:0",
                "pid": "1369725",
                "prio": "120",
                "orig_cpu": "0",
                "dest_cpu": "1",
            },
        ),
        SchedRecord(
            "sched_wakeup",
            1372378,
            1372379,
            4,
            3376096592207960,
            {
                "comm": "kworker/u8:0",
                "pid": "1369725",
                "prio": "120",
                "target_cpu": "001",
            },
        ),
        SchedRecord(
            "sched_stat_runtime",
            1372378,
            1372379,
            6,
            3376096592216000,
            {
                "comm": "sshd",
                "pid": "1372378",
                "runtime": "129400",
                "vruntime": "26130216",
            },
        ),
        SchedRecord(
            "sched_switch",
            1372378,
            1372379,
            0,
            3376096592218600,
            {
                "prev_comm": "sshd",
                "prev_pid": "1372378",
                "prev_prio": "120",
                "prev_state": "S",
                "next_comm": "swapper/0",
                "next_pid": "0",
                "next_prio": "120",
            },
        ),
    ]


if __name__ == "__main__":
    import unittest

    unittest.main()
