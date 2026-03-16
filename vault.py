import streamlit as st
from supabase import create_client
import os

# 1. Setup & Connection
st.set_page_config(page_title="Integrated OS: Strategic Vault", layout="wide")
supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_ANON_KEY"))

st.title("📚 Strategic Vault")
st.write("Deep-work interface for browsing missions and resources.")

# 2. Sidebar Filters
st.sidebar.header("Filters")
search_query = st.sidebar.text_input("🔍 Search Titles or Summaries")

# Fetch Missions for the dropdown
missions_res = supabase.table('missions').select('id, title').execute()
mission_options = {m['title']: m['id'] for m in missions_res.data}
selected_mission = st.sidebar.selectbox("🚀 Filter by Mission", ["All"] + list(mission_options.keys()))

# 3. Data Fetching Logic
query = supabase.table('resources').select('*, missions(title)')

if selected_mission != "All":
    query = query.eq('mission_id', mission_options[selected_mission])

res = query.order('created_at', desc=True).execute()
data = res.data

# 4. The Display Engine
if not data:
    st.warning("No resources found matching those filters.")
else:
    for item in data:
        # Simple Search Filter (Client-side for speed)
        if search_query.lower() in item['title'].lower() or search_query.lower() in (item['summary'] or "").lower():
            with st.container():
                col1, col2 = st.columns([3, 1])
                with col1:
                    st.subheader(f"[{item['category']}] {item['title']}")
                    st.write(item['summary'])
                    # Strategic Note highlighted for "Deep Work" context
                    if item.get('strategic_note'):
                        st.info(f"💡 **Strategic Note:** {item['strategic_note']}")
                with col2:
                    st.link_button("Open Resource", item['url'], use_container_width=True)
                    st.caption(f"Mission: {item.get('missions', {}).get('title', 'Unassigned')}")
                st.divider()
# --- COMMAND CENTER VIEW ---
tabs = st.tabs(["🎯 Missions", "📋 Active Tasks", "📚 Resource Vault"])

with tabs[0]:
    st.header("Active Missions")
    # Fetch from 'missions' table
    # Display as 'Goal Cards' with progress bars

with tabs[1]:
    st.header("Today's Battlefield")
    # Fetch from 'tasks' table where status = 'todo'
    # Use st.dataframe for a searchable, sortable list of Solvstrat/Crayon work

with tabs[2]:
    st.header("Research & Sparks")
    # This is the resource library code we discussed earlier