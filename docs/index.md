<img src="assets/fairy-intro.png" alt="" class="fairy-float" align="right" width="220">

# ROS F.A.I.R.y

ROS F.A.I.R.y (the `ros_fairy` ROS 2 package) makes field-mission data
[FAIR](references.md#fair): Findable, Accessible, Interoperable and Reusable,
without extra work in the field. The operator answers five questions; a
background service captures everything else about the robot while it records;
every saved mission becomes a checksummed [RO-Crate](references.md#ro-crate)
archive.

## For operators

- [Getting started](getting-started.md): install, set up a robot, check it's ready.
- [Operator guide](operator-guide.md): run a mission from start to finish, and
  what to do when something goes wrong.
- [Troubleshooting](troubleshooting.md): problems and fixes.

## Reference

- [Command reference](commands.md): every verb and option.
- [What is captured](what-is-captured.md): every piece of context, recording
  health checks and quality grading.
- [Configuration](configuration.md): `robot_identity.yaml`, paths, environment
  variables, permissions, the systemd unit.
- [Archive format](archive-format.md): the crate layout, the mission record
  schema, the RO-Crate entities, how saving stays crash-safe.
- [Standards and references](references.md): FAIR, RO-Crate and SOSA/SSN,
  with citations.

## For developers

- [Architecture](architecture.md): the watchdog's state machine, recorder
  detection, the harvest pipeline, recovery.
- [Development](development.md): code layout, conventions, tests, the sandbox
  robot and tools.
