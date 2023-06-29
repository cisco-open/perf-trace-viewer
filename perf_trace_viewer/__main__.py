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

# Convert linux `perf sched` data to Chrome Trace Event format.
#
# Run as in: perf_trace_viewer <input-file> <output-file>

import sys

if sys.version_info < (3, 10):
    print("ERROR: python 3.10 or later required", file=sys.stderr)
    sys.exit(1)

import json
import logging
import tarfile
from argparse import ArgumentParser, Namespace
from typing import IO, Iterable, Mapping, NoReturn, Sequence

from engine import process_perf_data
from parse_mdata import parse_mdata


# Main entrypoint
def main() -> None:
    # Setup
    logging.basicConfig(level=logging.INFO)
    opts = get_opts()

    # Do the processing
    data = process_file(opts)

    # Save the result
    with open(opts.output_filename, "w", encoding="utf-8") as f:
        json.dump(data, f)


# Process command-line options
def get_opts() -> Namespace:
    parser = ArgumentParser(
        description="Convert collected `perf sched` data to Chrome Trace Event format"
    )
    parser.add_argument("input_filename", help="perf data input")
    parser.add_argument("output_filename", help="JSON output file")
    parser.add_argument(
        "-s",
        "--skip",
        type=float,
        default=0.0,
        help="Number of seconds of data to skip",
    )
    parser.add_argument(
        "-d",
        "--duration",
        type=float,
        default=0.0,
        help="Number of seconds of data to process",
    )
    parser.add_argument(
        "-w",
        "--wait",
        type=float,
        default=3.0,
        help="Threshold (in ms) for tasks to appear in the waiting track",
    )
    return parser.parse_args()


# Run the engine on either compressed test data, or a real perf data file
def process_file(opts: Namespace) -> Sequence[Mapping[str, object]]:
    # Open the input file as a tarfile - the library will handle compression.
    # There should be exactly two files, in this order:
    #   - perf-mdata.txt (containing metadata)
    #   - perf.data.txt (containing the output of perf script)
    # We fully read in the mdata, then start streaming the perf data.
    tar = tarfile.open(opts.input_filename)
    mdata, proc_info, result = None, None, None
    for member in tar.getmembers():
        if member.name == "perf-mdata.txt":
            mdata, proc_info = parse_mdata(extract(tar, member))
        elif member.name == "perf.data.txt":
            if mdata is None or proc_info is None:
                die("ERROR: perf-mdata.txt not early enough - possible corruption?")
            lines = stream(extract(tar, member))
            # Send the stream to the engine, along with everything else it needs
            result = process_perf_data(
                lines, mdata, proc_info, opts.skip, opts.duration, opts.wait
            )

    if result is None:
        die("ERROR: perf.data.txt missing from tarfile - possible corruption?")
    else:
        return result


# Stream an IO object line by line
def stream(f: IO[bytes]) -> Iterable[str]:
    for line in f:
        yield line.decode("utf-8")


# Extract a member of a tarfile
def extract(tar: tarfile.TarFile, member: tarfile.TarInfo) -> IO[bytes]:
    file = tar.extractfile(member)
    if file is None:
        die(f"ERROR: Can't extract {member.name} - possible corruption?")
    else:
        return file


# Exit with an error message
def die(*msgs: str) -> NoReturn:
    for msg in msgs:
        print(msg, file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
