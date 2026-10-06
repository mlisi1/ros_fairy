"""Write ro-crate-metadata.json.

Hand-rolled JSON-LD so the entity layout matches the spec exactly. The crate
is generated from the MissionRecord only; the assembler supplies the inventory
of extra files it actually copied (this writer does no filesystem reads).
"""

import json
from pathlib import Path

from ros_fairy.manifest.schema import MissionRecord

ROS_FAIRY_ID = "https://github.com/gdl-res/ros_fairy"

_ENCODING = {
    "sqlite3": "application/x-sqlite3",
    "mcap": "application/x-mcap",
}

_FILE_ENCODING = {
    ".mcap": "application/x-mcap",
    ".db3": "application/x-sqlite3",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
}

_CONFIDENCE_USER_ID = "#confidence-user"


def build(record: MissionRecord, extra_files: list[dict] | None = None,
          license_url: str | None = None) -> dict:
    """The complete JSON-LD document as a dict.

    extra_files: [{"id": relpath, "name": ..., "encodingFormat": ...,
                   "sha256": ...?}] for files the assembler copied beyond
    mission_record.json and README.md.
    """
    extra_files = extra_files or []
    graph: list[dict] = []

    # PropertyValue objects are hoisted into their own @id'd entities and
    # referenced, so the crate is valid *flattened* JSON-LD (required for the
    # rocrate library to load it). _pv registers one (deduped by @id) and
    # returns the reference to embed in a parent.
    pv_entities: list[dict] = []
    _pv_seen: set[str] = set()

    def _pv(pv_id: str, name: str, value, description: str | None = None
            ) -> dict:
        if pv_id not in _pv_seen:
            _pv_seen.add(pv_id)
            entity = {"@id": pv_id, "@type": "PropertyValue",
                      "name": name, "value": value}
            if description is not None:
                entity["description"] = description
            pv_entities.append(entity)
        return {"@id": pv_id}

    def _confidence_user() -> dict:
        return _pv(_CONFIDENCE_USER_ID, "ros-fairy:confidence", "user")

    graph.append({
        "@id": "ro-crate-metadata.json",
        "@type": "CreativeWork",
        "conformsTo": {"@id": "https://w3id.org/ro/crate/1.1"},
        "about": {"@id": "./"},
        "sdPublisher": {"@id": ROS_FAIRY_ID},
    })

    description = record.intent.goal
    if record.intent.environment:
        description += f" Environment: {record.intent.environment}."
    if record.intent.notes:
        description += f" Notes: {record.intent.notes}."

    file_entities = [
        {"@id": "mission_record.json", "@type": "File",
         "name": "Mission record (machine-readable)",
         "encodingFormat": "application/json"},
        {"@id": "README.md", "@type": "File", "name": "Mission summary",
         "encodingFormat": "text/markdown"},
    ]
    for extra in extra_files:
        entity = {"@id": extra["id"], "@type": "File",
                  "name": extra["name"],
                  "encodingFormat": extra["encodingFormat"]}
        if extra.get("sha256"):
            entity["sha256"] = extra["sha256"]
        file_entities.append(entity)
    for cal in record.calibrations:
        if cal.archived_path:
            entity = {"@id": cal.archived_path, "@type": "File",
                      "name": f"Calibration: {cal.name}",
                      "encodingFormat": "application/yaml"
                      if (cal.format or "yaml") == "yaml"
                      else "application/octet-stream"}
            if cal.sha256:
                entity["sha256"] = cal.sha256
            file_entities.append(entity)

    bag_ids = [b.path.rstrip("/") + "/" for b in record.bags]

    root: dict = {
        "@id": "./",
        "@type": "Dataset",
        "identifier": record.identity.mission_id,
        "name": record.intent.goal,
        "description": description,
        "dateCreated": record.identity.created_at.isoformat(),
        "author": {"@id": "#operator"},
        "contentLocation": {"@id": "#place"},
        "keywords": ["robotics", "field-mission"] +
                    ([record.intent.environment]
                     if record.intent.environment else []),
        "mentions": {"@id": "#mission"},
        "hasPart": [{"@id": f["@id"]} for f in file_entities] +
                   [{"@id": bid} for bid in bag_ids],
    }
    if record.provenance.assembled_at:
        root["datePublished"] = record.provenance.assembled_at.isoformat()
    if record.robot:
        root["publisher"] = {"@id": "#organization"}
    if license_url:
        root["license"] = license_url
    graph.append(root)

    operator: dict = {"@id": "#operator", "@type": "Person",
                      "name": record.identity.operator_name,
                      "additionalProperty": _confidence_user()}
    if record.identity.operator_contact:
        operator["email"] = record.identity.operator_contact
    graph.append(operator)

    if record.robot:
        graph.append({"@id": "#organization", "@type": "Organization",
                      "name": record.robot.owner_organization,
                      "email": record.robot.owner_contact})

    graph.append({"@id": "#place", "@type": "Place",
                  "name": record.intent.location_name,
                  "additionalProperty": _confidence_user()})

    cal_paths = {c.name: c.archived_path for c in record.calibrations}
    if record.robot:
        graph.append({
            "@id": "#robot",
            "@type": ["Thing", "sosa:Platform"],
            "name": record.robot.name,
            "model": record.robot.platform,
            "serialNumber": record.robot.serial_number,
            "owner": {"@id": "#organization"},
            "sosa:hosts": [{"@id": f"#sensor-{s.sensor_id}"}
                           for s in record.sensors],
        })
    for sensor in record.sensors:
        sid = sensor.sensor_id
        props = [_pv(f"#sensor-{sid}-topic", "topic", sensor.topic)]
        if sensor.frame_id:
            props.append(
                _pv(f"#sensor-{sid}-frame_id", "frame_id", sensor.frame_id))
        detected = ("unknown" if sensor.detected_at_start is None
                    else str(sensor.detected_at_start).lower())
        props.append(_pv(f"#sensor-{sid}-detected_at_start",
                         "detected_at_start", detected))
        sensor_entity: dict = {
            "@id": f"#sensor-{sid}",
            "@type": "sosa:Sensor",
            "name": sensor.make_model,
            "description": f"{sensor.type} publishing on {sensor.topic}",
            "sosa:isHostedBy": {"@id": "#robot"},
            "additionalProperty": props,
        }
        archived = cal_paths.get(sensor.calibration_ref or "")
        if archived:
            sensor_entity["subjectOf"] = {"@id": archived}
        graph.append(sensor_entity)

    instruments = []
    if record.robot:
        instruments.append({"@id": "#robot"})
    if record.software.ros_distro:
        instruments.append({"@id": "#ros2"})
    if record.software.python_env:
        instruments.append({"@id": "#python-runtime"})
    instruments += [{"@id": f"#container-{c.name}"}
                    for c in record.software.docker_containers or []]
    mission: dict = {
        "@id": "#mission",
        "@type": "CreateAction",
        "name": f"Field mission {record.identity.mission_id}",
        "description": record.intent.goal,
        "agent": {"@id": "#operator"},
        "location": {"@id": "#place"},
        "result": {"@id": "./"},
        "additionalProperty": _confidence_user(),
    }
    if instruments:
        mission["instrument"] = instruments
    starts = [b.start_time for b in record.bags if b.start_time is not None]
    ends = [b.end_time for b in record.bags if b.end_time is not None]
    if starts:
        mission["startTime"] = min(starts).isoformat()
    if ends:
        mission["endTime"] = max(ends).isoformat()
    graph.append(mission)

    graph.append({"@id": ROS_FAIRY_ID, "@type": "SoftwareApplication",
                  "name": "ros-fairy",
                  "version": record.provenance.ros_fairy_version})
    if record.software.ros_distro:
        graph.append({"@id": "#ros2", "@type": "SoftwareApplication",
                      "name": "ROS 2", "version": record.software.ros_distro,
                      "url": "https://ros.org"})
    if record.software.python_env:
        pe = record.software.python_env
        py_props = [_pv("#python-runtime-executable", "executable",
                        pe.executable)]
        if pe.venv_path:
            py_props.append(_pv("#python-runtime-venv_path", "venv_path",
                                pe.venv_path))
        graph.append({"@id": "#python-runtime",
                      "@type": "SoftwareApplication",
                      "name": "Python",
                      "version": pe.version,
                      "additionalProperty": py_props})
    for container in record.software.docker_containers or []:
        entity = {"@id": f"#container-{container.name}",
                  "@type": "SoftwareApplication",
                  "name": container.name,
                  "softwareVersion": container.image}
        if container.digest:
            entity["identifier"] = container.digest
        graph.append(entity)

    bag_file_entities: list[dict] = []
    for i, bag in enumerate(record.bags, start=1):
        bag_dir_id = bag.path.rstrip("/")
        entity = {
            "@id": bag_dir_id + "/",
            "@type": "Dataset",
            "name": f"Recording {i}",
            "contentSize": str(bag.size_bytes),
            "encodingFormat": _ENCODING.get(bag.storage_format,
                                            "application/octet-stream"),
            "variableMeasured": [
                _pv(f"#bag{i}-measure-{j}", t.name, t.message_count,
                    description=t.type)
                for j, t in enumerate(bag.topics, start=1)],
        }
        # Omitted when the recording clock was unreliable (start/end unknown).
        if bag.start_time is not None:
            entity["dateCreated"] = bag.start_time.isoformat()
        if bag.start_time is not None and bag.end_time is not None:
            entity["temporalCoverage"] = (f"{bag.start_time.isoformat()}/"
                                          f"{bag.end_time.isoformat()}")
        comments = [w.plain_text for w in bag.health_warnings]
        if len(comments) == 1:
            entity["comment"] = comments[0]
        elif comments:
            entity["comment"] = comments
        # Per-file checksums (recorded at archive time) become File entities
        # with sha256, listed under the bag Dataset's hasPart. This makes the
        # bag bytes verifiable by any RO-Crate tool, not just `ros2 fairy verify`.
        parts = []
        for rel, digest in sorted(bag.file_sha256.items()):
            file_id = f"{bag_dir_id}/{rel}"
            bag_file_entities.append({
                "@id": file_id,
                "@type": "File",
                "name": Path(rel).name,
                "encodingFormat": _FILE_ENCODING.get(
                    Path(rel).suffix, "application/octet-stream"),
                "sha256": digest,
            })
            parts.append({"@id": file_id})
        if parts:
            entity["hasPart"] = parts
        graph.append(entity)

    graph.extend(file_entities)
    graph.extend(bag_file_entities)
    graph.extend(pv_entities)

    return {
        "@context": [
            "https://w3id.org/ro/crate/1.1/context",
            {"sosa": "http://www.w3.org/ns/sosa/",
             "ssn": "http://www.w3.org/ns/ssn/"},
        ],
        "@graph": graph,
    }


def write(record: MissionRecord, crate_root: Path,
          extra_files: list[dict] | None = None,
          license_url: str | None = None) -> None:
    document = build(record, extra_files, license_url)
    (crate_root / "ro-crate-metadata.json").write_text(
        json.dumps(document, indent=2, ensure_ascii=False) + "\n")
