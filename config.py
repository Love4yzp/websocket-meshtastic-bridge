from pydantic import Field
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    # Server settings
    HOST: str = Field(default="0.0.0.0", description="WebSocket server host")
    PORT: int = Field(default=5800, description="WebSocket server port")
    
    # Meshtastic settings
    SERIAL_PORT: str | None = Field(default=None, description="Serial port for Meshtastic device")
    RECONNECT_ATTEMPTS: int = Field(default=3, description="Number of reconnection attempts")
    RECONNECT_DELAY: int = Field(default=5, description="Delay between reconnection attempts in seconds")
    
    # Message settings
    MAX_MESSAGE_LENGTH: int = Field(default=200, description="Maximum message length")
    RATE_LIMIT_MESSAGES: int = Field(default=10, description="Maximum messages per minute")
    
    # Logging
    LOG_LEVEL: str = Field(default="INFO", description="Logging level")
    
    class Config:
        env_file = ".env"
        case_sensitive = True

settings = Settings()
