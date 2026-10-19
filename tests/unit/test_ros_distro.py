from fairy.utils import ros_distro


def test_detect_reads_env(monkeypatch):
    monkeypatch.setenv("ROS_DISTRO", "jazzy")
    assert ros_distro.detect() == "jazzy"


def test_detect_normalises_case_and_whitespace(monkeypatch):
    monkeypatch.setenv("ROS_DISTRO", "  Humble ")
    assert ros_distro.detect() == "humble"


def test_detect_none_when_unsourced(monkeypatch):
    monkeypatch.delenv("ROS_DISTRO", raising=False)
    assert ros_distro.detect() is None
    monkeypatch.setenv("ROS_DISTRO", "   ")
    assert ros_distro.detect() is None


def test_default_storage_known_distros():
    assert ros_distro.default_storage("jazzy") == "mcap"
    assert ros_distro.default_storage("rolling") == "mcap"
    assert ros_distro.default_storage("humble") == "sqlite3"
    assert ros_distro.default_storage("iron") == "sqlite3"


def test_default_storage_unknown_falls_back():
    assert ros_distro.default_storage("ros_z9000") == \
        ros_distro.DEFAULT_STORAGE_FALLBACK == "sqlite3"


def test_default_storage_uses_detected_distro(monkeypatch):
    monkeypatch.setenv("ROS_DISTRO", "jazzy")
    assert ros_distro.default_storage() == "mcap"
    monkeypatch.delenv("ROS_DISTRO", raising=False)
    assert ros_distro.default_storage() == "sqlite3"


def test_capabilities(monkeypatch):
    monkeypatch.setenv("ROS_DISTRO", "JAZZY")
    caps = ros_distro.capabilities()
    assert caps.name == "jazzy"
    assert caps.default_storage == "mcap"


def test_infer_from_packages_picks_most_common_distro():
    pkgs = ["ros-jazzy-rclcpp", "ros-jazzy-rclpy", "ros-jazzy-nav2-msgs",
            "ros-humble-rosbag2", "ros-dev-tools", "python3-colcon"]
    assert ros_distro.infer_from_packages(pkgs) == "jazzy"


def test_infer_from_packages_none_when_no_distro_packages():
    assert ros_distro.infer_from_packages(["ros-dev-tools", "vim"]) is None
    assert ros_distro.infer_from_packages([]) is None
    assert ros_distro.infer_from_packages(None) is None
