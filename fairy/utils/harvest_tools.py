"""The outside tools the harvest uses, and the packages that provide them.

Without one, the matching part of the harvest is reported ``partial`` and
that detail is missing from every mission. One list, read by ``install.sh``
(which offers to install what's missing) and by ``doctor`` (which warns).
Docker isn't here: whether a robot runs containers is its own choice.

For install.sh, run as a module::

    python3 -m fairy.utils.harvest_tools apt    # missing apt packages
    python3 -m fairy.utils.harvest_tools pip    # missing pip packages
    python3 -m fairy.utils.harvest_tools list   # what's missing, and why
"""

import importlib.util
import os
import shutil
import sys
from typing import NamedTuple


class Tool(NamedTuple):
    command: str
    package: str  # the apt package that provides it
    gives: str    # what the harvest records with it, in plain words


class Module(NamedTuple):
    module: str
    package: str  # the pip package
    gives: str


TOOLS = (
    Tool("lsusb", "usbutils", "USB devices and their serial numbers"),
    Tool("lspci", "pciutils", "PCI devices (GPUs, network cards)"),
    Tool("v4l2-ctl", "v4l-utils", "cameras"),
    Tool("udevadm", "udev", "device details and the udev rules applied"),
    Tool("setpriv", "util-linux",
         "replaying udev rules safely (without root)"),
    Tool("dmesg", "util-linux", "kernel messages about connected devices"),
)

MODULES = (
    Module("rocrate", "rocrate",
           "the full RO-Crate check in `ros2 fairy verify`"),
)

# The watchdog runs as root, whose PATH has the sbin directories; an
# operator's shell may not.
_SEARCH_PATH = os.pathsep.join(
    [os.environ.get("PATH", ""), "/usr/local/sbin", "/usr/sbin", "/sbin"])


def missing_tools() -> list[Tool]:
    return [t for t in TOOLS if shutil.which(t.command, path=_SEARCH_PATH)
            is None]


def missing_modules() -> list[Module]:
    return [m for m in MODULES if importlib.util.find_spec(m.module) is None]


def apt_packages(tools: list[Tool]) -> list[str]:
    return list(dict.fromkeys(t.package for t in tools))


def install_hint(tools: list[Tool], modules: list[Module]) -> str:
    """The commands that install them, for a person to type."""
    hints = []
    if tools:
        hints.append(f"sudo apt install {' '.join(apt_packages(tools))}")
    if modules:
        names = " ".join(m.package for m in modules)
        hints.append(f"sudo python3 -m pip install --break-system-packages "
                     f"{names}")
    return "; ".join(hints)


def main(argv: list[str]) -> int:
    what = argv[0] if argv else "list"
    if what == "apt":
        print(" ".join(apt_packages(missing_tools())))
    elif what == "pip":
        print(" ".join(m.package for m in missing_modules()))
    elif what == "list":
        for tool in missing_tools():
            print(f"  {tool.command} ({tool.package}): {tool.gives}")
        for module in missing_modules():
            print(f"  {module.module} (Python): {module.gives}")
    else:
        print(f"unknown request {what!r}: apt, pip or list", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
