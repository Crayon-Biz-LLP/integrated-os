import os
import sys
from supabase import create_client
from dotenv import load_dotenv

sys.path.append(os.getcwd())
load_dotenv()
supa = create_client(os.environ['SUPABASE_URL'], os.environ['SUPABASE_SERVICE_ROLE_KEY'])
res = supa.table('graph_type_overrides').select('*').execute()
print(res)
