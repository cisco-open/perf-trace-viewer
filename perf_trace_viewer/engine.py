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

# This module provides the main engine that transforms perf sched data into
# trace events.
#
# This is more of a "nasty ball of tightly coupled mutable state" than one would
# like, but it is what it is (a quick hack that has since been tidied up
# somewhat). The main tasks that have to be accomplished are:
#
# - Walking the entire input once as a stream, accumulating important events
#   into memory. We can't use perf's built-in python scription interface,
#   because that doesn't give us all the events we need (in particular, the
#   PERF_RECORD_COMM events providing process/thread hierarchy and names). We
#   then do any further processing, before return the result.
#
# - In the intended application, there are multiple namespaces, and `perf sched
#   record` is run from "inside" one namespace. But the pid data we get is from
#   the root namespace, as if the `perf sched record` had been run from outside
#   the container. So we need to map those.
#
# - We add pseudo-tracks for per-CPU usage, long blocking, and kernel usage.
#
# - In the original (even hackier) version of this script, round-robin-scheduled
#   threads were inferred heuristically, based on the absence of CFS stats.
#   Since then, we always collect the metadata, which gives us precise knowledge
#   of scheduling policy and priority. But this more recent knowledge is sort of
#   bolted on.

import functools
from collections import defaultdict
from typing import Callable, DefaultDict, Dict, Iterable, List, Mapping, Set, Union

from parse_mdata import ProcStat
from parse_perf_script import CommRecord, ExitRecord, ForkRecord, SchedRecord, parse
from trace_event import (
    BeginEvent,
    EndEvent,
    Event,
    InstantEvent,
    ProcessLabelEvent,
    ProcessNameEvent,
    ProcessSortIndexEvent,
    ThreadNameEvent,
    ThreadSortIndexEvent,
)
from utils import EventList, PidMapper, IncludeThisTimestamp, Spans, Stats

# A number that when negated, bigger than highest possible cpu index. Used as a
# virtual CPU index for threads waiting longer than wait_threshold (by default 3ms).
WAIT_CPU_ID = -1_000_000

# Special "process" names for running and long-waiting processes
RUNNING_TASK = "Running"
WAITING_TASK = "Waiting"


# Process an iterable of lines, and return the data
def process_perf_data(
    lines: Iterable[str],
    _mdata: Dict[str, str],
    proc_info: Dict[int, ProcStat],
    skip: float,
    duration: float,
    wait: float,
) -> List[Mapping[str, object]]:
    # Determine whether a thread is a kernel thread, using supplied proc_info
    # (see eg https://stackoverflow.com/questions/12213445/identifying-kernel-threads)
    def is_kernel(pid: int) -> bool:
        try:
            PF_KTHREAD = 0x00200000  # from linux/sched.h
            return bool(proc_info[pid].flags & PF_KTHREAD)
        except KeyError:
            return False

    # Fire up the engine!
    engine = Engine(skip, duration, wait, is_kernel)
    return engine.process(lines)


