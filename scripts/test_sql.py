import os
import sys
sys.path.append(os.getcwd())
from dotenv import load_dotenv
load_dotenv()
from supabase import create_client
supa = create_client(os.environ['SUPABASE_URL'], os.environ['SUPABASE_SERVICE_ROLE_KEY'])
res = supa.table('graph_type_overrides').select('*').execute()
print(res)
