from meshtastic.serial_interface import SerialInterface
from meshtastic.util import findPorts
import time

def test_connection():
    try:
        # 尝试自动发现设备
        ports = findPorts()
        if not ports:
            print("未找到 Meshtastic 设备")
            return
            
        print(f"找到设备端口: {ports}")
        interface = SerialInterface(ports[0])
        
        # 打印基本信息
        print("\n基本信息:")
        print(f"我的节点信息: {interface.getMyNodeInfo()}")
        print(f"所有节点: {interface.nodes}")
        
        # 打印配置
        print("\n配置信息:")
        print(f"配置: {interface.showInfo()}")
        
        # 关闭连接
        interface.close()
        
    except Exception as e:
        print(f"错误: {str(e)}")
        raise e

if __name__ == "__main__":
    test_connection()
