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

# This module provides various standalone helper classes.

from collections import defaultdict
from typing import (
    Callable,
    DefaultDict,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Set,
    Tuple,
)

from trace_event import BeginEvent, DurationEvent, EndEvent, Event

# Time conversion: perf thinks in ns, but Trace Viewer in µs. This conversion
# does mean it gets some things wrong (eg has to spill adjacent events into
# multiple rows because it mistakenly thinks they overlap) but is the easiest
# way to get something mostly sensible.
TIME_CONVERSION = 1000


class PidMapper:
    """
    Maintain various mappings:
     - internal kernel pid, to user-facing intra-namespace (pid, tid) pairs
     - internal kernel pid to comm (program name)
     - backup (currently unresolved) internal-pid-to-external-pid-tid-pairs
    """

    def __init__(self) -> None:
        # Map ipid to (opid, otid)
        self.in2out: Dict[int, Tuple[int, int]] = {}

        # Map (opid, otid) to ipid
        self.out2in: Dict[Tuple[int, int], int] = {}

        # Backup map ipid to (opid, otid)
        self.backup_in2out: Dict[int, Tuple[int, int]] = {}

        # Map ipid to comm (program name)
        self.ipid2comm: Dict[int, str] = {}

        # Which opids have been seen
        self.opid_seen: Set[int] = set()

        # Pseudo-pids are low user-visible pid values (for things like the
        # kernel and per-CPU virtual tracks) that are guaranteed not to clash
        # with any real pids.
        self.pseudo_pids: Set[int] = set()

    # Try to create a new mapping from ipid to (opid, otid), and return whether
    # or not a new association was created (or it has already been seen).
    def new_map(self, ipid: int, opid: int, otid: int) -> bool:
        if ipid in self.in2out or opid == 0:
            return False
        self.in2out[ipid] = (opid, otid)
        self.out2in[(opid, otid)] = ipid
        return True

    # Add a backup association from ipid to (opid, otid) pair.
    def add_backup(self, ipid: int, opid: int, otid: int) -> None:
        self.backup_in2out[ipid] = (opid, otid)

    # Iterate over the backup associations.
    def backup_items(self) -> Iterable[Tuple[int, int, int]]:
        for ipid, (opid, otid) in self.backup_in2out.items():
            if ipid not in self.in2out:
                yield (ipid, opid, otid)

    # Try to map an ipid to an (opid, otid) pair. Can throw a KeyError.
    def in_to_out(self, ipid: int) -> Tuple[int, int]:
        opid, otid = self.in2out[ipid]
        self.opid_seen.add(opid)
        return opid, otid

    # Try to map an (opid, otid) pair to ipid. Can throw a KeyError.
    def out_to_in(self, opid: int, otid: int) -> int:
        return self.out2in[(opid, otid)]

    # Add an ipid to comm association.
    def add_comm(self, ipid: int, comm: str) -> None:
        if ipid not in self.ipid2comm:
            self.ipid2comm[ipid] = comm

    # Look up comm by ipid. Can throw a KeyError.
    def get_comm(self, ipid: int) -> str:
        return self.ipid2comm[ipid]

    # Walk all allocated pseudo-pids.
    def all_pseudo_opids(self) -> Iterable[int]:
        return self.pseudo_pids

    # Generate a free low pid that doesn't clash with a real one (or another pseudo one).
    def new_pseudo_opid(self) -> int:
        pid = 0
        while pid in self.pseudo_pids or pid in self.opid_seen:
            pid += 1
        self.pseudo_pids.add(pid)
        return pid


class EventList:
    """
    Build up a sequence of events, that can eventually be exported as JSON.
    """

    def __init__(self, pid_mapper: PidMapper) -> None:
        self.pid_mapper = pid_mapper
        self.events: List[Event] = []
        self.pending_events: DefaultDict[int, List[Event]] = defaultdict(list)

    def aslist(self) -> List[Mapping[str, object]]:
        return [ev.asdict() for ev in self.events]

    # Append an event to the sequence
    def append(self, ev: Event, timestamp_already_corrected: bool = False) -> None:
        if not timestamp_already_corrected and ev.ts is not None:
            # Timestamps need converting from ns (perf) to µs (Trace Event).
            ev.ts /= TIME_CONVERSION
        self.events.append(ev)

    # Map a DurationEvent from internal kernel pid to user-visible (pid, tid)
    # and append to event log.
    def mappend(self, ipid: int, ev: Event) -> None:
        assert isinstance(ev, DurationEvent)
        assert ev.ts is not None
        ev.ts /= TIME_CONVERSION
        try:
            ev.pid, ev.tid = self.pid_mapper.in_to_out(ipid)
            self.events.append(ev)
        except KeyError:
            self.pending_events[ipid].append(ev)

    # Add an association bewteen ipid and (opid, otid).
    def maybe_add_mapping(self, ipid: int, opid: int, otid: int) -> None:
        if self.pid_mapper.new_map(ipid, opid, otid) and ipid in self.pending_events:
            # Process any pending events, now we know the association
            for ev in self.pending_events[ipid]:
                ev.pid, ev.tid = opid, otid
                self.events.append(ev)
            del self.pending_events[ipid]

    # Walk the saved events, invoking a callback for each event of specified type.
    def for_each_event_of_type(
        self, cls: type, callback: Callable[[Event], None]
    ) -> None:
        for ev in self.events:
            if isinstance(ev, cls):
                callback(ev)


