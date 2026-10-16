# Command reference

Every verb is `ros2 fairy <verb>`. `ros2 fairy` on its own lists them.

## Common behaviour

- **`--debug`** (every verb): verbose logging to stderr, and full tracebacks
  instead of the friendly error message.
- **`--json`** (where listed): machine-readable output on stdout. Errors are
  `{"status": "error", "error": "<message>"}`.
- **Mission identifiers.** Verbs that take a mission accept, in order of
  precedence: an archive folder path, a number (`1` = newest, `2` = the one
  before), or a mission ID such as `m-20261002-082109-1a2b`.
- **Exit codes.**

  | Code | Meaning |
  | --- | --- |
  | `0` | Success, including "you declined, nothing changed". |
  | `1` | The command refused or failed. |
  | `2` | It needed to ask a question but there was no terminal (use `--yes` where available). |
  | `130` | Cancelled with Ctrl-C. Nothing was changed. |

| Verb | Summary |
| --- | --- |
| [`setup`](#setup) | One-time robot setup: identity file, directories, watchdog service |
| [`doctor`](#doctor) | Check that this robot is ready to capture a mission |
| [`mission_start`](#mission_start) | Answer five quick questions to describe the mission |
| [`mission_record`](#mission_record) | Record mission data (wraps `ros2 bag record`) |
| [`mission_status`](#mission_status) | Show what the recording assistant is doing right now |
| [`mission_close`](#mission_close) | Review the finished mission and save or discard it |
| [`mission_abort`](#mission_abort) | Abandon the open mission without saving |
| [`drop`](#drop) | Take one recording out of the open mission (the opposite of `adopt`) |
| [`mission_edit`](#mission_edit) | Change a saved mission's briefing |
| [`mission_delete`](#mission_delete) | Permanently delete saved missions |
| [`list`](#list) | List the missions saved on this robot |
| [`diff`](#diff) | Compare two missions and show what changed |
| [`verify`](#verify) | Check that a saved archive is complete and unmodified |
| [`export`](#export) | Package missions into portable, checksummed files |
| [`repair`](#repair) | Make unplayable (bad-clock) recordings playable |
| [`adopt`](#adopt) | Attach a bag recorded outside `mission_record` |
| [`reindex`](#reindex) | Rebuild the mission list from the archives on disk |
| [`add_operator`](#add_operator) | Let more accounts record missions |
| [`move_data`](#move_data) | Move the recordings and saved missions to another folder or disk |

## setup

```
ros2 fairy setup [--operator USER]... [--var-dir DIR] [--ros-setup PATH]
                 [--debug]
ros2 fairy setup --refresh-env [--from-container NAME] [--ros-setup PATH]
                 [--debug]
ros2 fairy setup --show
ros2 fairy setup [--robot-name NAME] [--platform TEXT] [--serial TEXT]
                 [--organization TEXT] [--contact-email EMAIL]
                 [--license URL_OR_SPDX] [--add-sensor SPEC]...
                 [--set-sensor ID SPEC]... [--remove-sensor ID]...
                 [--record-topics TOPICS] [--storage NAME] [--yes]
```

<!-- TODO: replace with a real screenshot of `ros2 fairy setup` -->
![Screenshot placeholder: ros2 fairy setup, to be added](assets/screenshots/setup-placeholder.svg)

The interactive robot setup described in
[Set up the robot](getting-started.md#2-set-up-the-robot). Run it as your
normal user: only the final step uses `sudo`, and the answers are passed to it
on stdin.

| Option | |
| --- | --- |
| `--operator USER` | Another account that will record missions; repeatable. Skips the question about other operators. An account that doesn't exist stops setup before any question. |
| `--refresh-env` | Only re-capture the ROS environment from this shell and restart the watchdog; no questions. See [Changing the robot's configuration later](getting-started.md#changing-the-robots-configuration-later). |
| `--from-container NAME` | Take the DDS discovery settings from this running container instead of this shell, for a robot whose ROS runs in Docker; with or without `--refresh-env`. See [Robots whose ROS runs in Docker](getting-started.md#robots-whose-ros-runs-in-docker). |
| `--var-dir DIR` | Where to keep recordings and saved missions (default `/var/ros-fairy`); skips that question. Only while there are no missions yet: to move existing ones, use [`move_data`](#move_data). |
| `--ros-setup PATH` | Only when running setup directly as root (`sudo ros-fairy-setup`): the ROS 2 `setup.bash` to source. Auto-detected when there is exactly one `/opt/ros/*/setup.bash`. |

It refuses (exit 1) if ROS isn't sourced, no ROS nodes are visible, or the
service (which runs as root) couldn't load ros_fairy, e.g. when it is only
installed in a home directory. It also fails if `sudo` isn't available, or the
service doesn't come up within 10 s (then check
`journalctl -u ros-fairy-watchdog`). Declining the review writes nothing and
exits 0.

`ros-fairy-setup` is the same command as a standalone script.

### Changing one part of the configuration

```bash
ros2 fairy setup --show
ros2 fairy setup --set-sensor gps0 topic=/gps/fix
ros2 fairy setup --remove-sensor lidar0 \
    --add-sensor id=lidar1,type=lidar,model="Ouster OS2",topic=/points
```

These flags change only what they name in
[`robot_identity.yaml`](configuration.md#robot_identityyaml), without the
wizard and without needing ROS. Every flag that takes a sensor can be repeated,
and they combine in one call: one diff, one write.

| Option | |
| --- | --- |
| `--show` | Print the configuration as the recording assistant reads it, or the mistake in it. |
| `--robot-name`, `--platform`, `--serial` | The robot. The name is 40 characters at most. |
| `--organization`, `--contact-email` | The owner. |
| `--license URL_OR_SPDX` | The missions' default license: a URL, or an SPDX identifier such as `CC-BY-4.0` (saved as its `https://spdx.org/licenses/` URL). `""` removes it. |
| `--add-sensor SPEC` | `id=ID,type=TYPE,model=MAKE_MODEL,topic=TOPIC`, optionally `,frame=FRAME` and `,calibration=FILE`. `TYPE` is `gps`, `lidar`, `camera`, `imu`, `sonar` or `other`. Quote a value that has a comma: `model="Foo, Inc. X1"`. |
| `--set-sensor ID SPEC` | Change a sensor with the same `KEY=VALUE` pairs: `id=` renames it, an empty `frame=` or `calibration=` removes that. |
| `--remove-sensor ID` | Remove a sensor, and its calibration entry if no other sensor uses it. |
| `--record-topics TOPICS` | What [`mission_record`](#mission_record) records: `/fix,/scan`, or `all`. |
| `--storage NAME` | `mission_record`'s storage (`mcap`, `sqlite3`), or `default` for rosbag2's. |
| `--yes`, `-y` | Write without asking. |

The result is checked with the same rules the recording assistant uses before
anything is written. A mistake is refused with what's wrong, and nothing
changes. Otherwise the change is shown as a diff of the file and confirmed.
Then it is written through `sudo`, keeping the previous file as
`robot_identity.yaml.bak`. A calibration file must exist; it is saved by its
absolute path. When the robot's ROS graph is visible, a sensor topic nobody
publishes gets a warning, not a refusal (the sensor may be off).

Nothing needs restarting: the configuration is read again at every recording.
**Saved missions are unaffected:** each keeps the configuration it was recorded
with, and `verify` checks it against that. A sensor added later doesn't make an
older mission look incomplete. [`diff`](#diff) between a mission from before
and one from after shows the change.

The edit flags can't be combined with the wizard's flags (`--operator`,
`--var-dir`) or `--refresh-env`. A configuration with a mistake (edited by
hand) can be fixed with them: `doctor` names the mistake.

## doctor

```
ros2 fairy doctor [--json] [--debug]
```

<!-- TODO: replace with a real screenshot of `ros2 fairy doctor` -->
![Screenshot placeholder: ros2 fairy doctor, to be added](assets/screenshots/doctor-placeholder.svg)

Read-only checks, each reported as ok, warning, failure or skipped:

| Check | Fails / warns when |
| --- | --- |
| Robot identity | `robot_identity.yaml` is missing ("Robot is not set up") or has a mistake ("The robot configuration has a mistake", with what's wrong; fix it with [`setup`'s flags](#changing-one-part-of-the-configuration)) |
| Data folder | says where missions are kept; fails if `paths.yaml` can't be used, warns if this shell's `ROS_FAIRY_VAR_DIR` points elsewhere than the service, or missions were left behind in `/var/ros-fairy` |
| Recording assistant | the watchdog isn't running, or has been recording with no heartbeat for 5 minutes |
| Service interpreter | the installed service's interpreter can't load ros_fairy (only installed in a home directory) |
| Watchdog code | the running watchdog's code differs from the installed code (restart it) |
| ROS graph | `ros2` isn't on `PATH`, ROS isn't reachable, or no nodes are running |
| ROS environment | `ROS_DISTRO` isn't set in this shell |
| Service environment | `watchdog.env` is missing, or differs from this shell's ROS environment (distro, DDS discovery settings, DDS profile, root-owned overlays); the hint is `ros2 fairy setup --refresh-env`. When a running ROS container matches the service, it is this shell that's off, and the hint is the `export` that fixes it |
| ROS containers | a running container with `ROS_DISTRO` in its environment has other DDS discovery settings than the service (hint: `--refresh-env --from-container NAME`), or is on its own Docker network; skipped when Docker can't be asked |
| Last harvest | the last ROS capture failed |
| Clock | the system clock isn't NTP-synchronised |
| MCAP support | the `mcap` Python package is missing |
| Harvest tools | an optional tool the harvest uses is missing (`lsusb`, `lspci`, `v4l2-ctl`, `udevadm`, `setpriv`, `dmesg`, or the `rocrate` package); the hint gives the install commands |
| Disk space | less than 1 GiB free for the spool |
| Docker | (skipped when Docker isn't used) |
| Saved missions | an interrupted save is waiting to be finished |

Result: **READY**, **READY (with warnings)** or **NOT READY**. Exit code 1 only on
NOT READY. `--json` prints `{"result": ..., "checks": [{status, title, detail, hint}]}`.

## mission_start

```
ros2 fairy mission_start [--operator NAME] [--goal TEXT] [--location PLACE]
                         [--environment TEXT] [--notes TEXT] [--yes] [--json]
                         [--debug]
```

<!-- TODO: replace with a real screenshot of `ros2 fairy mission_start` -->
![Screenshot placeholder: ros2 fairy mission_start, to be added](assets/screenshots/mission_start-placeholder.svg)

Asks the [five briefing questions](operator-guide.md#1-start-the-mission-mission_start)
and saves them to the spool. It also records the shell's DDS settings for the
watchdog (`session.env`).

| Option | |
| --- | --- |
| `--operator`, `--goal`, `--location` | The required answers. Each one given skips its question. |
| `--environment`, `--notes` | The optional answers. |
| `--yes`, `-y` | Replace an unfinished mission that has no recordings without asking. |
| `--json` | Print `{"mission_id": ..., "operator": ..., "goal": ..., "location": ...}`; everything else goes to stderr. Needs the three required answers and implies `--yes`. |

With no answer options it asks all five questions. With some, it asks only the
required ones still missing, on a terminal. With no terminal to ask on (a
script, a launch file) it exits 2 and names the missing options. An empty
`--goal ""` counts as missing.

It refuses (exit 1) while a recording is running, or while the spool holds
recordings that haven't been saved or discarded (`--yes` doesn't change that).
It asks before replacing an unfinished mission that has no recordings;
without a terminal and without `--yes` that exits 2 and keeps the old
mission. Recordings made while no mission was open are not taken into the
new one with `--yes`: they stay on disk, and `adopt` attaches them.

## mission_record

```
ros2 fairy mission_record [--yes] [--debug] [<ros2 bag record options>]
```

Runs:

```
ros2 bag record <topics…|--all> [--storage <format>] --output <spool>/bags/<mission_id>_<YYYYmmdd-HHMMSS> [<your options>]
```

Topics and storage come from `recording:` in
[`robot_identity.yaml`](configuration.md#robot_identityyaml). Without a mission
open, the folder prefix is `unbriefed`.

| Option | |
| --- | --- |
| `--yes`, `-y` | Record without asking about low disk space, a missing briefing or an unsynchronised clock. |
| any `ros2 bag record` option | Passed on as typed, e.g. `--max-bag-size 1000000000`, `--compression-mode file --compression-format zstd`, `--qos-profile-overrides-path qos.yaml`, or topics. `ros2 fairy mission_record -h` lists them, and Tab completes them. |

`mission_record` takes `ros2 bag record`'s options directly: they come from the
installed rosbag2, so a typo or an option this rosbag2 doesn't have stops it at
once with the usual usage message (exit 2), before any question or check.
Then:

- `-o`/`--output` is refused: recordings go to the spool, where the watchdog
  finds them.
- A topic selection of your own (topics, `--topics`, `--services`,
  `--topic-types`, `-a`/`--all`, `--all-topics`, `--all-services`,
  `-e`/`--regex`) replaces the one from `robot_identity.yaml`, and it says so.
  Exclusions (`--exclude-…`) are added to it.
- `--storage` of your own replaces the configured one.
- Everything else is passed as written.

A recording compressed as whole files (`--compression-mode file`) is saved like
any other, but its timing can't be checked: see
[recording health](what-is-captured.md#recording-health).

Before recording it checks that `ros2` exists (exit 1 if not), free disk space
(at least 1 GiB, otherwise it asks), that a briefing exists, that the watchdog
is running (warning only) and clock sync (asks).

Ctrl-C forwards one SIGINT so rosbag2 finishes the file cleanly. Two further
presses force it to stop. SIGTERM and SIGHUP are handled the same way. Exit 1
if the recorder exits with an error; the data captured so far is kept.

## mission_status

```
ros2 fairy mission_status [--json] [--debug]
```

<!-- TODO: replace with a real screenshot of `ros2 fairy mission_status` -->
![Screenshot placeholder: ros2 fairy mission_status, to be added](assets/screenshots/mission_status-placeholder.svg)

Shows:

| Row | Content |
| --- | --- |
| Assistant | not running · not responding · recording (since when) · wrapping up · watching |
| Briefing | operator and goal, or "not started yet" |
| Recording | the active bag and its size so far |
| Recordings waiting | recordings to close, with total size, then one line each: its number (#1, #2, … for [`drop`](#drop)), start time, length, size, and where it is if it wasn't made in the spool |
| Context captured | per harvest module: ✓ captured, ⚠ partial, – not used on this robot, ✗ will keep trying |

`--json` keys: `assistant`, `watchdog_state`, `mission_context`, `spool_bags`,
`recordings`, `recording_ids` (path → number), `recording_now`. Always exits 0.

## mission_close

```
ros2 fairy mission_close [--note TEXT] [--yes] [--debug]
```

<!-- TODO: replace with a real screenshot of `ros2 fairy mission_close` -->
![Screenshot placeholder: ros2 fairy mission_close, to be added](assets/screenshots/mission_close-placeholder.svg)

| Option | |
| --- | --- |
| `--note TEXT` | Post-mission notes; skips the notes question. |
| `--yes`, `-y` | Save without asking; the notes stay as they are (set them with `--note`). Also finishes an interrupted save. The briefing must already exist. |

In order:

1. Offers to finish any save that was interrupted.
2. Refuses (exit 1) while a recording is in progress. Waits up to 245 s for
   the watchdog to finish a capture.
3. Picks up spool recordings the watchdog hadn't processed.
4. Asks missing briefing answers and notes.
5. Builds the mission record, grades its quality, checks for duplicates, and
   shows the review.
6. **Save** (default yes, or no when the quality is poor). If you don't save, it
   asks whether to **discard** (delete the spool recordings and capture) or
   **keep** (change nothing).

Exit 1 when there is nothing recorded, the briefing is missing and there is no
terminal to ask in, or the save fails. When a save fails before completing,
your data is left exactly as it was.

## mission_abort

```
ros2 fairy mission_abort [--yes] [--debug]
```

<!-- TODO: replace with a real screenshot of `ros2 fairy mission_abort` -->
![Screenshot placeholder: ros2 fairy mission_abort, to be added](assets/screenshots/mission_abort-placeholder.svg)

Abandons the open mission. It asks to confirm, and again before deleting
spool recordings. Recordings made outside ros_fairy are never deleted.
`--yes` skips both questions. It refuses while a recording is in progress. To
take out only one recording and keep the mission, use
[`drop`](#drop).

## drop

```
ros2 fairy drop [RECORDING] [--yes] [--debug]
```

Takes one recording out of the open mission; the briefing and the other
recordings stay. It is the opposite of [`adopt`](#adopt).

| Option | |
| --- | --- |
| `RECORDING` | Its number as [`mission_status`](#mission_status) shows it (`3` or `#3`), or its path. Without it, the recordings are listed and it asks which; with no terminal it lists them and exits 2. |
| `--yes`, `-y` | Don't ask before deleting or detaching it. |

**The numbers.** Each recording gets a number (#1, #2, …) when the watchdog
takes it into the mission: one made with `mission_record` or noticed on its
own once it has been processed, one attached with `adopt` at once. A number
never changes and is never given to another recording of the same mission, so
after `drop 2` the others keep theirs, and a new recording gets the next
unused one. A recording still being processed has no number yet (`–` in the
list): use its path. The numbers only name recordings while the mission is
open; `mission_record.json` doesn't keep them (the raw `harvest/harvest.json`
copy in the crate still shows them).

A recording in the spool (made with `mission_record`) is **deleted**, after a
confirmation (default no). One made elsewhere and only referenced (noticed by
the watchdog, or attached with `adopt`) is **detached**: it stays on disk, and
`ros2 fairy adopt <path>` attaches it again (with a new number).

It refuses (exit 1) while a recording is in progress or still being processed,
or while `mission_close` runs. Exit 1 also for a number or path that isn't in
the mission.

## mission_edit

```
ros2 fairy mission_edit [MISSION] [--operator NAME] [--goal TEXT]
                        [--location PLACE] [--environment TEXT] [--notes TEXT]
                        [--redact --by NAME] [--yes] [--debug]
```

Changes a saved mission's briefing: the operator, goal, place, environment and
notes. `MISSION` is a number (`1` = newest, the default), an archive folder or
a mission ID.

| Option | |
| --- | --- |
| `--operator`, `--goal`, `--location` | New values (they can't be empty). |
| `--environment`, `--notes` | New values; `""` clears them. |
| `--redact` | Also remove the old values (see below). Needs `--by` and your sudo password. |
| `--by NAME` | With `--redact`: your full name, recorded with it (asked on a terminal if missing). |
| `--yes`, `-y` | Don't ask before saving the changes. |

Without options it asks the five questions with the saved answers as defaults:
Enter keeps one, `-` clears an optional one. With no terminal and no options it
exits 2. It shows each change (now → will be) and asks before saving.

The briefing is in four files of the crate, rewritten together:
`mission_record.json` (the new values, and the change in
[`provenance.edits`](archive-format.md#the-mission-record)), `README.md`
(rendered again, keeping the warnings it was saved with),
`ro-crate-metadata.json` (only the values that depend on the briefing, plus
`dateModified`) and `checksums.sha256` (those three files' lines). The mission
list is updated. The recordings, the other files and the folder's name don't
change, so paths to the mission still work.

- **The archive must be as it was saved.** If `checksums.sha256` doesn't match
  a file, it refuses (exit 1) rather than re-hash a change made by hand:
  `verify` shows what changed.
- **Exports go stale.** A bundle exported before no longer matches. Its export
  record is dropped, so `export --all` exports the mission again, and the
  message gives the `export … --force` command that replaces the old bundle.
- **Crash-safe.** The new files are written next to the old ones and swapped in
  once all are complete. If it's interrupted, `verify` says so and the next
  `mission_edit` of that mission finishes the swap (or throws away a
  half-written edit, leaving the mission as it was).
- **Newer records** (saved by a newer ros_fairy, with fields this version
  doesn't know) are refused: rewriting them would lose those fields.

**Redacting.** A normal edit keeps the old values in the mission's history
(`provenance.edits`). When the old text shouldn't be there at all (a person's
name or a phone number in the notes), `--redact` removes it:

```bash
ros2 fairy mission_edit 1 --notes "weather fine" --redact --by "Ana Rossi"
```

- The changed fields' old values aren't kept, and earlier history entries of
  those fields are blanked (marked `redacted`). The entry still records the
  time, your account, your name (`by`) and which fields changed. Fields not
  being changed keep their history.
- If the place or the operator is redacted, the archive folder (whose name is
  built from them) is renamed, and the mission list follows.
- The mission list (`index.db`) is compacted, so no old page of it keeps the
  text.
- The write runs as root through `sudo`, which asks for your password: the
  system's auth log keeps the exact command next to your account. A wrong
  password changes nothing. It asks before saving (default no), and with no
  terminal it needs `--by`.
- **Copies made before are out of its reach:** bundles exported earlier (the
  message names the last one) and backups still hold the old values. Delete or
  replace them.

## mission_delete

```
ros2 fairy mission_delete (MISSION_ID | --today | --all) [--debug]
```

| Form | |
| --- | --- |
| `MISSION_ID` | One mission, by exact ID only (not a number or a path). |
| `--today` | Every mission recorded today. Uses `sudo`. |
| `--all` | Every saved mission. Uses `sudo`. |

Needs an interactive terminal. It shows what will and won't be deleted,
asks to confirm, then prompts you to type a confirmation phrase exactly. It only ever deletes folders directly inside the archive directory.
A mission saved by another account can be deleted with `sudo` after asking.
There is no `--yes`.

## list

```
ros2 fairy list [--operator NAME] [--location PLACE] [--since YYYY-MM-DD]
                [--until YYYY-MM-DD] [--quality {ok,degraded,poor}]
                [--search WORDS] [--exported | --not-exported]
                [--limit N | --all] [--path] [--json] [--debug]
```

<!-- TODO: replace with a real screenshot of `ros2 fairy list` -->
![Screenshot placeholder: ros2 fairy list, to be added](assets/screenshots/list-placeholder.svg)

| Option | |
| --- | --- |
| `--operator`, `--location` | Case-insensitive substring match. |
| `--since`, `--until` | Local dates, both inclusive. |
| `--quality` | Only missions with this verdict. |
| `--search WORDS` | Only missions whose goal or notes contain every word, in any order. Case and accents are ignored (`citta` finds "Città"); quote a phrase to keep it together: `--search '"north pier" sonar'`. |
| `--exported`, `--not-exported` | Only missions exported at least once, or never (what `export --all` would export). |
| `--limit N` | Show the latest N (default 20; at least 1). |
| `--all` | Show every mission. |
| `--path` | Add the archive folder column. |

Columns: date, mission ID, goal, location, operator, duration, size, warnings,
data quality, and a ✓ under **Exp** once the mission has been exported. The
newest mission is last. Filters combine: every one must match. `--json` prints
`{"missions": [...], "total": N, "shown": M}`; each mission carries the index
columns, `notes` included, and `export`: `null`, or the last export's
`exported_at`, `bundle_path`, `format` and `sha256`.

An export stays marked after the bundle is moved or deleted (sending it
somewhere is the point). It is forgotten when the bundle no longer matches the
mission, after [`mission_edit`](#mission_edit).

## diff

```
ros2 fairy diff [OLDER NEWER] [--json] [--debug]
```

<!-- TODO: replace with a real screenshot of `ros2 fairy diff` -->
![Screenshot placeholder: ros2 fairy diff, to be added](assets/screenshots/diff-placeholder.svg)

With no arguments, compares the two most recent missions (`2` and `1`). Give
both or neither.

It only shows sections that differ: mission context, software (packages, Python,
containers), sensors, ROS graph, parameters, robot description (URDF),
`/tf_static`, USB and udev, and recordings. tf2's randomly-named internal nodes
are ignored. If a mission was saved by a newer ros_fairy, it says what it
couldn't compare.

## verify

```
ros2 fairy verify [MISSION] [--json] [--debug]
```

<!-- TODO: replace with a real screenshot of `ros2 fairy verify` -->
![Screenshot placeholder: ros2 fairy verify, to be added](assets/screenshots/verify-placeholder.svg)

`MISSION` is a number (`1` = newest, the default), a mission ID, an archive
folder, or an exported bundle. Read-only. Checks that:

- `mission_record.json` loads and validates;
- `ro-crate-metadata.json` is valid JSON. It is fully loaded as JSON-LD when the
  `rocrate` package is installed; otherwise a warning suggests installing it;
- `README.md` and `harvest/harvest.json` are present;
- every bag file matches the sha256 recorded at save time (older archives
  without checksums get a structural check against `metadata.yaml`);
- every calibration file matches its sha256;
- every other file matches `checksums.sha256`, and none were added;
- every file the RO-Crate lists exists;
- the mission is in the index, at this path (not for a bundle);
- no [`mission_edit`](#mission_edit) was left half-done. Edits made after saving
  are listed (how many, the last one's time and account, which fields).

Result: **PASS**, **PASS (with notes)** or **FAIL**. Exit 1 on FAIL.
`--json` prints `{"archive": ..., "bundle": true|false, "result": ...,
"checks": [...]}`.

### An exported bundle

```bash
ros2 fairy verify ~/Downloads/2026-10-16_07-28-24_lab_ana.tar.zst
```

A `.zip`, `.tar`, `.tar.gz` or `.tar.zst` from [`export`](#export) is checked
without unpacking it. It is recognised by its content, so a renamed bundle
works too. It is read once, start to end, with no temporary copy, and checked
against:

- **its `.sha256` file**, if there is one next to it. A mismatch fails: the
  bundle changed, or wasn't copied completely. The other checks still run and
  say which files are affected;
- **everything a folder is checked for**, except the index: a bundle is a copy,
  often on another computer. Any account can verify one, also one outside the
  `ros-fairy` group.

A bundle cut short, or one whose compressed data is damaged, fails with "can't
be read". In a damaged zip only the damaged files are lost; the rest are still
checked.

## export

```
ros2 fairy export [MISSION | --all | --today] [--exclude ID]... [-o PATH]
                  [--format {zip,tar,tar.gz,tar.zst}] [--compress] [--force]
                  [--yes] [--json] [--debug]
```

| Option | |
| --- | --- |
| `MISSION` | Defaults to the newest. |
| `--all` | Every mission not yet exported successfully. |
| `--today` | Every mission recorded today. |
| `--exclude`, `-e ID` | Skip this mission (repeatable; with `--all`/`--today`). |
| `--output`, `-o` | A file name (one mission) or a directory. Default: the current directory. |
| `--format` | `zip` (default), `tar`, or a compressed tar: `tar.gz` or `tar.zst`. |
| `--compress`, `-z` | Compress the bundle (see below). |
| `--force` | Overwrite existing files. |
| `--yes`, `-y` | Don't ask before batch exports. Implied by `--json`. |

Each bundle holds the mission folder under one top-level folder, next to a
`sha256sum`-compatible `<bundle>.sha256`. It is uncompressed unless asked:

- **`--compress` with a zip** deflates each file that shrinks, so the zip still
  opens everywhere. Files already compressed are stored as they are: those
  compressed whole (`.mcap.zstd`, `.db3.zstd`, images) and MCAPs with
  compressed chunks. Each file is judged by compressing a sample of it.
- **`--compress` with a tar** makes a `.tar.zst`. For a recipient without
  zstd, name `--format tar.gz` instead (slower, a little larger).

Bags recorded with rosbag2's defaults aren't compressed, and often shrink by
half or more. On five real recordings (5 MB to 2.8 GB: navigation, lidar,
point-cloud maps, sqlite3), zstd left 13–65% of the size and deflate 14–67%;
zstd ran at 1–2.5 GB/s on all cores, deflate at 45–180 MB/s on one. So a
deflated zip of a big mission takes minutes where a `.tar.zst` takes seconds.
To open a `.tar.zst`: `tar --zstd -xf <file>` (needs the `zstd` package).

The bundle is written to a `.part` file and renamed when complete. Missions
that fail `verify` are exported with a warning. The archive is never modified.
Exit 1 if any export failed. The recipient can check the bundle as it is, with
[`verify`](#verify).

## repair

```
ros2 fairy repair [MISSION | BAG_DIR] [-o DIR] [--all] [--duration SECONDS]
                  [--force] [--json] [--debug]
```

| Option | |
| --- | --- |
| `MISSION` / `BAG_DIR` | A mission (default: newest) or a single bag folder. |
| `--output`, `-o` | Where to write. Default: `./<name>_repaired`. Must be empty unless `--force`. |
| `--all` | Re-stamp every recording, not only those with a broken clock. |
| `--duration` | Playback length in seconds for each repaired recording. |

Writes new bags with evenly spaced synthetic timestamps and a regenerated
`metadata.yaml`, so they play without `ros2 bag reindex`. Message order, types
and bytes are unchanged. Originals are never modified. MCAP recordings come out
as one `.mcap` file; sqlite3 recordings keep their `.db3` files, splits
included. Bags in another storage format, or compressed as whole files
(`.mcap.zstd`, `.db3.zstd`), are reported as unsupported.

`tools/restamp_bag.py <in> <out> [--duration S]` does the same for one bag
without needing the CLI.

## adopt

```
ros2 fairy adopt BAGDIR [--json] [--debug]
```

Attaches a rosbag2 folder (`metadata.yaml` plus a `.mcap`/`.db3` file) to the
current mission, where it is. If the watchdog hasn't captured anything yet, it
runs a capture now. The bag is copied into the archive at `mission_close`.

It says which number the recording got in the mission (`--json`:
`recording_id`); [`drop`](#drop) with that number takes it out again.

Refuses while the watchdog is busy recording. Adopting the same bag twice is a
no-op.

## reindex

```
ros2 fairy reindex [--json] [--debug]
```

Rebuilds `index.db` from every `archive/*/mission_record.json`. Unreadable
folders are reported and skipped. If the same mission was saved twice, the most
recent save is listed and you are told about the other. Export history is
kept.

`--json` prints `{"missions": N, "archive_dir": ..., "skipped": [...], "duplicates": {...}}`.

## add_operator

```
ros2 fairy add_operator USER [USER...] [--debug]
```

Lets more accounts record and save missions, by adding them to the
`ros-fairy` group (which owns the spool, the archive and the index). It asks
for your password through `sudo` when it isn't run as root. Accounts already in
the group are left alone.

Membership applies from each account's next login. In a terminal that's
already open, `newgrp ros-fairy` starts a shell that has it.

Exit 1 if the robot isn't set up yet, an account doesn't exist, or `usermod`
failed for one of them; the others are still added.

The verbs that record or save (`mission_start`, `mission_record`,
`mission_close`, `mission_abort`, `adopt`) check this access before asking
anything. An account outside the group is told to ask for
`ros2 fairy add_operator <its name>`; one added after its terminal was opened
is told to log in again or run `newgrp ros-fairy`.

## move_data

```
ros2 fairy move_data DIR [--yes] [--debug]
```

Moves the data folder (recordings, saved missions, the mission list, the
watchdog's state and logs) to `DIR`, a new or empty folder, e.g. on a bigger
disk. It asks first (`--yes` skips the question), then, through `sudo`:

1. stops the watchdog;
2. on the same disk, renames the folder (instant); on another disk, copies it
   into a hidden folder next to `DIR`, compares every file's size with the
   original, renames the copy into place, and only then removes the original;
3. records `DIR` in `/etc/ros-fairy/paths.yaml` (removes the file when `DIR`
   is `/var/ros-fairy`);
4. rebuilds the mission list (`reindex`) and starts the watchdog again.

Every shell, and the watchdog, uses the new place at once. If anything fails
before step 3, the missions stay where they were and the watchdog is started
again.

It refuses (exit 1) while a mission is open (save it with `mission_close` or
abandon it with `mission_abort` first), a save is unfinished, a recording is
being processed, `ROS_FAIRY_VAR_DIR` is set in the shell, or `DIR` isn't
empty, is inside the current folder (or the other way round), or is on a
disk without room for the missions plus 1 GiB.
