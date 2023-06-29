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

# This module runs golden tests, that validate the output against a set of
# known-good JSON outputs.
#
# This is NOT run automatically by the CI, since the data files can get pretty
# big. But it's here in case you want to use it, when doing a refactor or
# something.


import json
import multiprocessing
import os
import subprocess
import unittest
from typing import Optional, Tuple


# Run a single test, comparing against provided JSON output. We spawn a process
# each time, since we want the parallelism, and the code under test uses global
# vairables, so we might as well.
def run_test(files: Tuple[str, str]) -> Optional[str]:
    json_file_path, input_file_path = files
    print("test starting:", input_file_path)

    # Load the JSON file
    with open(json_file_path) as f:
        expected_output = json.load(f)

    # Generate the output
    p = subprocess.Popen(
        ["python", ".", input_file_path, "/dev/stdout"],
        stdout=subprocess.PIPE,
    )
    stdout, _stderr = p.communicate()
    actual_output = json.loads(stdout)

    # Do the comparison
    with open(json_file_path) as f:
        expected_output = json.load(f)
    print("test done:", input_file_path)
    if actual_output != expected_output:
        return f"{input_file_path} output != {json_file_path}"
    else:
        return None


class Tests(unittest.TestCase):
    # For each test data, spawn a test to compare its output with the known-good
    # JSON.
    def test_output_matches_json(self) -> None:
        test_data_dir = "test_data"

        # Iterate over the files in the test data directory
        files = []
        for filename in os.listdir(test_data_dir):
            if filename.endswith(".json"):
                json_file_path = f"{test_data_dir}/{filename}"
                input_file_path = json_file_path[:-5] + ".xz"
                files.append((json_file_path, input_file_path))

        with multiprocessing.Pool(multiprocessing.cpu_count()) as p:
            results = p.map(run_test, files)

        # Assert that each test succeeded
        for result in results:
            self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
