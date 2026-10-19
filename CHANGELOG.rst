^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
Changelog for package ros_fairy
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

Forthcoming
-----------
First public release (0.1.0), for ROS 2 Jazzy on Ubuntu 24.04. It is
usable and field-tested on one robot, but nothing is stable yet: the
command line and the mission record format (schema 1.3) may still change
before 1.0.

* Missions: ``mission_start`` asks five plain-language questions (or takes
  them as options, for scripts and launch files); ``mission_record`` wraps
  ``ros2 bag record``, takes its options, and checks disk space, the
  briefing, the clock and any recording already running first; ``mission_close`` reviews the mission,
  grades its data quality, warns about duplicates, and saves or discards
  it; ``mission_status`` shows what is going on; ``mission_abort``,
  ``drop`` and ``adopt`` abandon a mission, take a recording out, or bring
  one in.
* A background watchdog service notices every recording, including plain
  ``ros2 bag record`` runs, the rosbag2 recorder node, and recorders inside
  Docker containers, and captures the context while it runs, retrying
  while ROS can't be reached. It follows the DDS settings the recording
  runs with (domain, RMW, discovery range), including those a container's
  start-up scripts export.
* Captured automatically: the robot and its owner, the ROS graph with
  every node's parameters, the URDF and ``/tf_static``, the sensors seen
  publishing, ROS and Python packages, Docker containers and their
  packages, hardware devices, USB power management and the udev rules that
  apply, system information, and the exact ros_fairy build that captured
  and saved each mission.
* Recording health: gaps, low data rates, clock jumps, cut-off recordings
  and declared sensors that never published, in plain language. Each
  mission is graded ok, degraded or poor, and ``list`` filters by grade.
* Archive: each mission is an RO-Crate folder with its recordings, a
  ``mission_record.json`` and a ``README.md``, checksummed file by file.
  Saving is crash-safe: an interrupted save is finished or rolled back,
  never left half-done.
* Afterwards: ``list`` (filters and word search), ``diff`` between two
  missions, ``verify`` of an archive or of an exported bundle without
  unpacking it, ``export`` to ``.zip``, ``.tar``, ``.tar.gz`` or
  ``.tar.zst``, ``mission_edit`` of the briefing (with ``--redact``),
  ``mission_delete``, ``repair`` of recordings made with a bad clock, and
  ``reindex``. Most verbs also print ``--json`` for scripts.
* Robot setup: ``setup`` writes the robot's identity, its sensors and
  recording defaults, and installs the watchdog service; its flags change
  one part later, and ``--refresh-env`` (or ``--from-container``) takes a
  changed ROS environment. ``doctor`` checks that the robot is ready and
  says how to fix what isn't. Operators record through the ``ros-fairy``
  group (``add_operator`` adds more accounts), and ``move_data`` moves the
  data to another disk.
* Installed with ``install.sh`` (pip, offering the optional tools the
  harvest uses) or in a colcon workspace; removed with ``uninstall.sh``,
  which keeps the saved missions. Documentation at
  https://mlisi1.github.io/ros_fairy/.

Known limits of this release:

* ROS 2 Jazzy only. Humble needs changes to ``install.sh`` and the
  recorder's keyboard controls.
* Meant for a robot's own computer: the watchdog follows every recording
  on the machine, so on a workstation it also follows unrelated ones.
* The robot's configuration is changed with ``setup``'s flags; there is no
  interactive editor yet.
