"""ros-fairy: FAIR-compliant mission data capture for ROS 2 field robots."""

__version__ = "0.1.0"

# Record format, "MAJOR.MINOR". A minor bump only adds fields: an older
# ros-fairy still reads the record (setting the new fields aside). A major
# bump changes or removes fields: older versions refuse it and say so.
# 1.1 (2026-10-06): usb, udev_rules, ros_graph.parameters_not_captured,
#   docker_containers[].python_packages; apt_ros_versions and
#   docker_containers may be null.
# 1.2 (2026-10-06): provenance.harvested_by / assembled_by (the ros-fairy
#   build — version, git commit, code id — that captured the context and
#   that saved the crate).
# 1.3 (2026-10-15): provenance.edits — each change made to the briefing
#   after saving (`ros2 fairy mission_edit`): when, by which account, and
#   every changed field's value before and after, unless redacted
#   (`--redact`: old values removed, the person's name recorded).
SCHEMA_VERSION = "1.3"
