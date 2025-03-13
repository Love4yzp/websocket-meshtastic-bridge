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

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

class MeshtasticBridge:
    def __init__(self, serial_port: str, host: str = '0.0.0.0', port: int = 8000):
        self.serial_port = serial_port
        self.host = host
        self.port = port
        self.interface: Optional[SerialInterface] = None
        self.node_list: List[Dict] = []
        self.connected_clients: Set[WebSocketServerProtocol] = set()
        self.running = False
        self.lock = threading.Lock()
        self.message_queue = asyncio.Queue()
        self.loop = None
        self.my_node_id = None  # 添加当前节点ID
        
    async def handle_client(self, websocket: WebSocketServerProtocol):
        """处理单个客户端连接"""
        try:
            # 添加客户端到连接集合
            with self.lock:
                self.connected_clients.add(websocket)
            logger.info(f"新客户端连接，当前连接数: {len(self.connected_clients)}")
            
            # 发送初始节点列表
            await websocket.send(json.dumps({
                'type': 'node_list',
                'data': self.node_list
            }))
            
            # 处理客户端消息
            async for message in websocket:
                try:
                    data = json.loads(message)
                    await self.handle_client_message(data)
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
            # 移除断开的客户端
            with self.lock:
                self.connected_clients.remove(websocket)
            logger.info(f"客户端断开连接，当前连接数: {len(self.connected_clients)}")
    
    async def handle_client_message(self, data: Dict):
        """处理客户端发送的消息"""
        try:
            message_type = data.get('type')
            if message_type == 'send_message':
                message = data.get('message', '')
                destination = data.get('destination')
                
                if not message:
                    raise ValueError("消息内容不能为空")
                
                success = self.send_to_meshtastic(message, destination)
                if not success:
                    raise Exception("发送消息失败")
                    
            elif message_type == 'get_nodes':
                # 重新获取节点列表
                self.update_node_list()
                
            else:
                raise ValueError(f"未知的消息类型: {message_type}")
                
        except Exception as e:
            logger.error(f"处理客户端消息时出错: {str(e)}")
            raise
    
    def send_to_meshtastic(self, message: str, destination: Optional[str] = None) -> bool:
        """发送消息到 Meshtastic 网络"""
        if not self.interface:
            logger.error("Meshtastic 接口未初始化")
            return False
            
        try:
            if destination:
                self.interface.sendText(message, destinationId=destination)
                logger.info(f"消息已发送到节点 {destination}: {message}")
            else:
                self.interface.sendText(message)
                logger.info(f"消息已广播: {message}")
            return True
        except Exception as e:
            logger.error(f"发送消息到 Meshtastic 失败: {str(e)}")
            return False
    
    def on_meshtastic_receive(self, packet: Dict, interface: SerialInterface):
        """处理从 Meshtastic 接收到的消息（同步版本）"""
        try:
            if packet['decoded']['portnum'] == 'TEXT_MESSAGE_APP':
                message = packet['decoded']['payload'].decode('utf-8')
                from_id = packet['fromId']
                sender = next((node['user']['shortName'] for node in self.node_list if node['id'] == from_id), 'Unknown')
                
                # 获取通道信息
                channel = packet.get('channel', 0)
                
                msg_obj = {
                    'type': 'text',
                    'from': from_id,
                    'sender': sender,
                    'channel': channel
                    'message': message,
                    'timestamp': datetime.now().isoformat(),
                }
                
                logger.info(f"收到消息: {sender}: {message} (Channel: {channel})")
                # 将消息放入队列
                asyncio.run_coroutine_threadsafe(self.message_queue.put(msg_obj), self.loop)
                
            elif packet['decoded']['portnum'] == 'POSITION_APP':
                position_data = packet.get('decoded', {}).get('position', {})
                from_id = packet['fromId']
                sender = next((node['user']['shortName'] for node in self.node_list if node['id'] == from_id), 'Unknown')
                
                # 获取通道信息
                channel = packet.get('channel', 0)
                
                position_obj = {
                    'type': 'position',
                    'from': from_id,
                    'sender': sender,
                    'position': {
                        'latitude': position_data.get('latitude', 0),
                        'longitude': position_data.get('longitude', 0),
                        'altitude': position_data.get('altitude', 0),
                        'time': datetime.fromtimestamp(position_data.get('time', 0)).isoformat() if position_data.get('time') else datetime.now().isoformat()
                    },
                    'timestamp': datetime.now().isoformat(),
                    'channel': channel
                }
                
                logger.info(f"收到位置更新: {sender} (Channel: {channel})")
                # 将消息放入队列
                asyncio.run_coroutine_threadsafe(self.message_queue.put(position_obj), self.loop)
                
        except Exception as e:
            logger.error(f"处理 Meshtastic 消息时出错: {str(e)}")
    
    async def process_message_queue(self):
        """处理消息队列中的消息"""
        while self.running:
            try:
                message = await self.message_queue.get()
                await self.broadcast_message(message)
            except Exception as e:
                logger.error(f"处理消息队列时出错: {str(e)}")
    
    async def broadcast_message(self, message: Dict):
        """广播消息到所有连接的客户端"""
        with self.lock:
            disconnected_clients = set()
            for client in self.connected_clients:
                try:
                    await client.send(json.dumps(message))
                except Exception as e:
                    logger.error(f"发送消息到客户端失败: {str(e)}")
                    disconnected_clients.add(client)
            
            # 移除断开的客户端
            self.connected_clients -= disconnected_clients
    
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
                        },
                        'position': node.get('position', {}),
                        'lastHeard': node.get('lastHeard', 0)
                    }
                    for node_id, node in node_info.items()
                ]
                logger.info(f"节点列表已更新，共 {len(self.node_list)} 个节点")
        except Exception as e:
            logger.error(f"更新节点列表失败: {str(e)}")
    
    def init_meshtastic_interface(self) -> bool:
        """初始化 Meshtastic 接口"""
        try:
            logger.info(f"正在初始化 Meshtastic 接口，串口: {self.serial_port}")
            self.interface = SerialInterface(self.serial_port)
            
            # 获取当前节点ID
            self.my_node_id = self.interface.getMyNodeInfo().get('num', None)
            logger.info(f"当前节点ID: {self.my_node_id}")
            
            self.update_node_list()
            
            # 订阅消息接收回调
            pub.subscribe(self.on_meshtastic_receive, "meshtastic.receive")
            logger.info("已订阅 meshtastic.receive 事件")
            
            return True
        except Exception as e:
            logger.error(f"初始化 Meshtastic 接口失败: {str(e)}")
            return False
    
    async def health_check(self):
        """健康检查和重连机制"""
        while self.running:
            if not self.interface or not hasattr(self.interface, '_SerialInterface__radioConfig'):
                logger.warning("Meshtastic 接口断开，尝试重新连接...")
                if self.interface:
                    try:
                        self.interface.close()
                    except:
                        pass
                self.interface = None
                self.init_meshtastic_interface()
            
            await asyncio.sleep(30)
    
    async def start(self):
        """启动 WebSocket 服务器"""
        if not self.init_meshtastic_interface():
            logger.error("无法初始化 Meshtastic 接口，程序退出")
            return
        
        self.running = True
        self.loop = asyncio.get_running_loop()
        
        # 启动健康检查和消息处理
        asyncio.create_task(self.health_check())
        asyncio.create_task(self.process_message_queue())
        
        # 启动 WebSocket 服务器
        async with serve(self.handle_client, self.host, self.port):
            logger.info(f"WebSocket 服务器已启动在 {self.host}:{self.port}")
            await asyncio.Future()  # 运行直到被取消

def main():
    bridge = MeshtasticBridge(
        serial_port='/dev/cu.usbmodemD83BDA45A9C81', # test device
        serial_port='/dev/ttyACM0'
        host='0.0.0.0',
        port=8000
    )
    
    try:
        asyncio.run(bridge.start())
    except KeyboardInterrupt:
        logger.info("程序被用户中断")
    except Exception as e:
        logger.error(f"程序异常退出: {str(e)}")

if __name__ == "__main__":
    main()
