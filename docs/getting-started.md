# Getting started

This page takes a robot from nothing to a first saved mission.

## Requirements

- Linux with systemd (the watchdog runs as a system service).
- ROS 2 Humble, Jazzy or newer, sourced in the shell you install from.
- Python 3.10 or newer, as the system `python3`: the watchdog service runs
  `/usr/bin/python3`.
- `sudo` rights for the one-time setup.
- Optional: Docker, if the robot runs software in containers. Without it,
  container snapshots are skipped.

## 1. Install the package

```bash
git clone https://github.com/mlisi1/ros_fairy.git
cd ros_fairy
./install.sh            # add --dev for pytest, rocrate, ruff and mypy
```

`install.sh` is a thin wrapper around `pip install`:

1. It removes any stale `build/` and `*.egg-info` in the checkout. pip would
   otherwise reuse an old `build/` and resurrect deleted modules. If an
   earlier root install left them root-owned, it uses `sudo` to remove them.
2. It runs `python3 -m pip install .`.
3. If that fails because the system Python is managed by the OS (PEP 668) or
   isn't writable, it retries as root with `--break-system-packages`, asking
   for your password through `sudo`.
4. If the filesystem is read-only, it stops and explains: sudo won't help.

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

> **The service can't see a workspace in your home directory.** The watchdog
> runs as root and drops any `PYTHONPATH` entry that root doesn't own (see
> [watchdog.env](configuration.md#watchdogenv)). For a robot in the field,
> prefer the system-wide `./install.sh`.

## 2. Set up the robot

Run as your normal user, in a shell where ROS 2 is sourced **and the robot's
nodes are running**:

```bash
ros2 fairy setup
```

Setup refuses to continue if `ROS_DISTRO` isn't set or no ROS nodes are
visible. Installing a watchdog that can't see the robot would only produce
empty missions.

It then asks a few questions (existing answers are offered as defaults):

- **Robot:** name, platform (make and model), serial number or asset tag.
- **Owner:** organisation and contact email.
- **Sensors (optional):** for each one, an id (`gps0`), a type (`gps`, `lidar`,
  `camera`, `imu`, `sonar`, `other`), make and model, the topic it publishes
  on, an optional TF frame and an optional calibration file. Live topics are
  listed to choose from.

After a review screen, the only step that needs root asks for your password
through `sudo`. It:

1. writes `/etc/ros-fairy/robot_identity.yaml`
   ([format](configuration.md#robot_identityyaml));
2. creates the `ros-fairy` system group and the directories under
   `/var/ros-fairy`, owned by that group;
3. adds **you** (the account that ran `sudo`) to the `ros-fairy` group;
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
> Every other operator account must be added by hand:
> `sudo usermod -aG ros-fairy <user>`.

### Changing the robot's configuration later

Re-run `ros2 fairy setup` whenever the robot's sensors change, or after
changing its ROS environment (distro, overlay, domain, RMW). It offers your
existing answers as defaults, so you only retype what changed.

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
ROS reachable, service environment matching your shell, last harvest, clock
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
