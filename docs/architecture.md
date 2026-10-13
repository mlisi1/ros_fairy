# Architecture

ros_fairy has two halves that share one directory tree:

- **The CLI** (`ros2 fairy <verb>`), run by the operator. It asks the briefing
  questions, starts recordings, and makes the save/discard decision.
- **The watchdog** (`ros-fairy-watchdog.service`), always running. It notices
  recordings, harvests the robot's context while they run, and finalises the
  bag records when they stop. It **never archives anything**: saving is the
  operator's decision at `mission_close`.

They communicate only through files under `/var/ros-fairy`: mostly the spool,
plus the watchdog's status file (see [configuration.md](configuration.md#paths)).

![Architecture overview: the operator's commands and the watchdog service only share files under /var/ros-fairy; mission_close saves each mission into the archive and the index.](assets/diagrams/architecture-overview.svg#only-light)
![Architecture overview: the operator's commands and the watchdog service only share files under /var/ros-fairy; mission_close saves each mission into the archive and the index.](assets/diagrams/architecture-overview-dark.svg#only-dark)

## The watchdog

Source: `ros_fairy/watchdog/watchdog.py`, `ros_fairy/watchdog/recorder_scan.py`.

### Startup

1. Records its own build (see [Provenance of ros_fairy itself](#provenance-of-ros_fairy-itself)).
2. Logs to stdout (the systemd journal) and mirrors INFO and above to
   `spool/watchdog.log`. That file is capped at 1 MiB (the oldest half is dropped)
   and is archived with the mission as `harvest/watchdog.log`.
3. **Sanitises its environment.** The service runs as root, so `LD_PRELOAD` is
   dropped, and any `PATH`, `PYTHONPATH` or `LD_LIBRARY_PATH` entry that is not
   root-owned, or that others can write, is removed (with a warning).
4. If `$HOME/.ros/log` isn't writable and `ROS_LOG_DIR` is unset, it uses
   `/var/ros-fairy/log` so rcl can log.
5. Runs [crash recovery](#crash-and-power-loss-recovery), then the main loop.
   The main loop never exits on an error: it logs the error and carries on.

### State machine

| State | Meaning |
| --- | --- |
| `IDLE` | Nothing is being recorded. |
| `RECORDING` | A bag is being written; a harvest runs in the background. |
| `FINALISING` | The bag has stopped; waiting for the harvest to finish before writing the bag record. |

**IDLE → RECORDING** when any of these happens:
- a `.mcap` or `.db3` file appears in a new folder under `spool/bags/` (inotify);
- a folder that already holds a storage file is moved into the spool;
- the `/proc` scan finds a recorder writing somewhere else (see
  [Recordings made outside ros_fairy](#recordings-made-outside-ros_fairy));
- after a restart, a bag is found that is still being written.

On entering RECORDING the watchdog starts the
[harvest pipeline](#the-harvest-pipeline) on a background thread.

**RECORDING → FINALISING** when either:
- the bag's `metadata.yaml` is written and the recorder process has exited; or
- nothing has been written to the bag for **30 s** (`BAG_INACTIVITY_S`) and the
  recorder process is gone. If the recorder is still alive, the bag is not
  finalised: MCAP buffers whole chunks, so a quiet bag isn't necessarily a
  finished one.

**FINALISING → IDLE** once the harvest has finished, or after at most **240 s**
(`HARVEST_WAIT_S`). In that case the bag is finalised with whatever context is
already on disk, and the late harvest result is discarded. The bag record is
then appended to `harvest.json`.

**A second bag while busy** is queued and picked up as soon as the first is
finalised.

If inotify overflows, the watchdog re-scans the whole spool, so no bag is
missed.

### Harvest retries

If ROS couldn't be reached (`ros_graph` is `failed` or `timeout`), the full
harvest is retried every **60 s** (`ROS_RETRY_INTERVAL_S`) for as long as the
recording runs. If only the robot description (URDF, `/tf_static`) was
missing, it is retried at most **twice** per recording
(`MAX_DESCRIPTION_RETRIES`), keeping the graph already captured.

When captures are merged, a failed ROS capture never overwrites an earlier
successful one.

### Recordings made outside ros_fairy

inotify only sees the spool, but people also run a plain `ros2 bag record` in
another terminal, the bare recorder node
(`ros2 run rosbag2_transport recorder --ros-args -p storage.uri:=…`), or a
recorder inside a Docker container. So every **1 s**
(`FOREIGN_SCAN_INTERVAL_S`; about 6 ms per scan) the watchdog reads `/proc` for
rosbag2 recorder processes. This is pure `/proc` reading: it needs no ROS
environment, so it works whatever the ROS distro.

- **Which processes:** any whose arguments contain `bag record`, or whose
  executable is `rosbag2_transport/recorder`.
- **Where it writes**, in this order: `-o`/`--output`; `-p storage.uri:=…`
  (which beats `--params-file`); the `rosbag2_*` folder in its working
  directory that it holds an open storage file in; the newest active
  `rosbag2_*` folder there.
- **Containers:** a recorder in another mount namespace is inspected through
  `/proc/<pid>/root`. Its output folder is translated to the matching host path
  by comparing its mount table with the host's (`/proc/<pid>/mountinfo`), and
  the translation is verified by inode. If the host has no mount for that
  folder (for example the container's private overlay), the watchdog warns
  once and suggests `ros2 fairy adopt`.
- **Opting out:** a recorder started with `ROS_FAIRY_IGNORE` set in its
  environment (to anything except an empty string or `0`) is ignored.
- **Short recordings:** a recorder seen once before it wrote anything is still
  caught when it exits. One whose whole life fits between two scans is not.

Bags found this way get `source: detected` in the mission record (spool bags get
`mission_record`, `ros2 fairy adopt` gives `adopted`).

### DDS environment: `watchdog.env` and `session.env`

The harvest has to see the same ROS graph as the operator's shell. The
watchdog's environment comes from `/etc/ros-fairy/watchdog.env`, a snapshot
taken at `ros2 fairy setup` and loaded by systemd. The operator's domain or RMW
may have changed since then, so `mission_start` and `mission_record` also write
the live shell's settings to `spool/session.env`.

That file is group-writable, so it is **not trusted**. Only these discovery
keys are taken from it:

```
ROS_DOMAIN_ID  RMW_IMPLEMENTATION  ROS_LOCALHOST_ONLY
ROS_AUTOMATIC_DISCOVERY_RANGE  ROS_STATIC_PEERS
```

Loader paths (`PATH`, `LD_LIBRARY_PATH`, `PYTHONPATH`, `AMENT_PREFIX_PATH`) are
never taken from it. For a recording found by the `/proc` scan, the same keys
are read from the recorder process's own environment instead. A key the source
doesn't set falls back to the `watchdog.env` value.

### The status file

`/var/ros-fairy/watchdog.state` (JSON) is rewritten on every state change,
after every harvest and every **60 s** as a heartbeat. `mission_status`,
`doctor` and the other commands read it to tell whether the watchdog is alive
and what it is doing. It holds the state, the active bag, the time of the last
bag activity, the harvest status, the number of ROS nodes captured, the
watchdog's pid and its build/code id.

If the file can't be written, the watchdog carries on without it.

### Crash and power-loss recovery

At startup the watchdog checks every unfinalised folder in `spool/bags/`:

| Folder holds | What happens |
| --- | --- |
| `metadata.yaml` | Finalised offline ("finished while the assistant was not running"). |
| A storage file that is still being written (a live recorder, or modified in the last 30 s) | Recording is resumed; any others are queued. |
| A storage file nothing is writing any more | Finalised offline ("was cut off"). |

A bag cut off by a hard crash has no `metadata.yaml`. Its record is rebuilt by
reading the storage file directly, and it gets the warning *"The recording ended
unexpectedly and may be incomplete."* If the storage can't be read, file
modification times are used for its timing.

`ros2 fairy mission_close` also offers to finish or discard
[interrupted saves](archive-format.md#how-a-save-works).

## The harvest pipeline

`run_pipeline()` runs each module in order. It never raises: a module that
fails is recorded with its status, and the others still run. The result is
`spool/harvest.json`; its contents are described in
[what-is-captured.md](what-is-captured.md).

| # | Module | Statuses |
| --- | --- | --- |
| 1 | `robot_identity` | `ok`, `failed` |
| 2 | `system_info` | `ok`, `failed` |
| 3 | `python_env` | `ok`, `partial` |
| 4 | `hardware_devices` (includes udev/USB) | `ok`, `partial`, `skipped` |
| 5 | `ros_graph` | `ok`, `partial` (some parameters missing), `failed` |
| – | `ros_descriptions` (URDF and `/tf_static`, from the same capture) | `ok`, `absent` (nobody publishes them), `timeout`, `failed` |
| 6 | `docker_info` | `ok`, `skipped` (Docker not available) |

### One DDS participant for the whole ROS capture

`ros_fairy/harvest/ros_snapshot.py` runs as a short-lived child process
(`python -m ros_fairy.harvest.ros_snapshot`). A hang can be killed, and no rclpy
state lives in the watchdog. A single rclpy node:

1. waits for discovery to settle (2–8 s);
2. lists nodes and topics;
3. fetches **every node's parameters** with concurrent
   `list_parameters`/`get_parameters` service calls (20 s budget);
4. reads the latched `/robot_description` and `/tf_static` (including
   namespaced ones).

The whole child is killed after 51 s.

An earlier version ran one `ros2 param dump` per node. Each call is a separate
DDS participant, and on a robot already near CycloneDDS's per-host participant
limit the harvest failed outright. It could also starve the robot's own nodes
of participant slots. The snapshot therefore also sets
`ParticipantIndex=none` for its own participant, so it is found by multicast
and takes no slot. This affects only this process: the robot's DDS
configuration is not changed.

Hidden nodes (a name segment starting with `_`) and tf2's
`/transform_listener_impl_*` are skipped when fetching parameters.

### Privileges

- `udevadm test`, which replays udev rules to see which ones apply, runs as
  `nobody` through `setpriv`. Replaying rules as root could change the
  hardware. If `setpriv` is missing, that step is skipped.
- `lsusb -v` and `dmesg` may be restricted; the module is then `partial`.

## Provenance of ros_fairy itself

Every harvest and every saved mission records which ros_fairy produced it
(`ros_fairy/build_info.py`):

- **Installed package:** `setup.py` writes `ros_fairy/_build_info.py` at build
  time, recording the git commit, `git describe`, branch, whether the tree was
  dirty, and the build time. An installed package has no `.git`, so build time
  is the only chance to record these.
- **Source checkout:** the same details are read from git directly.
- **`code_id`:** a 12-character SHA-256 over all of ros_fairy's `.py` files.
  It changes when the code is edited after the build, which is how
  `mission_status` and `doctor` can tell that a running watchdog is out of
  date and needs a restart.

The build is recorded twice: the watchdog that harvested
(`provenance.harvested_by`) and the CLI that saved (`provenance.assembled_by`).
