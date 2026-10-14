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

**Permission denied on the spool, archive or index.**
Your account isn't in the `ros-fairy` group, or you haven't logged in again since
being added: `sudo usermod -aG ros-fairy $USER`, then log out and back in. Check
with `id -nG`.

## Capture

**Context captured shows ✗ for the ROS graph.**
The watchdog can't see the robot's nodes. It retries every 60 s while recording.
Usually its DDS settings differ from the robot's: `doctor` warns when your
shell's `ROS_DOMAIN_ID`/`RMW_IMPLEMENTATION` differ from `watchdog.env`. Starting
the mission with `mission_start` in the robot's shell passes the right
settings on (see [session.env](configuration.md#sessionenv)). If the domain
or RMW changed permanently, re-run `ros2 fairy setup`.

A ROS overlay in a home directory isn't visible to the service (it runs as
root, and drops paths root doesn't own). Package lists and the URDF may be
incomplete in that case.

**The robot description is "absent".**
Nothing publishes `/robot_description` or `/tf_static` (namespaced versions count
too). That's normal on robots without a URDF. If it says "timeout", there are
publishers but nothing arrived within the time allowed. The watchdog retries
twice per recording.

**Hardware or udev is "partial".**
`lsusb -v` or `dmesg` were refused, or the time budget ran out. If `setpriv`
isn't installed, the "which rules applied" trace is skipped (it is never run
as root).

**A `ros2 bag record` I didn't want ended up in the mission.**
The watchdog attaches any recording it sees on the machine. Start throw-away
recordings with `ROS_FAIRY_IGNORE=1`. There is no command to detach a single recording.
`mission_abort` drops the whole mission; recordings made outside ros_fairy stay
on disk.

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
