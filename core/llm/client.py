import os
from google import genai
from typing import List

_gemini_clients = None

def get_gemini_clients() -> List[genai.Client]:
    global _gemini_clients
    if _gemini_clients is not None:
        return _gemini_clients
        
    _gemini_clients = []
    
    # Primary Key
    api_key_1 = os.getenv("GEMINI_API_KEY")
    if not api_key_1:
        raise ValueError("GEMINI_API_KEY environment variable not set")
    _gemini_clients.append(genai.Client(api_key=api_key_1))
    
    # Secondary Key
    api_key_2 = os.getenv("GEMINI_API_KEY_2")
    if api_key_2:
        _gemini_clients.append(genai.Client(api_key=api_key_2))
        
    # Tertiary Key
    api_key_3 = os.getenv("GEMINI_API_KEY_3")
    if api_key_3:
        _gemini_clients.append(genai.Client(api_key=api_key_3))
        
    return _gemini_clients

def get_gemini_client() -> genai.Client:
    """Returns primary client for backward compatibility."""
    return get_gemini_clients()[0]
