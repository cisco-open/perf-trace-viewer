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

# This module parses the "perf-mdata.txt" file written by the `collect` script.
# The per-process data is saved in /proc/<pid>/stat format, which is documented
# in proc(5) - eg https://man7.org/linux/man-pages/man5/proc.5.html .

import re
import unittest
from collections import namedtuple
from typing import IO, Dict, Tuple

# Roughly parse out a line from /proc/<pid>/stat.
STAT = re.compile(r"^(\d+) \((.*)\) (\w) ([\d -]+)$")

# Named tuple for the fields in /proc/<pid>/stat. Thanks, CoPilot!
# fmt: off
ProcStat = namedtuple("ProcStat", (
     "pid", "comm", "state", "ppid", "pgrp", "session", "tty_nr", "tpgid",
    "flags", "minflt", "cminflt", "majflt", "cmajflt", "utime", "stime",
    "cutime", "cstime", "priority", "nice", "num_threads", "itrealvalue",
    "starttime", "vsize", "rss", "rsslim", "startcode", "endcode",
    "startstack", "kstkesp", "kstkeip", "signal", "blocked", "sigignore",
    "sigcatch", "wchan", "nswap", "cnswap", "exit_signal", "processor",
    "rt_priority", "policy", "delayacct_blkio_ticks", "guest_time",
    "cguest_time", "start_data", "end_data", "start_brk", "arg_start",
    "arg_end", "env_start", "env_end", "exit_code"
))
# fmt: on


# Parse the mdata file.
def parse_mdata(raw_input: IO[bytes]) -> Tuple[Dict[str, str], Dict[int, ProcStat]]:
    mdata = {}
    procs = {}
    for rawline in raw_input:
        line = rawline.decode("utf-8")
        if line.startswith("## "):
            # comment
            pass
        elif line.startswith("# "):
            # Key-value pair
            match line.split(":", maxsplit=1):
                case key, val:
                    mdata[key[2:]] = val.strip()
        else:
            # Line from /proc/<pid>/stat. There are two sets of these: one set
            # following a `## before` comment, from before the `perf sched
            # record`, and a second set following a `## after` comment. By just
            # saving everything into a pid-indexed dict, we prefer more recent
            # data to older data if we get both. The hope is we"ll have some
            # information on each process seen during the recording, but if not
            # (eg a process is started and exits during the recording) then that
            # just means some diagnostic value is lost.
            m = STAT.match(line)
            assert m is not None
            # Parse line at least into suitable types - don"t fully decode at
            # this stage since we don"t know which fields will actually be used.
            pid = int(m.group(1))
            comm = m.group(2)  # program name, truncated to 16 chars
            state = m.group(3)  # state, eg R for running, S for sleeping
            rest = [int(x) for x in m.group(4).split()]  # rest are ints
            procs[pid] = ProcStat(pid, comm, state, *rest)
    return mdata, procs


