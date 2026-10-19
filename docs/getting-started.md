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
   yes); when none is missing it says so and doesn't ask. Without them those
   details are left out of every mission:

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

colcon doesn't install Python dependencies. Install them system-wide with
pip, as root, so the watchdog service can load them too:

```bash
sudo python3 -m pip install --break-system-packages \
    "pydantic>=2.5" "rich>=13" "PyYAML>=6" "inotify_simple>=1.3" mcap zstandard
```

Don't use `rosdep install` for ros_fairy: on Jazzy it can't resolve all of
its dependencies, and it installs Ubuntu's `python3-pydantic`, version 1,
with which no `ros2 fairy` command loads. If that package is already
installed, add `--ignore-installed typing_extensions` to the command above.

> **The service can't use a workspace in your home directory.** It runs as
> root and only loads code from directories only root can change.
> `ros2 fairy setup` checks this before asking anything and stops. Use such a
> workspace for development, and install with `./install.sh` (from the
> checkout, e.g. `~/ros2_ws/src/ros_fairy`) on a robot that records.

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

To change one thing (a sensor added, removed or moved to another topic, a
calibration file, what `mission_record` records), use `setup`'s flags:

```bash
ros2 fairy setup --show                                   # what is set now
ros2 fairy setup --set-sensor gps0 calibration=/opt/cal/gps0_2026.yaml
ros2 fairy setup --add-sensor id=imu0,type=imu,model="Xsens MTi-630",topic=/imu
```

Each shows the change as a diff and asks before writing it. The previous file
is kept. The flags are listed in
[`setup`](commands.md#changing-one-part-of-the-configuration). Saved missions
keep the configuration they were recorded with.

To go through every question again, re-run `ros2 fairy setup`: it offers your
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
compose `environment:` entry, a `-e`, the image's `ENV`, an `export` in the
entrypoint script), not in the host shell. `doctor` compares the running ROS
containers' discovery settings with the watchdog's:

```
! A ROS container is on a different ROS partition
  service → container: navstack (as its ROS processes run): ROS_DOMAIN_ID: 0 → 7
  if the container is right, run `ros2 fairy setup --refresh-env --from-container navstack`
```

Where the settings come from:

- **What the container's ROS processes really run with.** Every 10 seconds
  the watchdog (which runs as root) reads the environment of the processes
  that have ROS in it. It writes their discovery settings, and nothing else
  from their environment, to `ros_processes.json` in the data folder. That
  includes what start-up scripts export, and containers with no `ROS_DISTRO`
  in their configuration (ROS sourced by a script). "(as its ROS processes
  run)" marks these.
- **Otherwise, the container's configuration**, from `docker inspect`: right
  after the container starts, before the watchdog's next look, or while the
  watchdog isn't running.

When a container's nodes don't all agree (one script exports a domain,
another doesn't), `doctor` says so ("A ROS container's nodes are on different
ROS partitions") and `--from-container` refuses to pick one.

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

- **Containers whose nodes haven't started yet.** Until a process with ROS in
  its environment runs, a container without `ROS_DISTRO` in its configuration
  isn't recognised as a ROS container.
- **DDS profile files** (`CYCLONEDDS_URI`, `FASTRTPS_DEFAULT_PROFILES_FILE`,
  `FASTDDS_DEFAULT_PROFILES_FILE`). They name paths inside the container, so
  they are neither compared nor copied; `--from-container` warns when the
  container uses one, and the watchdog keeps the host shell's.
- **Containers on their own Docker network.** The host can't discover their
  nodes by multicast, whatever the domain. `doctor` warns; run them with
  `--network host` (compose: `network_mode: host`) or configure DDS to reach
  them.
- **Docker access.** An account that may not use Docker (not in the `docker`
  group) still gets the comparison from the watchdog's findings, without the
  containers' names: "the container whose first process is 4242". With no
  findings either, the check is skipped and says why. `--from-container`
  needs Docker.
- **Defaults are Jazzy's.** An unset setting is compared as its Jazzy default:
  domain `0`, `rmw_fastrtps_cpp`, `SUBNET`, localhost-only off.

You can also edit the file directly, and check it:

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

`doctor` runs sixteen read-only checks: identity, where missions are kept,
watchdog alive, up to date and able to load ros_fairy, ROS reachable, service
environment matching your shell and the robot's ROS containers, last harvest,
clock synchronisation, MCAP support, the harvest's optional tools, disk space,
Docker and incomplete saves. It ends
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
uninstalls the Python package (from the interpreter the service ran with). It
re-runs itself under `sudo` when needed.

It **keeps your saved missions**: `/var/ros-fairy/archive` and
`/var/ros-fairy/index.db` are left in place, and their location is printed at
the end. Delete them by hand if you really want them gone.
