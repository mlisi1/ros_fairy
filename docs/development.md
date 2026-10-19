# Development

How the code is laid out and tested, for anyone changing it. How to propose a
change (branches, the checks to run, commits) is in
[CONTRIBUTING.md](https://github.com/mlisi1/ros_fairy/blob/main/CONTRIBUTING.md).

## Setup

```bash
./install.sh --dev           # also installs pytest, rocrate, ruff and mypy
pytest                       # unit + integration, no ROS needed
pytest -m ros -v             # live smoke tests, on a box with ROS 2 sourced
ruff check . && mypy ros_fairy
```

`pytest`, `ruff` and `mypy` run against the checkout, so they see your changes
straight away. The installed command and the watchdog don't: `install.sh`
makes a normal (not editable) install. **To try a change on the robot,
re-run `./install.sh` and restart the watchdog**
(`sudo systemctl restart ros-fairy-watchdog`); otherwise `doctor` reports that
the watchdog runs different code.

**Continuous integration** (`.github/workflows/ci.yml`) runs the same checks
on every push to `main` and `devel` and on every pull request: `pytest`,
`ruff check .`, `mypy ros_fairy` and `mkdocs build --strict`, on Python 3.12
(as on Jazzy / Ubuntu 24.04), with no ROS installed. All four must pass, so
run them before pushing. A test that needs something of this machine (a
sourced ROS, the `ros-fairy` group, a real tool) fails there: fake it as
in [Tests](#tests).

## Layout

```
ros_fairy/
├── command/fairy.py         the `ros2 fairy` command (ros2cli extension)
├── subcommands/             one module per verb; shared helpers in __init__.py
├── watchdog/                the service: state machine, /proc recorder scan, the
│                            containers' ROS processes, unit file
├── harvest/                 capture modules (identity, system, python, hardware,
│                            usb_udev, ros_graph + ros_snapshot, docker)
├── manifest/                pydantic schema, record builder, validator, quality grading
├── archive/                 crash-safe assembler, RO-Crate writer, SQLite index,
│                            identifier lookup, duplicate detection, briefing edits,
│                            reading exported bundles (for verify)
├── ui/                      rich rendering: briefing, review, status, diff
├── utils/                   paths (and paths.yaml), atomic file IO, ROS env
│                            handling, clock, bag readers, topic health, bag
│                            repair, operator accounts, the harvest's tools
└── build_info.py            which ros_fairy build is running
tools/sandbox/               a throwaway robot in Docker, scenarios to run on it,
                             and the docs' screenshots (screenshots/)
```

Verbs are registered as entry points in `setup.py` (`fairy.verb` group). A new
verb is a `VerbExtension` subclass with `add_arguments` and `main`, plus an
entry point. Wrap `main` in `guarded_main` so Ctrl-C, missing terminals and
unexpected errors get the standard messages and exit codes.

## Conventions

- **Plain language for operators.** Everything an operator reads is a full
  sentence that says what happened and what to do. Health warnings carry a
  pre-rendered `plain_text`, and UIs show that, never raw numbers. Engineer
  detail goes to `--debug` and the logs.
- **Never lose a recording.** Anything that moves or deletes data must leave it
  in one known place at every instant (see
  [How a save works](archive-format.md#how-a-save-works)). Checks such as
  duplicates and quality warn; they never block.
- **The watchdog never raises.** Harvest modules return a status rather than
  throwing; the main loop logs and carries on.
- **Files are written atomically** (`utils/fsio.py`): write to a temp file,
  fsync, rename.
- **The record format is versioned.** Adding fields to `manifest/schema.py`
  is a minor bump of `SCHEMA_VERSION`; changing or removing them is a major
  bump. See [Changing the record format](#changing-the-record-format).
- **The version** lives in `package.xml`, `setup.py`, `ros_fairy/__init__.py`
  and `CITATION.cff`; `tests/unit/test_packaging.py` checks they agree.
  The same test checks the service name, group and default paths written out
  in `uninstall.sh` and the systemd unit against the package.

## Changing the record format

`SCHEMA_VERSION` in `ros_fairy/__init__.py` versions `mission_record.json` as
`MAJOR.MINOR` ([Versioning](archive-format.md#versioning)). Saved missions
outlive the version that wrote them, so every version keeps reading the older
records, and reads a newer minor one by setting aside the fields it doesn't
know (`read_record` in `manifest/schema.py`).

To add a field (a minor bump):

1. Add it to its model in `manifest/schema.py` **with a default** (`None`, or
   an empty list), so records written before it still validate. Say in a
   comment which version added it.
2. Fill it in where the record is built (`manifest/builder.py`), or where it
   changes (as `archive/edit.py` does for `provenance.edits`).
3. Bump the minor number of `SCHEMA_VERSION` and add a line to the history
   comment above it: the version, the date, what was added.
4. In [Archive format](archive-format.md): the field in its table, the new
   number where it says "currently", and a row in the versions table.
5. Tests: a new record carries the field, and a record of the previous
   version (without it) still reads.

Changing or removing a field is a major bump: older versions refuse the
record and tell the user to update ros_fairy, and records already saved must
still read. Open an issue to discuss it first. A minor bump is almost always
possible instead: add the new field and keep the old one.

The mission index (`index.db`) has its own `DB_VERSION`
(`archive/index.py`). It can always be rebuilt from the records
(`ros2 fairy reindex`), but an existing index is upgraded in place on first
open, as DB version 4 did for the `notes` column.

## Tests

- `tests/unit/`: one file per area. Many are written against specific failure
  cases (a crash mid-save, a full disk, an unreadable file, a hung harvest).
- `tests/integration/test_mission_lifecycle.py`: start → record → close → list
  end to end, without ROS.
- `tests/integration/test_real_bags.py`: runs the analysis on real rosbag2
  recordings in `tests/fixtures/` (one sqlite3 and one MCAP, recorded on Jazzy).
- `tests/integration/test_ros_smoke.py` (marker `ros`): against a live ROS 2.
  It is deselected by default and skipped if ROS isn't sourced. Some tests need
  `demo_nodes_cpp`. It sets `ROS_FAIRY_IGNORE=1` on the processes it starts so a
  real watchdog on the same machine ignores them.

How things are faked:

| What | How |
| --- | --- |
| inotify and time | `FakeINotify`/`FakeClock` (`tests/unit/test_watchdog.py`), injected into `Watchdog(inotify=, clock=, pipeline=, harvest_in_thread=False, scan_recorders=)` |
| The harvest | `pipeline=` replaced by a function returning a canned document |
| Subprocesses | `subprocess.run` monkeypatched to return `CompletedProcess` |
| sysfs, udev rules, dpkg | a fake tree under `tmp_path`, by patching `usb_udev.ROOT` and `RULE_DIRS` (marker `real_usb_udev`; by default `usb_udev.harvest` is stubbed out for every test) |
| Recorders on the machine | `scan_recorders=no_recorders` (`tests/unit/test_watchdog.py`) for every test `Watchdog`, so a `ros2 bag record` running on the machine doesn't leak in |
| `/proc` | `recorder_scan.PROC` patched to a fabricated tree |
| Paths | the `fairy_dirs` fixture points `ROS_FAIRY_VAR_DIR`/`ROS_FAIRY_CONFIG_DIR` at `tmp_path` |
| Bags | `make_bag()` (sqlite3) and `make_mcap_bag()` in `tests/conftest.py` |

### The sandbox robot

`tools/sandbox/` runs ROS F.A.I.R.y on a throwaway robot in Docker, the way an
operator would: the real `install.sh`, `setup` with `sudo`, the watchdog as a
systemd service, and missions recorded from a terminal. Use it to try a change
end to end without touching your own machine's install.

```bash
tools/sandbox/sandbox.sh up            # build the image, boot the container
tools/sandbox/sandbox.sh install       # copy this checkout in, ./install.sh,
                                       #   restart the watchdog if set up
tools/sandbox/sandbox.sh run smoke.py  # setup, then one mission end to end
tools/sandbox/sandbox.sh shell         # look around as ana
tools/sandbox/sandbox.sh reset         # a fresh robot
```

The robot runs ROS 2 Jazzy on Ubuntu 24.04. The container has three
accounts: `ana` (the engineer, with password-less `sudo`, allowed to use
Docker) and `ben` and `cleo` (operators, no `sudo`). Docker runs inside it
too (the `vfs` storage driver), for robots whose stack is in containers: the
`containers.py` scenario builds a tiny local image from busybox, so nothing is
downloaded. Two publishers stand in for sensors: `/fix` and
`/scan`, as the units `fairy-talker-*`. The harvest's optional tools
(`lsusb`, `lspci`, `v4l2-ctl`) are left out, as on a fresh robot.

The checkout is mounted read-only at `/src`: re-run `install` after changing
the code. Scenarios in `tools/sandbox/scenarios/` are Python scripts that run
commands as these accounts and answer prompts on a real terminal
(`fairy_sandbox.py` has the helpers); they print a check per step and exit 1
if any failed. The container needs `--privileged` for systemd, so keep it to
your own machine.

The terminal screenshots in these docs are taken on the sandbox robot:

```bash
tools/sandbox/sandbox.sh reset && tools/sandbox/sandbox.sh install \
    && tools/sandbox/screenshots/take.sh
tools/sandbox/sandbox.sh reset         # afterwards: a clean robot again
```

`take.sh` installs a virtual X server, terminator and JetBrains Mono in the
container. Then `take_screenshots.py` sets the robot up through the real
wizard, records a few missions as `ana`, `ben` and `cleo`, and types each
command into a terminator window with `xdotool`, using the profile in
`terminator.config` (the docs' code-block colours). Between the last two
missions it adds an IMU and changes a parameter of `base_controller.py` (a
stand-in node: the talkers have no parameters), so `diff` has something to
show. The PNGs are copied to `docs/assets/screenshots/`, one per command
(`doctor.png`, `list.png`, …). Look at each one before committing: the output
is real, so a change in a command's output shows up there. Commit your changes
first, or `doctor` and `verify` show the version as `-dirty`.

## Documentation

The site is built with [MkDocs](https://www.mkdocs.org) and the Material theme,
from `mkdocs.yml` and `docs/`, at the versions pinned in `requirements-docs.txt`
(MkDocs 2.0 would break the theme):

```bash
pip install -r requirements-docs.txt
mkdocs serve                 # live preview at http://127.0.0.1:8000
mkdocs build --strict        # what to run before committing: fails on any broken link
```

It is published at <https://mlisi1.github.io/ros_fairy/> by
`.github/workflows/docs.yml`, on every push to `main` that changes it. A pull
request only builds it, so a broken link fails there before the merge.

- **Diagrams** are SVG files in `docs/assets/diagrams/`, never text art in a
  code block. Each has a `-dark.svg` twin, and pages include both:
  `name.svg#only-light` and `name-dark.svg#only-dark`.
- **Screenshots** go in `docs/assets/screenshots/`. Terminal ones are taken
  with `tools/sandbox/screenshots/take.sh` (see [The sandbox robot](#the-sandbox-robot)).
- **Links** use the page or section title as text (`[Paths](configuration.md#paths)`),
  not the file name.
- **Colours and fonts** live in `docs/stylesheets/extra.css`: cream and slate,
  with the sage green as the single accent.

## Tools

- **`tools/record_real_fixtures.sh [output_dir]`** regenerates
  `tests/fixtures/`. It runs `ros:jazzy` in Docker, starts the demo talker, and
  records six seconds of `/chatter` as sqlite3 and as MCAP. Needs Docker
  (run with `sudo`) and network access.
- **`tools/restamp_bag.py <in_bag> <out_bag> [--duration S]`** is the bag-repair
  logic of `ros2 fairy repair`, for one bag, without the CLI. It needs
  `ros_fairy` importable but not ROS.
