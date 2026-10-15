# Operator guide

This guide is for the person running the robot in the field. You need four
commands for a normal mission; the rest is for when something goes wrong.

![A mission: mission_start (five questions), then mission_record (Ctrl-C once to stop), then mission_close (save or discard); afterwards list, export, verify and diff.](assets/diagrams/mission-flow.svg#only-light)
![A mission: mission_start (five questions), then mission_record (Ctrl-C once to stop), then mission_close (save or discard); afterwards list, export, verify and diff.](assets/diagrams/mission-flow-dark.svg#only-dark)

Check where things stand at any time with `ros2 fairy mission_status`.

## Before you go

```bash
ros2 fairy doctor
```

<!-- TODO: replace the placeholder with a real screenshot of `ros2 fairy doctor` -->
![Screenshot placeholder: the output of ros2 fairy doctor, to be added](assets/screenshots/doctor-placeholder.svg)

If it says **NOT READY**, fix what it lists first: each line has a hint. A
**READY (with warnings)** is fine to record with, but read the warnings. An
unsynchronised clock, for example, makes recordings hard to play back.

## 1. Start the mission: `mission_start`

```bash
ros2 fairy mission_start
```

Five questions, under two minutes:

| # | Question | |
| --- | --- | --- |
| 1 | What's your name? | required; defaults to the last operator |
| 2 | In one sentence, what is this mission trying to do? | required |
| 3 | Where are you? (place name) | required |
| 4 | What's the environment like? | optional |
| 5 | Anything else worth noting? | optional; you can edit it at the end |

These answers are what make the mission findable later. Everything else about
the robot is captured automatically.

`mission_start` won't start a new mission over unsaved data:

- **A recording is running:** stop it first.
- **There are recordings from the previous mission:** save or discard them with
  `mission_close` first.
- **An unfinished mission with no recordings:** it asks whether to replace it.
- **Recordings made while no mission was open** (someone ran `ros2 bag
  record`): it lists them and asks whether to include them.

Pressing Ctrl-C during the questions changes nothing.

**From a script or a launch file**, give the answers as options; nothing is
asked:

```bash
ros2 fairy mission_start --operator Ana --goal "Survey the north pier" \
    --location "North pier" --environment marine --yes
```

`--yes` replaces an unfinished mission that has no recordings (one with
recordings is never replaced). Add `--json` to get the new mission's ID. In a
launch file:

```python
from launch import LaunchDescription
from launch.actions import ExecuteProcess


def generate_launch_description():
    return LaunchDescription([ExecuteProcess(
        cmd=["ros2", "fairy", "mission_start", "--yes",
             "--operator", "Ana", "--goal", "Survey the north pier",
             "--location", "North pier"],
        output="screen")])
```

A missing required answer stops it (exit 2) with the option to add. See
[`mission_start`](commands.md#mission_start) for the details.

## 2. Record: `mission_record`

```bash
ros2 fairy mission_record
```

This runs `ros2 bag record` for you, into the spool, with some checks first:

- **Disk:** under 1 GiB free, it asks before recording (default: no).
- **Briefing:** if you skipped `mission_start`, it still records, and asks the
  questions at `mission_close`.
- **Clock:** if the system clock isn't synchronised, it asks before recording
  (default: no). Recordings made with a bad clock play back badly; see
  [repair](#unplayable-recordings-repair).
- **Watchdog:** if the background assistant isn't running, it warns and still
  records, but robot details won't be captured.

`--yes` (`-y`) answers yes to all of these.

**What gets recorded** comes from the robot's
[`recording:` settings](configuration.md#robot_identityyaml): specific topics
and a storage format, or every topic by default.

**Options for `ros2 bag record`** can be given to `mission_record` directly:

```bash
ros2 fairy mission_record --max-bag-duration 600        # a new file every 10 min
ros2 fairy mission_record /camera/image_raw /imu/data   # only these topics
ros2 fairy mission_record -h                            # what you can pass
```

Topics chosen there replace the robot's `recording:` topics for this
recording. `-o`/`--output` isn't allowed: the recording has to go where the
assistant looks. A mistyped option stops it before anything is recorded. See
[`mission_record`](commands.md#mission_record) for the details.

**To stop, press Ctrl-C once** and wait for "saved". Pressing it again prints
*"Still saving the recording — please wait…"*. A third press forces it to stop,
and the recording may then be cut off. A cut-off recording is still kept and
is read back from the bag itself when you save.

You can run `mission_record` several times in one mission: each run is another
recording in the same mission. `mission_status` lists them with a number each
(#1, #2, …), which [`drop`](#one-recording-too-many-drop) uses to take one out.

### Recording without `mission_record`

`mission_record` is a convenience, not a requirement: record however you
prefer. The assistant watches for any rosbag2 recording on the robot and
attaches it to the open mission.

| How you record | For example |
| --- | --- |
| A plain `ros2 bag record`, in any terminal and any folder | `ros2 bag record -o run1 /fix /imu/data` |
| The rosbag2 recorder node, e.g. started from a launch file | `ros2 run rosbag2_transport recorder --ros-args -p storage.uri:=run1` |
| Either of them inside a Docker container | write to a volume mounted from the host |

What changes compared with `mission_record`:

- **No pre-recording checks.** Disk space, clock and briefing aren't checked
  for you: run `ros2 fairy doctor` first.
- **Check that it was noticed.** While it records, `ros2 fairy mission_status`
  shows it under *Recording*.
- **The recording stays where you wrote it.** `mission_close` copies it into
  the archive and leaves the original in place, so you need room for both.
- **A few recordings can't be seen:** one that starts and stops within about a
  second, or one written inside a container to a folder the host has no access
  to. Attach those with [`adopt`](#recordings-made-elsewhere).
- **Keep a test recording out** of the mission by starting it with
  `ROS_FAIRY_IGNORE=1`, e.g. `ROS_FAIRY_IGNORE=1 ros2 bag record -o scratch /chatter`.

## 3. Close: `mission_close`

```bash
ros2 fairy mission_close
```

This is the one decision that matters. It:

1. waits for the assistant to finish capturing, if it is still busy (up to about
   four minutes, with a spinner);
2. asks any briefing questions that are still missing;
3. offers to update your notes ("Anything else worth noting?"), keeping the
   current ones as the default;
4. shows a **review**: the briefing, each recording with its length, size and
   health warnings, what was captured about the robot, and a **data-quality
   verdict**;
5. asks whether to **save**.

| Verdict | Meaning | Default answer |
| --- | --- | --- |
| **ok** | Nothing important is missing. | Save |
| **degraded** | Usable, with gaps: a sensor that never published, some recordings with unusable timing. | Save |
| **poor** | Core content is missing: no ROS context was captured, no usable recording timing, or the robot isn't set up. | Don't save |

If you choose not to save, it asks whether to **throw the recording away**. Say
no and nothing changes: the recording stays in the spool, and you can run
`mission_close` again later.

It also warns, without blocking, when the mission looks like a **duplicate**:
- the same operator saved a mission at a very similar place name within the
  last two hours, which is often a typo of the same outing; or
- the very same recording is already in a saved mission (by content).

Saving moves the recordings into the archive and builds the
[RO-Crate](archive-format.md). It is safe to interrupt: if the power goes or
you press Ctrl-C, the next `mission_close` offers to finish the save, and your
data is never in a half-written state.

For scripts: `ros2 fairy mission_close --yes --note "text"` saves without
asking. The briefing must already exist.

## After the mission

```bash
ros2 fairy list                       # latest 20 missions, newest last
ros2 fairy list --all --path          # every mission, with folders
ros2 fairy list --operator ana --since 2026-10-01 --quality poor
ros2 fairy list --search "sonar pier"  # goal or notes mention both words
ros2 fairy export 1                   # newest mission → ./<name>.zip + .sha256
ros2 fairy export --today -o /media/usb/
ros2 fairy verify 1                   # is it intact?
ros2 fairy diff                       # what changed between the last two missions?
```

Commands that take a mission accept **a number** (`1` = newest, `2` = the one
before), **a mission ID** (`m-20261002-082109-1a2b`, shown by `list`) or **an
archive folder path**.

`export` writes one uncompressed `.zip` (or `.tar` with `--format tar`) with a
`sha256sum`-compatible `.sha256` file next to it. The receiver can check the
transfer with `sha256sum -c <file>.sha256`, then run `ros2 fairy verify` on the
unpacked folder. `export --all` exports every mission not yet exported.

## When something goes wrong

### A mission that couldn't go ahead: `mission_abort`

```bash
ros2 fairy mission_abort
```

Abandons the open mission without saving. It asks twice: once to abort, and
again before deleting recordings. Recordings in the spool are deleted.
Recordings made outside ros_fairy stay where they are on disk.

### One recording too many: `drop`

A test run, a false start, a recording of the wrong topics: take it out and
keep the rest of the mission. `mission_status` numbers the recordings:

```
Recordings waiting  3 (1.2 GB) — run: ros2 fairy mission_close
                     #1  10:02:11, 12 minutes, 640 MB
                     #2  10:15:40, 40 seconds, 21 MB
                     #3  10:17:03, 10 minutes, 550 MB
```

```bash
ros2 fairy drop 2        # takes #2 out; #1 and #3 keep their numbers
ros2 fairy drop          # lists them and asks which
```

A recording made with `mission_record` is deleted (it asks first). One made
elsewhere is only taken out of the mission and stays where it is; `adopt`
attaches it again.

### Recordings made elsewhere

A plain `ros2 bag record` or the recorder node is normally noticed on its own
(see [Recording without `mission_record`](#recording-without-mission_record)),
and listed under *Recordings waiting* in `mission_status`. If it wasn't (for
example, the bag was copied over from another machine), attach it yourself:

```bash
ros2 fairy adopt /path/to/rosbag2_folder
```

It says which number the recording got (`Adopted run1 as #4`); `ros2 fairy drop
4` takes it out again. The folder stays where it is until you save.
`mission_close` then copies it into the archive and leaves the original
untouched.

### Unplayable recordings: `repair`

Recordings made while the clock was not synchronised often have most messages
stamped in 1970, and `ros2 bag play` stalls on them. `mission_close` flags them as
having unusable timing.

```bash
ros2 fairy repair 1                   # newest mission → ./<name>_repaired/
ros2 fairy repair /path/to/bag        # a single bag folder
```

This writes **new**, playable bags (MCAP or sqlite3, like the originals) and
never touches the originals. The new timing is evenly spaced and synthetic:
fine for looking at the data, not for timing-sensitive processing. A recording
compressed as a whole file (`.mcap.zstd`, `.db3.zstd`) can't be repaired.
`--duration` sets the playback length.

### Deleting saved missions: `mission_delete`

```bash
ros2 fairy mission_delete m-20261002-082109-1a2b
ros2 fairy mission_delete --today     # batch forms ask for your sudo password
```

Deletes for good. It only accepts an exact mission ID (never a number, so a typo
can't delete the wrong one), or `--today`/`--all`. It asks you to confirm, then
prompts you to type a confirmation phrase, shown on screen.

### The mission list looks wrong: `reindex`

```bash
ros2 fairy reindex
```

Rebuilds the mission list from the folders in the archive. The archive is the
source of truth; the list is only a cache.

## Command cheat-sheet

| I want to… | Run |
| --- | --- |
| check the robot is ready | `ros2 fairy doctor` |
| see what's happening now | `ros2 fairy mission_status` |
| start / record / finish | `mission_start` · `mission_record` · `mission_close` |
| give up on this mission | `ros2 fairy mission_abort` |
| take one recording out of it | `ros2 fairy drop <#>` |
| attach a bag recorded elsewhere | `ros2 fairy adopt <folder>` |
| find a mission | `ros2 fairy list` |
| send a mission to someone | `ros2 fairy export <n>` |
| check a mission is intact | `ros2 fairy verify <n>` |
| compare two missions | `ros2 fairy diff <older> <newer>` |
| fix a bag that won't play | `ros2 fairy repair <n>` |

Every option is listed in the [command reference](commands.md).
