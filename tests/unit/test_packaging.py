"""Values that must be written out literally in more than one file.

setup.py and package.xml both need a version literal (ament_python requires
the package.xml one), so instead of build-time machinery this test pins them
to ``ros_fairy.__version__``. uninstall.sh and the systemd unit can't import
ros_fairy either (uninstall.sh must work on a broken install), so their names
and paths are checked against the package here.
"""

import re
import xml.etree.ElementTree as ET
from pathlib import Path

import ros_fairy
from ros_fairy.subcommands import setup
from ros_fairy.utils import paths

REPO = Path(__file__).resolve().parents[2]


def test_version_declarations_agree():
    package_xml = ET.parse(REPO / "package.xml").getroot()
    assert package_xml.findtext("version") == ros_fairy.__version__

    setup_py = (REPO / "setup.py").read_text()
    match = re.search(r"version=\"([^\"]+)\"", setup_py)
    assert match is not None, "setup.py has no version= literal"
    assert match.group(1) == ros_fairy.__version__


def _shell_default(script: str, name: str) -> str:
    """The value of ``NAME="..."`` or default of ``NAME="${VAR:-...}"``."""
    match = re.search(rf'^{name}="(?:\$\{{\w+:-)?([^"}}]+)', script, re.M)
    assert match is not None, f"{name} not found"
    return match.group(1)


def test_uninstall_script_matches_the_package():
    script = (REPO / "uninstall.sh").read_text()
    assert _shell_default(script, "SERVICE_NAME") == setup.SERVICE_NAME
    assert _shell_default(script, "GROUP_NAME") == setup.GROUP_NAME
    assert _shell_default(script, "CONFIG_DIR") == paths.DEFAULT_CONFIG_DIR
    assert _shell_default(script, "DEFAULT_VAR_DIR") == paths.DEFAULT_VAR_DIR
    assert f'"$CONFIG_DIR/{paths.paths_file().name}"' in script


def test_unit_files_match_the_package():
    shipped = REPO / "systemd" / setup.SERVICE_NAME
    packaged = REPO / "ros_fairy" / "watchdog" / setup.SERVICE_NAME
    assert shipped.read_text() == packaged.read_text()
    unit = packaged.read_text()
    assert f"EnvironmentFile=-{paths.DEFAULT_CONFIG_DIR}/watchdog.env" in unit
    assert f"Group={setup.GROUP_NAME}" in unit
    assert f"ExecStart={setup.SYSTEM_PYTHON} " in unit  # setup replaces it
    # Root, group ros-fairy, umask 0002: a cache it wrote would be
    # writable by every operator (found in /opt/ros after a real install).
    assert "Environment=PYTHONDONTWRITEBYTECODE=1" in unit


# setup.py's requirement -> its rosdep key in package.xml.
_ROSDEP_KEYS = {
    "setuptools": None,  # build only
    "pydantic": "python3-pydantic",
    "rich": "python3-rich",
    "PyYAML": "python3-yaml",
    "inotify_simple": "python3-inotify-simple-pip",
    "mcap": "python3-mcap-pip",
    "zstandard": "python3-zstandard",
}


def test_runtime_requirements_are_package_xml_dependencies():
    setup_py = (REPO / "setup.py").read_text()
    block = re.search(r"install_requires=\[(.*?)\]", setup_py, re.S).group(1)
    required = [re.split(r"[<>=!~ ]", r)[0]
                for r in re.findall(r"\"([^\"]+)\"", block)]
    unknown = [r for r in required if r not in _ROSDEP_KEYS]
    assert not unknown, f"add the rosdep key of {unknown} here and to " \
                        "package.xml"
    package_xml = ET.parse(REPO / "package.xml").getroot()
    depends = {e.text for e in package_xml.iter()
               if e.tag in ("depend", "exec_depend")}
    missing = [_ROSDEP_KEYS[r] for r in required
               if _ROSDEP_KEYS[r] and _ROSDEP_KEYS[r] not in depends]
    assert not missing, f"package.xml lacks exec_depend {missing}"
