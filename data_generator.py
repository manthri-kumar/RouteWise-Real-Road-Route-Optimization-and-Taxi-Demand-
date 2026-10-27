"""Deterministic synthetic mobility data for the RouteWise academic demo."""
import networkx as nx
import numpy as np
import pandas as pd

SEED, TRIP_COUNT, FARE_BASE, FARE_PER_KM = 42, 5_000, 40, 18
LOCATIONS = ["Karunagappally", "Railway Station", "KSRTC Bus Stand", "Oachira", "Chavara", "Sasthamkotta", "Mynagappally", "Thazhava", "Clappana", "Vallikavu", "Amritapuri", "Puthiyakavu"]
LAYOUT = {name: point for name, point in zip(LOCATIONS, [(49,48),(40,29),(53,28),(56,76),(79,73),(82,29),(67,52),(26,31),(18,53),(29,72),(17,78),(71,16)])}
ROADS = [(0,1,2),(0,2,1.2),(0,3,6),(0,4,7),(0,6,5),(0,7,7),(1,2,1.5),(1,11,4),(2,4,6),(3,7,5),(3,8,6),(4,9,5),(4,5,9),(5,6,5),(5,7,7),(6,7,4),(7,8,5),(8,9,6),(9,10,2),(10,11,6),(11,0,5)]

def build_road_graph():
    graph = nx.Graph(kind="simulated_road_network")
    for name in LOCATIONS:
        graph.add_node(name, x=LAYOUT[name][0], y=LAYOUT[name][1])
    for a, b, km in ROADS:
        graph.add_edge(LOCATIONS[a], LOCATIONS[b], distance_km=float(km), time_min=round(km*2.4+2,1), fare=float(km*FARE_PER_KM))
    return graph

def make_trips(n=TRIP_COUNT, seed=SEED):
    rng = np.random.default_rng(seed)
    probabilities = np.array([18,12,13,9,10,7,6,6,5,5,4,5], dtype=float); probabilities /= probabilities.sum()
    origins = rng.choice(LOCATIONS, size=n, p=probabilities)
    destinations = []
    for origin in origins:
        choices = [x for x in LOCATIONS if x != origin]
        weights = np.array([probabilities[LOCATIONS.index(x)] for x in choices]); weights /= weights.sum()
        destinations.append(rng.choice(choices, p=weights))
    road = build_road_graph()
    distances = np.array([nx.shortest_path_length(road, a, b, weight="distance_km") for a,b in zip(origins,destinations)])
    dates = pd.Timestamp("2026-09-01") + pd.to_timedelta(rng.integers(0,30*24*60,n), unit="m")
    return pd.DataFrame({"trip_id":[f"RTW-{i:05d}" for i in range(1,n+1)], "pickup":origins, "dropoff":destinations, "datetime":dates, "distance_km":distances.round(1), "travel_time_min":(distances*2.4+2).round(1), "estimated_fare_inr":(FARE_BASE+distances*FARE_PER_KM).round().astype(int)})

def build_demand_graph(trips):
    graph = nx.DiGraph(kind="synthetic_taxi_demand")
    pickups, dropoffs = trips.pickup.value_counts(), trips.dropoff.value_counts()
    for location in LOCATIONS:
        graph.add_node(location, pickups=int(pickups.get(location,0)), dropoffs=int(dropoffs.get(location,0)), x=LAYOUT[location][0], y=LAYOUT[location][1])
    for (a,b), count in trips.groupby(["pickup","dropoff"]).size().items():
        graph.add_edge(a,b,trips=int(count))
    return graph
