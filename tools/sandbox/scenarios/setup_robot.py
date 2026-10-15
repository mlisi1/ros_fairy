"""Set the sandbox robot up with `ros2 fairy setup`, answering as ana:
two sensors (gps0 on /fix, lidar0 on /scan)."""

import sys

from fairy_sandbox import IDENTITY, expect, finish, setup_robot, step

step("ros2 fairy setup (as ana)")
status = setup_robot()
expect(status == 0, f"setup exited {status}")
expect(IDENTITY.is_file(), f"{IDENTITY} written")
sys.exit(finish())
