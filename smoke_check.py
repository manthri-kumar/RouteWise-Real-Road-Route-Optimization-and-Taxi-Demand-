"""Offline checks for deterministic demand, OSM graph logic, OSRM parsing, and Flask contracts."""
import os
os.environ["ROUTEWISE_OFFLINE"]="1"
from unittest.mock import Mock, patch
import networkx as nx
import requests
from app import app, ROADS
from data_generator import build_demand_graph, make_trips
from graph_analysis import demand_communities
from routing_service import InvalidCoordinates, OSRMClient, OSMRoadService, RoutingUnavailable, choose_routes, validate_coordinates

trips_a,trips_b=make_trips(),make_trips(); demand=build_demand_graph(trips_a)
assert len(trips_a)==5000 and trips_a.equals(trips_b) and demand.graph["kind"]=="synthetic_taxi_demand"
groups,membership=demand_communities(demand); assert groups and len(membership)==12

graph=nx.MultiDiGraph()
graph.add_node(1,x=76.53,y=9.05); graph.add_node(2,x=76.54,y=9.05); graph.add_node(3,x=76.55,y=9.05)
graph.add_edge(1,2,key=0,length=1000,name="A Road"); graph.add_edge(2,3,key=0,length=1000,name="B Road"); graph.add_edge(1,3,key=0,length=3000,name="C Road")
service=OSMRoadService("unused.graphml"); service.graph=graph; service.source="test fixture"; service.nearest_node=lambda point:1 if point[1]<76.54 else 3
route=service.shortest((9.05,76.53),(9.05,76.55)); assert route["distance_km"]==2.0 and route["path"]==["1","2","3"]
closed=service.closure((9.05,76.53),(9.05,76.55),(1,2,0)); assert closed["after"]["distance_km"]==3.0 and graph.has_edge(1,2,0)
broken=graph.copy(); broken.remove_edge(1,3,0); disconnected=OSMRoadService("unused.graphml"); disconnected.graph=broken; disconnected.nearest_node=service.nearest_node
result=disconnected.closure((9.05,76.53),(9.05,76.55),(2,3,0)); assert result["unreachable"] is True

fake_payload={"code":"Ok","routes":[{"distance":4200,"duration":720,"geometry":{"type":"LineString","coordinates":[[76.53,9.05],[76.55,9.05]]},"legs":[{"steps":[]}]},{"distance":4900,"duration":690,"geometry":{"type":"LineString","coordinates":[[76.53,9.05],[76.55,9.05]]},"legs":[{"steps":[]}]}]}
response=Mock(); response.raise_for_status.return_value=None; response.json.return_value=fake_payload; session=Mock(); session.get.return_value=response
parsed=OSRMClient(session=session).route((9.05,76.53),(9.00,76.55)); assert len(parsed)==2 and choose_routes(parsed,"time")[0]["duration_min"]==11.5
response.json.return_value={"code":"Ok","routes":[fake_payload["routes"][0]]}; assert len(OSRMClient(session=session).route((9.05,76.53),(9.00,76.55)))==1
for coords in [((91,76),(9,76)),((9,181),(9,76))]:
    try:OSRMClient(session=session).route(*coords); raise AssertionError("invalid coordinates accepted")
    except InvalidCoordinates:pass
session.get.side_effect=requests.Timeout()
try:OSRMClient(session=session).route((9.05,76.53),(9.00,76.55)); raise AssertionError("timeout accepted")
except RoutingUnavailable as exc:assert "timed out" in str(exc)

with patch.dict("sys.modules",{"osmnx":Mock(load_graphml=Mock(return_value=graph))}):
    with patch("pathlib.Path.exists",return_value=True):
        cached=OSMRoadService("fixture.graphml"); assert cached.load(allow_download=False) is graph and cached.source=="cache"

ROADS.graph=graph; ROADS.source="offline test fixture"; ROADS.error=None; ROADS.nearest_node=service.nearest_node
client=app.test_client()
for endpoint in ["/","/api/overview","/api/network","/api/road-network","/api/landmark-network","/api/analysis","/api/trips?page=1&per_page=5","/api/trips/download"]:assert client.get(endpoint).status_code==200,endpoint
assert client.get("/api/route?source_lat=99&source_lon=76&target=Chavara&optimize=distance").status_code==400
route_response=client.get("/api/route?source=Karunagappally&target=Chavara&optimize=distance"); assert route_response.status_code==200
route_payload=route_response.get_json()["data"]; assert route_payload["routes"][0]["route_id"] and route_payload["provider_metadata"]["live_traffic"] is False
assert client.get("/api/closure?source=Karunagappally&target=Chavara&optimize=time&edge_u=1&edge_v=2&edge_key=0").status_code==400
assert validate_coordinates(9.05,76.53)==(9.05,76.53)
print("RouteWise smoke check passed: synthetic/real separation, cache loading, Dijkstra, closures, OSRM parsing/errors, and Flask contracts verified offline")
