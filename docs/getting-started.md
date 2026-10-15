# Getting started

This page takes a robot from nothing to a first saved mission.

## Requirements

- Linux with systemd (the watchdog runs as a system service).
- ROS 2 Jazzy (Ubuntu 24.04), sourced in the shell you install from. Other
  distros aren't supported yet.
- Python 3.10 or newer, in a root-owned location such as `/usr/bin/python3`:
  the watchdog service runs as root, with the interpreter `setup` runs under.
- `sudo` rights for the one-time setup.
- Optional: Docker, if the robot runs software in containers. Without it,
  container snapshots are skipped.

## 1. Install the package

```bash
git clone https://github.com/mlisi1/ros_fairy.git
cd ros_fairy
./install.sh            # add --dev for pytest, rocrate, ruff and mypy
```

| Option | |
| --- | --- |
| `--dev` | Also install pytest, rocrate, ruff and mypy. |
| `--with-deps` | Install the missing optional tools (step 6) without asking. |
| `--no-deps` | Don't install them, and don't ask. |

`install.sh` is a thin wrapper around `pip install`:

1. It removes any stale `build/` and `*.egg-info` in the checkout. pip would
   otherwise reuse an old `build/` and resurrect deleted modules. If an
   earlier root install left them root-owned, it uses `sudo` to remove them.
2. It runs `python3 -m pip install --no-user .`. Without `--no-user`, pip can
   quietly install into `~/.local` when it can't write system-wide, and the
   watchdog service can't use a copy there.
3. If that fails because the system Python is managed by the OS (PEP 668) or
   isn't writable, it retries as root with `--break-system-packages`, asking
   for your password through `sudo`.
4. If the filesystem is read-only, it stops and explains: sudo won't help.
5. If an older copy in your home directory (`~/.local`) would still be the one
   your shell uses, it says so and how to remove it.
6. It lists the optional tools the recording assistant uses to describe the
   robot that are missing, and asks "Install them now? [Y/n]" (Enter means
   yes). Without them those details are left out of every mission:

   | Tool | Package | Records |
   | --- | --- | --- |
   | `lsusb` | `usbutils` | USB devices and their serial numbers |
   | `lspci` | `pciutils` | PCI devices (GPUs, network cards) |
   | `v4l2-ctl` | `v4l-utils` | cameras |
   | `udevadm` | `udev` | device details and the udev rules applied |
   | `setpriv`, `dmesg` | `util-linux` | replaying udev rules safely; kernel messages about devices |
   | `rocrate` | Python package | the full RO-Crate check in `verify` |

   The apt packages are installed with `apt-get` (refreshing the package lists
   if needed), `rocrate` with pip like ros_fairy itself. With no terminal to
   ask on, it skips them and says so. Docker is never installed: whether the
   robot runs containers is your choice. `ros2 fairy doctor` lists whatever is
   still missing.

If `ros2` isn't on your `PATH`, the script only prints a reminder: ROS is
needed to *use* ros_fairy, not to install it.

### Or build it in a colcon workspace

The package is `ament_python`, so this works as well:

```bash
cd ~/ros2_ws/src && git clone https://github.com/mlisi1/ros_fairy.git
cd ~/ros2_ws && colcon build --packages-select ros_fairy
source install/setup.bash
```

colcon doesn't install Python dependencies. Install them with rosdep
(`rosdep install --from-paths src --ignore-src`) or pip: `pydantic>=2.5`,
`rich>=13`, `PyYAML>=6`, `inotify_simple>=1.3`, `mcap`.

> **The service can't use a workspace in your home directory.** It runs as
> root and only loads code from directories root owns. `setup` checks this and
> stops if the service couldn't load ros_fairy; install it with `./install.sh`
> in that case.

## 2. Set up the robot

Run as your normal user, in a shell where ROS 2 is sourced **and the robot's
nodes are running**:

```bash
ros2 fairy setup
```

Setup refuses to continue if `ROS_DISTRO` isn't set or no ROS nodes are
visible. Installing a watchdog that can't see the robot would only produce
empty missions.

**If the robot's ROS runs in Docker**, the host shell may not have the robot's
DDS settings (they are set in the container). Either export them in the shell
first (`export ROS_DOMAIN_ID=7`), or take them from the running container:

```bash
ros2 fairy setup --from-container navstack
```

