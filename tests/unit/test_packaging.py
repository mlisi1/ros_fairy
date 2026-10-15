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
