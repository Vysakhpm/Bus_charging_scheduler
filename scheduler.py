import json
from typing import Dict, List, Tuple, Any

class RuleEngine:
    """
    Decoupled rule engine for resolving priority conflicts in charger queues.
    """
    @staticmethod
    def calculate_priority(bus, wait_time: int, weights: Dict[str, float]) -> float:
        """
        Calculates a dynamic priority score for a bus in the charging queue.
        
        Priority = (wait_time + 1) * (individual_factor * w_individual) * (operator_factor * w_operator) * w_overall
        
        Args:
            bus: The Bus object waiting in the queue.
            wait_time: Number of minutes the bus has been waiting in the queue.
            weights: The weights configuration containing multipliers:
                - "individual": multiplier for individual bus factors
                - "operator": multiplier for operator factors
                - "overall": overall system-wide multiplier
                
        Returns:
            float: The calculated priority score (higher score = higher priority).
        """
        # 1. Individual physical urgency factor
        # Buses with lower remaining range need charging more urgently.
        # Scale range urgency: 0.0 (full charge) to 1.0 (empty charge).
        range_urgency = (bus.max_range - bus.remaining_range) / bus.max_range
        individual_factor = getattr(bus, 'individual_weight', 1.0) * (1.0 + range_urgency)
        
        # 2. Operator factor
        # Different operators can be mapped to different base priority levels
        operator_map = {
            "freshbus": 1.2,
            "flixbus": 1.1,
            "kpn": 1.0
        }
        operator_factor = operator_map.get(bus.operator.lower(), 1.0)
        
        # 3. Apply weights from JSON config
        w_individual = weights.get("individual", 1.0)
        w_operator = weights.get("operator", 1.0)
        w_overall = weights.get("overall", 1.0)
        
        # Calculate dynamic priority
        priority = (wait_time + 1) * (individual_factor * w_individual) * (operator_factor * w_operator) * w_overall
        return float(priority)


