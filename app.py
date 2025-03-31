import asyncio
import json
import logging
import threading
import time
import uuid
from datetime import datetime
from typing import Dict, List, Optional, Set
import signal
from websockets.server import serve, WebSocketServerProtocol
from meshtastic.serial_interface import SerialInterface
from meshtastic import portnums_pb2
from pubsub import pub
from meshtastic.util import findPorts
from config import settings
from models import MessageRequest, NodeInfo, WebSocketMessage, NodeList, MessageStatus
import asyncio_throttle

# 配置日志
logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

class MeshtasticBridge:
    def __init__(self):
        self.serial_port = settings.SERIAL_PORT
        self.host = settings.HOST
        self.port = settings.PORT
        self.interface: Optional[SerialInterface] = None
        self.node_list: List[Dict] = []
        self.connected_clients: Set[WebSocketServerProtocol] = set()
        self.running = False
        self.lock = threading.Lock()
        self.message_queue = asyncio.Queue()
        self.loop = None
        self.my_node_id = None
        self.throttler = asyncio_throttle.Throttler(
            rate_limit=settings.RATE_LIMIT_MESSAGES,
            period=60.0
        )
        self.shutdown_event = asyncio.Event()
        self.pending_messages: Dict[str, MessageStatus] = {}  # Track pending messages
        
    async def handle_client(self, websocket: WebSocketServerProtocol):
        """处理单个客户端连接"""
        try:
            # 等待确保服务器完全启动
            retry_count = 0
            while not self.running and retry_count < 5:
                await asyncio.sleep(1)
                retry_count += 1
                
            if not self.running:
                logger.error("服务器未完全启动，拒绝客户端连接")
                await websocket.close(1013, "服务器未就绪")
                return

            # 添加客户端到连接集合
            with self.lock:
                self.connected_clients.add(websocket)
            logger.info(f"新客户端连接，当前连接数: {len(self.connected_clients)}")
            
            try:
                # 发送初始节点列表
                node_list = NodeList(nodes=[NodeInfo(**node) for node in self.node_list])
                await websocket.send(WebSocketMessage(
                    type='node_list',
                    data=node_list.dict()
                ).json())
            except Exception as e:
                logger.error(f"发送初始节点列表失败: {str(e)}")
                return
            
            # 处理客户端消息
            async for message in websocket:
                try:
                    async with self.throttler:
                        data = MessageRequest.parse_raw(message)
                        await self.handle_client_message(data)
                except asyncio_throttle.ThrottleError:
                    await websocket.send(WebSocketMessage(
                        type='error',
                        message='消息发送过于频繁，请稍后再试'
                    ).json())
                except ValueError as e:
                    logger.error(f"消息验证失败: {str(e)}")
                    await websocket.send(WebSocketMessage(
                        type='error',
                        message=str(e)
                    ).json())
                except Exception as e:
                    logger.error(f"处理客户端消息时出错: {str(e)}")
                    await websocket.send(WebSocketMessage(
                        type='error',
                        message=str(e)
                    ).json())
                    
        except Exception as e:
            logger.error(f"处理客户端连接时出错: {str(e)}")
        finally:
            # 移除断开的客户端
            with self.lock:
                self.connected_clients.remove(websocket)
            logger.info(f"客户端断开连接，当前连接数: {len(self.connected_clients)}")
    
    async def handle_client_message(self, data: MessageRequest):
        """处理客户端发送的消息"""
        try:
            if len(data.message) > settings.MAX_MESSAGE_LENGTH:
                raise ValueError(f"消息长度超过限制 ({settings.MAX_MESSAGE_LENGTH} 字符)")
            
            if data.type == 'get_node_list':
                logger.info("收到获取节点列表请求")
                self.update_node_list()
                node_list = NodeList(nodes=[NodeInfo(**node) for node in self.node_list])
                await self.broadcast_message(WebSocketMessage(
                    type='node_list',
                    data=node_list.dict()
                ))
                return

            # Generate message ID if not provided
            message_id = data.message_id or str(uuid.uuid4())
            
            # Create message status
            message_status = MessageStatus(
                message_id=message_id,
                sent_time=datetime.now(),
                destination=data.destination,
                channel=data.channel,
                timeout=data.timeout
            )
            
            success = await self.send_to_meshtastic_with_ack(
                message_id=message_id,
                message=data.message,
                destination=data.destination,
                channel=data.channel,
                require_ack=data.require_ack,
                timeout=data.timeout
            )
            
            if success:
                await self.broadcast_message(WebSocketMessage(
                    type='success',
                    message='消息发送成功',
                    data={'message_id': message_id}
                ))
            else:
                await self.broadcast_message(WebSocketMessage(
                    type='error',
                    message='发送消息失败',
                    data={'message_id': message_id}
                ))
                
        except Exception as e:
            logger.error(f"处理客户端消息时出错: {str(e)}")
            logger.exception("详细错误信息：")
            await self.broadcast_message(WebSocketMessage(
                type='error',
                message=str(e)
            ))

    async def send_to_meshtastic_with_ack(self, message_id: str, message: str, 
                                         destination: Optional[str] = None, 
                                         channel: Optional[int] = None,
                                         require_ack: bool = True,
                                         timeout: float = 30.0) -> bool:
        """发送消息到 Meshtastic 网络并等待确认"""
        if not self.interface:
            logger.error("Meshtastic 接口未初始化")
            return False
            
        try:
            # Store message status
            self.pending_messages[message_id] = MessageStatus(
                message_id=message_id,
                sent_time=datetime.now(),
                destination=destination,
                channel=channel,
                timeout=timeout
            )
            
            # Send the message
            success = self.send_to_meshtastic(message, destination, channel)
            if not success:
                del self.pending_messages[message_id]
                return False

            if not require_ack:
                del self.pending_messages[message_id]
                return True

            # Wait for acknowledgment
            try:
                await asyncio.wait_for(
                    self._wait_for_ack(message_id),
                    timeout=timeout
                )
                return True
            except asyncio.TimeoutError:
                logger.warning(f"Message {message_id} timed out waiting for acknowledgment")
                if message_id in self.pending_messages:
                    self.pending_messages[message_id].status = "timeout"
                return False
            
        except Exception as e:
            logger.error(f"发送消息失败: {str(e)}")
            if message_id in self.pending_messages:
                self.pending_messages[message_id].status = "failed"
            return False

    async def _wait_for_ack(self, message_id: str):
        """等待消息确认"""
        while True:
            if message_id not in self.pending_messages:
                raise Exception("Message not found")
                
            if self.pending_messages[message_id].ack_received:
                self.pending_messages[message_id].status = "delivered"
                del self.pending_messages[message_id]
                return
                
            await asyncio.sleep(0.1)

    def on_meshtastic_receive(self, packet: Dict, interface: SerialInterface):
        """处理从 Meshtastic 接收到的消息"""
        try:
            port_num = packet['decoded']['portnum']
            from_id = packet['fromId']
            
            # Handle text messages
            if port_num == 'TEXT_MESSAGE_APP':
                message = packet['decoded']['payload'].decode('utf-8')
                node_info = self.interface.nodes.get(from_id, {})
                
                # 生成消息ID用于响应
                received_message_id = str(uuid.uuid4())
                
                # Check if this is an acknowledgment message
                if message.startswith('ACK:'):
                    ack_message_id = message[4:]
                    if ack_message_id in self.pending_messages:
                        self.pending_messages[ack_message_id].ack_received = True
                        logger.info(f"收到消息确认 ID: {ack_message_id}")
                        return

                # Regular message - send acknowledgment
                try:
                    logger.info(f"发送消息确认到 {from_id}")
                    self.interface.sendText(
                        text=f"ACK:{received_message_id}",
                        destinationId=from_id
                    )
                except Exception as e:
                    logger.error(f"发送确认失败: {str(e)}")
                
                # Forward message to WebSocket clients
                msg_obj = WebSocketMessage(
                    type='text',
                    data={
                        'messageId': received_message_id,
                        'fromId': from_id,
                        'nodeNum': node_info.get('num', 0),
                        'shortName': node_info.get('user', {}).get('shortName', ''),
                        'longName': node_info.get('user', {}).get('longName', ''),
                        'channel': packet.get('channel', 0),
                        'message': message,
                        'timestamp': datetime.now().isoformat()
                    }
                )
                
                asyncio.run_coroutine_threadsafe(
                    self.message_queue.put(msg_obj.dict()),
                    self.loop
                )
                logger.info(f"消息已转发到WebSocket客户端，来自: {from_id}")
                
            elif port_num == 'NODEINFO_APP':
                self.update_node_list()
                logger.info("节点信息已更新")
                
        except Exception as e:
            logger.error(f"处理 Meshtastic 消息时出错: {str(e)}")
            logger.exception("详细错误信息：")
    
    def init_meshtastic_interface(self) -> bool:
        """初始化 Meshtastic 接口"""
        try:
            logger.info("正在初始化 Meshtastic 接口...")
            
            # 尝试自动发现设备
            if self.serial_port is None:
                ports = findPorts()
                if not ports:
                    logger.error("未找到 Meshtastic 设备")
                    return False
                self.serial_port = ports[0]
                logger.info(f"自动选择串口: {self.serial_port}")
            
            # 创建接口连接
            self.interface = SerialInterface(self.serial_port)
            
            # 获取节点信息
            node_info = self.interface.getMyNodeInfo()
            if not node_info:
                logger.error("无法获取节点信息")
                return False
                
            self.my_node_id = node_info.get('nodeId')
            if not self.my_node_id:
                logger.error("无法获取本机节点ID")
                return False
                
            logger.info(f"当前节点ID: {self.my_node_id}")
            logger.info(f"节点信息: {self.interface.showInfo()}")
            
            # 更新节点列表
            self.update_node_list()
            
            # 订阅消息
            pub.subscribe(self.on_meshtastic_receive, "meshtastic.receive")
            logger.info("Meshtastic 接口初始化成功")
            
            return True
            
        except Exception as e:
            logger.error(f"初始化失败: {str(e)}")
            if self.interface:
                try:
                    self.interface.close()
                except:
                    pass
                self.interface = None
            return False

    async def process_message_queue(self):
        """处理消息队列中的消息"""
        while self.running:
            try:
                message = await self.message_queue.get()
                if not isinstance(message, dict):
                    message = message.dict()
                await self.broadcast_message(WebSocketMessage(**message))
                self.message_queue.task_done()
            except Exception as e:
                logger.error(f"处理消息队列时出错: {str(e)}")
                logger.exception("详细错误信息：")
            await asyncio.sleep(0.1)  # 避免CPU过度使用
            
    def update_node_list(self):
        """更新节点列表"""
        try:
            if not self.interface:
                logger.error("Meshtastic 接口未初始化")
                return
                
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
                    'nodeId': node.get('nodeId', ''),
                    'num': node.get('num', 0),
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
            logger.exception("详细错误信息：")
    
    async def start(self):
        """启动 WebSocket 服务器"""
        if not self.init_meshtastic_interface():
            raise RuntimeError("Meshtastic 接口初始化失败")
            
        self.loop = asyncio.get_running_loop()
        self.running = True
        
        # 启动消息处理循环
        asyncio.create_task(self.process_message_queue())
        
        async with serve(self.handle_client, self.host, self.port):
            logger.info(f"WebSocket 服务器已启动: ws://{self.host}:{self.port}")
            await self.shutdown_event.wait()
    
    async def shutdown(self):
        """优雅关闭服务器"""
        logger.info("正在关闭服务器...")
        self.running = False
        
        # 关闭所有客户端连接
        for client in self.connected_clients:
            try:
                await client.close(1001, "服务器关闭")
            except:
                pass
                
        # 清理 Meshtastic 接口
        if self.interface:
            try:
                self.interface.close()
            except:
                pass
                
        self.shutdown_event.set()
        logger.info("服务器已关闭")

async def main():
    bridge = MeshtasticBridge()
    
    # 注册信号处理
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(bridge.shutdown()))
    
    try:
        await bridge.start()
    except Exception as e:
        logger.error(f"服务器启动失败: {str(e)}")
        await bridge.shutdown()

if __name__ == "__main__":
    asyncio.run(main())
