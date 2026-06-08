import time
from typing import Protocol, Dict, Optional

class SwappableStorage(Protocol):
    def get(self, key: str) -> Optional[str]: ...
    def set(self, key: str, value: str, ttl_seconds: Optional[int] = None) -> None: ...
    def delete(self, key: str) -> None: ...

class LocalMemoryStorage:
    def __init__(self):
        self._data: Dict[str, tuple[str, Optional[float]]] = {}
    
    def get(self, key: str) -> Optional[str]:
        if key in self._data:
            value, expires_at = self._data[key]
            if expires_at is None or time.time() < expires_at:
                return value
            else:
                del self._data[key]
        return None
        
    def set(self, key: str, value: str, ttl_seconds: Optional[int] = None) -> None:
        expires_at = time.time() + ttl_seconds if ttl_seconds is not None else None
        self._data[key] = (value, expires_at)
        
    def delete(self, key: str) -> None:
        self._data.pop(key, None)

# Default to process-local dict for Phase 1
breaker_storage = LocalMemoryStorage()

class CircuitBreaker:
    def __init__(self, name: str, threshold: int = 5, window_s: int = 60, storage: SwappableStorage = None):
        self.name = name
        self.threshold = threshold
        self.window_s = window_s
        self.storage = storage or breaker_storage
        
    def _key(self) -> str:
        return f"cb:{self.name}:fails"
        
    def record_failure(self):
        key = self._key()
        fails = int(self.storage.get(key) or "0") + 1
        self.storage.set(key, str(fails), ttl_seconds=self.window_s)
        
    def record_success(self):
        self.storage.delete(self._key())
        
    def is_open(self) -> bool:
        fails = int(self.storage.get(self._key()) or "0")
        return fails >= self.threshold