ROS itself (the distro and overlays the watchdog runs `ros2` with) still
comes from the host shell; see [Robots whose ROS runs in
Docker](#robots-whose-ros-runs-in-docker) for what is taken and what isn't.

It then asks a few questions (existing answers are offered as defaults):

- **Robot:** name, platform (make and model), serial number or asset tag.
- **Owner:** organisation and contact email.
- **Sensors (optional):** for each one, an id (`gps0`), a type (`gps`, `lidar`,
  `camera`, `imu`, `sonar`, `other`), make and model, the topic it publishes
  on, an optional TF frame and an optional calibration file. Live topics are
  listed to choose from.
- **Where to keep missions:** `/var/ros-fairy` unless you choose another
  folder, e.g. on a bigger disk (`--var-dir DIR` in a script). Missions that
  already exist are moved with [`move_data`](commands.md#move_data) instead.
- **Operators:** the other accounts that will record missions (names separated
  by spaces). You are added anyway. In a script, give them with
  `--operator USER` (repeatable) instead.

After a review screen, the only step that needs root asks for your password
through `sudo`. It:

1. writes `/etc/ros-fairy/robot_identity.yaml`
   ([format](configuration.md#robot_identityyaml));
2. records the data folder in `/etc/ros-fairy/paths.yaml` if it isn't
   `/var/ros-fairy`, then creates the `ros-fairy` system group and the
   directories in the data folder, owned by that group;
3. adds **you** (the account that ran `sudo`) and the operators you named to
   the `ros-fairy` group;
4. writes `/etc/ros-fairy/watchdog.env` from your shell's ROS environment;
5. installs, enables and starts `ros-fairy-watchdog.service`, then waits up to
   10 s for it to come up.

> **The new group only applies to new logins.** Either log out and back in, or
> keep working in the same terminal by starting a shell that already has it:
>
> ```bash
> newgrp ros-fairy        # this terminal only; other terminals still need a new login
> ```
>
> To add operators later: `ros2 fairy add_operator <user>`
> ([details](commands.md#add_operator)).

### Changing the robot's configuration later

Re-run `ros2 fairy setup` whenever the robot's sensors change. It offers your
existing answers as defaults, so you only retype what changed.

When only the robot's ROS environment changes (distro, overlay,
`ROS_DOMAIN_ID`, RMW, DDS profile), run this from a shell that has the new one
instead:

```bash
ros2 fairy setup --refresh-env
```

It asks nothing: it shows what changes, updates the open mission's settings if
there is one, re-captures `watchdog.env`, restarts the watchdog, and ends with
`doctor`'s checks of the service. `ros2 fairy doctor` tells you when it's
needed ("Background service's ROS environment is out of date").

#### Robots whose ROS runs in Docker

When the stack runs in containers, its DDS settings usually change there (a
compose `environment:` entry, a `-e`, the image's `ENV`), not in the host
shell. `doctor` reads the running containers that have `ROS_DISTRO` in their
environment and compares their discovery settings with the watchdog's:

```
! A ROS container is on a different ROS partition
  service → container: navstack: ROS_DOMAIN_ID: 0 → 7
  if the container is right, run `ros2 fairy setup --refresh-env --from-container navstack`
```

That command takes `ROS_DOMAIN_ID`, `RMW_IMPLEMENTATION`,
`ROS_AUTOMATIC_DISCOVERY_RANGE`, `ROS_LOCALHOST_ONLY` and `ROS_STATIC_PEERS`
from the container (a setting the container leaves unset is unset for the
watchdog too), and everything else from the host shell, then does what
`--refresh-env` does.

Afterwards a host shell still on the old domain is the odd one out, and
`doctor` says so instead of asking for another refresh ("This shell is on a
different ROS partition than the robot", with the `export` that fixes it). To
record from that shell, export the robot's settings in it first:
`mission_record` records on the shell's domain, and `mission_start` and
`mission_record` hand the shell's discovery settings to the watchdog for that
mission.

What it can't see:

- **Variables set inside the container's own start-up**: an `export` in the
  entrypoint script, a `.bashrc` or a sourced file. `docker inspect` only knows
  the container's configuration. Put DDS settings in the compose file's
  `environment:` (or `env_file:`) so both ROS F.A.I.R.y and other tools can
  see them, or export them in the host shell and use plain `--refresh-env`.
- **Containers without `ROS_DISTRO` in their configuration.** The official ROS
  images set it; a hand-made image that only sources ROS in a script isn't
  recognised as a ROS container.
- **DDS profile files** (`CYCLONEDDS_URI`, `FASTRTPS_DEFAULT_PROFILES_FILE`,
  `FASTDDS_DEFAULT_PROFILES_FILE`). They name paths inside the container, so
  they are neither compared nor copied; `--from-container` warns when the
  container uses one, and the watchdog keeps the host shell's.
- **Containers on their own Docker network.** The host can't discover their
  nodes by multicast, whatever the domain. `doctor` warns; run them with
  `--network host` (compose: `network_mode: host`) or configure DDS to reach
  them.
- **Docker access.** The account running `doctor` must be allowed to use Docker
  (the `docker` group); otherwise the check is skipped and says why.
- **Defaults are Jazzy's.** An unset setting is compared as its Jazzy default:
  domain `0`, `rmw_fastrtps_cpp`, `SUBNET`, localhost-only off.

For small changes (a sensor's topic, a calibration file, the `recording:`
section), you can also edit the file directly and check it:

```bash
sudo nano /etc/ros-fairy/robot_identity.yaml
ros2 fairy doctor       # reports any mistake in the file, in plain language
```

The format is described in [Configuration](configuration.md#robot_identityyaml).
Changes apply from the next recording; no restart is needed.

## 3. Check that the robot is ready

```bash
ros2 fairy doctor
```

`doctor` runs a dozen read-only checks: identity, watchdog alive and up to date,
ROS reachable, service environment matching your shell and the robot's ROS
containers, last harvest, clock
synchronisation, MCAP support, disk space, Docker and incomplete saves. It ends
with **READY**, **READY (with warnings)** or **NOT READY**, with a hint for every
problem. See [Troubleshooting](troubleshooting.md).

## 4. Record a first mission

```bash
ros2 fairy mission_start    # five questions
ros2 fairy mission_record   # Ctrl-C to stop; a plain ros2 bag record or the
                            # rosbag2 recorder node works too: the watchdog detects them
ros2 fairy mission_close    # review, then save
ros2 fairy list             # it's there
```

The [operator guide](operator-guide.md) walks through each step.

## Uninstall

```bash
./uninstall.sh              # -y to skip the confirmation
```

It stops and removes the service, then deletes `/etc/ros-fairy`, the spool,
`watchdog.state`, the ROS log directory and the `ros-fairy` group, and
uninstalls the Python package. It re-runs itself under `sudo` when needed.

It **keeps your saved missions**: `/var/ros-fairy/archive` and
`/var/ros-fairy/index.db` are left in place, and their location is printed at
the end. Delete them by hand if you really want them gone.
