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
            logger.info(f"收到客户端消息: {data}")  # 记录收到的完整消息
            
            # 检查是否是请求节点列表的命令
            if data.get('type') == 'get_node_list':
                logger.info("收到获取节点列表请求")
                # 先更新节点列表
                self.update_node_list()
                # 然后发送给客户端
                await self.broadcast_message({
                    'type': 'node_list',
                    'data': self.node_list
                })
                return
            
            # 处理发送消息的请求
            message = data.get('message', '')
            destination = data.get('destination')
            channel = data.get('channel', 0)
            
            if not message:
                raise ValueError("消息内容不能为空")
            
            # logger.info(f"正在处理消息: message={message}, destination={destination}, channel={channel}")
            
            success = self.send_to_meshtastic(
                message=message,
                destination=destination,
                channel=channel
            )
            if success:
                await self.broadcast_message({
                    'type': 'success',
                    'message': '消息发送成功'
                })
            else:
                await self.broadcast_message({
                    'type': 'error',
                    'message': '发送消息失败'
                })
                
        except Exception as e:
            logger.error(f"处理客户端消息时出错: {str(e)}")
            logger.exception("详细错误信息：")
            await self.broadcast_message({
                'type': 'error',
                'message': str(e)
            })
    
    def send_to_meshtastic(self, message: str, destination: Optional[str] = None, channel: Optional[int] = None) -> bool:
        """发送消息到 Meshtastic 网络"""
        if not self.interface:
            logger.error("Meshtastic 接口未初始化")
            return False
            
        try:
            logger.info(f"准备发送消息: message={message}, destination={destination}, channel={channel}")
            
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

            if destination:
                # DM: 使用 nodeId 发送到特定节点
                logger.info(f"尝试发送 DM 到节点 {destination}")
                self.interface.sendText(
                    text=message,
                    destinationId=destination
                )
                logger.info(f"DM 发送成功")
            else:
                # 广播: 在指定频道发送
                logger.info(f"尝试在频道 {channel} 广播消息")
                self.interface.sendText(
                    text=message,
                    channelIndex=channel if channel is not None else 0
                )
                logger.info(f"广播发送成功")
            return True
        except Exception as e:
            logger.error(f"发送消息到 Meshtastic 失败: {str(e)}")
            logger.exception("详细错误信息：")
            return False
    
    def on_meshtastic_receive(self, packet: Dict, interface: SerialInterface):
        """处理从 Meshtastic 接收到的消息（同步版本）"""
        try:
            port_num = packet['decoded']['portnum']
            
            # 只处理我们关心的消息类型
            if port_num == 'TEXT_MESSAGE_APP':
                message = packet['decoded']['payload'].decode('utf-8')
                from_id = packet['fromId']
                node_info = self.interface.nodes.get(from_id, {})
                
                msg_obj = {
                    'type': 'text',
                    'fromId': from_id,
                    'nodeNum': node_info.get('num', 0),
                    'shortName': node_info.get('user', {}).get('shortName', ''),
                    'longName': node_info.get('user', {}).get('longName', ''),
                    'channel': packet.get('channel', 0),
                    'message': message,
                    'timestamp': datetime.now().isoformat()
                }
                
                # 转发到 WebSocket
                asyncio.run_coroutine_threadsafe(self.message_queue.put(msg_obj), self.loop)
                logger.info(f"收到文本消息: {message} (来自: {from_id})")
                
            elif port_num == 'NODEINFO_APP':
                # 处理节点信息更新
                logger.info("收到节点信息更新")
                self.update_node_list()
                
            elif port_num == 'POSITION_APP':
                # 如果需要处理位置信息，可以在这里添加
                pass
            else:
                # 对于其他类型的消息，只记录日志但不转发
                logger.debug(f"收到未处理的消息类型: {port_num}")
                
        except Exception as e:
            logger.error(f"处理 Meshtastic 消息时出错: {str(e)}")

    def _get_message_type(self, port_num: str) -> str:
        """根据端口号获取消息类型"""
        type_mapping = {
            'TEXT_MESSAGE_APP': 'text',
            'POSITION_APP': 'position',
            'NODEINFO_APP': 'nodeinfo',
            'ROUTING_APP': 'routing',
            'ADMIN_APP': 'admin',
            'TELEMETRY_APP': 'telemetry',
            'REMOTEHARD_APP': 'remote',
            'SIMULATOR_APP': 'simulator'
            # 可以根据需要添加更多类型
        }
        return type_mapping.get(port_num, 'unknown')
    
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
    
    async def health_check(self):
        """健康检查和重连机制"""
        while self.running:
            try:
                if not self.interface:
                    logger.warning("Meshtastic 接口未连接，尝试连接...")
                    if self.init_meshtastic_interface():
                        logger.info("Meshtastic 接口连接成功")
                        await self._notify_connection_status(True)
                else:
                    # 检查连接状态
                    is_healthy = await self._check_connection_health()
                    if not is_healthy:
                        logger.warning("检测到连接异常，尝试重新连接...")
                        await self._safe_close_interface()
                        await self._notify_connection_status(False)
                        self.interface = None
                
            except Exception as e:
                logger.error(f"健康检查出错: {str(e)}")
                logger.exception("详细错误信息：")
            
            await asyncio.sleep(30)  # 30秒检查一次

    async def _check_connection_health(self) -> bool:
        """检查连接健康状态"""
        try:
            if not self.interface:
                return False

            # 尝试发送一个简单的请求（使用 asyncio 避免阻塞）
            loop = asyncio.get_running_loop()
            try:
                # 设置超时时间为 3 秒
                await asyncio.wait_for(
                    loop.run_in_executor(None, self._check_node_info),
                    timeout=3.0
                )
                return True
            except asyncio.TimeoutError:
                logger.warning("获取节点信息超时")
                return False
            except Exception as e:
                logger.warning(f"获取节点信息失败: {str(e)}")
                return False

        except Exception as e:
            logger.error(f"健康检查过程出错: {str(e)}")
            return False

    def _check_node_info(self):
        """检查节点信息（在执行器中运行）"""
        try:
            # 尝试获取节点信息来验证连接
            node_info = self.interface.getMyNodeInfo()
            if not node_info:
                raise Exception("无法获取节点信息")
            return True
        except Exception as e:
            logger.warning(f"节点信息检查失败: {str(e)}")
            raise

    async def _safe_close_interface(self):
        """安全关闭接口"""
        if self.interface:
            try:
                # 使用 asyncio 避免阻塞
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, self._close_interface)
            except Exception as e:
                logger.error(f"关闭接口时出错: {str(e)}")

    def _close_interface(self):
        """实际执行关闭操作"""
        try:
            # 1. 取消所有订阅
            try:
                pub.unsubscribe(self.on_meshtastic_receive, "meshtastic.receive")
            except:
                pass

            # 2. 关闭串口连接
            try:
                self.interface.close()
            except:
                pass

            # 3. 清理资源
            self.interface = None
            self.my_node_id = None
            self.node_list = []

        except Exception as e:
            logger.error(f"清理资源时出错: {str(e)}")
            raise
    
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
