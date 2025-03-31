import asyncio
import json
import logging
import threading
import time
from datetime import datetime
from typing import Dict, List, Optional, Set
from websockets.server import serve, WebSocketServerProtocol
from meshtastic.serial_interface import SerialInterface
from meshtastic import portnums_pb2
from pubsub import pub
from meshtastic.util import findPorts

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

class MeshtasticBridge:
    def __init__(self, serial_port: str, host: str = '0.0.0.0', port: int = 5800):
        self.serial_port = serial_port
        self.host = host
        self.port = port
        self.interface: Optional[SerialInterface] = None
        self.node_list: List[Dict] = []
        self.connected_clients: Set[WebSocketServerProtocol] = set()
        self.running = False
        self.lock = threading.Lock()
        
        # 消息队列
        self.outbound_queue = asyncio.Queue()
        self.inbound_queue = asyncio.Queue()
        
        self.loop = None
        self.my_node_id = None
    
    async def handle_client(self, websocket: WebSocketServerProtocol):
        """处理单个客户端连接"""
        try:
            with self.lock:
                self.connected_clients.add(websocket)
            logger.info(f"新客户端连接，当前连接数: {len(self.connected_clients)}")
            
            # 发送初始节点列表
            await websocket.send(json.dumps({
                'type': 'node_list',
                'data': self.node_list
            }))
            
            async for message in websocket:
                try:
                    data = json.loads(message)
                    await self.handle_client_message(data, websocket)
                except json.JSONDecodeError:
                    logger.error("收到无效的 JSON 消息")
                    await websocket.send(json.dumps({
                        'type': 'error',
                        'message': '无效的消息格式'
                    }))
                except Exception as e:
                    logger.error(f"处理客户端消息时出错: {str(e)}")
                    await websocket.send(json.dumps({
                        'type': 'error',
                        'message': str(e)
                    }))
        except Exception as e:
            logger.error(f"处理客户端连接时出错: {str(e)}")
        finally:
            with self.lock:
                self.connected_clients.remove(websocket)
            logger.info(f"客户端断开连接，当前连接数: {len(self.connected_clients)}")
    
    async def handle_client_message(self, data: Dict, websocket: WebSocketServerProtocol):
        """处理客户端发送的消息"""
        try:
            message = data.get('message', '')
            destination = data.get('destination')
            channel = data.get('channel', 0)
            
            if not message:
                raise ValueError("消息内容不能为空")
            
            # 创建消息对象
            message_obj = {
                'text': message,
                'destination': destination,
                'channel': channel,
                'timestamp': time.time(),
                'source_client': websocket
            }
            
            # 发送消息
            success = await self._send_message_to_meshtastic(
                message=message,
                destination=destination,
                channel=channel
            )
            
            if not success:
                raise Exception("发送消息失败")
            
        except Exception as e:
            logger.error(f"处理客户端消息时出错: {str(e)}")
            await websocket.send(json.dumps({
                'type': 'error',
                'message': str(e)
            }))
    
    async def _send_message_to_meshtastic(self, message: str, destination: Optional[str] = None, channel: Optional[int] = None) -> bool:
        """发送消息到 Meshtastic 网络"""
        if not self.interface:
            logger.error("Meshtastic 接口未初始化")
            return False
            
        try:
            # 验证目标节点是否存在（如果是 DM）
            if destination:
                if not destination.startswith('!'):
                    logger.error(f"无效的目标节点ID格式: {destination}")
                    return False
                if destination not in self.interface.nodes:
                    logger.error(f"目标节点不存在: {destination}")
                    return False
            
            # 验证频道号（如果是广播）
            if channel is not None and (channel < 0 or channel > 7):
                logger.error(f"无效的频道号: {channel}")
                return False

            # 使用 asyncio 执行器运行同步的 sendText 方法
            loop = asyncio.get_running_loop()
            if destination:
                # DM: 使用 nodeId 发送到特定节点
                await loop.run_in_executor(
                    None,
                    lambda: self.interface.sendText(
                        text=message,
                        destinationId=destination
                    )
                )
            else:
                # 广播: 在指定频道发送
                await loop.run_in_executor(
                    None,
                    lambda: self.interface.sendText(
                        text=message,
                        channelIndex=channel if channel is not None else 0
                    )
                )
            return True
        except Exception as e:
            logger.error(f"发送消息到 Meshtastic 失败: {str(e)}")
            logger.exception("详细错误信息：")
            return False
    
    def on_meshtastic_receive(self, packet: Dict, interface: SerialInterface):
        """处理从 Meshtastic 接收到的消息"""
        try:
            port_num = packet['decoded']['portnum']
            
            # 只处理文本消息
            if port_num == 'TEXT_MESSAGE_APP':
                message = packet['decoded']['payload'].decode('utf-8')
                from_id = packet['fromId']
                node_info = self.interface.nodes.get(from_id, {})
                
                try:
                    # 解析接收到的消息
                    message_data = json.loads(message)
                    received_timestamp = message_data.get('ss')
                    
                    if received_timestamp:
                        # 计算响应时间（转换为秒）
                        current_time = int(time.time() * 1000)
                        response_time = (current_time - received_timestamp) / 1000.0
                        logger.info(f"消息响应时间: {response_time:.3f}秒")
                        
                        msg_obj = {
                            'type': 'text',
                            'fromId': from_id,
                            'nodeNum': node_info.get('num', 0),
                            'shortName': node_info.get('user', {}).get('shortName', 'Unknown'),
                            'longName': node_info.get('user', {}).get('longName', ''),
                            'channel': packet.get('channel', 0),
                            'message': message,
                            'timestamp': datetime.now().isoformat(),
                            'response_time_s': response_time
                        }
                    else:
                        msg_obj = {
                            'type': 'text',
                            'fromId': from_id,
                            'nodeNum': node_info.get('num', 0),
                            'shortName': node_info.get('user', {}).get('shortName', 'Unknown'),
                            'longName': node_info.get('user', {}).get('longName', ''),
                            'channel': packet.get('channel', 0),
                            'message': message,
                            'timestamp': datetime.now().isoformat()
                        }
                except json.JSONDecodeError:
                    # 如果消息不是JSON格式，使用默认消息对象
                    msg_obj = {
                        'type': 'text',
                        'fromId': from_id,
                        'nodeNum': node_info.get('num', 0),
                        'shortName': node_info.get('user', {}).get('shortName', 'Unknown'),
                        'longName': node_info.get('user', {}).get('longName', ''),
                        'channel': packet.get('channel', 0),
                        'message': message,
                        'timestamp': datetime.now().isoformat()
                    }
                
                # 将消息放入接收队列
                asyncio.run_coroutine_threadsafe(
                    self.inbound_queue.put(msg_obj),
                    self.loop
                )
                
                logger.info(f"收到文本消息: {message} (来自: {from_id})")
                
        except Exception as e:
            logger.error(f"处理接收到的消息时出错: {str(e)}")
            logger.exception("详细错误信息：")
    
    async def process_inbound_queue(self):
        """处理接收队列中的消息"""
        while self.running:
            try:
                message = await self.inbound_queue.get()
                await self.broadcast_message(message)
                self.inbound_queue.task_done()
            except Exception as e:
                logger.error(f"处理接收队列时出错: {str(e)}")
                await asyncio.sleep(1)
    
    async def broadcast_message(self, message: Dict):
        """广播消息到所有连接的客户端"""
        disconnected_clients = set()
        
        for client in self.connected_clients:
            try:
                await client.send(json.dumps(message))
            except Exception as e:
                logger.error(f"向客户端发送消息失败: {str(e)}")
                disconnected_clients.add(client)
        
        # 移除断开的客户端
        self.connected_clients -= disconnected_clients
    
    def init_meshtastic_interface(self) -> bool:
        """初始化 Meshtastic 接口"""
        try:
            logger.info(f"正在初始化 Meshtastic 接口，串口: {self.serial_port}")
            logger.info("尝试创建 SerialInterface 实例...")
            self.interface = SerialInterface(self.serial_port)
            logger.info("SerialInterface 实例创建成功")
            
            # 获取当前节点ID
            logger.info("正在获取节点信息...")
            self.my_node_id = self.interface.getMyNodeInfo().get('nodeId', None)
            logger.info(f"当前节点ID: {self.my_node_id}")
            
            logger.info("正在更新节点列表...")
            self.update_node_list()
            logger.info("节点列表更新完成")
            
            # 订阅消息接收回调
            logger.info("正在订阅消息接收事件...")
            pub.subscribe(self.on_meshtastic_receive, "meshtastic.receive")
            logger.info("已订阅 meshtastic.receive 事件")
            
            return True
        except Exception as e:
            logger.error(f"初始化 Meshtastic 接口失败: {str(e)}")
            logger.exception("详细错误信息：")
            return False
    
    def update_node_list(self):
        """更新节点列表"""
        try:
            if self.interface:
                node_info = self.interface.nodes
                self.node_list = [
                    {
                        'id': node_id,
                        'user': {
                            'shortName': node.get('user', {}).get('shortName', 'Unknown'),
                            'longName': node.get('user', {}).get('longName', ''),
                            'macaddr': node.get('user', {}).get('macaddr', ''),
                            'hwModel': node.get('user', {}).get('hwModel', ''),
                        },
                        'position': {
                            'latitude': node.get('position', {}).get('latitude', 0),
                            'longitude': node.get('position', {}).get('longitude', 0),
                            'altitude': node.get('position', {}).get('altitude', 0),
                            'time': node.get('position', {}).get('time', 0),
                        },
                        'deviceMetrics': {
                            'batteryLevel': node.get('deviceMetrics', {}).get('batteryLevel', 0),
                            'voltage': node.get('deviceMetrics', {}).get('voltage', 0),
                            'channelUtilization': node.get('deviceMetrics', {}).get('channelUtilization', 0),
                            'airUtilTx': node.get('deviceMetrics', {}).get('airUtilTx', 0),
                        },
                        'nodeId': node.get('nodeId', 0),
                        'snr': node.get('snr', 0),
                        'lastHeard': node.get('lastHeard', 0),
                        'rssi': node.get('rssi', 0),
                        'channel': node.get('channel', 0)
                    }
                    for node_id, node in node_info.items()
                ]
                logger.info(f"节点列表已更新，共 {len(self.node_list)} 个节点")
        except Exception as e:
            logger.error(f"更新节点列表失败: {str(e)}")
    
    async def start(self):
        """启动 WebSocket 服务器"""
        if not self.init_meshtastic_interface():
            logger.error("无法初始化 Meshtastic 接口，程序退出")
            return
        
        self.running = True
        self.loop = asyncio.get_running_loop()
        
        # 启动健康检查和消息处理
        asyncio.create_task(self.process_inbound_queue())
        
        # 启动 WebSocket 服务器
        async with serve(self.handle_client, self.host, self.port):
            logger.info(f"WebSocket 服务器已启动在 {self.host}:{self.port}")
            await asyncio.Future()  # 运行直到被取消

def main():
    bridge = None
    try:
        # 尝试自动查找串口
        available_ports = findPorts()
        
        if not available_ports:
            logger.error("未找到任何 Meshtastic 设备")
            return
            
        if len(available_ports) > 1:
            logger.warning(f"找到多个设备: {available_ports}")
            logger.info(f"将使用第一个设备: {available_ports[0]}")
            
        serial_port = available_ports[0]
        logger.info(f"自动选择串口: {serial_port}")
        
        bridge = MeshtasticBridge(
            serial_port=serial_port,
            host='0.0.0.0',
            port=5800
        )
        
        asyncio.run(bridge.start())
    except KeyboardInterrupt:
        logger.info("程序被用户中断")
    except Exception as e:
        logger.error(f"程序异常退出: {str(e)}")
        logger.exception("详细错误信息：")
    finally:
        if bridge and bridge.interface:
            try:
                bridge.interface.close()
                logger.info("串口已关闭")
            except:
                pass

if __name__ == "__main__":
    main()
