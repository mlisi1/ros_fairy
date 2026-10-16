# Archive format

Each saved mission is one folder in `/var/ros-fairy/archive/`, and each folder
is a valid [RO-Crate 1.1](references.md#ro-crate): a directory of data
plus a JSON-LD file describing it. You can read it without ros_fairy:
everything is JSON, YAML, text or standard rosbag2.

## Folder name

```
<YYYY-MM-DD_HH-MM-SS>_<place>_<operator>
2026-10-02_08-21-09_marsh-creek-north-bank_ana
```

The time is the mission's creation time in local time. The place and operator
are lowercased ASCII, with other characters turned into `-`, up to 40
characters each. If the name is taken, `_2`, `_3`, … is appended.

## Contents

```
<crate>/
├── ro-crate-metadata.json       RO-Crate JSON-LD                           always
├── mission_record.json          the full mission record (schema below)     always
├── README.md                    human-readable summary                     always
├── checksums.sha256             sha256 of every file outside bags/         always
├── bags/
│   └── <recording>/             rosbag2 folders, unchanged                 one per recording
├── harvest/
│   ├── harvest.json             the raw capture, as the watchdog wrote it  always
│   ├── watchdog.log             what the watchdog did during the mission
│   ├── robot_description.urdf   if a URDF was published
│   ├── tf_static.json           if static transforms were captured
│   ├── pip_freeze.txt
│   ├── lsusb_verbose.txt
│   ├── dmesg_usb.txt
│   ├── udev_trace.txt           which udev rules matched which device
│   └── udev_rules/<path>        each non-default udev rule file, at its original path
├── calibrations/<file>          calibration files declared for the robot's sensors
└── docker/
    ├── containers.json          raw `docker inspect`
    └── compose/<project>/<file> Compose files of running containers
```

Files without "always" are present only when there was something to write.

### Checksums

- **Recordings:** a sha256 for every file in every bag, in
  `mission_record.json` (`bags[].file_sha256`) and in the RO-Crate. Spool
  recordings are hashed before they are moved into the archive; recordings
  made elsewhere are hashed after being copied in.
- **Everything else:** `checksums.sha256`, in `sha256sum -c` format:

  ```bash
  cd <crate> && sha256sum -c checksums.sha256
  ```

`ros2 fairy verify` checks both, and more (see [`verify`](commands.md#verify)).

## The mission record

`mission_record.json` is the source of truth for a mission: the index, `list`,
`diff` and `verify` are all built from it.

| Section | Contents |
| --- | --- |
| `schema_version` | Record format version (currently `1.3`). |
| `identity` | `mission_id`, `created_at` (UTC), `operator_name`, `operator_contact` |
| `intent` | `goal`, `location_name`, `environment`, `notes` |
| `robot` | `name`, `platform`, `serial_number`, `owner_organization`, `owner_contact`. `null` if the robot wasn't set up. |
| `sensors[]` | `sensor_id`, `type`, `make_model`, `topic`, `frame_id`, `calibration_ref`, `detected_at_start` |
| `software` | `ros_distro`, `ros_packages`, `apt_ros_versions`, `docker_containers[]` (with each container's `ros_packages` and `python_packages`), `ros_fairy_version`, `python_env` |
| `ros_graph` | `captured_at`, `nodes`, `topics[]`, `parameters` (per node), `parameters_not_captured`, `robot_description` (the path of the archived URDF), `tf_static`, `complete` |
| `calibrations[]` | `name`, `source_path`, `format`, `archived_path`, `sha256` |
| `bags[]` | `path` (`bags/<name>`), `source`, `storage_format`, `size_bytes`, `start_time`, `end_time`, `duration_s`, `message_count`, `topics[]`, `health_warnings[]`, `file_sha256` |
| `hardware_devices[]` | One entry per device found. |
| `usb` | `usbcore`, `driver_parameters`, `devices[]` (each with `power`, `interfaces`, `serial_ports`, `udev_rules_applied`), `ports[]` |
| `udev_rules` | `custom[]` (archived files, with `reason`, `sha256`, `archived_path`), `default[]` |
| `provenance` | `ros_fairy_version`, `schema_version`, `harvested_at`, `assembled_at`, `hostname`, `kernel`, `arch`, `clock_synchronized`, `harvest_status`, `field_confidence`, `data_quality`, `harvested_by`, `assembled_by`, `edits[]` |

All timestamps carry a timezone. `null` consistently means *not captured / couldn't
tell*, as distinct from an empty list, which means *captured, and there were none*.
For example, `docker_containers: null` means Docker wasn't reachable, while `[]`
means no containers were running.

Each `provenance.edits[]` entry is one change to the briefing made after saving
with [`mission_edit`](commands.md#mission_edit): `at` (when), `account` (who ran
it) and `changes`, mapping each changed field (`operator_name`, `goal`,
`location_name`, `environment`, `notes`) to its `before` and `after` values.
The record keeps the edited values; the history shows what they replaced.

A redaction (`mission_edit --redact`) has `redacted: true` and `by` (the name
of the person who did it; `account` is the one behind `sudo`). Its fields have
no `before`, and earlier entries of the same fields are blanked: their
`before` and `after` are `null` and `redacted` is `true`.

Each `health_warnings[]` entry has `topic`, `sensor_id`, `kind`, `plain_text`,
`start_offset_s` and `duration_s`. The kinds are listed in
[Recording health](what-is-captured.md#recording-health).

### Versioning

`schema_version` is `MAJOR.MINOR`:

- **Minor** bumps only **add** fields. An older ros_fairy still reads the
  record; it sets the unknown fields aside and says so (`verify` and `diff`
  report what they couldn't check).
- **Major** bumps change or remove fields. An older ros_fairy refuses the record
  and asks you to update.

| Version | Changes |
| --- | --- |
| 1.0 | First version. |
| 1.1 | Added `usb`, `udev_rules`, `ros_graph.parameters_not_captured`, `docker_containers[].python_packages`; `apt_ros_versions` and `docker_containers` may be `null`. |
| 1.2 | Added `provenance.harvested_by` and `provenance.assembled_by`. |
| 1.3 | Added `provenance.edits` (with `redacted` and `by` for redactions). An edited record is rewritten in the current version. |

## The RO-Crate

`ro-crate-metadata.json` is flattened JSON-LD using the RO-Crate 1.1 context plus
the W3C [SOSA/SSN](https://www.w3.org/TR/vocab-ssn/) vocabulary for the robot
and its sensors. The [`rocrate`](https://pypi.org/project/rocrate/) library can
load it.

| Entity | Type | Describes |
| --- | --- | --- |
| `./` | `Dataset` | The mission. `identifier` = mission ID, `name` = goal, `dateCreated`, `dateModified` (the last `mission_edit`, if any), `keywords`, `license` (from `owner.default_license`, if set), `publisher`, `author`, `contentLocation`. |
| `#mission` | `CreateAction` | The run itself. `agent` (operator), `location`, `startTime`/`endTime` from the recordings, `instrument` (robot and software), `result` = `./`. |
| `#operator` | `Person` | The operator. |
| `#organization` | `Organization` | The robot's owner. |
| `#place` | `Place` | Where the mission took place. |
| `#robot` | `sosa:Platform` | The robot: `model`, `serialNumber`, `owner`, `sosa:hosts` its sensors. |
| `#sensor-<id>` | `sosa:Sensor` | Each declared sensor: topic, frame, whether it was detected, and its calibration file (`subjectOf`). |
| `https://github.com/mlisi1/ros_fairy` | `SoftwareApplication` | The ros_fairy build that saved the crate (version, commit). |
| `#ros2`, `#python-runtime`, `#container-<name>` | `SoftwareApplication` | The ROS distro, the Python runtime and each container. |
| `bags/<name>/` | `Dataset` | Each recording: size, format, time span, one `variableMeasured` per topic (message count and type), health warnings as `comment`, and its files with their `sha256`. |
| other files | `File` | Every other file in the crate, `checksums.sha256` included (without a `sha256` of its own), each listed once even when several sensors share a calibration file. |

Values typed in by the operator (operator, place, the mission action) carry an
`additionalProperty` `ros-fairy:confidence = user`, which separates them from
captured values.

## How a save works

`mission_close` saves the mission so that **at no point can the archive hold a
half-written mission, and at no point is a recording only partially in one
place**:

1. **Build.** Everything is assembled in `archive/.staging/<name>/`: the
   harvest files, calibrations, and copies of recordings made outside the spool.
   Free space is checked first (with a 64 MiB margin). A failure or Ctrl-C at
   this stage throws the staging copy away. Nothing else was touched.
2. **Move.** Spool recordings are moved into staging. Within one filesystem this
   is a rename. Across filesystems each recording is copied, flushed to disk,
   checked against its sha256 and only then removed from the spool. If this
   stage fails, the recordings are moved back.
3. **Commit.** Staging is flushed to disk and renamed into `archive/<name>/` in
   one atomic step. Then the index is updated and the spool is cleared.

A small plan file, `archive/.staging/<name>.plan.json`, records which stage the
save has reached (`building`, `moving` or `committed`) and every move made.
After a crash or power cut, the next `mission_close` (or `--yes`) reads it and
either discards the attempt (`building`), finishes it (`moving`), or just does
the remaining bookkeeping (`committed`). Before finishing a move it checks the
staged copy against the recording's sha256; one that no longer matches is
replaced from the spool's copy, if that one is whole, and if neither is, the
save stops with nothing changed. `doctor` and `list` warn while such a save
is pending.

> Never delete anything in `archive/.staging/` by hand: during the `moving`
> stage it may hold the only copy of a recording.

Only one save runs at a time per robot (a lock in `.staging/`).

## The index

`/var/ros-fairy/index.db` is a SQLite cache of the archive used by `list`,
`diff`, duplicate detection and `export --all`:

- `missions`: one row per mission, with ID, time, operator, location, goal,
  path, total duration and size, number of recordings and warnings, robot,
  versions, quality, notes;
- `mission_bags`: a content fingerprint per recording, used to spot the same
  recording saved twice;
- `exports`: when each mission was last exported, and where.

`ros2 fairy reindex` rebuilds it from the `mission_record.json` files (keeping
the export history).
