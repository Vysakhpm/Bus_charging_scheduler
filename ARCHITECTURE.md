# System Architecture Design: Declarative Discrete-Event Bus Charging Scheduler

This document details the architectural decisions, design patterns, and engineering paradigms implemented in the **Bus Charging Scheduler and Simulation Engine**. The system is engineered to model complex real-time fleet operations, charger queue contentions, and dynamic prioritization rules under high-concurrency loads (20+ buses).

---

## 1. The Core Engine: Pluggable Discrete-Event Simulator (DES)

Static scheduling models and linear programming arrays (e.g. Mixed-Integer Linear Programming) formulate transit allocation as a centralized, deterministic optimization matrix. While mathematically neat in static environments, they fail the moment real-world variables are introduced:
- **Brittleness under Staggered Starts**: Static arrays operate on predefined arrival windows. If departures are staggered (e.g., staggered perfectly by 15 minutes to prevent queue waits) or dynamically delayed, the entire matrix must be reconstructed and re-solved from scratch.
- **Inability to Model Time-Series Congestion**: Linear math cannot model dynamic queues, state transitions (e.g. waiting to charging), or transactional resources naturally. When 20 buses concurrently converge on a bottleneck station, static solvers experience combinatorial explosion.
- **Failures under Variable Speeds**: If a road segment experiences traffic, changing a vehicle's transit duration breaks the static matrix indices.

### Natively Resolving Concurrency via DES

To bypass these limitations, this architecture implements a **Pluggable Event-Driven Simulation Engine**:
1. **Dynamic Time Step Simulation**: The engine simulates the physical movement of the 20-bus fleet along the transit network at a 1-minute time-step resolution. State transitions (e.g. `DRIVING` -> `WAITING` -> `CHARGING` -> `DRIVING`) emerge naturally.
2. **Transactional Queueing & Locks**: Chargers are modeled as discrete locked resources. When a bus arrives at a station and finds the charger occupied, it enters a wait queue. The simulator invokes the `RuleEngine` to re-sort wait priorities at each clock tick.
3. **Linear Complexity Scaling**: The simulation scales linearly at $\mathcal{O}(T \times N)$, where $T$ is the total simulation duration in minutes and $N$ is the fleet size, instead of exponentially like centralized solvers.

---

## 2. Data De-coupling: A Stateless Execution Engine

The core scheduling engine in `engine.py` is designed to be **completely stateless and domain-agnostic**. The entire topology of the physical world, vehicle roster, and operational weights are abstracted and declared in [scenarios.json](file:///d:/Bus_Charging_Scheduler/scenarios.json).

- **Domain-Agnostic Topologies**: The engine is completely unaware of what "Bengaluru", "Kochi", or intermediate station "B" physically represent. It treats the world as a directed graph of nodes (`from`, `to`) and distance values (`distance_km`). It parses the graph at runtime to dynamically construct milestone vectors for both forward and reverse directions.
- **Regression Isolation**: Physical properties—such as charging speed, vehicle battery capacities, and segment coordinates—reside exclusively in the JSON data layer. You can rearrange segments, adjust speed parameters, or replace station names without modifying a single line of Python code, completely preventing software regressions in the simulation core.
- **Thread Safety**: Because the engine maintains no global singleton states or environment configurations, operations teams can safely spin up multiple simulation threads concurrently to run separate scenarios in parallel.

---

## 3. The "What If" Matrix (Absorbing Future Shocks)

The declarative data layer enables the system to absorb major infrastructure changes and business asks with **zero code modifications**:

| Target Business Ask / Physical Detour | Current Data Schema Support | Resolution in scenarios.json | How Simulator Natively Handles it |
| :--- | :--- | :--- | :--- |
| **Double charger capacity at Station B** (e.g., expand Station B from 1 to 2 chargers). | Supported via the `capacity` key in the `stations` configuration map. | Locate station `"B"` in the JSON and update `"capacity": 1` to `"capacity": 2`. | The engine's allocation loop reads the capacity integer, automatically pooling the chargers and letting two buses charge concurrently. |
| **Introduce a 5th station** (e.g., add Station E between Station D and Kochi). | Supported by expanding the `route_segments` array and `stations` object. | Split segment `D -> Kochi` into `D -> E` and `E -> Kochi` in the JSON array; define Station `"E"` with its capacity. | The milestone builder parses the new nodes, automatically calculating travel durations and battery range consumption. |
| **Increase bus range to 300km** (e.g., retrofitting vehicles with larger batteries). | Supported via the global config or local bus object overrides. | Adjust `"bus_max_range": 300` in the global config, or define `"max_range": 300` for specific bus objects. | The engine initializes vehicle state machines with the updated battery ranges, applying them to transit calculations instantly. |
| **Introduce a detoured segment length** (e.g., detouring around highway construction). | Supported via the `distance_km` parameter in `route_segments`. | Update the detour distance (e.g., increase `"distance_km": 120` to `150`) for the target segment. | The milestone vector constructor recalculates cumulative distance targets and travel times dynamically. |

---

## 4. Live-Tuning Demonstration

Below are concrete, production-grade Python examples demonstrating how easily this codebase is tuned or extended:

### Snippet A: Live Weight Tuning in Code
Dispatchers or automated scripts can dynamically adjust parameters (such as during peak grid hours) and inject them into the simulation engine:

```python
import json
from engine import BusScheduler

# 1. Parse the declarative configurations
with open("scenarios.json", "r") as f:
    scenarios_data = json.load(f)
    
scenario = scenarios_data["scenarios"]["scenario_1"]

# 2. Tune weights dynamically to prioritize low-battery vehicles
custom_weights = {
    "individual": 9.5,  # High urgency factor for range
    "operator": 1.0,    # Flat operator hierarchy
    "overall": 2.5      # Scale overall wait times
}

# 3. Inject tuned parameters into the scheduler and run
scheduler = BusScheduler(scenario["config"], custom_weights)
results = scheduler.run_simulation(scenario["buses"])
```

### Snippet B: Injecting a New Soft Priority Rule ("FRESHBUS Priority Premium")
Adding new operational logic—such as prioritizing commercial partners like "FRESHBUS" in queues—requires editing only the priority scoring method in the decoupled `RuleEngine`:

```python
# Inside engine.py -> class RuleEngine
@staticmethod
def calculate_priority(bus, wait_time: int, weights: dict) -> float:
    # --- NEW CUSTOM PRIORITY RULE: FRESHBUS EXPRESS PREMIUM ---
    # Give a substantial priority boost if the bus belongs to operator FRESHBUS,
    # letting them bypass other commercial operators at bottleneck stations.
    operator_premium = 1000.0 if bus.operator.upper() == "FRESHBUS" else 0.0

    # Calculate physical battery range urgency factor
    range_urgency = (bus.max_range - bus.remaining_range) / bus.max_range
    individual_factor = getattr(bus, 'individual_weight', 1.0) * (1.0 + range_urgency)

    # Standard priority calculations
    base_priority = (wait_time + 1) * (individual_factor * weights.get("individual", 1.0))
    
    # Consolidate standard scoring with operator premium
    return float(base_priority + operator_premium)
```

---

## 5. Architectural Integrity and Audit Trails

Every vehicle state transition is logged chronologically to verify scheduling correctness:
- **Range Bounds Checks**: In-transit step checks raise explicit errors if a vehicle runs out of battery range, preventing invalid simulation states from completing silently.
- **Resource Lock Guard**: Station capacities prevent double-allocations, locking charging slots until a `CHARGING_COMPLETED` event is fired.
- **Verifiable Output Schema**: Timelines track simulated range and distance precisely, creating an auditable database of scheduling correctness.
