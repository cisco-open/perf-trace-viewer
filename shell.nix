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

# If you use direnv and nix, this provides a reproducible development
# environment. If not, you can just ignore it.
with import <nixpkgs> { };
mkShell {
  nativeBuildInputs = [
    python3
    python3Packages.mypy
    python3Packages.isort
    python3Packages.pylint
    black
    shellcheck
    xz
  ];
}
