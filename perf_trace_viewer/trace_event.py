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

# This module enables creating and outputing Google Trace Events
#
# For the Trace Event specification, see
# https://docs.google.com/document/d/1CvAClvFfyA5R-PhYUmn5OOQtYMH4h6I0nSsKchNAySU/preview

import dataclasses
from dataclasses import dataclass
from typing import Dict, Mapping, Optional, Union


@dataclass
class Event:
    """
    Base class for any trace event
    """

    name: str  # Name of event, as displayed in Trace Viewer
    ph: str = "?"  # Event type, must be overridden
    # key-val pairs that appear in event box at bottom of UI
    args: Optional[Dict[str, Union[str, int]]] = None
    ts: Optional[float] = None  # Timestamp in µs
    pid: Optional[int] = None  # Pid (in Trace Viewer sense)
    tid: Optional[int] = None  # Tid

    # Return event as a dict
    def asdict(self) -> Mapping[str, Union[str, int, float]]:
        # Get a regular dict, then delete any None values
        d = dataclasses.asdict(self)
        keys_to_delete = [k for k, v in d.items() if v is None]
        for k in keys_to_delete:
            del d[k]
        return d


@dataclass
class DurationEvent(Event):
    "An event that delimits a span, so BeginEvent or EndEvent"


@dataclass
class BeginEvent(DurationEvent):
    "An event that starts a span"
    ph: str = "B"


@dataclass
class EndEvent(DurationEvent):
    "An event that ends a span"
    ph: str = "E"


@dataclass
class InstantEvent(Event):
    "A standalone event"
    ph: str = "i"
    s: str = "p"  # scope ('g' for global, 'p' for process, 't' for thread)


@dataclass
class MetadataEvent(Event):
    "A metadata record"
    ph: str = "M"


@dataclass
class ProcessNameEvent(MetadataEvent):
    "Metadata that associates a user-visible pid with a process name"
    name = "process_name"

    def __init__(self, pid: int, process_name: str) -> None:
        self.pid = pid
        self.args = {"name": process_name}


@dataclass
class ThreadNameEvent(MetadataEvent):
    "Metadata that associates a user-visible (pid, tid) pair with a thread name"
    name = "thread_name"

    def __init__(self, pid: int, tid: int, thread_name: str) -> None:
        self.pid = pid
        self.tid = tid
        self.args = {"name": thread_name}


@dataclass
class ProcessLabelEvent(MetadataEvent):
    "Metadata that associates a label with a process"
    name = "process_labels"

    def __init__(self, pid: int, label: str) -> None:
        self.pid = pid
        self.args = {"labels": label}


@dataclass
class ProcessSortIndexEvent(MetadataEvent):
    """
    Metadata that associates a user-visible pid with a sort index, specifying
    the order in which processes are listed in the viewer
    """

    name = "process_sort_index"

    def __init__(self, pid: int, index: int) -> None:
        self.pid = pid
        self.args = {"sort_index": index}


@dataclass
class ThreadSortIndexEvent(MetadataEvent):
    """
    Metadata that associates a user-visible pid with a sort index, specifying
    the order in which processes are listed in the viewer
    """

    name = "thread_sort_index"

    def __init__(self, pid: int, tid: int, index: int) -> None:
        self.pid = pid
        self.tid = tid
        self.args = {"sort_index": index}
