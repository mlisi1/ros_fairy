# A throwaway robot for trying ROS F.A.I.R.y end to end: ROS 2, systemd
# (the watchdog is a system service), sudo, and three operator accounts.
# Built and driven by tools/sandbox/sandbox.sh; see docs/development.md.
ARG BASE=ros:jazzy-ros-base
FROM ${BASE}

ENV DEBIAN_FRONTEND=noninteractive

# systemd as PID 1, sudo for setup, pexpect to answer prompts from scripts.
# The harvest's optional tools (usbutils, pciutils, v4l-utils) are left out
# on purpose: a fresh robot often lacks them.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        systemd systemd-sysv dbus sudo \
        python3-pip python3-pexpect \
        rsync git procps less nano \
    && rm -rf /var/lib/apt/lists/*

# Units a container can't run, or that would only slow the boot.
RUN systemctl mask \
        systemd-udevd.service systemd-udevd-kernel.socket \
        systemd-udevd-control.socket systemd-firstboot.service \
        getty.target console-getty.service \
        systemd-remount-fs.service sys-kernel-debug.mount \
        sys-kernel-tracing.mount \
    && systemctl set-default multi-user.target

# ana: the engineer who sets the robot up (sudo, no password, so scripts
#      don't stall). ben, cleo: plain operators with no sudo.
RUN for user in ana ben cleo; do useradd --create-home --shell /bin/bash "$user"; done \
    && usermod -aG sudo ana \
    && echo 'ana ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/ana \
    && chmod 0440 /etc/sudoers.d/ana

# Every login shell has ROS sourced, as on a real robot. The distro is fixed
# at build time: login shells don't inherit the image's ENV. DDS uses UDP
# only (see fastdds-udp.xml).
COPY fastdds-udp.xml /etc/fastdds-udp.xml
RUN { echo "source /opt/ros/${ROS_DISTRO}/setup.bash"; \
      echo "export FASTRTPS_DEFAULT_PROFILES_FILE=/etc/fastdds-udp.xml"; \
    } > /etc/profile.d/ros.sh \
    && echo "[ -n \"\$ROS_DISTRO\" ] || . /etc/profile.d/ros.sh" \
        >> /etc/bash.bashrc

STOPSIGNAL SIGRTMIN+3
ENTRYPOINT []
CMD ["/sbin/init"]
