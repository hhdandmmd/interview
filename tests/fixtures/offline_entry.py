"""测试专用离线启动器：禁止TCP连接后执行同一个正式CLI，不模拟来源成功。"""

import runpy
import socket


def reject_connection(*args, **kwargs):
    """任何隐式联网立即失败，避免把代理配置误当断网验证。"""
    raise AssertionError("离线验收进程禁止建立网络连接")


socket.socket.connect = reject_connection
socket.socket.connect_ex = reject_connection
runpy.run_module("finpipe", run_name="__main__")
