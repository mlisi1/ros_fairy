"""A stand-in for the robot's base controller, for the screenshots: it
publishes /odom and has parameters (`ros2 topic pub`, which the other
sandbox talkers use, offers no parameter service), so a mission captures
them and `ros2 fairy diff` can show one changing with `ros2 param set`."""

import rclpy
from nav_msgs.msg import Odometry
from rclpy.node import Node


class BaseController(Node):
    def __init__(self):
        super().__init__("base_controller")
        self.declare_parameter("max_speed", 1.0)
        self.declare_parameter("wheel_radius", 0.165)
        self.declare_parameter("odom_frame", "odom")
        self.odom = self.create_publisher(Odometry, "/odom", 10)
        self.create_timer(0.1, self.publish)

    def publish(self):
        message = Odometry()
        message.header.stamp = self.get_clock().now().to_msg()
        message.header.frame_id = self.get_parameter("odom_frame").value
        message.child_frame_id = "base_link"
        self.odom.publish(message)


def main():
    rclpy.init()
    try:
        rclpy.spin(BaseController())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
