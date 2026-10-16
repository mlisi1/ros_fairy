# What is captured

The operator supplies five answers. Everything else on this page is captured
automatically by the watchdog while a recording runs. It is written to
`spool/harvest.json`, then into the mission record and the archive. How the
capture runs is described in [The harvest pipeline](architecture.md#the-harvest-pipeline).

Every module reports a status, which `mission_status` shows live and the saved
mission keeps in `provenance.harvest_status`:

| Status | Meaning |
| --- | --- |
| `ok` | Captured. |
| `partial` | Captured with gaps (e.g. `lsusb -v` was denied, some parameters timed out). |
| `skipped` | Not used on this robot (no Docker, no hardware tools). |
| `absent` | Robot description only: nobody publishes a URDF or static transforms. |
| `timeout` | Robot description only: publishers exist but nothing arrived. |
| `failed` | Couldn't be captured. ROS failures are retried every 60 s while recording. |

## From the operator

| Field | Source |
| --- | --- |
| Operator, goal, place, environment, notes | the [briefing](operator-guide.md#1-start-the-mission-mission_start) |
| Mission ID (`m-<YYYYMMDD>-<HHMMSS>-<4 hex>`), creation time | generated |

The mission record tags every field with how it was obtained
(`provenance.field_confidence`): `user` for the briefing answers, `auto` for
everything else.

## Robot and owner

From [`robot_identity.yaml`](configuration.md#robot_identityyaml): robot name,
platform, serial number, owning organisation and contact, declared sensors,
calibration files (copied into the archive with their sha256) and the default
data license.

Each declared sensor also gets `detected_at_start`: whether its topic was seen
in the ROS graph when recording started (`true`, `false`, or unknown if ROS was
unreachable). This is corrected afterwards from what the recordings actually
contain.

## System

| Field | How |
| --- | --- |
| Hostname, kernel, CPU architecture | the operating system |
| ROS distro | the environment, or inferred from installed packages |
| Installed `ros-*` Debian packages and versions | `dpkg-query` |
| Clock synchronised? | `timedatectl` (NTP) |

## ROS graph

Captured through a single DDS participant (see
[One DDS participant for the whole ROS capture](architecture.md#one-dds-participant-for-the-whole-ros-capture)):

- **Nodes** and **topics** with their types.
- **Every node's parameters**, with their values. Parameters whose value
  couldn't be fetched in time are listed under `parameters_not_captured`, and
  the graph is marked incomplete.
- **Robot description:** the URDF from `/robot_description` (also archived as
  `harvest/robot_description.urdf`) and the static transforms from `/tf_static`,
  including namespaced ones. Conflicting static transforms are logged.
- **ROS packages** available on the host (`ros2 pkg list`).

## Software

- **Python environment** of the interpreter running ros_fairy: version,
  executable, virtualenv, every installed package with its version and
  installer, and whether it is an editable install. `pip freeze` is
  archived as `harvest/pip_freeze.txt`.
- **Docker:** for each running container, its name, image and image digest, and
  its Compose project with the Compose files (archived under `docker/compose/`).
  If the container has ROS, its `ros2 pkg list` and Python packages are captured
  too. The raw `docker inspect` output is archived as `docker/containers.json`.
  It includes each container's environment variables, so a password or token
  passed to a container that way ends up in the archive too: keep secrets in
  files or Docker secrets, not in `-e`/`environment:`.
- **ros_fairy itself:** version, git commit, `git describe`, branch, dirty flag,
  build time and a hash of its code, both for the watchdog that harvested and
  for the CLI that saved (see
  [Provenance of ros_fairy itself](architecture.md#provenance-of-ros_fairy-itself)).

## Hardware

- **Devices** from `lsusb`, `lspci`, `/dev/video*` with `v4l2-ctl`, and serial
  ports (`/dev/ttyUSB*`, `/dev/ttyACM*`, `/dev/serial/by-id`), with vendor,
  product, serial number, driver (the USB driver, such as `uvcvideo` or
  `ftdi_sio`, when udev knows it) and selected udev properties. Full
  `lsusb -v` and the USB-related `dmesg` lines are archived as text files.
- **USB management**, read from sysfs, for every USB device: speed, USB
  version, power draw, power management (autosuspend, runtime status, wakeup),
  quirks, drivers and interfaces, and the **FTDI `latency_timer`** of USB serial
  adapters. For every hub port: connect type, USB3 link power management,
  over-current count. Also the global `usbcore` parameters and the bound drivers'
  module parameters.

### udev rules

A robot's udev rules are often what makes a sensor work: fixed device names,
permissions, latency timers, disabled autosuspend. They are rarely written down
anywhere. ros_fairy records them in three ways:

1. **Which rule files are the robot's own.** A file is *default* if a Debian
   package ships it and it is unmodified (its md5 still matches dpkg's). Every
   other file is archived in full under `harvest/udev_rules/`, with the reason:
   `local` (`/etc/udev/rules.d`, `/run/udev/rules.d`), `modified` (a package
   file edited locally) or `unpackaged`. The record also notes when a local file
   masks or overrides a default one. Default files are only listed.
2. **Which rules applied to each USB device.** `udevadm test` replays the rules
   for every USB device and its children and names each matching `file:line`
   (`harvest/udev_trace.txt`). It runs as `nobody`, so replaying can never change
   the hardware.
3. **Whether they are in effect.** Every attribute a non-default rule sets is
   read back from the device. A rule that is installed but not in effect
   (the device was plugged in before the rule existed, or the write failed)
   is flagged: *"udev rule X should set Y to Z, but it is W"*.

## Recordings

For each bag: its source (`mission_record`, `detected` for a recording found
running elsewhere, or `adopted`), storage format, size, start, end, duration,
message count, and each topic's type, message count and average rate. The
archive adds a sha256 for every bag file.

### Recording health

Each recording is checked when it is finalised. Every problem becomes a warning
with a plain-language sentence, which is what the review and README show:

| Kind | Raised when |
| --- | --- |
| `never_published` | A declared sensor's topic recorded no messages. Also raised when a recording was cut off before rosbag2 could finish it. |
| `compressed_transport` | (Information only.) A declared camera was recorded only as `…/compressed`, `…/compressedDepth` or `…/theora`. |
| `compressed_file` | (Information only.) The recording is compressed as whole files (`.mcap.zstd`, `.db3.zstd`, rosbag2's `--compression-mode file`). It is saved and checksummed like any other, but its timing can't be read, so gap, rate and clock checks are skipped. |
| `gap` | A topic went silent: at the start, in the middle or at the end. |
| `low_rate` | A topic's average rate was under 25% of its busiest 10-second window. |
| `truncated` | A storage file was cut off. |
| `unreliable_clock` | Most message timestamps are implausible (e.g. 1970), so the timing can't be trusted. Gap and rate checks are skipped. |
| `clock_step` | The clock jumped backwards by more than 1 s, or forwards by more than 60 s, during the recording. |

Gap detection adapts to each topic's own rhythm, so slow or bursty topics don't
raise false alarms. A silence counts as a gap only if it exceeds 1 s, **and**
5× the topic's median interval, **and** 2× its usual long pauses (the 95th
percentile of intervals). Topics with fewer than 10 messages are skipped
unless they belong to a declared sensor. Command and event topics (`/cmd_vel`,
`/joy`, `/rosout`, action feedback, …) are not checked at all. Repeated gaps on
one topic are summarised ("dropped out 7 times, for 2 minutes in total") so
one flaky channel can't swamp the review.

**Timing is recovered from the messages, not from `metadata.yaml`.** rosbag2
takes the bag's start from its earliest timestamp, so a single message
stamped in 1970 makes a ten-minute bag claim to start in 1970 and last decades.
ros_fairy reads the receive time of every message (sqlite3 and MCAP, without
decompressing indexed MCAP chunks), discards implausible stamps, stitches over
clock steps, and computes the real start and duration. If most stamps are
implausible, the timing is reported as unknown rather than guessed. Such
recordings can be made playable with [`repair`](commands.md#repair).

A recording compressed as whole files (`.mcap.zstd`, `.db3.zstd`) can't be
read message by message. Its start and duration come from `metadata.yaml`, and
it gets a `compressed_file` note instead of the gap, rate and clock checks.

## Data quality

At `mission_close` the mission is graded:

| Level | When |
| --- | --- |
| **poor** | No ROS context was captured (graph failed or empty), **or** no recording has usable timing, **or** the robot isn't set up. |
| **degraded** | Some recordings have unusable timing, **or** a declared sensor produced no data in any recording, **or** some declared sensors weren't detected. |
| **ok** | None of the above. |

The verdict is stored as `provenance.data_quality`, shown by `list` and usable
as a filter (`list --quality poor`).