class Engine:
    def __init__(
        self,
        seconds_to_skip: float,
        seconds_to_process: float,
        wait_threshold: float,
        is_kernel: Callable[[int], bool],
    ) -> None:
        self.wait_threshold = wait_threshold
        self.is_kernel = is_kernel
        self.pid_mapper = PidMapper()
        self.events = EventList(self.pid_mapper)
        self.spans = Spans(self.events)
        self.stats = Stats()
        self.include_ts = IncludeThisTimestamp(seconds_to_skip, seconds_to_process)
        self.ipid_waiting_since: Dict[int, int] = {}
        self.waiting_pids: DefaultDict[int, Set[int]] = defaultdict(set)

    def process(self, lines: Iterable[str]) -> List[Mapping[str, object]]:
        # Process all the input in one pass
        for line in lines:
            self.process_line(line)

        rr_ipids = set()
        # Final cleanup: first heuristically infer RR-scheduled threads...
        for ipid, opid, otid in self.pid_mapper.backup_items():
            self.events.maybe_add_mapping(ipid, opid, otid)
            rr_ipids.add(ipid)

        # ...then the final post-processing
        self.events.for_each_event_of_type(
            ThreadNameEvent, functools.partial(self.tidy_thread_names, rr_ipids)
        )
        self.add_kernel_and_cpu_events()
        self.add_sort_index()

        # Return result
        return self.events.aslist()

    # Handle any single entry
    def process_line(self, line: str) -> None:
        match parse(line):
            case SchedRecord(rec_type, opid, otid, cpu, ts, args):
                if not self.include_ts.should_include(ts):
                    return
                match rec_type:
                    case "sched_switch":
                        self.sched_switch(opid, otid, cpu, ts, **args)

                    case "sched_wakeup":
                        ipid = int(args["pid"])
                        # Occasionally with kernel threads (especially rcuop, but sometimes
                        # ktimersoftd), it's possible to get more than one wakeup before we're
                        # scheduled. Coalesce these into a single one.
                        if ipid not in self.waiting_pids[cpu]:
                            self.waiting_pids[cpu].add(ipid)
                            self.spans.add(
                                ipid,
                                BeginEvent(
                                    WAITING_TASK,
                                    ts=ts,
                                    args={"prio": int(args["prio"])},
                                ),
                            )
                            self.ipid_waiting_since[ipid] = ts
                        self.pid_mapper.add_comm(ipid, args["comm"])

                    case "sched_stat_runtime":
                        ipid = int(args["pid"])
                        # First process the mapping from "inside-pid-namespace kernel-pid" to
                        # "outside-pid-namespace user-visible pid/tid"
                        self.events.maybe_add_mapping(ipid, opid, otid)
                        # Second, save the runtime stats
                        self.stats.save_stats(
                            cpu, ts, ipid, int(args["runtime"]), int(args["vruntime"])
                        )

            case CommRecord(name, pid, tid):
                self.events.append(ThreadNameEvent(pid, tid, name))
                if pid == tid:
                    self.events.append(ProcessNameEvent(pid, name))

            case ForkRecord(pid, tid, ppid, ptid, opid, otid, cpu, ts):
                evargs: Dict[str, Union[str, int]]
                if pid == ppid:
                    name = "thread_spawn"
                    evargs = {"pid": pid, "tid": tid, "parent tid": ptid, "cpu": cpu}
                else:
                    name = "process fork"
                    evargs = {
                        "pid": pid,
                        "tid": tid,
                        "parent pid": ppid,
                        "parent tid": ptid,
                        "cpu": cpu,
                    }
                self.events.append(
                    InstantEvent(name=name, pid=opid, tid=otid, ts=ts, args=evargs)
                )

            case ExitRecord(pid, tid, opid, otid, cpu, ts):
                evargs = {"pid": pid, "tid": tid, "cpu": cpu}
                self.events.append(
                    InstantEvent("thread_exit", pid=opid, tid=otid, ts=ts, args=evargs)
                )

    # Handle a sched_switch event
    def sched_switch(
        self,
        opid: int,
        otid: int,
        cpu: int,
        ts: int,
        prev_state: str,
        prev_comm: str,
        next_comm: str,
        **kwargs: str,
    ) -> None:
        # Extract integer parameters passed by dictionary
        prev_pid = int(kwargs["prev_pid"])
        next_pid = int(kwargs["next_pid"])
        next_prio = int(kwargs["next_prio"])

        # Assemble and save the metadata for the thread that has just completed. We
        # need to coerce the type, since we're adding str values to an int-valued
        # args return value.
        int_args = self.stats.thread_just_ended(prev_pid, next_pid, cpu, ts)
        args: Dict[str, Union[str, int]] = dict(int_args)
        args["end_state"] = expand_state(prev_state)

        # End the currently-running thread's running span
        self.spans.add(prev_pid, EndEvent(RUNNING_TASK, ts=ts, args=args))

        # Then end the new thread's waiting state, and start its running state
        self.spans.add(next_pid, EndEvent(WAITING_TASK, ts=ts))
        try:
            self.waiting_pids[cpu].remove(next_pid)
        except KeyError:
            pass
        self.spans.add(
            next_pid, BeginEvent(RUNNING_TASK, ts=ts, args={"prio": next_prio})
        )

        # Do the same for the virtual CPU thread
        if prev_pid != 0:
            self.spans.add(-cpu, EndEvent(str(prev_pid), ts=ts, args=args))
        if next_pid != 0:
            self.spans.add(-cpu, BeginEvent(str(next_pid), ts=ts))

        # If the new thread has been waiting long enough, create a virtual CPU entry
        try:
            waiting_since = self.ipid_waiting_since[next_pid]
            del self.ipid_waiting_since[next_pid]
            ms_waiting = (ts - waiting_since) / 1_000_000
            if ms_waiting >= self.wait_threshold:
                self.spans.add(WAIT_CPU_ID, BeginEvent(str(next_pid), ts=waiting_since))
                self.spans.add(WAIT_CPU_ID, EndEvent(str(next_pid), ts=ts))
        except KeyError:
            pass

        # If we haven't already done some mappings, do so now
        if prev_pid != 0 and not self.is_kernel(prev_pid):
            self.pid_mapper.add_backup(prev_pid, opid, otid)
        self.pid_mapper.add_comm(prev_pid, prev_comm)
        self.pid_mapper.add_comm(next_pid, next_comm)

    def tidy_thread_names(self, rr_ipids: Set[int], ev: Event) -> None:
        try:
            # If we can do the mapping, add that info to the thread
            # name. Asserts are mostly for mypy's benefit.
            assert ev.pid is not None and ev.tid is not None and ev.args is not None
            ipid = self.pid_mapper.out_to_in(ev.pid, ev.tid)
            thread_name = ev.args["name"]
            assert isinstance(thread_name, str)
            if ipid in rr_ipids:
                thread_name += " [𝗥𝗥]"
            ev.args["name"] = f"{thread_name} #{ipid}"
        except KeyError:
            pass

    # Once we have all the data, output a sort index to bring the busiest processes
    # to the top.
    def add_sort_index(self) -> None:
        # First construct the runtime per opid (adding up contributions from each thread)
        opid_runtime: DefaultDict[int, int] = defaultdict(int)
        for ipid, runtime in self.stats.runtime_items():
            try:
                pid, _tid = self.pid_mapper.in_to_out(ipid)
            except KeyError:
                pid = ipid
            opid_runtime[pid] += runtime
        # Now output sort indices based on those totals
        max_runtime = 0
        for opid, runtime in opid_runtime.items():
            self.events.append(ProcessSortIndexEvent(opid, -runtime))
            max_runtime = max(runtime, max_runtime)
        # Finally, keep pseudo-pids at the top
        max_runtime += 1
        for pid in self.pid_mapper.all_pseudo_opids():
            self.events.append(ProcessSortIndexEvent(pid, -max_runtime))

    # Group together threads without metadata into a 𝘬𝘦𝘳𝘯𝘦𝘭 pseudo-process,
    # and the CPU running/waiting pseudo-threads into a 𝘊𝘗𝘜𝘴 one.
    #
    # Note that the kernel description is a bit vague - this is because this is
    # expected to be just kernel threads, but sometimes we see scheduling events
    # for processes/threads outside the namespace (eg for the docker daemon
    # itself, while running inside a container). I've not found any way to
    # distinguish these from genuine kernel threads.
    def add_kernel_and_cpu_events(self) -> None:
        # Create tracks for the two pseudo-processes.
        cpid = self.pid_mapper.new_pseudo_opid()
        self.events.append(ProcessNameEvent(cpid, "𝘊𝘗𝘜𝘴"))
        self.events.append(
            ProcessLabelEvent(cpid, "(Virtual process representing CPU usage)")
        )
        kpid = self.pid_mapper.new_pseudo_opid()
        self.events.append(ProcessNameEvent(kpid, "𝘬𝘦𝘳𝘯𝘦𝘭"))
        self.events.append(
            ProcessLabelEvent(kpid, "(Virtual process for kernel and unknown threads)")
        )

        # Now that we have a virtual pid for the kernel track, walk over the
        # data and move any kernel threads into that. It's inefficient - we
        # could have done it as we went along, had we known what the virtual
        # kernel pid would be - but we don't know what the kernel pseudo-pid
        # will be until we've walked the data once (since it is a small integer
        # not used by any "real" process).
        #
        # Note that most kernel threads have already been indentified by being
        # in self.events.pending_events (ie their scheduling events were not
        # preceeded by a COMM record explaining what process/thread is
        # represented by that kernel pid), so this just mops up the rest.
        kpids = {pid for pid in self.pid_mapper.opid_seen if self.is_kernel(pid)}

        def maybe_move_to_kernel(ev: Event) -> None:
            if ev.pid == ev.tid and ev.pid in kpids:
                ev.pid = kpid

        self.events.for_each_event_of_type(Event, maybe_move_to_kernel)

        # Go through the events for pseudo-processes that have had no metadata
        # provided by the data itself (ie COMM events).
        for tid, evs in self.events.pending_events.items():
            if tid < 0:
                # Virtual CPU entry
                cpu = -tid
                if tid == WAIT_CPU_ID:
                    name = f"𝘞𝘢𝘪𝘵𝘪𝘯𝘨 ≥ {int(self.wait_threshold):.1f} 𝘮𝘴"
                else:
                    name = f"𝘊𝘗𝘜 {cpu}"
                self.events.append(ThreadNameEvent(cpid, cpu, name))
                for ev in evs:
                    ev.pid = cpid
                    ev.tid = cpu
                    ipid = int(ev.name)
                    comm = self.pid_mapper.get_comm(ipid)
                    ev.name = f"{comm} #{ipid}"
                    self.events.append(ev, timestamp_already_corrected=True)
            else:
                # Kernel thread entry
                if tid == 0:
                    name = "𝘪𝘥𝘭𝘦"
                    # Ensure idle thread is at the top.
                    self.events.append(ThreadSortIndexEvent(kpid, tid, -1))
                else:
                    name = f"{self.pid_mapper.get_comm(tid)} #{tid}"
                self.events.append(ThreadNameEvent(kpid, tid, name))
                for ev in evs:
                    ev.pid = kpid
                    ev.tid = tid
                    if tid == 0:
                        ev.name = "𝘪𝘥𝘭𝘦"
                    self.events.append(ev, timestamp_already_corrected=True)


# Decode the kernel state indicating why a thread was de-scheduled
# (from https://perfetto.dev/docs/data-sources/cpu-scheduling)
DECODE_STATE = {
    "R": "Runnable",
    "R+": "Runnable (Preempted)",
    "S": "Sleeping",
    "D": "Uninterruptible Sleep",
    "T": "Stopped",
    "t": "Traced",
    "X": "Exit (Dead)",
    "Z": "Exit (Zombie)",
    "x": "Task Dead",
    "I": "Idle",
    "K": "Wake Kill",
    "W": "Waking",
    "P": "Parked",
    "N": "No Load",
}


# Translate a short state code into a more self-describing comment
def expand_state(state: str) -> str:
    expanded = DECODE_STATE.get(state, "Unknown")
    return f"{state} [{expanded}]"
