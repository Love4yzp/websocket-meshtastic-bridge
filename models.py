from pydantic import BaseModel, Field, validator
from typing import Optional, List, Dict
from datetime import datetime

class MessageRequest(BaseModel):
    type: str = Field(..., description="Message type")
    message: str = Field(..., description="Message content")
    destination: Optional[str] = Field(None, description="Destination node ID")
    channel: Optional[int] = Field(0, description="Channel number (0-7)")
    message_id: Optional[str] = None  # Added for tracking
    require_ack: bool = Field(True, description="Whether to require acknowledgment")
    timeout: float = Field(30.0, description="Timeout in seconds")
    
    @validator('channel')
    def validate_channel(cls, v):
        if v is not None and not (0 <= v <= 7):
            raise ValueError("Channel must be between 0 and 7")
        return v
    
    @validator('destination')
    def validate_destination(cls, v):
        if v is not None and not v.startswith('!'):
            raise ValueError("Destination must start with '!'")
        return v

class MessageStatus(BaseModel):
    message_id: str
    sent_time: datetime
    status: str = "pending"  # pending, delivered, failed, timeout
    retries: int = 0
    destination: Optional[str] = None
    channel: Optional[int] = None
    ack_received: bool = False
    timeout: float = 30.0  # seconds

class NodeInfo(BaseModel):
    nodeId: str
    num: int = 0
    shortName: str = ""
    longName: str = ""
    latitude: float = 0.0
    longitude: float = 0.0
    altitude: float = 0.0
    batteryLevel: int = 0
    voltage: float = 0.0
    channelUtilization: float = 0.0
    snr: float = 0.0
    lastHeard: int = 0
    rssi: int = 0
    channel: int = 0

class WebSocketMessage(BaseModel):
    type: str
    data: Dict = {}
    message: Optional[str] = None
    timestamp: datetime = Field(default_factory=datetime.now)

class NodeList(BaseModel):
    nodes: List[NodeInfo]