#
# Tests
#
class TestParseMdata(unittest.TestCase):
    # Test program name parsing, which is the only non-trivial part of
    # /proc/<pid>/stat parsing.
    def test_comm(self) -> None:
        for line, expected_comm in (
            ("42 (foo) S 1 -2 3", "foo"),
            ("42 (foo with spaces) S 1 -2 3", "foo with spaces"),
            ("42 ((foo)) S 1 -2 3", "(foo)"),
            ("42 (foo with )random)() S 1 -2 3", "foo with )random)("),
        ):
            m = STAT.match(line)
            assert m is not None
            parsed_comm = m.group(2)
            self.assertEqual(parsed_comm, expected_comm)

    # Test overall mdata parsing (and document the expected format).
    def test_parse_mdata(self) -> None:
        raw_input = b"""\
## System performance data for https://github.com/cisco-open/perf-trace-viewer
# date: Tue Jul 18 16:10:18 UTC 2023
# system: Linux xr-vm_node0_RSP0_CPU0 3.14.23-WR7.0.0.2_standard #1 SMP Wed Feb 19 08:56:10 PST 2020 x86_64 x86_64 x86_64 GNU/Linux
# duration: 10 seconds
# perf-version: perf version 3.14.23
# perf-sched-cmd: perf sched record --mmap-pages 8M sleep 10 --aio
# perf-script-cmd: perf script --show-task-events --fields pid,tid,cpu,time,event,trace --ns
## before
1 (init) S 0 1 1 42 1 4202752 2750 3190270 1 559 2 14 7921 2767 20 0 1 0 22698 28897280 480 18446744073709551615 94075734745088 94075735046540 140731912490512 140731912489592 140174869709891 0 0 4096 536962595 18446744071765192153 0 0 17 3 0 0 0 0 0 94075737145592 94075737155264 94075757477888 140731912494870 140731912494881 140731912494881 140731912495085 0
10236 (wanphy_proc) S 3901 10236 42 0 -1 4202752 5174 1808 0 0 38 7 0 0 20 0 6 0 34222 8171171840 4333 18446744073709551615 93970763452416 93970763463572 140723891487136 140723891485840 140083330865987 0 0 0 17582 18446744073709551615 0 0 17 2 0 0 0 0 0 93970765561856 93970765563680 93970770280448 140723891489507 140723891489519 140723891489519 140723891490787 0
10237 (ssh_server) S 3901 10237 42 0 -1 4202752 7386 10556 0 1 67 14 65 15 20 0 12 0 34223 8826036224 6216 18446744073709551615 94165094514688 94165094747684 140724922712336 140724922710704 140635724155715 0 88583 0 17582 18446744073709551615 0 0 17 0 0 0 0 0 0 94165096844840 94165096873280 94165117935616 140724922718949 140724922718960 140724922718960 140724922720228 0
## after
1 (init) S 0 1 1 34816 1 4202752 2750 3190270 1 559 2 14 7921 2767 20 0 1 0 22698 28897280 480 18446744073709551615 94075734745088 94075735046540 140731912490512 140731912489592 140174869709891 0 0 4096 536962595 18446744071765192153 0 0 17 3 0 0 0 0 0 94075737145592 94075737155264 94075757477888 140731912494870 140731912494881 140731912494881 140731912495085 0
10236 (wanphy_proc) S 3901 10236 3806 0 -1 4202752 5174 1808 0 0 38 7 0 0 20 0 6 0 34222 8171171840 4333 18446744073709551615 93970763452416 93970763463572 140723891487136 140723891485840 140083330865987 0 0 0 17582 18446744073709551615 0 0 17 2 0 0 0 0 0 93970765561856 93970765563680 93970770280448 140723891489507 140723891489519 140723891489519 140723891490787 0
10237 (ssh_server) S 3901 10237 3806 0 -1 4202752 7386 10556 0 1 67 14 65 15 20 0 12 0 34223 8826036224 6216 18446744073709551615 94165094514688 94165094747684 140724922712336 140724922710704 140635724155715 0 88583 0 17582 18446744073709551615 0 0 17 0 0 0 0 0 0 94165096844840 94165096873280 94165117935616 140724922718949 140724922718960 140724922718960 140724922720228 0
10238 (ssh_backup_serv) S 3901 10238 3806 0 -1 4202752 6298 1810 0 0 59 9 0 0 20 0 9 0 34223 8595910656 5380 18446744073709551615 94686349664256 94686349760644 140721224446864 140721224445456 140667692329795 0 88583 0 17582 18446744073709551615 0 0 17 1 0 0 0 0 0 94686351857800 94686351875360 94686377046016 140721224452823 140721224452841 140721224452841 140721224454109 0
"""
        expected_mdata = {
            "date": "Tue Jul 18 16:10:18 UTC 2023",
            "system": "Linux xr-vm_node0_RSP0_CPU0 3.14.23-WR7.0.0.2_standard #1 SMP Wed Feb 19 08:56:10 PST 2020 x86_64 x86_64 x86_64 GNU/Linux",
            "duration": "10 seconds",
            "perf-version": "perf version 3.14.23",
            "perf-sched-cmd": "perf sched record --mmap-pages 8M sleep 10 --aio",
            "perf-script-cmd": "perf script --show-task-events --fields pid,tid,cpu,time,event,trace --ns",
        }
        # fmt: off
        expected_procs = {
            1: ProcStat(
                1, "init", "S", 0, 1, 1, 34816, 1, 4202752, 2750, 3190270,
                1, 559, 2, 14, 7921, 2767, 20, 0, 1, 0, 22698, 28897280,
                480, 18446744073709551615, 94075734745088, 94075735046540,
                140731912490512, 140731912489592, 140174869709891, 0, 0,
                4096, 536962595, 18446744071765192153, 0, 0, 17, 3, 0, 0,
                0, 0, 0, 94075737145592, 94075737155264, 94075757477888,
                140731912494870, 140731912494881, 140731912494881,
                140731912495085, 0
            ),
            10236: ProcStat(
                10236, "wanphy_proc", "S", 3901, 10236, 3806, 0, -1,
                4202752, 5174, 1808, 0, 0, 38, 7, 0, 0, 20, 0, 6,
                0, 34222, 8171171840, 4333, 18446744073709551615,
                93970763452416, 93970763463572, 140723891487136,
                140723891485840, 140083330865987, 0, 0, 0, 17582,
                18446744073709551615, 0, 0, 17, 2, 0, 0, 0, 0, 0,
                93970765561856, 93970765563680, 93970770280448,
                140723891489507, 140723891489519, 140723891489519,
                140723891490787, 0
            ),
            10237: ProcStat(
                10237, "ssh_server", "S", 3901, 10237, 3806, 0, -1,
                4202752, 7386, 10556, 0, 1, 67, 14, 65, 15, 20, 0,
                12, 0, 34223, 8826036224, 6216, 18446744073709551615,
                94165094514688, 94165094747684, 140724922712336,
                140724922710704, 140635724155715, 0, 88583, 0, 17582,
                18446744073709551615, 0, 0, 17, 0, 0, 0, 0, 0, 0,
                94165096844840, 94165096873280, 94165117935616,
                140724922718949, 140724922718960, 140724922718960,
                140724922720228, 0
            ),
            10238: ProcStat(
                10238, "ssh_backup_serv", "S", 3901, 10238, 3806, 0,
                -1, 4202752, 6298, 1810, 0, 0, 59, 9, 0, 0, 20, 0,
                9, 0, 34223, 8595910656, 5380, 18446744073709551615,
                94686349664256, 94686349760644, 140721224446864,
                140721224445456, 140667692329795, 0, 88583, 0, 17582,
                18446744073709551615, 0, 0, 17, 1, 0, 0, 0, 0, 0,
                94686351857800, 94686351875360, 94686377046016,
                140721224452823, 140721224452841, 140721224452841,
                140721224454109, 0
            ),
        }
        # fmt: on
        import io

        mdata, procs = parse_mdata(io.BytesIO(raw_input))
        self.assertEqual(mdata, expected_mdata)
        self.assertEqual(procs, expected_procs)


if __name__ == "__main__":
    unittest.main()