class Bus:
    """
    Represents a bus inside the scheduling simulation, maintaining its state and history.
    """
    def __init__(self, bus_id: str, operator: str, direction: str, departure_time_str: str, max_range: float):
        self.id = bus_id
        self.operator = operator
        self.direction = direction
        self.departure_time_str = departure_time_str
        self.departure_time = self._parse_time_str(departure_time_str)
        self.max_range = max_range
        
        # Simulation state
        self.remaining_range = max_range
        self.state = "PENDING"  # PENDING, DRIVING, WAITING, CHARGING, COMPLETED
        self.distance_traveled = 0.0
        self.current_station = None
        self.queue_entry_time = None
        self.charge_end_time = None
        self.individual_weight = 1.0
        
        # Milestone sequence (populated by scheduler)
        self.milestones: List[Tuple[str, float]] = []
        
        # Chronological event log
        self.timeline: List[Dict[str, Any]] = []

    def _parse_time_str(self, time_str: str) -> int:
        """Converts HH:MM format to minutes since midnight."""
        parts = time_str.split(":")
        return int(parts[0]) * 60 + int(parts[1])

    def log_event(self, time: int, event_type: str, station: str = None, description: str = ""):
        """Appends an event to the chronological timeline of the bus."""
        self.timeline.append({
            "time": time,
            "time_str": self.format_minutes(time),
            "event": event_type,
            "station": station,
            "remaining_range": round(self.remaining_range, 1),
            "distance_traveled": round(self.distance_traveled, 1),
            "description": description
        })

    @staticmethod
    def format_minutes(minutes: int) -> str:
        """Formats simulation minutes back into HH:MM string."""
        hours = (minutes // 60) % 24
        mins = minutes % 60
        return f"{hours:02d}:{mins:02d}"

    def __repr__(self):
        return f"Bus({self.id}, State={self.state}, Range={self.remaining_range:.1f}km, Pos={self.distance_traveled:.1f}km)"


class BusScheduler:
    """
    Discrete-event scheduler that simulates bus movements and handles charger queues.
    """
    def __init__(self, config: Dict[str, Any], weights: Dict[str, float]):
        self.config = config
        self.weights = weights
        
        self.bus_max_range = float(config.get("bus_max_range", 240))
        self.speed_kmh = float(config.get("speed_kmh", 60))
        self.charging_duration_mins = int(config.get("charging_duration_mins", 25))
        
        self.route_segments = config.get("route_segments", [])
        self.stations_config = config.get("stations", {})
        
        # Build forward and reverse milestones
        self.forward_milestones, self.reverse_milestones = self._build_milestones()

    def _build_milestones(self) -> Tuple[List[Tuple[str, float]], List[Tuple[str, float]]]:
        """
        Dynamically constructs the route milestones list for both forward and reverse directions.
        Each milestone is a tuple of (station_name, cumulative_distance_from_origin).
        """
        if not self.route_segments:
            return [], []
        
        # 1. Forward direction milestones
        forward = []
        forward.append((self.route_segments[0]["from"], 0.0))
        cumulative = 0.0
        for seg in self.route_segments:
            cumulative += float(seg["distance_km"])
            forward.append((seg["to"], cumulative))
            
        # 2. Reverse direction milestones
        reverse = []
        reverse.append((forward[-1][0], 0.0))
        cumulative_rev = 0.0
        for seg in reversed(self.route_segments):
            cumulative_rev += float(seg["distance_km"])
            reverse.append((seg["from"], cumulative_rev))
            
        return forward, reverse

    def _get_next_milestone(self, bus: Bus) -> Tuple[str, float]:
        """Returns the next milestone (station_name, distance) the bus is driving towards."""
        for name, dist in bus.milestones:
            if dist > bus.distance_traveled:
                return name, dist
        return None, None

    def run_simulation(self, buses_data: List[Dict[str, Any]]) -> Tuple[List[Bus], Dict[str, Any]]:
        """
        Runs the discrete-event simulator at a 1-minute time-step resolution.
        
        Args:
            buses_data: List of bus configurations parsed from JSON.
            
        Returns:
            Tuple[List[Bus], Dict[str, Any]]: List of enriched Bus objects and station utilization logs.
        """
        # Instantiate and enrich Bus objects
        buses = []
        for b_data in buses_data:
            bus = Bus(
                bus_id=b_data["id"],
                operator=b_data["operator"],
                direction=b_data["direction"],
                departure_time_str=b_data["departure_time"],
                max_range=self.bus_max_range
            )
            # Assign milestones based on direction
            if bus.direction == "Bengaluru -> Kochi":
                bus.milestones = self.forward_milestones
            elif bus.direction == "Kochi -> Bengaluru":
                bus.milestones = self.reverse_milestones
            else:
                raise ValueError(f"Unknown direction: {bus.direction}")
            buses.append(bus)

        # Initialize station queues and charger states
        station_queues = {name: [] for name in self.stations_config.keys()}
        station_chargers = {name: [] for name in self.stations_config.keys()}  # Holds buses currently charging
        
        # Initialize utilization log containers
        station_logs = {
            name: {
                "capacity": self.stations_config[name]["capacity"],
                "utilization_timeline": [],  # (time, occupied_chargers)
                "queue_lengths": [],         # (time, queue_length)
                "max_queue_length": 0,
                "wait_times": [],            # list of wait times (minutes)
                "charging_sessions": [],     # list of dicts with session details
                "total_charging_sessions": 0
            }
            for name in self.stations_config.keys()
        }

        # Start simulation from the earliest departure time
        if not buses:
            return [], station_logs
            
        start_time = min(b.departure_time for b in buses)
        t = start_time
        
        # Run until all buses are COMPLETED
        while not all(b.state == "COMPLETED" for b in buses):
            
            # --- STEP 1: Handle Departures ---
            for bus in buses:
                if bus.state == "PENDING" and t >= bus.departure_time:
                    bus.state = "DRIVING"
                    bus.log_event(t, "DEPARTURE", station=bus.milestones[0][0], description="Departed from origin")

            # --- STEP 2: Handle Charging Completions ---
            for station_name in self.stations_config.keys():
                currently_charging = station_chargers[station_name]
                finished_charging = []
                
                for bus in currently_charging:
                    if t >= bus.charge_end_time:
                        bus.remaining_range = bus.max_range
                        bus.state = "DRIVING"
                        bus.log_event(
                            t, 
                            "CHARGING_COMPLETED", 
                            station=station_name, 
                            description=f"Charging complete. Range restored to {bus.max_range}km."
                        )
                        bus.log_event(t, "RESUMED_JOURNEY", station=station_name, description="Resumed journey")
                        finished_charging.append(bus)
                
                # Remove finished buses from chargers
                for bus in finished_charging:
                    currently_charging.remove(bus)

            # --- STEP 3: Move Driving Buses ---
            step_distance = self.speed_kmh / 60.0  # distance traveled in 1 minute
            
            for bus in buses:
                if bus.state != "DRIVING":
                    continue
                
                next_name, next_dist = self._get_next_milestone(bus)
                if next_dist is None:
                    # Already completed
                    continue
                
                distance_to_next = next_dist - bus.distance_traveled
                
                # Check if bus will reach/pass the station in this 1-minute step
                if step_distance >= distance_to_next:
                    # Snap to the station milestone to prevent position drift
                    bus.distance_traveled = next_dist
                    bus.remaining_range -= distance_to_next
                    
                    if bus.remaining_range < 0:
                        raise ValueError(f"Bus {bus.id} ran out of charge before arriving at {next_name}!")
                        
                    # Check if this station is the final destination
                    if next_name == bus.milestones[-1][0]:
                        bus.state = "COMPLETED"
                        bus.log_event(t, "ARRIVAL", station=next_name, description="Arrived at final destination")
                    else:
                        # Arrived at intermediate station
                        bus.log_event(t, "ARRIVED_AT_STATION", station=next_name, description=f"Arrived at intermediate station {next_name}")
                        
                        # Determine if charging is needed for the next leg
                        sub_name, sub_dist = self._get_next_milestone(bus)
                        dist_to_sub = sub_dist - bus.distance_traveled
                        
                        if bus.remaining_range < dist_to_sub:
                            # Must queue for charging
                            bus.state = "WAITING"
                            bus.queue_entry_time = t
                            bus.current_station = next_name
                            station_queues[next_name].append(bus)
                            bus.log_event(t, "QUEUED", station=next_name, description=f"Range ({round(bus.remaining_range, 1)}km) < next stop distance ({round(dist_to_sub, 1)}km). Entered charging queue.")
                        else:
                            # Sufficient range to pass through
                            bus.log_event(t, "PASSED_STATION", station=next_name, description=f"Sufficient range ({round(bus.remaining_range, 1)}km) to reach next milestone. Passing through.")
                else:
                    # Normal driving step
                    bus.distance_traveled += step_distance
                    bus.remaining_range -= step_distance
                    
                    if bus.remaining_range < 0:
                        raise ValueError(f"Bus {bus.id} ran out of charge during transit!")

            # --- STEP 4: Resolve Charging Queues & Allocate Chargers ---
            for station_name in self.stations_config.keys():
                capacity = self.stations_config[station_name]["capacity"]
                queue = station_queues[station_name]
                currently_charging = station_chargers[station_name]
                
                # Update queue logs
                station_logs[station_name]["queue_lengths"].append((t, len(queue)))
                station_logs[station_name]["max_queue_length"] = max(
                    station_logs[station_name]["max_queue_length"], len(queue)
                )
                
                # Calculate wait times and dynamic priorities for everyone in queue
                priority_list = []
                for bus in queue:
                    wait_time = t - bus.queue_entry_time
                    priority_score = RuleEngine.calculate_priority(bus, wait_time, self.weights)
                    priority_list.append((priority_score, wait_time, bus))
                
                # Sort by priority score (descending)
                priority_list.sort(key=lambda x: x[0], reverse=True)
                
                # Allocate available chargers
                available_slots = capacity - len(currently_charging)
                for _ in range(available_slots):
                    if not priority_list:
                        break
                    
                    # Pop the highest priority bus
                    score, wait_time, bus = priority_list.pop(0)
                    queue.remove(bus)
                    
                    # Start charging
                    bus.state = "CHARGING"
                    bus.charge_end_time = t + self.charging_duration_mins
                    bus.log_event(
                        t, 
                        "CHARGING_STARTED", 
                        station=station_name, 
                        description=f"Charger allocated (priority score: {round(score, 2)}, waited: {wait_time} mins)."
                    )
                    
                    currently_charging.append(bus)
                    
                    # Record logs
                    station_logs[station_name]["wait_times"].append(wait_time)
                    station_logs[station_name]["total_charging_sessions"] += 1
                    station_logs[station_name]["charging_sessions"].append({
                        "bus_id": bus.id,
                        "operator": bus.operator,
                        "start_time": t,
                        "start_time_str": Bus.format_minutes(t),
                        "end_time": bus.charge_end_time,
                        "end_time_str": Bus.format_minutes(bus.charge_end_time),
                        "wait_time": wait_time
                    })
                
                # Log charger occupancy
                station_logs[station_name]["utilization_timeline"].append((t, len(currently_charging)))

            # Advance simulation clock by 1 minute
            t += 1
            
        # Post-simulation metric calculations
        sim_end_time = t
        total_sim_duration = sim_end_time - start_time
        
        for name, logs in station_logs.items():
            capacity = logs["capacity"]
            
            # Compute average and max wait times
            if logs["wait_times"]:
                logs["average_wait_time"] = round(sum(logs["wait_times"]) / len(logs["wait_times"]), 1)
                logs["max_wait_time"] = max(logs["wait_times"])
            else:
                logs["average_wait_time"] = 0.0
                logs["max_wait_time"] = 0
                
            # Compute utilization rate
            total_charger_minutes = total_sim_duration * capacity
            if total_charger_minutes > 0:
                total_occupied_minutes = sum(occ for _, occ in logs["utilization_timeline"])
                logs["utilization_rate"] = round(total_occupied_minutes / total_charger_minutes, 3)
            else:
                logs["utilization_rate"] = 0.0
                
        return buses, station_logs


if __name__ == "__main__":
    # Self-contained verification demo
    try:
        with open("scenarios.json", "r") as f:
            scenarios_data = json.load(f)
            
        scenario = scenarios_data["scenarios"]["scenario_1"]
        config = scenario["config"]
        weights = scenario["weights"]
        buses = scenario["buses"]
        
        print("Initializing scheduler...")
        scheduler = BusScheduler(config, weights)
        
        print("Running discrete-event simulation...")
        enriched_buses, station_utilization = scheduler.run_simulation(buses)
        
        print("\n=== SIMULATION RESULTS ===")
        for bus in enriched_buses:
            print(f"\nTimeline for {bus.id} ({bus.operator}) - Route: {bus.direction}:")
            for event in bus.timeline:
                station_part = f" at {event['station']}" if event['station'] else ""
                print(f"  [{event['time_str']}] {event['event']}{station_part} | Range: {event['remaining_range']}km | {event['description']}")
                
        print("\n=== STATION UTILIZATION LOGS ===")
        for station, logs in station_utilization.items():
            print(f"\nStation {station}:")
            print(f"  Capacity: {logs['capacity']} charger(s)")
            print(f"  Total charging sessions: {logs['total_charging_sessions']}")
            print(f"  Charger Utilization Rate: {round(logs['utilization_rate'] * 100, 1)}%")
            print(f"  Max Queue Length: {logs['max_queue_length']}")
            print(f"  Average Wait Time: {logs['average_wait_time']} mins")
            print(f"  Max Wait Time: {logs['max_wait_time']} mins")
            
    except Exception as e:
        print(f"Error executing verification script: {e}")
        import traceback
        traceback.print_exc()
