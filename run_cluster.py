import asyncio
from dotenv import load_dotenv
load_dotenv()

from core.pulse.engine import discover_new_clusters
asyncio.run(discover_new_clusters())
