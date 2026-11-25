# HiPS: code to run the experiments

Code for the paper *Small Language Model Planning for Multi-Cell Wi-Fi SLA Control: A Two-Timescale
Plan-and-Solve Study in MAC-Level Emulation* (E. Mulyana and T. Juhana). It contains the emulation testbed
(Mininet-WiFi with a patched wmediumd), every controller (HiPS with its Meta-Planner, prompts, JSON schemas,
guardrail and rules-only planner; the Flat-Agent; the Heuristic and the load balancers), the configuration of
every experiment and the settings of the two machines the paper used.

The data of every run, the figure source data and a description of every column are on Zenodo:
[10.5281/zenodo.23934509](https://doi.org/10.5281/zenodo.23934509) (all versions). They are not repeated here.

## Contents

```
testbed/
  run-campaign.sh          runs a queue of experiments, resumes after interruptions, retries failed runs
  process-result.py        collects the runs' outputs into results.xlsx (called by run-campaign.sh)
  testbed.yaml             the one file to edit on a new machine (CPU sets, Ollama server)
  testbed.e1.yaml          CPU sets of the paper's emulation host (merged over testbed.yaml on a host named e1)
  requirements.txt         Python packages for a native install
  common/                  emulator, traffic, telemetry, KPIs, tool server, controllers (common/agents/)
  common/agents/prompts/   system prompts of the Meta-Planner, the Flat-Agent and the SLM execution agents
  common/agents/schemas/   JSON schemas of plans (sub-goals) and actions
  experiment-N/            config.yaml (parameters, seeds, sub-experiments) and run.sh for Experiments 1-9 and 11
  install/                 native installation of the emulation host and of an Ollama host, host tuning
  install/wmediumd-ojsdn.patch   the wmediumd patch as one diff against upstream commit 63e9fc9
  docker/                  container image of the emulation host and its wrapper (the paper's runs used it)
bench/
  intent_bench.py, rescore_intent.py, run_intent_bench.sh   Experiment 10b (offline, no emulator)
```

## Machines and settings used in the paper

| | Emulation host | GPU node |
|---|---|---|
| Hardware | Intel Core i7-13700 (8 P-cores = CPUs 0-15, 8 E-cores = CPUs 16-23), 62 GB | NVIDIA GeForce RTX 4090 (24 GB), AMD Ryzen 7 255 |
| Software | Ubuntu 24.04.5, kernel 7.0.0-38, Docker; the image of `testbed/docker/` | Ollama 0.40.6 in Docker, port 11434 |
| Settings | `processor.max_cstate=1 intel_idle.max_cstate=1`, CPU governor `performance`; CPU sets of `testbed.e1.yaml` (orchestrator, telemetry and agents on 16-23; emulation on 0-15); patched medium (4 MiB netlink buffer, contention per channel, 100-ms queue bound) | `OLLAMA_MAX_LOADED_MODELS=2`, `OLLAMA_NUM_PARALLEL=1`, `OLLAMA_KEEP_ALIVE=30m`, `OLLAMA_FLASH_ATTENTION=1`, `OLLAMA_KV_CACHE_TYPE=q8_0`; CPU governor `performance` for Experiment 11 |
| Used for | every emulated run (Experiments 1-8, 10a and 11) | every language-model call; Experiments 9 and 10b |

Per request: `temperature` 0, the run's seed, `think` false, `num_predict` 512 (execution agents 320),
`keep_alive` -1. Models (Ollama tags): `qwen3.5:4b` (default planner), `qwen3.5:2b`, `qwen3.5:9b`,
`phi3:mini` (Experiment 5a), `qwen3.5:0.8b` (SLM execution agents, 5c), `gemma4:26b-a4b-it-q4_K_M` and
`phi4` (5f and 10b). With `OLLAMA_MAX_LOADED_MODELS=1`, every switch between the planner and the execution-agent
model of 5c reloads a model, which changes the latencies.

Image versions: Mininet-WiFi 90c2d1d (headless patch), wmediumd (https://github.com/ramonfontes/wmediumd, commit
63e9fc9) with the patches of `install/wmediumd_ojsdn.py`, hostapd and
wpa_supplicant 2.11, iperf3 3.18, irtt 0.9.0, Open vSwitch 3.3.9, Python packages of `docker/requirements.lock`.

## Setup

1. GPU node: `sudo bash testbed/install/install-llm-host.sh` (or any Ollama 0.40.6 server), then
   `bash testbed/install/pull-models.sh --host http://<gpu-node>:11434`; add the Stage 2 planners with
   `--models "gemma4:26b-a4b-it-q4_K_M phi4"`.
2. Emulation host: in `testbed/testbed.yaml`, set `llm.host` to the Ollama server and the CPU sets
   `cores.orch` / `cores.emu` to whole physical cores of your machine (or add `testbed.<hostname>.yaml`).
   `bash testbed/install/check-net.sh` checks that the emulated subnet 10.0.0.0/24 does not collide with your
   network (`OJSDN_LAN_RANGES` = your LAN's CIDRs).
3. Either the container (as in the paper):
   ```
   bash testbed/docker/ojsdn-docker.sh build
   sudo bash testbed/docker/ojsdn-docker.sh host-prep --cstates     # reboot afterwards; host-undo reverts it
   bash testbed/docker/ojsdn-docker.sh check
   bash testbed/docker/ojsdn-docker.sh smoke
   ```
   or a native install: `sudo bash testbed/install/install-emu-host.sh --no-ollama`, then
   `sudo bash testbed/install/tune-host.sh`.

## Running the experiments

Each experiment is a queue item: the experiment number, optionally with a sub-experiment letter.

```
bash testbed/docker/ojsdn-docker.sh campaign --queue "2 3"     # container; status|logs|stop to follow it
sudo -E testbed/run-campaign.sh --queue "2 3"                   # native install
```

Every run writes its folder under `testbed/output/experiment-N/` (configuration, per-window KPIs, events,
tool and agent logs, summary row) and appends to `summary_expN.csv`; finished runs are skipped when a queue is
started again, so a campaign can be stopped and resumed. `results.xlsx` collects all of them.

| Paper | Queue items | Notes |
|---|---|---|
| Experiment 1 | `1a 1b 1c 1d 1e` | radio calibration, tool latency, telemetry overhead, model latency; 1e = 1a with data-path and position checks |
| Experiments 2-4 | `2 3 4` | mobility, hotspot, scale |
| Experiment 5 | `5a 5b 5c 5d 5f` | ablation; 5f = planners of other families (Stage 2); 5e (CPU inference) was not run |
| Experiment 6 | `6a 6b 6c 6d` | client background scanning |
| Experiment 7 | `7p 7m` | overload: pilot, then main runs |
| Experiment 8 | `8a 8b 8c` | rules-only planner at scale |
| Experiment 9 | `9` | offline prompt benchmark (no emulator, no root) |
| Experiment 10a | `6e 6f 6g 6h 6i` | coverage pressure, configured in `experiment-6/config.yaml`: pilot, main at exponent 3.7, 3.4, sustained offload, Heuristic at 10 s |
| Experiment 10b | `bench/run_intent_bench.sh OUTDIR` | offline intent-change benchmark of the four planner models, then the rescoring; `PYTHON=` a Python with `testbed/requirements.txt`, Ollama server from `llm.host` |
| Experiment 11 | `11m 11p 11t 11c 11f` | Stage 3: medium check, pilot, load-balancer tuning, confirmatory test, main floor |

Experiment 11 sets two values from its own preliminary runs, by rules fixed before the runs: the path-loss
exponent from `11p` (3.7 unless Unmanaged already reaches a telemedicine SLA of 50 % there) and the tuned load
balancer from `11t` (the variant with the highest mean telemedicine SLA). `experiment-11/config.yaml` holds the
values these rules gave in the paper (exponent 3.7, floor -78 dBm); to repeat the procedure, run `11m 11p 11t`,
apply the rules to the new results, edit the marked lines, then run `11c 11f`. Seeds 11-20 of `11c` are used
by no other experiment.

## License

The code is under the MIT License:

```
MIT License

Copyright (c) 2026 Eueung Mulyana and Tutun Juhana

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

wmediumd is not included: the image build and `install/patch-wmediumd.sh` fetch it from
https://github.com/ramonfontes/wmediumd at commit 63e9fc995cfb260065c7a0d4c2b14f715f224436 and apply
`install/wmediumd_ojsdn.py`. `testbed/install/wmediumd-ojsdn.patch` gives the same changes as one diff against
that commit; like wmediumd, it is under the GNU General Public License version 2 or later, not the MIT License.
