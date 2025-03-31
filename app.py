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
        self.connection_ready = asyncio.Event()  # 添加连接就绪状态
        self.last_successful_check = 0  # 上次成功健康检查的时间戳
        self.transmit_lock = asyncio.Lock()  # 添加发送锁，确保半双工通信
        self.outgoing_message_queue = asyncio.Queue()  # 添加发送消息队列
        self.last_transmit_time = 0  # 上次发送消息的时间戳
        self.consecutive_sends = 0  # 连续发送消息的计数
        self.last_forced_receive = 0  # 上次强制接收窗口的时间戳
        self.max_consecutive_sends = 2  # 最大连续发送次数，之后强制接收窗口，从3减少到2
        self.resource_contention_count = 0  # 资源竞争计数
        self.interface_lock = threading.RLock()  # 添加接口访问锁，可重入
        self.receive_window_duration = 6.0  # 接收窗口持续时间（秒），设为6秒以适应空中时间
        self.min_time_between_sends = 2.0  # 两次发送之间的最小时间间隔（秒）

    async def handle_client(self, websocket: WebSocketServerProtocol):
        """处理单个socket Client连接"""
        try:
            # 等待连接就绪
            try:
                await asyncio.wait_for(self.connection_ready.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning("等待连接就绪超时，但仍将继续处理socket Client")
                
            # 添加客户端到连接集合
            with self.lock:
                self.connected_clients.add(websocket)
            logger.info(f"新socket Client连接，当前连接数: {len(self.connected_clients)}")
            
            # 发送初始Node列表
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
                    logger.error(f"处理socket Client消息时出错: {str(e)}")
                    await websocket.send(json.dumps({
                        'type': 'error',
                        'message': str(e)
                    }))
                    
        except Exception as e:
            logger.error(f"处理socket Client连接时出错: {str(e)}")
        finally:
            # 移除断开的客户端
            with self.lock:
                self.connected_clients.remove(websocket)
            logger.info(f"socket Client断开连接，当前连接数: {len(self.connected_clients)}")
    
    async def handle_client_message(self, data: Dict):
        """处理客户端发送的消息"""
        try:
            logger.info(f"收到客户端消息: {data}")  # 记录收到的完整消息
            
            # 检查是否是请求Node列表的命令
            if data.get('type') == 'get_node_list':
                logger.info("收到获取Node列表请求")
                # 先更新Node列表
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
            
            # 将消息放入发送队列，而不是立即发送
            await self.outgoing_message_queue.put({
                'message': message,
                'destination': destination,
                'channel': channel,
                'timestamp': time.time()
            })
            
            # 通知客户端消息已加入队列
            await self.broadcast_message({
                'type': 'queued',
                'message': '消息已加入发送队列'
            })
                
        except Exception as e:
            logger.error(f"处理客户端消息时出错: {str(e)}")
            logger.exception("详细错误信息：")
            await self.broadcast_message({
                'type': 'error',
                'message': str(e)
            })
    
    async def send_to_meshtastic(self, message: str, destination: Optional[str] = None, channel: Optional[int] = None) -> bool:
        """发送消息到 Meshtastic 网络（异步版本）"""
        if not self.interface:
            logger.error("Meshtastic 接口未初始化")
            return False
            
        try:
            logger.info(f"准备发送消息: message={message}, destination={destination}, channel={channel}")
            
            # 验证目标Node是否存在（如果是 DM）
            if destination:
                if not destination.startswith('!'):
                    logger.error(f"无效的目标Node ID格式: {destination}")
                    return False
                
                # 使用锁保护Node访问
                async with asyncio.Lock():
                    # 在异步上下文中执行同步操作
                    has_node = await self.loop.run_in_executor(
                        None,
                        lambda: hasattr(self.interface, 'nodes') and destination in self.interface.nodes
                    )
                    if not has_node:
                        logger.error(f"目标Node不存在: {destination}")
                        return False
            
            # 验证频道号（如果是广播）
            if channel is not None and (channel < 0 or channel > 7):
                logger.error(f"无效的频道号: {channel}")
                return False

            # 记录发送时间
            self.last_transmit_time = time.time()

            # 使用异步锁保护接口访问
            async with asyncio.Lock():
                # 在异步上下文中执行同步的发送操作
                if destination:
                    # DM: 使用 nodeId 发送到特定Node
                    logger.info(f"尝试发送 DM 到Node {destination}")
                    success = await self.loop.run_in_executor(
                        None,
                        lambda: self._execute_send(message, destination=destination)
                    )
                    if success:
                        logger.info(f"DM 发送成功")
                else:
                    # 广播: 在指定频道发送
                    channel_idx = channel if channel is not None else 0
                    success = await self.loop.run_in_executor(
                        None,
                        lambda: self._execute_send(message, channel_index=channel_idx)
                    )
                    if success:
                        logger.info(f"广播发送成功")
                
                return success
                
        except OSError as e:
            # 处理资源暂时不可用的错误
            if "Resource temporarily unavailable" in str(e):
                self.resource_contention_count += 1
                logger.warning(f"资源暂时不可用 (计数: {self.resource_contention_count}), 将重试")
                # 如果连续出现多次资源竞争，考虑重置接口
                if self.resource_contention_count >= 3:
                    logger.error("连续多次资源竞争，将在下次健康检查中重置接口")
                    self.connection_ready.clear()  # 标记连接为未就绪
                return False
            else:
                logger.error(f"发送消息到 Meshtastic 失败: {str(e)}")
                logger.exception("详细错误信息：")
                return False
        except Exception as e:
            logger.error(f"发送消息到 Meshtastic 失败: {str(e)}")
            logger.exception("详细错误信息：")
            return False
    
    def _execute_send(self, message: str, destination: Optional[str] = None, channel_index: Optional[int] = None) -> bool:
        """执行实际的发送操作（同步版本，在线程池中执行）"""
        try:
            with self.interface_lock:
                if destination:
                    self.interface.sendText(
                        text=message,
                        destinationId=destination
                    )
                else:
                    self.interface.sendText(
                        text=message,
                        channelIndex=channel_index
                    )
            return True
        except Exception as e:
            logger.error(f"执行发送操作失败: {str(e)}")
            return False
    
    async def process_outgoing_messages(self):
        """处理发送消息队列，考虑半双工通信的限制"""
        while self.running:
            try:
                # 检查是否需要强制接收窗口 - 只在有发送活动后才考虑
                current_time = time.time()
                if self.consecutive_sends > 0 and (self.consecutive_sends >= self.max_consecutive_sends or (current_time - self.last_forced_receive > 6)):
                    logger.debug(f"强制接收窗口 (连续发送: {self.consecutive_sends}, 上次接收窗口: {current_time - self.last_forced_receive:.1f}秒前)")
                    # 强制接收窗口
                    await self.force_receive_window()
                    continue
                
                # 等待队列中有消息
                if self.outgoing_message_queue.empty():
                    # 如果没有消息要发送，不要创建不必要的接收窗口，除非有发送活动
                    if self.consecutive_sends > 0 and current_time - self.last_forced_receive > 6.0:
                        # 只有在有过发送活动且超过6秒没有接收窗口时才创建
                        await self.force_receive_window()
                    else:
                        await asyncio.sleep(0.5)
                    continue
                
                # 检查是否可以发送（连接就绪且获取发送锁）
                if not self.connection_ready.is_set():
                    logger.warning("连接未就绪，延迟发送消息")
                    await asyncio.sleep(1)
                    continue
                
                # 获取发送锁（确保半双工通信）
                try:
                    async with self.transmit_lock:
                        # 检查距离上次发送是否有足够的间隔
                        current_time = time.time()
                        time_since_last_transmit = current_time - self.last_transmit_time
                        if time_since_last_transmit < self.min_time_between_sends:
                            await asyncio.sleep(self.min_time_between_sends - time_since_last_transmit)
                        
                        # 从队列获取消息
                        message_data = await self.outgoing_message_queue.get()
                        
                        # 发送消息（现在是异步的）
                        success = await self.send_to_meshtastic(
                            message=message_data['message'],
                            destination=message_data.get('destination'),
                            channel=message_data.get('channel')
                        )
                        
                        if success:
                            # 更新连续发送计数
                            self.consecutive_sends += 1
                            self.resource_contention_count = 0  # 重置资源竞争计数
                            
                            # 通知客户端发送结果
                            await self.broadcast_message({
                                'type': 'success',
                                'message': '消息发送成功'
                            })
                            
                            # 发送后不要立即创建短接收窗口，除非是最后一条消息
                            if self.consecutive_sends >= self.max_consecutive_sends:
                                logger.debug("达到最大连续发送次数，准备接收回传")
                        else:
                            # 如果是资源竞争，重新入队消息
                            if self.resource_contention_count > 0:
                                logger.info("由于资源竞争，消息将重新入队")
                                # 将消息放回队列前端
                                temp_queue = asyncio.Queue()
                                await temp_queue.put(message_data)
                                while not self.outgoing_message_queue.empty():
                                    item = await self.outgoing_message_queue.get()
                                    await temp_queue.put(item)
                                self.outgoing_message_queue = temp_queue
                                
                                # 等待更长时间后重试
                                await asyncio.sleep(2.0)
                            else:
                                await self.broadcast_message({
                                    'type': 'error',
                                    'message': '发送消息失败'
                                })
                        
                        # 发送后等待一小段时间，让设备有时间切换到接收模式
                        # 增加等待时间，确保有足够的时间接收回传
                        await asyncio.sleep(2.0)  # 从1秒增加到2秒
                    
                    # 在发送锁释放后，只有在队列为空时才等待回传
                    # 这样可以避免不必要的等待，提高吞吐量
                    if success and self.consecutive_sends > 0 and self.outgoing_message_queue.empty():
                        # 只有在队列为空时才等待回传，避免不必要的延迟
                        logger.debug("队列为空，等待回传响应")
                        # 等待3-6秒的空中时间
                        await asyncio.sleep(4.0)  # 等待足够长的时间以接收回传
                
                except Exception as e:
                    logger.error(f"处理消息时出错: {str(e)}")
                    await asyncio.sleep(1)
            
            except Exception as e:
                logger.error(f"处理发送消息队列时出错: {str(e)}")
                await asyncio.sleep(1)  # 出错后暂停一下
    
    async def force_receive_window(self):
        """强制创建一个接收窗口，暂停发送"""
        try:
            logger.debug("开始强制接收窗口")
            async with self.transmit_lock:
                # 重置计数器
                self.consecutive_sends = 0
                self.last_forced_receive = time.time()
                
                # 等待一段时间，让设备有机会接收消息
                # 这段时间内，由于持有发送锁，不会发送任何消息
                # 增加到6秒，以适应LoRa的空中时间
                await asyncio.sleep(self.receive_window_duration)  # 从3秒增加到6秒接收窗口
                
            logger.debug("强制接收窗口结束")
        except Exception as e:
            logger.error(f"强制接收窗口时出错: {str(e)}")
    
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
                    logger.error(f"发送消息到socket Client失败: {str(e)}")
                    disconnected_clients.add(client)
            
            # 移除断开的客户端
            self.connected_clients -= disconnected_clients
    
    def update_node_list(self):
        """更新Node列表"""
        try:
            if self.interface and hasattr(self.interface, 'nodes'):
                node_info = self.interface.nodes
                if node_info:  # 添加空值检查
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
                    logger.info(f"Node列表已更新，共 {len(self.node_list)} 个Node")
                else:
                    logger.warning("Node信息为空，无法更新Node列表")
        except Exception as e:
            logger.error(f"更新Node列表失败: {str(e)}")
    
    def init_meshtastic_interface(self) -> bool:
        """初始化 Meshtastic 接口"""
        try:
            logger.info(f"正在初始化 Meshtastic 接口，串口: {self.serial_port}")
            
            # 确保之前的接口已关闭
            if self.interface:
                try:
                    with self.interface_lock:
                        self.interface.close()
                except:
                    pass
                self.interface = None
            
            # 重置连接就绪状态和计数器
            self.connection_ready.clear()
            self.resource_contention_count = 0
            self.consecutive_sends = 0
            
            # 尝试多次初始化接口
            max_attempts = 3
            for attempt in range(1, max_attempts + 1):
                try:
                    logger.info(f"尝试创建 SerialInterface 实例 (尝试 {attempt}/{max_attempts})...")
                    
                    # 在创建接口前等待一段时间，确保串口资源已释放
                    if attempt > 1:
                        time.sleep(2)
                    
                    self.interface = SerialInterface(self.serial_port)
                    logger.info("SerialInterface 实例创建成功")
                    break
                except OSError as e:
                    if "Resource temporarily unavailable" in str(e) and attempt < max_attempts:
                        logger.warning(f"资源暂时不可用，将重试 ({attempt}/{max_attempts})")
                        time.sleep(2)  # 等待资源释放
                    else:
                        raise
            
            if not self.interface:
                logger.error("所有初始化尝试都失败")
                return False
            
            # 获取当前节点ID - 使用多种备选方案
            logger.info("正在获取节点信息...")
            try:
                with self.interface_lock:
                    node_info = self.interface.getMyNodeInfo()
                # 尝试多种方式获取节点ID
                if node_info:
                    # 方法1: 直接从 nodeId 字段获取
                    self.my_node_id = node_info.get('nodeId')
                    
                    # 方法2: 如果上面失败，尝试从 id 字段获取
                    if not self.my_node_id:
                        self.my_node_id = node_info.get('id')
                    
                # 方法3: 如果上面都失败，尝试从 interface.myInfo 获取
                if not self.my_node_id and hasattr(self.interface, 'myInfo'):
                    self.my_node_id = self.interface.myInfo.get('id')
                
                # 如果所有方法都失败，使用默认值
                if not self.my_node_id:
                    self.my_node_id = "!local"
                    logger.warning("无法获取节点ID，使用默认值: !local")
                else:
                    logger.info(f"成功获取节点ID: {self.my_node_id}")
            except Exception as e:
                logger.error(f"获取节点ID失败: {str(e)}")
                self.my_node_id = "!local"  # 使用默认值
            
            # 更新Node列表
            logger.info("正在更新Node列表...")
            self.update_node_list()
            logger.info("Node列表更新完成")
            
            # 订阅消息接收回调
            logger.info("正在订阅消息接收事件...")
            pub.subscribe(self.on_meshtastic_receive, "meshtastic.receive")
            logger.info("已订阅 meshtastic.receive 事件")
            
            # 标记连接就绪
            self.connection_ready.set()
            self.last_successful_check = time.time()
            self.last_forced_receive = time.time()  # 初始化最后强制接收窗口时间
            
            return True
        except Exception as e:
            logger.error(f"初始化 Meshtastic 接口失败: {str(e)}")
            logger.exception("详细错误信息：")
            self.connection_ready.clear()
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
                        # 如果连接失败，等待较短时间后重试
                        await asyncio.sleep(5)
                        continue
                else:
                    # 检查连接状态
                    is_healthy = await self._check_connection_health()
                    if is_healthy:
                        self.last_successful_check = time.time()
                        if not self.connection_ready.is_set():
                            logger.info("连接恢复正常，设置连接就绪状态")
                            self.connection_ready.set()
                    else:
                        # 如果超过60秒没有成功的健康检查，则重置连接
                        current_time = time.time()
                        if current_time - self.last_successful_check > 60:
                            logger.warning("检测到连接异常超过60秒，尝试重新连接...")
                            self.connection_ready.clear()  # 清除连接就绪状态
                            await self._safe_close_interface()
                            await self._notify_connection_status(False)
                            self.interface = None
                
                # 健康检查后，只有在有发送活动时才考虑创建接收窗口
                # 这避免了不必要的接收窗口
                if self.interface and self.connection_ready.is_set() and self.consecutive_sends > 0:
                    # 只有在有发送活动时才创建接收窗口
                    await self.force_receive_window()
                
            except Exception as e:
                logger.error(f"健康检查出错: {str(e)}")
                logger.exception("详细错误信息：")
            
            # 减少健康检查间隔，提高响应速度
            await asyncio.sleep(30)  # 健康检查间隔保持在30秒
    
    async def _notify_connection_status(self, is_connected: bool):
        """通知所有客户端连接状态"""
        try:
            await self.broadcast_message({
                'type': 'connection_status',
                'connected': is_connected,
                'timestamp': datetime.now().isoformat()
            })
        except Exception as e:
            logger.error(f"通知连接状态时出错: {str(e)}")
    
    async def _check_connection_health(self) -> bool:
        """检查连接健康状态"""
        try:
            if not self.interface:
                return False

            # 如果资源竞争计数过高，认为连接不健康
            if self.resource_contention_count >= 3:
                logger.warning(f"资源竞争计数过高 ({self.resource_contention_count})，认为连接不健康")
                return False

            # 尝试发送一个简单的请求（使用 asyncio 避免阻塞）
            loop = asyncio.get_running_loop()
            try:
                # 设置超时时间为 2 秒
                await asyncio.wait_for(
                    loop.run_in_executor(None, self._check_node_info),
                    timeout=2.0
                )
                # 成功检查后重置资源竞争计数
                self.resource_contention_count = 0
                return True
            except asyncio.TimeoutError:
                logger.warning("获取节点信息超时")
                return False
            except OSError as e:
                if "Resource temporarily unavailable" in str(e):
                    self.resource_contention_count += 1
                    logger.warning(f"健康检查时资源暂时不可用 (计数: {self.resource_contention_count})")
                    return False
                else:
                    logger.warning(f"获取节点信息失败: {str(e)}")
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
            with self.interface_lock:
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
                with self.interface_lock:
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
        # 设置初始状态
        self.running = True
        self.loop = asyncio.get_running_loop()
        
        # 初始化 Meshtastic 接口
        if not self.init_meshtastic_interface():
            logger.error("无法初始化 Meshtastic 接口，将在健康检查中重试")
            # 不立即退出，让健康检查机制尝试重连
        
        # 启动健康检查和消息处理
        asyncio.create_task(self.health_check())
        asyncio.create_task(self.process_message_queue())
        asyncio.create_task(self.process_outgoing_messages())  # 添加发送消息队列处理
        
        # 启动 WebSocket 服务器
        async with serve(self.handle_client, self.host, self.port):
            logger.info(f"WebSocket 服务器已启动在 {self.host}:{self.port}")
            await asyncio.Future()  # 运行直到被取消

    def on_meshtastic_receive(self, packet: Dict, interface: SerialInterface):
        """处理从 Meshtastic 接收到的消息（同步版本）"""
        try:
            # 确保接收时不发送（半双工通信）
            asyncio.run_coroutine_threadsafe(self._acquire_transmit_lock_temporarily(), self.loop)
            
            # 检查包是否为空
            if not packet or 'decoded' not in packet:
                logger.warning("收到空包或无法解码的包")
                return
                
            port_num = packet['decoded'].get('portnum')
            if not port_num:
                logger.warning("包中没有端口号")
                return
            
            # 只处理我们关心的消息类型
            if port_num == 'TEXT_MESSAGE_APP':
                # 确保payload存在且可解码
                if 'payload' not in packet['decoded']:
                    logger.warning("文本消息中没有payload")
                    return
                    
                try:
                    message = packet['decoded']['payload'].decode('utf-8')
                except Exception as e:
                    logger.error(f"解码消息失败: {str(e)}")
                    return
                    
                from_id = packet.get('fromId')
                if not from_id:
                    logger.warning("消息中没有发送者ID")
                    from_id = "unknown"
                    
                # 安全获取节点信息
                node_info = {}
                with self.interface_lock:
                    if self.interface and hasattr(self.interface, 'nodes'):
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
                logger.info(f"收到文本消息: {message} (来自Node: {from_id})")
                
                # 重置资源竞争计数，因为成功接收了消息
                self.resource_contention_count = 0
                
            elif port_num == 'NODEINFO_APP':
                # 处理节点信息更新
                logger.info("收到Node信息更新")
                self.update_node_list()
                
            elif port_num == 'POSITION_APP':
                # 如果需要处理位置信息，可以在这里添加
                pass
            else:
                # 对于其他类型的消息，只记录日志但不转发
                logger.debug(f"收到未处理的消息类型: {port_num}")
                
        except Exception as e:
            logger.error(f"处理 Meshtastic 消息时出错: {str(e)}")
    
    async def _acquire_transmit_lock_temporarily(self):
        """临时获取发送锁，用于接收消息时阻止发送"""
        try:
            async with self.transmit_lock:
                # 持有锁一小段时间，确保接收过程中不会发送
                await asyncio.sleep(0.2)
        except Exception as e:
            logger.error(f"获取发送锁时出错: {str(e)}")

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
