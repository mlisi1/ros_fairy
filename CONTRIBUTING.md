# Contributing to ROS F.A.I.R.y

Thank you for helping. This page is the short version; the
[Development](https://mlisi1.github.io/ros_fairy/development/) page of the
documentation has the details (the code's layout, how the tests fake the
robot, the sandbox robot, the documentation site).

## Branches and pull requests

- `main` holds releases; `devel` is where work happens. Branch from `devel`
  and open your pull request against it.
- One change per pull request: a fix, a feature, or a documentation update.
  Say what it changes for the operator or the engineer, and how you tested it.
- Found a bug? An issue with what you ran, what you expected, and what
  happened (the output of `ros2 fairy doctor` helps) is already a
  contribution.

## Before you push

```bash
./install.sh --dev                 # once: also installs pytest, rocrate, ruff, mypy
pytest                             # unit + integration, no ROS needed
ruff check . && mypy fairy
mkdocs build --strict              # if you touched docs/ (pip install -r requirements-docs.txt)
```

Continuous integration runs these four on every push and pull request, on
Python 3.12 with no ROS installed; all of them must pass. A change that
touches the watchdog, `setup` or recording is also worth one run on the
sandbox robot, a throwaway robot in Docker:

```bash
tools/sandbox/sandbox.sh reset && tools/sandbox/sandbox.sh install
tools/sandbox/sandbox.sh run smoke.py    # setup, then one mission end to end
tools/sandbox/sandbox.sh down            # when you're done
```

## Tests

- **A fix comes with a test** that fails without it. Many tests are written
  against one failure case (a crash mid-save, a full disk, a hung harvest);
  name yours after the case it guards.
- **Tests must not depend on the machine.** CI has no ROS, no `ros-fairy`
  group, no system-wide `fairy` package and no recorders running. Fake what the
  test needs (the paths, `/proc`, subprocesses, the clock) as the existing
  tests do; the table in
  [How things are faked](https://mlisi1.github.io/ros_fairy/development/#tests)
  lists how.
- Tests that need a live ROS 2 are marked `ros` and run with `pytest -m ros`.

## Conventions

The [conventions](https://mlisi1.github.io/ros_fairy/development/#conventions)
in short:

- Everything an operator reads is a plain sentence saying what happened and
  what to do; engineer detail goes to `--debug` and the logs.
- Never lose a recording: data that is moved or deleted is in one known
  place at every instant. Checks warn; they never block a save.
- The watchdog never raises, and files are written atomically
  (`utils/fsio.py`).
- Code reads like the code around it: the same naming, comment density and
  helpers.
- A change people can see (a command's output, an option, a file) updates the
  documentation in the same pull request. If it changes a command's output,
  the screenshots may need re-taking (`tools/sandbox/screenshots/take.sh`).

## Changing the mission record format

The record (`mission_record.json`) is versioned by `SCHEMA_VERSION` in
`fairy/__init__.py`. Saved missions live for years, so every version must
keep reading the older ones. The steps are in
[Changing the record format](https://mlisi1.github.io/ros_fairy/development/#changing-the-record-format).
In short: adding a field is a minor bump, changing or removing one is a major
bump, and a major bump needs an issue first.

## Commits and the changelog

- Commit messages: a short subject in the imperative ("Fix …", "Add …"),
  then a body that says why, in plain sentences.
- Add a line to the `Forthcoming` section of `CHANGELOG.rst` for anything a
  user would notice.

## License

By contributing, you agree that your contributions are licensed under the
[Apache License 2.0](LICENSE), like the rest of the project.
