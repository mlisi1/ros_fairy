# Configuration

ros_fairy has very little to configure. Everything lives in two places, and
both are written by `ros2 fairy setup`.

## Paths

| Path | What it holds |
| --- | --- |
| `/etc/ros-fairy/robot_identity.yaml` | The robot, its owner and its sensors ([below](#robot_identityyaml)). |
| `/etc/ros-fairy/watchdog.env` | The ROS environment the watchdog service runs with ([below](#watchdogenv)). |
| `/etc/ros-fairy/paths.yaml` | Where the data folder is, when it isn't `/var/ros-fairy` ([below](#moving-the-data-folder)). |
| `/var/ros-fairy/spool/` | The mission in progress. |
| `/var/ros-fairy/spool/bags/` | Recordings made with `mission_record`. |
| `/var/ros-fairy/spool/mission_context.json` | The briefing answers. |
| `/var/ros-fairy/spool/harvest.json` | Everything the watchdog captured ([What is captured](what-is-captured.md)). |
| `/var/ros-fairy/spool/session.env` | The DDS settings of the shell that started the mission ([below](#sessionenv)). |
| `/var/ros-fairy/spool/watchdog.log` | The watchdog's log for this mission (archived with it). |
| `/var/ros-fairy/archive/` | Saved missions, one RO-Crate folder each ([Archive format](archive-format.md)). |
| `/var/ros-fairy/archive/.staging/` | Saves in progress. **Never delete anything here by hand**: run `mission_close`. |
| `/var/ros-fairy/index.db` | The mission list (SQLite). A cache: `ros2 fairy reindex` rebuilds it from the archive. |
| `/var/ros-fairy/watchdog.state` | The watchdog's live status. |
| `/var/ros-fairy/log/` | ROS log directory for the watchdog, used when root has no writable `~/.ros/log`. |

The rows below `/etc` are in the **data folder**, `/var/ros-fairy` unless it
was moved.

### Moving the data folder

To keep recordings and saved missions on another disk, choose the folder at
setup (the wizard asks, or `ros2 fairy setup --var-dir /data/ros-fairy`), or
move what is already there with
[`ros2 fairy move_data`](commands.md#move_data). Either one writes the place
into `/etc/ros-fairy/paths.yaml`:

```yaml
var_dir: /data/ros-fairy
```

Every process reads this file: operator shells, the watchdog service,
`sudo`, cron jobs and launch files. A change applies at once, with no new
login. Don't edit it to move missions that already exist (they would stay
behind, unlisted): use `move_data`. A file that can't be used (no `var_dir`,
a relative path, broken YAML) stops every command with a plain message rather
than quietly using `/var/ros-fairy`.

For tests and one-off experiments, two environment variables still override
everything:

| Variable | Default | Moves |
| --- | --- | --- |
| `ROS_FAIRY_VAR_DIR` | `paths.yaml`, else `/var/ros-fairy` | the data folder, for this shell only |
| `ROS_FAIRY_CONFIG_DIR` | `/etc/ros-fairy` | the identity file, `watchdog.env` and `paths.yaml` |

The service never sees a shell's variables, so a shell with
`ROS_FAIRY_VAR_DIR` set works on different missions than the watchdog
(`doctor` warns about it). A moved config folder also needs a systemd drop-in
(`sudo systemctl edit ros-fairy-watchdog`), because the unit file hard-codes
`EnvironmentFile=-/etc/ros-fairy/watchdog.env`.

### Permissions

- The `ros-fairy` group owns the data folder and everything below it.
  Directories are `2775` (setgid, so new files inherit the group), the index is
  `0664`, and saved crates are made group-writable.
- Every operator account must be in that group: `setup` adds the ones you
  name, and `ros2 fairy add_operator <user>` adds more. Without it, the verbs
  that record or save stop before asking anything and say so.
- `/etc/ros-fairy` and `robot_identity.yaml` are world-readable and
  root-writable.
- The watchdog runs as **root** with group `ros-fairy` and `UMask=0002`.

## robot_identity.yaml

Created by `ros2 fairy setup`, and changed one part at a time with its
[flags](commands.md#changing-one-part-of-the-configuration) (`--show` prints
it). You can also edit it by hand. It is read again at every harvest, so no
restart is needed.

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
| `recording` | none | `topics` may be a list or a single string. A topic selection or `--storage` given to `mission_record` replaces these for that recording. |

All required fields must be non-empty strings. A broken file is reported in
plain language by `doctor` and in the harvest status. Missions recorded while
it is broken are graded **poor**: "the robot isn't set up".

**Why declare sensors?** A declared sensor is checked by name. A sensor that
published nothing during a recording produces a *never published* warning
("GPS (gps0) produced no data at all during this recording."). Its gaps are itemised rather than
summarised. A camera that only published `…/compressed` is recognised
as such. Undeclared topics only get the generic checks. See
[Recording health](what-is-captured.md#recording-health).

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

When the robot's ROS environment changes, regenerate it from a shell that has
the new one with `ros2 fairy setup --refresh-env` (no questions; it also
restarts the watchdog). `doctor` warns when the file no longer matches your
shell: the distro, any DDS discovery setting (`ROS_DOMAIN_ID`,
`RMW_IMPLEMENTATION`, `ROS_AUTOMATIC_DISCOVERY_RANGE`, `ROS_LOCALHOST_ONLY`,
`ROS_STATIC_PEERS`), a DDS profile file, or a root-owned overlay. An unset
setting counts as its Jazzy default (domain `0`, `rmw_fastrtps_cpp`,
`SUBNET`), so stating a default isn't a difference.

For a robot whose ROS runs in Docker, `doctor` also compares the discovery
settings of the running ROS containers, and
`ros2 fairy setup --refresh-env --from-container NAME` takes them from a
container instead of the shell. See [Robots whose ROS runs in
Docker](getting-started.md#robots-whose-ros-runs-in-docker), including what
it can't see.

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
`mission_record` (see [Recordings made outside ros_fairy](architecture.md#recordings-made-outside-ros_fairy)),
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
| `ExecStart=/usr/bin/python3 -m ros_fairy.watchdog.watchdog` | The interpreter `setup` ran under, if root owns it and it can load ros_fairy as the service would; otherwise `/usr/bin/python3`. `setup` stops if neither can. |
| `Group=ros-fairy`, `UMask=0002` | Operators can read and clean up what it writes. |
| `Restart=always`, `RestartSec=5` | It recovers from crashes on its own ([crash recovery](architecture.md#crash-and-power-loss-recovery)). |
| `Nice=10` | It yields CPU to the robot's own software. |

Useful commands:

```bash
systemctl status ros-fairy-watchdog
journalctl -u ros-fairy-watchdog -f
sudo systemctl restart ros-fairy-watchdog    # after updating ros_fairy
```
