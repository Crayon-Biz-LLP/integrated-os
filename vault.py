import streamlit as st
from supabase import create_client
import os

# 1. SECURITY CHECK (Simple Password Gate)
def check_password():
    if "password_correct" not in st.session_state:
        st.session_state["password_correct"] = False
    
    if not st.session_state["password_correct"]:
        pw = st.text_input("Enter Vault Secret", type="password")
        # You'll need to add VAULT_PASSWORD to your Vercel/Streamlit secrets
        if pw == os.getenv("VAULT_PASSWORD", "danny123"): 
            st.session_state["password_correct"] = True
            st.rerun()
        return False
    return True

if not check_password():
    st.stop()

# 2. INITIALIZE SUPABASE (Using Service Role to bypass RLS)
url = os.getenv("SUPABASE_URL")
key = os.getenv("SUPABASE_SERVICE_ROLE_KEY") # Use the God Key here
supabase = create_client(url, key)

st.set_page_config(page_title="Integrated OS | Command Center", layout="wide")
st.title("🕹️ Integrated OS: Command Center")

# 3. TABS FOR DEEP WORK
tabs = st.tabs(["🚀 MISSIONS", "📋 BATTLEFIELD (TASKS)", "📚 RESOURCE VAULT"])

# --- TAB 1: MISSIONS ---
with tabs[0]:
    st.header("Active Strategic Missions")
    missions = supabase.table('missions').select('*').eq('status', 'active').execute()
    if missions.data:
        for m in missions.data:
            with st.expander(f"🎯 {m['title']}", expanded=True):
                st.write(f"**Status:** {m['status'].upper()}")
                st.caption(f"Created: {m['created_at']}")
    else:
        st.info("No active missions. Let the AI define one in your next Pulse.")

# --- TAB 2: TASKS ---
with tabs[1]:
    st.header("The Battlefield")
    tasks = supabase.table('tasks').select('*').order('priority', desc=True).execute()
    if tasks.data:
        # Show as a searchable dataframe/table
        st.dataframe(tasks.data, use_container_width=True, hide_index=True)
    else:
        st.success("Battlefield clear. No pending tasks.")

# --- TAB 3: RESOURCES ---
with tabs[2]:
    st.header("Strategic Research & Sparks")
    search = st.text_input("🔍 Search Library")
    res = supabase.table('resources').select('*, missions(title)').order('created_at', desc=True).execute()
    
    if res.data:
        for item in res.data:
            if not search or search.lower() in item['title'].lower():
                col1, col2 = st.columns([4, 1])
                with col1:
                    st.markdown(f"### [{item['title']}]({item['url']})")
                    st.write(item['summary'] or "No summary available.")
                    if item['strategic_note']:
                        st.info(f"💡 {item['strategic_note']}")
                with col2:
                    st.write(f"📂 {item.get('missions', {}).get('title', 'General')}")
                st.divider()