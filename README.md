<p align="center">
  <img src="ros_fairy_logo.jpg" alt="ROS F.A.I.R.y" width="420">
</p>

<p align="center">
  <strong>Make ROS 2 field mission data FAIR-compliant with zero friction:</strong><br>
  automatic context capture, plain-language briefings, RO-Crate archives.
</p>

---

## What it is

`ros_fairy` is a ROS 2 CLI extension (`ros2 fairy ...`) plus a background watchdog
service. An operator answers five questions before a run; everything else —
robot identity, ROS graph and node descriptions, sensors seen publishing,
Python environment, Docker images, hardware devices, system info — is harvested
automatically and written alongside the bags as an
[RO-Crate](https://www.researchobject.org/ro-crate/) archive that can be
verified, diffed, and shared as a single checksummed file.

## Install

```bash
git clone https://github.com/gdl-res/ros_fairy.git
cd ros_fairy
./install.sh                   # pip-installs the package (asks for sudo
                                #   only if it actually needs root)
ros2 fairy setup                # identity file, directories, watchdog service
                                #   — no sudo up front; prompts for your
                                #   password only when it writes /etc
ros2 fairy doctor               # confirm the robot is ready to capture
```

Prefer a colcon workspace instead (`ament_python`, ROS 2 Humble/Jazzy or
newer)? That works too — `install.sh` is just `pip install`, nothing it does
is colcon-specific:

```bash
cd ~/ros2_ws/src && git clone https://github.com/gdl-res/ros_fairy.git
cd ~/ros2_ws && colcon build --packages-select ros_fairy
source install/setup.bash
ros2 fairy setup
ros2 fairy doctor
```

### Uninstall

```bash
./uninstall.sh
```

Removes the watchdog service, `/etc/ros-fairy`, the `ros-fairy` group, and the
Python package — a clean reverse of `install.sh` + `setup`. It does **not**
touch your saved missions (`/var/ros-fairy/archive`, `/var/ros-fairy/index.db`);
it just reminds you where they are.

## A mission, start to finish

```bash
ros2 fairy mission_start       # five questions describing the run
ros2 fairy mission_record      # wraps `ros2 bag record` with safety checks
ros2 fairy mission_close       # review the briefing, then save or discard
ros2 fairy list                # missions saved on this robot
ros2 fairy export 1            # bundle the newest mission + sha256 sidecar
```

## Commands

| Verb | What it does |
| --- | --- |
| `setup` | One-time robot setup: identity file, directories, watchdog service |
| `doctor` | Check that this robot is ready to capture a mission |
| `mission_start` | Answer five quick questions to describe the mission |
| `mission_record` | Record mission data (wraps `ros2 bag record`) |
| `mission_status` | Show what the recording assistant is doing right now |
| `mission_close` | Review the finished mission and save it or discard it |
| `list` | List the missions saved on this robot |
| `diff` | Compare two missions and show what changed |
| `verify` | Check that a saved archive is complete and unmodified |
| `export` | Package a mission — or `--all` unexported ones, or `--today`'s — into portable files |
| `repair` | Make unplayable (bad-clock) recordings playable |
| `adopt` | Ingest a bag recorded outside `mission_record` |
| `reindex` | Rebuild the mission list from the archives on disk |

## Where things live

| Path | Contents |
| --- | --- |
| `/etc/ros-fairy` | `robot_identity.yaml`, watchdog environment |
| `/var/ros-fairy/spool` | live harvest, session env, in-progress bags |
| `/var/ros-fairy/archive` | saved mission crates and the mission index |

Both roots are overridable with `ROS_FAIRY_CONFIG_DIR` and `ROS_FAIRY_VAR_DIR`.

## Development

```bash
pip install -e '.[dev]'
pytest                        # unit + integration, no ROS required
pytest -m ros                 # live smoke tests, on a sourced ROS 2 box
ruff check . && mypy ros_fairy
```

## License

Apache-2.0 — see [LICENSE](LICENSE).
