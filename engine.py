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
        """
        # 1. Individual physical battery urgency factor
        range_urgency = (bus.max_range - bus.remaining_range) / bus.max_range
        individual_factor = getattr(bus, 'individual_weight', 1.0) * (1.0 + range_urgency)
        
        # 2. Operator factor mapping
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
    Represents a bus inside the simulation, maintaining its state and history.
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
        parts = time_str.split(":")
        return int(parts[0]) * 60 + int(parts[1])

    def log_event(self, time: int, event_type: str, station: str = None, description: str = ""):
        """Logs an event, keeping range and distance precision-formatted to 4 decimals."""
        self.timeline.append({
            "time": time,
            "time_str": self.format_minutes(time),
            "event": event_type,
            "station": station,
            "remaining_range": round(float(self.remaining_range), 4),
            "distance_traveled": round(float(self.distance_traveled), 4),
            "description": description
        })

    @staticmethod
    def format_minutes(minutes: int) -> str:
        hours = (minutes // 60) % 24
        mins = minutes % 60
        return f"{hours:02d}:{mins:02d}"


class BusScheduler:
    """
    Discrete-event simulation engine with dynamic queue priority resolution.
    """
    def __init__(self, config: Dict[str, Any], weights: Dict[str, float]):
        self.config = config
        self.weights = weights
        
        self.bus_max_range = float(config.get("bus_max_range", 240))
        self.speed_kmh = float(config.get("speed_kmh", 60))
        self.charging_duration_mins = int(config.get("charging_duration_mins", 25))
        
        self.route_segments = config.get("route_segments", [])
        self.stations_config = config.get("stations", {})
        
        # Build milestones dynamically from topology graph
        self.forward_milestones, self.reverse_milestones = self._build_milestones()

    def _build_milestones(self) -> Tuple[List[Tuple[str, float]], List[Tuple[str, float]]]:
        if not self.route_segments:
            return [], []
        
        forward = []
        forward.append((self.route_segments[0]["from"], 0.0))
        cumulative = 0.0
        for seg in self.route_segments:
            cumulative += float(seg["distance_km"])
            forward.append((seg["to"], cumulative))
            
        reverse = []
        reverse.append((forward[-1][0], 0.0))
        cumulative_rev = 0.0
        for seg in reversed(self.route_segments):
            cumulative_rev += float(seg["distance_km"])
            reverse.append((seg["from"], cumulative_rev))
            
        return forward, reverse

    def _get_next_milestone(self, bus: Bus) -> Tuple[str, float]:
        for name, dist in bus.milestones:
            if dist > bus.distance_traveled:
                return name, dist
        return None, None

    def run_simulation(self, buses_data: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Runs the simulation and returns structured simulated buses, station metrics, and aggregate metrics.
        """
        # Create and initialize Bus objects
        buses = []
        for b_data in buses_data:
            dep_time_str = b_data.get("departure_time") or b_data.get("departure")
            bus = Bus(
                bus_id=b_data["id"],
                operator=b_data["operator"],
                direction=b_data["direction"],
                departure_time_str=dep_time_str,
                max_range=self.bus_max_range
            )
            if bus.direction == "Bengaluru -> Kochi":
                bus.milestones = self.forward_milestones
            elif bus.direction == "Kochi -> Bengaluru":
                bus.milestones = self.reverse_milestones
            else:
                raise ValueError(f"Unknown direction: {bus.direction}")
            buses.append(bus)

        # Queues and allocation maps
        station_queues = {name: [] for name in self.stations_config.keys()}
        station_chargers = {name: [] for name in self.stations_config.keys()}
        
        # Chronological session logs per station
        station_charging_sequences = {name: [] for name in self.stations_config.keys()}
        station_wait_times = {name: [] for name in self.stations_config.keys()}
        
        # Reconstruct queue timeline to find max queue length
        station_queue_timeline = {name: [] for name in self.stations_config.keys()}

        if not buses:
            return {
                "simulated_buses": [],
                "station_logs": {
                    name: {
                        "charging_sequence": [],
                        "utilization_percent": 0.0,
                        "max_queue": 0,
                        "avg_wait": 0.0,
                        "max_wait": 0
                    }
                    for name in self.stations_config.keys()
                },
                "metrics": {
                    "Fleet Size": 0,
                    "Total Charging Sessions": 0,
                    "Fleet Average Wait Time": 0.0,
                    "Max Bus Wait Time": 0
                }
            }

        start_time = min(b.departure_time for b in buses)
        t = start_time
        
        all_wait_times = []
        
        while not all(b.state == "COMPLETED" for b in buses):
            
            # --- 1. Depart Pending Buses ---
            for bus in buses:
                if bus.state == "PENDING" and t >= bus.departure_time:
                    bus.state = "DRIVING"
                    bus.log_event(t, "DEPARTURE", station=bus.milestones[0][0], description="Departed from origin")

            # --- 2. Complete Charging Sessions ---
            for station_name in self.stations_config.keys():
                currently_charging = station_chargers[station_name]
                finished = []
                
                for bus in currently_charging:
                    if t >= bus.charge_end_time:
                        bus.remaining_range = bus.max_range
                        bus.state = "DRIVING"
                        bus.log_event(
                            t, 
                            "CHARGING_COMPLETED", 
                            station=station_name, 
                            description=f"Charging complete. Range restored to {self.bus_max_range:.1f}km."
                        )
                        bus.log_event(t, "RESUMED_JOURNEY", station=station_name, description="Resumed journey")
                        finished.append(bus)
                
                for bus in finished:
                    currently_charging.remove(bus)

            # --- 3. Move Driving Buses ---
            step_distance = self.speed_kmh / 60.0
            
            for bus in buses:
                if bus.state != "DRIVING":
                    continue
                
                next_name, next_dist = self._get_next_milestone(bus)
                if next_dist is None:
                    continue
                
                distance_to_next = next_dist - bus.distance_traveled
                
                if step_distance >= distance_to_next:
                    # Snap to milestone station
                    bus.distance_traveled = next_dist
                    bus.remaining_range -= distance_to_next
                    
                    if bus.remaining_range < 0:
                        raise ValueError(f"Bus {bus.id} ran out of battery range during transit!")
                        
                    if next_name == bus.milestones[-1][0]:
                        bus.state = "COMPLETED"
                        bus.log_event(t, "ARRIVAL", station=next_name, description="Arrived at final destination")
                    else:
                        bus.log_event(t, "ARRIVED_AT_STATION", station=next_name, description=f"Arrived at intermediate station {next_name}")
                        
                        sub_name, sub_dist = self._get_next_milestone(bus)
                        dist_to_sub = sub_dist - bus.distance_traveled
                        
                        if bus.remaining_range < dist_to_sub:
                            bus.state = "WAITING"
                            bus.queue_entry_time = t
                            bus.current_station = next_name
                            station_queues[next_name].append(bus)
                            bus.log_event(
                                t, 
                                "QUEUED", 
                                station=next_name, 
                                description=f"Range ({bus.remaining_range:.4f}km) < next stop distance. Entered charging queue."
                            )
                        else:
                            bus.log_event(
                                t, 
                                "PASSED_STATION", 
                                station=next_name, 
                                description=f"Sufficient range ({bus.remaining_range:.4f}km) to reach next milestone. Passing through."
                            )
                else:
                    bus.distance_traveled += step_distance
                    bus.remaining_range -= step_distance
                    
                    if bus.remaining_range < 0:
                        raise ValueError(f"Bus {bus.id} ran out of battery range during transit!")

            # --- 4. Resolve Queues & Allocate Chargers ---
            for station_name in self.stations_config.keys():
                capacity = self.stations_config[station_name]["capacity"]
                queue = station_queues[station_name]
                currently_charging = station_chargers[station_name]
                
                # Dynamic priorities
                priority_list = []
                for bus in queue:
                    wait_time = t - bus.queue_entry_time
                    score = RuleEngine.calculate_priority(bus, wait_time, self.weights)
                    priority_list.append((score, wait_time, bus))
                
                priority_list.sort(key=lambda x: x[0], reverse=True)
                
                # Record queue state at this minute.
                # If a bus is occupying/waiting (even if wait time is 0), we count it as queue load.
                # To capture max_queue = 1 even when wait time is 0, we count the number of buses 
                # in the queue or charger that need/just started charging at this station.
                # Specifically: count the size of the queue before popping, plus any active chargers.
                # If a bus is occupying the charger, queue size + occupied chargers represents the overall load.
                # The spec states: "Max Queue Length: This should evaluate to 1 if a bus is occupying the charger, even if wait time is 0."
                # Therefore, load = len(queue) + len(currently_charging)
                # Since capacity = 1, if a bus is charging, this will be at least 1!
                current_load = len(queue) + len(currently_charging)
                station_queue_timeline[station_name].append(current_load)
                
                available_slots = capacity - len(currently_charging)
                for _ in range(available_slots):
                    if not priority_list:
                        break
                    
                    score, wait_time, bus = priority_list.pop(0)
                    queue.remove(bus)
                    
                    bus.state = "CHARGING"
                    bus.charge_end_time = t + self.charging_duration_mins
                    bus.log_event(
                        t, 
                        "CHARGING_STARTED", 
                        station=station_name, 
                        description=f"Charger allocated (priority score: {score:.2f}, waited: {wait_time} mins)."
                    )
                    
                    currently_charging.append(bus)
                    station_wait_times[station_name].append(wait_time)
                    all_wait_times.append(wait_time)
                    
                    station_charging_sequences[station_name].append({
                        "Bus": bus.id,
                        "Operator": bus.operator.upper(),
                        "Start": Bus.format_minutes(t),
                        "End": Bus.format_minutes(bus.charge_end_time),
                        "Wait": wait_time
                    })
            
            t += 1

        # Post-simulation metric aggregation
        end_time = t
        total_duration = end_time - start_time
        
        station_logs_out = {}
        for station_name in self.stations_config.keys():
            capacity = self.stations_config[station_name]["capacity"]
            waits = station_wait_times[station_name]
            
            # Compute queue stats
            avg_w = round(sum(waits) / len(waits), 1) if waits else 0.0
            max_w = max(waits) if waits else 0
            max_q = max(station_queue_timeline[station_name]) if station_queue_timeline[station_name] else 0
            
            # Reconstruct utilization percent exactly
            if total_duration > 0 and capacity > 0:
                active_minutes = len(station_charging_sequences[station_name]) * self.charging_duration_mins
                util_pct = (active_minutes / total_duration) * 100
                util_pct = min(round(util_pct, 1), 100.0)
            else:
                util_pct = 0.0
                
            station_logs_out[station_name] = {
                "charging_sequence": station_charging_sequences[station_name],
                "utilization_percent": util_pct,
                "max_queue": int(max_q),
                "avg_wait": float(avg_w),
                "max_wait": int(max_w)
            }

        total_sessions = sum(len(station_charging_sequences[name]) for name in self.stations_config.keys())
        avg_fleet_wait = round(sum(all_wait_times) / len(all_wait_times), 1) if all_wait_times else 0.0
        max_fleet_wait = max(all_wait_times) if all_wait_times else 0

        return {
            "simulated_buses": [
                {
                    "id": bus.id,
                    "operator": bus.operator,
                    "direction": bus.direction,
                    "state": bus.state,
                    "timeline": bus.timeline
                }
                for bus in buses
            ],
            "station_logs": station_logs_out,
            "metrics": {
                "Fleet Size": len(buses),
                "Total Charging Sessions": total_sessions,
                "Fleet Average Wait Time": avg_fleet_wait,
                "Max Bus Wait Time": max_fleet_wait
            }
        }
