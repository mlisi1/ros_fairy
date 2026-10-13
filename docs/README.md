<img src="assets/fairy-intro.png" alt="" class="fairy-float" align="right" width="220">

# ros_fairy documentation

ros_fairy makes ROS 2 field-mission data FAIR (Findable, Accessible,
Interoperable, Reusable) without extra work in the field. The operator answers
five questions; a background service captures everything else about the robot
while it records; every saved mission becomes a checksummed
[RO-Crate](https://www.researchobject.org/ro-crate/) archive.

## For operators

- [Getting started](getting-started.md): install, set up a robot, check it's ready.
- [Operator guide](operator-guide.md): run a mission from start to finish, and
  what to do when something goes wrong.
- [Troubleshooting](troubleshooting.md): problems and fixes.

## Reference

- [Command reference](commands.md): every verb and option.
- [Configuration](configuration.md): `robot_identity.yaml`, paths, environment
  variables, permissions, the systemd unit.
- [What is captured](what-is-captured.md): every piece of context, recording
  health checks and quality grading.
- [Archive format](archive-format.md): the crate layout, the mission record
  schema, the RO-Crate entities, how saving stays crash-safe.

## For developers

- [Architecture](architecture.md): the watchdog's state machine, recorder
  detection, the harvest pipeline, recovery.
- [Development](development.md): code layout, conventions, tests and tools.
