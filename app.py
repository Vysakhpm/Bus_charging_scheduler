import streamlit as st
import json
import pandas as pd
from engine import BusScheduler

# 1. Set page layout to wide and configure tab metadata
st.set_page_config(
    page_title="Bus Charging Scheduler Engine",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Load scenarios.json configuration cache
@st.cache_data
def load_scenarios():
    with open("scenarios.json", "r") as f:
        return json.load(f)

try:
    scenarios_data = load_scenarios()
except Exception as e:
    st.error(f"Failed to load scenarios.json: {e}")
    st.stop()

scenarios = scenarios_data.get("scenarios", {})
scenario_keys = list(scenarios.keys())

# --- 1. SIDEBAR CONFIGURATION ---
st.sidebar.title("⚙️ Priority Queue Resolution Weights")

w_individual = st.sidebar.slider(
    "Individual Bus Urgency Weight",
    min_value=0.0,
    max_value=10.0,
    value=4.30,
    step=0.1,
    help="Weight assigned to remaining range urgency."
)

w_operator = st.sidebar.slider(
    "Operator Weight Multiplier",
    min_value=0.0,
    max_value=10.0,
    value=4.80,
    step=0.1,
    help="Weight assigned to operator class priority differences."
)

w_overall = st.sidebar.slider(
    "Overall Weight Multiplier",
    min_value=0.0,
    max_value=10.0,
    value=1.00,
    step=0.1,
    help="Global wait time scaling factor."
)

active_weights = {
    "individual": w_individual,
    "operator": w_operator,
    "overall": w_overall
}

# --- 2. HEADER SECTION ---
st.title("🚌 Bus Charging Scheduler & Simulation Engine")
st.markdown("An enterprise-grade declarative discrete-event simulator...")
st.markdown("---")

# Scenario select dropdown
selected_scenario_key = st.selectbox("Select Active Scenario Configuration", scenario_keys)
scenario = scenarios[selected_scenario_key]

config = scenario.get("config", {})
buses_data = scenario.get("buses", [])

# Inspect JSON configuration file in expander
with st.expander("🔍 Inspect Declarative Scenario Input Config (scenarios.json)", expanded=False):
    st.json(scenario)

# --- EXECUTE THE DISCRETE EVENT SIMULATOR ---
scheduler = BusScheduler(config, active_weights)
try:
    results = scheduler.run_simulation(buses_data)
except Exception as e:
    st.error(f"Simulation execution halted: {e}")
    st.stop()

simulated_buses = results["simulated_buses"]
station_logs = results["station_logs"]
metrics = results["metrics"]

# --- 3. FLEET METRICS BANNER ---
st.subheader("📊 Fleet Performance Indicators")

m1, m2, m3, m4 = st.columns(4)
m1.metric("Fleet Size", metrics["Fleet Size"])
m2.metric("Total Charging Sessions", metrics["Total Charging Sessions"])
m3.metric("Fleet Average Wait Time", f"{metrics['Fleet Average Wait Time']} mins")
m4.metric("Max Bus Wait Time", f"{metrics['Max Bus Wait Time']} mins")

# --- 4. PER-BUS TIMETABLE ---
st.markdown("---")
st.subheader("🗓️ Per-bus Timetable & Event Log")

for bus in simulated_buses:
    # Color-coded descriptor matching: 🚌 bus_01 (KPN) — Route: Bengaluru -> Kochi 🟢
    with st.expander(f"🚌 {bus['id']} ({bus['operator'].upper()}) — Route: {bus['direction']} 🟢", expanded=False):
        timeline_df = pd.DataFrame(bus["timeline"])
        if not timeline_df.empty:
            # Reorganize and select matching columns
            display_df = timeline_df[[
                "time_str", "event", "station", "remaining_range", "distance_traveled", "description"
            ]].copy()
            display_df.columns = [
                "Sim Time", "Event Type", "Station", "Remaining Range (km)", "Distance Traveled (km)", "Details"
            ]
            
            # Format ranges and distances explicitly to exactly 4 decimal places
            display_df["Remaining Range (km)"] = display_df["Remaining Range (km)"].apply(lambda v: f"{float(v):.4f}")
            display_df["Distance Traveled (km)"] = display_df["Distance Traveled (km)"].apply(lambda v: f"{float(v):.4f}")
            
            # Show dataframe with hidden index
            st.dataframe(display_df, hide_index=True, width='stretch')
        else:
            st.warning("No events were logged for this vehicle.")

# --- 5. STATION LOGS ---
st.markdown("---")
st.subheader("⚡ Station Charger Utilization & Queue Logs")

# Dynamic metric computation helper per station from stateless logs
station_names = ["A", "B", "C", "D"]
station_cols = st.columns(len(station_names))

for idx, station_name in enumerate(station_names):
    logs = station_logs.get(station_name, {})
    sessions = logs.get("charging_sequence", [])
    
    with station_cols[idx]:
        st.markdown(f"### 📍 Station {station_name}")
        
        # Bullets formatted using direct HTML rendering for specific coloring requirements
        st.markdown(f"""
        - Capacity: 1 charger(s)
        - Charger Utilization: <span style='color:#2e8b57'>{logs['utilization_percent']}%</span>
        - Max Queue Length: <span style='color:#2e8b57'>{logs['max_queue']}</span>
        - Average Wait Time: <span style='color:#2e8b57'>{logs['avg_wait']} mins</span>
        - Max Wait Time: <span style='color:#2e8b57'>{logs['max_wait']} mins</span>
        """, unsafe_allow_html=True)
        
        st.markdown("##### Charging Sequence (Chronological)")
        if sessions:
            sessions_df = pd.DataFrame(sessions)
            st.dataframe(sessions_df, hide_index=True, width='stretch')
        else:
            st.info("No charging sessions occurred at this station.")