class Spans:
    """
    Manage generated spans (BeginEvent - EndEvent durations). This is mostly due
    to limitations in the legacy TraceViewer. Perfetto seems pretty happy
    accepting arbitrary streams of interleaved and overlapping Begin/End Events,
    and just piecing it all together. But TraceViewer assumes that a single span
    comes as a contiguous pair of adjacent Begin/End events. So this little
    helper "saves up" BeginEvents, and only when a matching EndEvent is found,
    adds both to the EventList. While we're at it, entirely drop any spans of
    zero length, since they won't show up in the UI at any zoom level anyway.
    """

    def __init__(self, events: EventList):
        self.events = events
        self.begin_events: Dict[int, BeginEvent] = {}

    # Add either a BeginEvent or EndEvent
    def add(self, ipid: int, event: DurationEvent) -> None:
        match event:
            case BeginEvent():
                # Just remember it for now. There are no overlapping spans for a
                # single track (what is presented to the user as a (pid, tid)
                # pair), and at this stage, the unique identifier for that track
                # is the ipid (kernel sense of pid).
                self.begin_events[ipid] = event
            case EndEvent():
                # Find the matching BeginEvent, and process them as a pair.
                try:
                    begin_event = self.begin_events[ipid]
                    del self.begin_events[ipid]
                    assert begin_event.ts is not None and event.ts is not None
                    if begin_event.ts < event.ts:
                        self.events.mappend(ipid, begin_event)
                        self.events.mappend(ipid, event)
                except KeyError:
                    pass


class IncludeThisTimestamp:
    """
    Statefully determine whether a timestamp is in the requested interval
    """

    def __init__(self, delay: float = 0, duration: float = 0.0) -> None:
        self.delay = int(delay * 1_000_000_000)
        self.starting_ts: Optional[int] = None
        if duration:
            self.end = duration * 1_000_000_000 + self.delay
        else:
            self.end = float("infinity")

    # Return whether or not the entry with the provieded timestamp should be included.
    def should_include(self, ts: int) -> bool:
        if self.starting_ts is None:
            if ts > 0:
                self.starting_ts = ts
            return True
        if ts - self.starting_ts < self.delay or ts - self.starting_ts > self.end:
            include = False
        else:
            include = True
        return include


class Stats:
    """
    Maintain scheduling stats
    """

    def __init__(self) -> None:
        self.cpu_stats: Dict[int, Tuple[int, int, int, int]] = {}
        self.ipid_runtime: DefaultDict[int, int] = defaultdict(int)
        self.ipid_start_running_ts: Dict[int, int] = {}

    # When a thread's execution has just ended, update the stats with the
    # execution times, and return a dictionary with whatever info we can piece together.
    def thread_just_ended(
        self, ipid_just_stopped: int, ipid_starting_next: int, cpu: int, ts: int
    ) -> Dict[str, int]:
        args = {}
        if ipid_just_stopped in self.ipid_start_running_ts:
            started_running = self.ipid_start_running_ts[ipid_just_stopped]
            stat_ts, stat_pid, runtime, vruntime = self.cpu_stats.get(cpu, (0, 0, 0, 0))
            if stat_pid == ipid_just_stopped and stat_ts >= started_running:
                args["CFS runtime (ns)"] = runtime
                args["CFS vruntime (ns)"] = vruntime
                self.ipid_runtime[ipid_just_stopped] += runtime
            else:
                # We didn't get a stat for this run interval, so proxy a best guess
                approx_runtime = ts - started_running
                self.ipid_runtime[ipid_just_stopped] += approx_runtime
                args["Non-CFS runtime (ns)"] = approx_runtime
        self.ipid_start_running_ts[ipid_starting_next] = ts
        return args

    # Save stats when we get it from a scheduling stats entry.
    def save_stats(
        self, cpu: int, ts: int, ipid: int, runtime: int, vruntime: int
    ) -> None:
        self.cpu_stats[cpu] = (ts, ipid, runtime, vruntime)

    # Walk the record of all ipids and their total runtime.
    def runtime_items(self) -> Iterable[Tuple[int, int]]:
        return self.ipid_runtime.items()
