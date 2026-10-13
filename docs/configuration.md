# Configuration

ros_fairy has very little to configure. Everything lives in two places, and
both are written by `ros2 fairy setup`.

## Paths

| Path | What it holds |
| --- | --- |
| `/etc/ros-fairy/robot_identity.yaml` | The robot, its owner and its sensors ([below](#robot_identityyaml)). |
| `/etc/ros-fairy/watchdog.env` | The ROS environment the watchdog service runs with ([below](#watchdogenv)). |
| `/var/ros-fairy/spool/` | The mission in progress. |
| `/var/ros-fairy/spool/bags/` | Recordings made with `mission_record`. |
| `/var/ros-fairy/spool/mission_context.json` | The briefing answers. |
| `/var/ros-fairy/spool/harvest.json` | Everything the watchdog captured ([what-is-captured.md](what-is-captured.md)). |
| `/var/ros-fairy/spool/session.env` | The DDS settings of the shell that started the mission ([below](#sessionenv)). |
| `/var/ros-fairy/spool/watchdog.log` | The watchdog's log for this mission (archived with it). |
| `/var/ros-fairy/archive/` | Saved missions, one RO-Crate folder each ([archive-format.md](archive-format.md)). |
| `/var/ros-fairy/archive/.staging/` | Saves in progress. **Never delete anything here by hand**: run `mission_close`. |
| `/var/ros-fairy/index.db` | The mission list (SQLite). A cache: `ros2 fairy reindex` rebuilds it from the archive. |
| `/var/ros-fairy/watchdog.state` | The watchdog's live status. |
| `/var/ros-fairy/log/` | ROS log directory for the watchdog, used when root has no writable `~/.ros/log`. |

### Moving the directories

| Variable | Default | Moves |
| --- | --- | --- |
| `ROS_FAIRY_CONFIG_DIR` | `/etc/ros-fairy` | the identity file and `watchdog.env` |
| `ROS_FAIRY_VAR_DIR` | `/var/ros-fairy` | the spool, archive, index, state and log |

Set them for every operator shell **and** for the service. The unit file
hard-codes `EnvironmentFile=-/etc/ros-fairy/watchdog.env`, so a relocated
config directory also needs a systemd drop-in
(`sudo systemctl edit ros-fairy-watchdog`). `setup` and `uninstall.sh` carry
both variables through `sudo`.

### Permissions

- The `ros-fairy` group owns `/var/ros-fairy` and everything below it.
  Directories are `2775` (setgid, so new files inherit the group), the index is
  `0664`, and saved crates are made group-writable.
- Every operator account must be in that group. Without it, commands fail with
  a message telling you to run `sudo usermod -aG ros-fairy <user>` (then log in
  again).
- `/etc/ros-fairy` and `robot_identity.yaml` are world-readable and
  root-writable.
- The watchdog runs as **root** with group `ros-fairy` and `UMask=0002`.

## robot_identity.yaml

Created by `ros2 fairy setup`. You can also edit it by hand: it is read again at
every harvest, so no restart is needed.

```yaml
robot:
  name: Heron-02
  platform: Clearpath Heron USV
  serial_number: H02-2031-XK
owner:
  organization: Example Marine Robotics Lab
  contact_email: fleet@example.org
  default_license: https://spdx.org/licenses/CC-BY-4.0  # optional
sensors:                          # optional
  - sensor_id: gps0
    type: gps
    make_model: u-blox ZED-F9P
    topic: /fix
    frame_id: gps_link            # optional
    calibration: gps0_cal         # optional, names a calibrations[] entry
calibrations:                     # optional
  - name: gps0_cal
    source_path: /opt/cal/gps0.yaml
    format: yaml                  # optional
recording:                        # optional, used by mission_record
  topics: [/fix, /depth]          # default: record all topics
  storage: mcap                   # default: rosbag2's default
```

| Section | Required fields | Notes |
| --- | --- | --- |
| `robot` | `name`, `platform`, `serial_number` | |
| `owner` | `organization`, `contact_email` | `default_license` becomes the RO-Crate's `license`; without it the crate has none. |
| `sensors[]` | `sensor_id`, `type`, `make_model`, `topic` | `sensor_id` must be unique. Topics are made absolute. |
| `calibrations[]` | `name`, `source_path` | The file is copied into each saved mission with its sha256. |
| `recording` | none | `topics` may be a list or a single string. |

All required fields must be non-empty strings. A broken file is reported in
plain language by `doctor` and in the harvest status. Missions recorded while
it is broken are graded **poor**: "the robot isn't set up".

**Why declare sensors?** A declared sensor is checked by name. A sensor that
published nothing during a recording produces a *never published* warning
("GPS (gps0) produced no data at all during this recording."). Its gaps are itemised rather than
summarised. A camera that only published `…/compressed` is recognised
as such. Undeclared topics only get the generic checks. See
[what-is-captured.md](what-is-captured.md#recording-health).

## watchdog.env

A systemd `EnvironmentFile` (`KEY=value` lines) holding the ROS environment
of the shell that ran `setup`:

- every `ROS_*`, `AMENT_*`, `RMW_*` and `COLCON_*` variable;
- `PATH`, `LD_LIBRARY_PATH`, `PYTHONPATH`, `CMAKE_PREFIX_PATH`;
- `CYCLONEDDS_URI`, `FASTRTPS_DEFAULT_PROFILES_FILE`,
  `FASTDDS_DEFAULT_PROFILES_FILE`.

`ROS_FAIRY_*` variables are never stored.

Because the service runs as root, `LD_PRELOAD` is dropped, and every
`PATH`/`PYTHONPATH`/`LD_LIBRARY_PATH` entry that isn't owned by root or is
writable by others is removed: both when the file is written and again when the
watchdog starts. A ROS install under `/opt/ros` is unaffected. An overlay in
someone's home directory is not visible to the service.

Re-run `ros2 fairy setup` to regenerate the file. `doctor` warns when your
shell's `ROS_DOMAIN_ID` or `RMW_IMPLEMENTATION` no longer matches it.

## session.env

Written to the spool by `mission_start` and `mission_record` from the operator's
shell, so that the harvest sees the same ROS graph even if the domain or RMW has
changed since setup. The file is group-writable, so the watchdog takes only
these keys from it, and never loader paths:

```
ROS_DOMAIN_ID  RMW_IMPLEMENTATION  ROS_LOCALHOST_ONLY
ROS_AUTOMATIC_DISCOVERY_RANGE  ROS_STATIC_PEERS
```

## ROS_FAIRY_IGNORE

The watchdog notices **any** `ros2 bag record` on the machine, not just
`mission_record` (see [architecture.md](architecture.md#recordings-made-outside-ros_fairy)),
and attaches it to the current mission. To keep a recording out, start it with
the variable set:

```bash
ROS_FAIRY_IGNORE=1 ros2 bag record -o scratch /chatter
```

Any value other than empty or `0` counts.

## The systemd unit

`ros-fairy-watchdog.service` is installed into `/etc/systemd/system/` by
`setup`:

| Setting | Why |
| --- | --- |
| `After=local-fs.target` only | It must keep working when ROS, the network or Docker are down. It only needs the spool. |
| `EnvironmentFile=-/etc/ros-fairy/watchdog.env` | The ROS environment. The `-` lets it start without one. |
| `ExecStart=/usr/bin/python3 -m ros_fairy.watchdog.watchdog` | The system interpreter. |
| `Group=ros-fairy`, `UMask=0002` | Operators can read and clean up what it writes. |
| `Restart=always`, `RestartSec=5` | It recovers from crashes on its own ([crash recovery](architecture.md#crash-and-power-loss-recovery)). |
| `Nice=10` | It yields CPU to the robot's own software. |

Useful commands:

```bash
systemctl status ros-fairy-watchdog
journalctl -u ros-fairy-watchdog -f
sudo systemctl restart ros-fairy-watchdog    # after updating ros_fairy
```
