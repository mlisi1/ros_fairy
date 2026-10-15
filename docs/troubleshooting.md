# Troubleshooting

Start with `ros2 fairy doctor`: every problem it finds comes with a hint. For
anything unexpected, re-run the failing command with `--debug` and look at the
watchdog's log:

```bash
journalctl -u ros-fairy-watchdog -n 200
cat /var/ros-fairy/spool/watchdog.log      # this mission's log, also archived
```

## Setup and installation

**`setup` says ROS isn't visible.**
Setup only installs the watchdog when it can see the robot. Source ROS 2 in the
shell you run it from, start the robot's nodes, and check that `ros2 node list`
shows them. Running as root? Use `--ros-setup /opt/ros/<distro>/setup.bash` if
several distros are installed.

**"Recording assistant is not running".**
`systemctl status ros-fairy-watchdog` and `journalctl -u ros-fairy-watchdog`
show why. If `doctor` also says "Background service can't load ros_fairy",
it is only installed in a home directory, where the service (running as root)
won't load it from: install it with `./install.sh` (see
[Install the package](getting-started.md#1-install-the-package)) and re-run
`ros2 fairy setup`.

**"Watchdog runs different code than this command".**
ros_fairy was updated but the service still runs the old code:
`sudo systemctl restart ros-fairy-watchdog`.

**"... paths.yaml says where the missions are kept, but ...".**
`/etc/ros-fairy/paths.yaml` was edited by hand and can't be used. Every command
stops rather than use another folder. Put back a line such as
`var_dir: /data/ros-fairy` (an absolute path), or set it again with
`ros2 fairy setup --var-dir <folder>`.

**`doctor`: "This shell uses a different data folder".**
`ROS_FAIRY_VAR_DIR` is set in your shell, so its commands work on other
missions than the watchdog records. `unset ROS_FAIRY_VAR_DIR`; to move the
missions for everyone, use `ros2 fairy move_data`.

**`doctor`: "Missions left behind in /var/ros-fairy".**
`paths.yaml` was pointed elsewhere by hand, leaving missions in the old place,
where they aren't listed. Move them into the new folder's `archive/` and run
`ros2 fairy reindex`, or delete them.

**"Your account can't record missions on this robot yet", or permission denied
on the spool, archive or index.**
Your account isn't in the `ros-fairy` group. Someone with `sudo` adds it with
`ros2 fairy add_operator <your username>`; then log out and back in.

**"You were added to the 'ros-fairy' group after this terminal was opened".**
Group membership only applies to new logins. Log out and back in, or run
`newgrp ros-fairy` in that terminal. Check with `id -nG`.

## Capture

**Context captured shows ✗ for the ROS graph.**
The watchdog can't see the robot's nodes. It retries every 60 s while recording.
Usually its DDS settings differ from the robot's: `doctor` says "Background
service's ROS environment is out of date" and lists what differs from your
shell. Starting the mission with `mission_start` in the robot's shell passes
the discovery settings on for that mission (see
[session.env](configuration.md#sessionenv)). If the robot's environment changed
for good, run `ros2 fairy setup --refresh-env` from a shell that has it.

A ROS overlay in a home directory isn't visible to the service (it runs as
root, and drops paths root doesn't own). Package lists and the URDF may be
incomplete in that case.

**The robot description is "absent".**
Nothing publishes `/robot_description` or `/tf_static` (namespaced versions count
too). That's normal on robots without a URDF. If it says "timeout", there are
publishers but nothing arrived within the time allowed. The watchdog retries
twice per recording.

**Hardware or udev is "partial".**
A tool it uses is missing (`ros2 fairy doctor` says which, and how to install
it; or re-run `./install.sh --with-deps`), `lsusb -v` or `dmesg` were refused,
or the time budget ran out. If `setpriv` isn't installed, the "which rules
applied" trace is skipped (it is never run as root).

**`doctor`: "A ROS container is on a different ROS partition".**
The robot's container has another `ROS_DOMAIN_ID`, RMW or discovery setting
than the watchdog. If the container is right, run
`ros2 fairy setup --refresh-env --from-container <name>`. If the container
sets more in its start-up script than in its configuration, `doctor` can't see
it: see [Robots whose ROS runs in Docker](getting-started.md#robots-whose-ros-runs-in-docker).

**`doctor`: "This shell is on a different ROS partition than the robot".**
The watchdog matches the robot's container; this shell doesn't. Nothing needs
refreshing: run the `export` in the hint to work with the robot from this
shell (including `mission_record`).

**`doctor`: "A ROS container can't be discovered from the host".**
It runs on its own Docker network. Run it with `--network host` (compose:
`network_mode: host`), or configure DDS (static peers, a discovery server) so
the host can reach it.

**`doctor`: "ROS containers not checked".**
The account running `doctor` may not use Docker. Run it from an account in the
`docker` group to include the check.

**A `ros2 bag record` I didn't want ended up in the mission.**
The watchdog attaches any recording it sees on the machine. Take it out with
`ros2 fairy drop <#>`, with the number `mission_status` shows for it (it stays
on disk, since ros_fairy didn't make it).
Start throw-away recordings with `ROS_FAIRY_IGNORE=1` so they aren't attached
in the first place.

**A recording made inside a container wasn't picked up.**
The container writes to a folder the host can't see (its private overlay, not
a bind mount). Write to a mounted volume instead, or copy the bag out and run
`ros2 fairy adopt <folder>`.

**A very short recording made elsewhere was missed.**
Recorders are found by scanning `/proc` once per second. One that starts and
stops between two scans can't be seen. Attach it with `adopt`.

## Recording and closing

**"Disk space is very low".**
Less than 1 GiB is free for the spool. Save or export older missions and delete
them, or free space elsewhere on that filesystem.

**"System clock is not synchronised".**
Check `timedatectl`. Recordings made with a bad clock have timestamps near 1970
and play back badly; `ros2 fairy repair` can make them playable afterwards, but
the real fix is NTP (or a GPS/PTP time source) before recording.

**`mission_close` keeps waiting.**
The watchdog is finishing a capture (up to about four minutes). If it never
finishes, check the journal: the harvest is abandoned after 240 s and the
mission can be closed with what was captured.

**The mission is graded "poor".**
The review lists the reasons. The most common is that no ROS context was captured
(see above). Saving anyway is a deliberate choice: you can still save, or keep it
in the spool while you investigate.

**The review says a recording "wasn't checked for gaps or low data rates".**
It was recorded with `--compression-mode file`, so its files are compressed as a
whole and their timing can't be read. It is still saved and checksummed. To get
the checks, record without file compression; per-message compression
(`--compression-mode message`) and MCAP's own chunk compression are fine.

**"Someone else is saving or reviewing a mission".**
Another `mission_close` is running on this robot. Only one save runs at a time.

**A save was interrupted (crash, power cut, Ctrl-C).**
Run `ros2 fairy mission_close` again: it offers to finish the save. Never delete
anything in `archive/.staging/` by hand. If it reports a save as *stuck*, the
staging folder still holds the data; look at it before removing anything.

## After saving

**`list` is empty or wrong, or `verify` says the mission isn't in the index.**
Run `ros2 fairy reindex`. The index is only a cache of the archive.

**`verify` fails.**
A file is missing or has changed since saving. If the archive was copied from
elsewhere, copy it again and check the bundle's `.sha256` first. `verify` can't
repair anything: it only tells you what's wrong.

**A recording won't play (`ros2 bag play` stalls).**
It was most likely recorded with an unsynchronised clock. Run
`ros2 fairy repair <mission>`, which writes playable copies to
`./<name>_repaired/` and leaves the originals alone. It works on MCAP and
sqlite3 recordings, but not on ones compressed as whole files.
