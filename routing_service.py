"""Real-road OSM graph loading, OSRM routing, and closure simulation."""
from __future__ import annotations

import math
import os
from pathlib import Path

import networkx as nx
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

OSRM_BASE_URL = os.getenv("OSRM_BASE_URL", "https://router.project-osrm.org").rstrip("/")
OSRM_TIMEOUT = float(os.getenv("OSRM_TIMEOUT_SECONDS", "12"))
FARE_BASE = float(os.getenv("ROUTEWISE_FARE_BASE_INR", "45"))
FARE_PER_KM = float(os.getenv("ROUTEWISE_FARE_PER_KM_INR", "18"))
FARE_PER_MIN = float(os.getenv("ROUTEWISE_FARE_PER_MIN_INR", "1.5"))
CACHE_PATH = Path(os.getenv("OSM_GRAPH_CACHE", Path(__file__).parent / "data" / "karunagappally_drive.graphml"))

# Stable landmark coordinates used by the UI and as graph snapping inputs.
LOCATIONS = {
    "Karunagappally": (9.0544, 76.5353), "Railway Station": (9.0507, 76.5369),
    "KSRTC Bus Stand": (9.0558, 76.5358), "Oachira": (9.1327, 76.5152),
    "Chavara": (8.9967, 76.5270), "Sasthamkotta": (9.0368, 76.6250),
    "Mynagappally": (9.0167, 76.5500), "Thazhava": (9.0980, 76.5290),
    "Clappana": (9.1045, 76.5014), "Vallikavu": (9.0818, 76.4864),
    "Amritapuri": (9.0895, 76.4860), "Puthiyakavu": (9.0697, 76.5480),
}
BOUNDING_BOX = (9.16, 8.96, 76.66, 76.46)  # north, south, east, west


class RoutingUnavailable(RuntimeError): pass
class InvalidCoordinates(ValueError): pass


def validate_coordinates(lat, lon):
    try: lat, lon = float(lat), float(lon)
    except (TypeError, ValueError) as exc: raise InvalidCoordinates("Coordinates must be numeric latitude and longitude values.") from exc
    if not -90 <= lat <= 90 or not -180 <= lon <= 180:
        raise InvalidCoordinates("Latitude must be -90..90 and longitude -180..180.")
    return lat, lon


def estimated_fare(distance_km, duration_min):
    return round(FARE_BASE + distance_km * FARE_PER_KM + duration_min * FARE_PER_MIN)


def geometry_coordinates(graph, path):
    coordinates = []
    for u, v in zip(path, path[1:]):
        edges = graph.get_edge_data(u, v)
        _, data = min(edges.items(), key=lambda item: float(item[1].get("length", math.inf)))
        geometry = data.get("geometry")
        segment = list(geometry.coords) if geometry is not None else [(graph.nodes[u]["x"], graph.nodes[u]["y"]), (graph.nodes[v]["x"], graph.nodes[v]["y"])]
        if coordinates and segment and coordinates[-1] == segment[-1]: segment.reverse()
        coordinates.extend(segment if not coordinates else segment[1:])
    return [[float(lon), float(lat)] for lon, lat in coordinates]


def path_stats(graph, path):
    distance_m = 0.0
    for u, v in zip(path, path[1:]):
        distance_m += min(float(data.get("length", 0)) for data in graph.get_edge_data(u, v).values())
    distance_km = distance_m / 1000
    duration_min = distance_km / 28 * 60
    return {"path":[str(n) for n in path], "distance_km":round(distance_km,2), "duration_min":round(duration_min,1), "estimated_fare_inr":estimated_fare(distance_km,duration_min), "geometry":{"type":"LineString","coordinates":geometry_coordinates(graph,path)}}


class OSMRoadService:
    def __init__(self, cache_path=CACHE_PATH):
        self.cache_path = Path(cache_path); self.graph = None; self.error = None; self.source = None; self._landmark_network = None

    def load(self, allow_download=True):
        try:
            import osmnx as ox
            if self.cache_path.exists():
                self.graph = ox.load_graphml(self.cache_path); self.source = "cache"
            elif allow_download:
                north, south, east, west = BOUNDING_BOX
                self.graph = ox.graph_from_bbox((west, south, east, north), network_type="drive", simplify=True, retain_all=False)
                self.cache_path.parent.mkdir(parents=True, exist_ok=True)
                ox.save_graphml(self.graph, self.cache_path); self.source = "OpenStreetMap download"
            else: raise RoutingUnavailable(f"OSM cache not found at {self.cache_path}.")
            self.error = None
        except Exception as exc:
            self.graph = None; self.error = str(exc); self.source = None
        return self.graph

    def require(self):
        if self.graph is None: raise RoutingUnavailable(self.error or "Real OSM road graph is unavailable.")
        return self.graph

    def nearest_node(self, coordinates):
        lat, lon = validate_coordinates(*coordinates)
        graph=self.require()
        return min(graph.nodes,key=lambda node:(float(graph.nodes[node]["x"])-lon)**2+(float(graph.nodes[node]["y"])-lat)**2)

    def shortest(self, source_coordinates, target_coordinates):
        graph = self.require(); source=self.nearest_node(source_coordinates); target=self.nearest_node(target_coordinates)
        try: path=nx.shortest_path(graph,source,target,weight="length",method="dijkstra")
        except nx.NetworkXNoPath as exc: raise RoutingUnavailable("No directed driving path exists between the selected points.") from exc
        route=path_stats(graph,path); route.update({"provider":"OpenStreetMap + NetworkX", "optimization_cost":route["distance_km"], "optimized_for":"distance"})
        return route

    def closure(self, source_coordinates, target_coordinates, edge):
        graph=self.require(); u,v,key=(int(edge[0]),int(edge[1]),int(edge[2])); source=self.nearest_node(source_coordinates); target=self.nearest_node(target_coordinates)
        if not graph.has_edge(u,v,key): raise ValueError("The selected directed OSM edge does not exist in the cached graph.")
        before_path=nx.shortest_path(graph,source,target,weight="length"); changed=graph.copy(); changed.remove_edge(u,v,key)
        before=path_stats(graph,before_path)
        try: after=path_stats(changed,nx.shortest_path(changed,source,target,weight="length"))
        except nx.NetworkXNoPath: after=None
        delta=None if after is None else {"distance_km":round(after["distance_km"]-before["distance_km"],2),"duration_min":round(after["duration_min"]-before["duration_min"],1),"estimated_fare_inr":after["estimated_fare_inr"]-before["estimated_fare_inr"]}
        edge_data=graph.get_edge_data(u,v,key); geometry=edge_data.get("geometry")
        coordinates=list(geometry.coords) if geometry is not None else [(graph.nodes[u]["x"],graph.nodes[u]["y"]),(graph.nodes[v]["x"],graph.nodes[v]["y"])]
        return {"closed_edge":{"u":str(u),"v":str(v),"key":key,"name":edge_data.get("name") or "Unnamed road","geometry":{"type":"LineString","coordinates":[[float(lon),float(lat)] for lon,lat in coordinates]}},"before":before,"after":after,"delta":delta,"unreachable":after is None,"provider":"OpenStreetMap graph + NetworkX; hypothetical closure only (not sent to OSRM)."}

    def landmark_network(self, neighbor_count=3):
        if self._landmark_network is not None:return self._landmark_network
        graph=self.require(); names=list(LOCATIONS); snapped={name:self.nearest_node(LOCATIONS[name]) for name in names}; edges=[]
        for source in names:
            lat,lon=LOCATIONS[source]
            neighbors=sorted((target for target in names if target!=source),key=lambda target:(LOCATIONS[target][0]-lat)**2+(LOCATIONS[target][1]-lon)**2)[:neighbor_count]
            for target in neighbors:
                try:
                    path=nx.shortest_path(graph,snapped[source],snapped[target],weight="length",method="dijkstra"); stats=path_stats(graph,path)
                except nx.NetworkXNoPath:continue
                edges.append({"id":f"{source}::{target}","source":source,"target":target,"distance_km":stats["distance_km"],"duration_min":stats["duration_min"],"provider":"Aggregated landmark connection computed on directed OSM roads"})
        self._landmark_network={"nodes":[{"id":name,"lat":LOCATIONS[name][0],"lon":LOCATIONS[name][1]} for name in names],"edges":edges,"directed":True,"meaning":"Each edge is an aggregated landmark-to-landmark NetworkX path over real directed OSM roads, not an individual road segment."}
        return self._landmark_network

    def candidate_edges(self, limit=40):
        graph=self.require(); center=self.nearest_node(LOCATIONS["Karunagappally"]); nodes=list(nx.single_source_shortest_path_length(graph,center,cutoff=15))
        rows=[]
        for u,v,key,data in graph.edges(nodes,keys=True,data=True):
            name=data.get("name") or "Unnamed road"; name=", ".join(name) if isinstance(name,list) else str(name)
            rows.append({"u":str(u),"v":str(v),"key":int(key),"name":name,"length_m":round(float(data.get("length",0)),1),"oneway":bool(data.get("oneway",False))})
            if len(rows)>=limit:return rows
        return rows

    def status(self):
        graph=self.graph
        return {"available":graph is not None,"source":self.source,"cache_path":str(self.cache_path),"error":self.error,"nodes":graph.number_of_nodes() if graph else 0,"edges":graph.number_of_edges() if graph else 0,"directed":True,"multigraph":True,"bbox":{"north":BOUNDING_BOX[0],"south":BOUNDING_BOX[1],"east":BOUNDING_BOX[2],"west":BOUNDING_BOX[3]}}

    def analysis(self, max_nodes=450, sample_k=80):
        graph=self.require(); center=self.nearest_node(LOCATIONS["Karunagappally"])
        nearby=list(nx.single_source_shortest_path_length(graph.to_undirected(),center,cutoff=12))[:max_nodes]
        simple=nx.DiGraph()
        for u,v,data in graph.subgraph(nearby).edges(data=True):
            length=float(data.get("length",0))
            if not simple.has_edge(u,v) or length<simple[u][v]["length"]: simple.add_edge(u,v,length=length)
        k=min(sample_k,simple.number_of_nodes())
        centrality=nx.betweenness_centrality(simple,k=k,weight="length",seed=42,normalized=True)
        ranked=sorted(centrality.items(),key=lambda item:item[1],reverse=True)[:8]
        undirected=simple.to_undirected(); critical=[]
        for u,v in list(nx.bridges(undirected))[:8]:
            critical.append({"u":str(u),"v":str(v),"disconnects_bounded_subgraph":True,"length_m":round(float(undirected[u][v].get("length",0)),1)})
        return {"scope":{"nodes":simple.number_of_nodes(),"edges":simple.number_of_edges(),"sample_k":k,"approximate":True},"centrality":[{"osm_node_id":str(node),"score":round(score,5),"lat":float(graph.nodes[node]["y"]),"lon":float(graph.nodes[node]["x"])} for node,score in ranked],"critical_connections":critical,"connectivity":{"weakly_connected":nx.is_weakly_connected(graph),"strongly_connected":nx.is_strongly_connected(graph)},"methodology":"Approximate weighted betweenness on a bounded central subgraph; OSM edge length is traversal cost. Critical connections are bridges whose removal disconnects that bounded undirected analysis subgraph. Full-city analysis is intentionally bounded for interactive performance."}


class OSRMClient:
    def __init__(self, base_url=OSRM_BASE_URL, timeout=OSRM_TIMEOUT, session=None):
        self.base_url=base_url.rstrip("/"); self.timeout=timeout; self.session=session or requests.Session()
        if session is None:
            retry=Retry(total=2,connect=2,read=1,backoff_factor=.35,status_forcelist=(429,500,502,503,504),allowed_methods=("GET",)); self.session.mount("https://",HTTPAdapter(max_retries=retry)); self.session.headers.update({"User-Agent":"RouteWise-Academic-Demo/1.0"})

    def route(self, source_coordinates, target_coordinates):
        s_lat,s_lon=validate_coordinates(*source_coordinates); t_lat,t_lon=validate_coordinates(*target_coordinates)
        url=f"{self.base_url}/route/v1/driving/{s_lon},{s_lat};{t_lon},{t_lat}"
        try: response=self.session.get(url,params={"alternatives":"true","steps":"true","overview":"full","geometries":"geojson"},timeout=self.timeout); response.raise_for_status()
        except requests.Timeout as exc: raise RoutingUnavailable("OSRM timed out; try again later or use the distance mode with the cached OSM graph.") from exc
        except requests.RequestException as exc: raise RoutingUnavailable("OSRM routing service is currently unavailable.") from exc
        try: payload=response.json()
        except (ValueError,TypeError) as exc: raise RoutingUnavailable("OSRM returned an invalid non-JSON response.") from exc
        if payload.get("code")!="Ok": raise RoutingUnavailable(payload.get("message") or "OSRM could not calculate a route.")
        routes=[]
        for item in payload.get("routes",[]):
            if not isinstance(item,dict) or "distance" not in item or "duration" not in item or not isinstance(item.get("geometry"),dict): continue
            km=float(item["distance"])/1000; minutes=float(item["duration"])/60
            routes.append({"distance_km":round(km,2),"duration_min":round(minutes,1),"estimated_fare_inr":estimated_fare(km,minutes),"geometry":item["geometry"],"steps":[{"name":s.get("name") or "Unnamed road","distance_m":round(float(s.get("distance",0))),"duration_min":round(float(s.get("duration",0))/60,1),"instruction":s.get("maneuver",{}).get("type","")} for leg in item.get("legs",[]) for s in leg.get("steps",[])],"provider":"OSRM public driving profile"})
        if not routes: raise RoutingUnavailable("OSRM returned no driving routes for these coordinates.")
        return routes


def choose_routes(osrm_routes, optimize):
    key={"time":"duration_min","fare":"estimated_fare_inr","distance":"distance_km"}[optimize]; ordered=sorted(osrm_routes,key=lambda route:route[key]); best=ordered[0]
    for index,route in enumerate(ordered,1):
        route["rank"]=index; route["optimized_for"]=optimize; route["optimization_cost"]=route[key]; delta=round(route[key]-best[key],2)
        route["explanation"]=(f"Recommended among {len(ordered)} OSRM-returned candidate route(s): lowest {key.replace('_',' ')} ({route[key]})." if index==1 else f"OSRM alternative {index}: {delta:+g} {('min' if optimize=='time' else 'INR' if optimize=='fare' else 'km')} versus the recommendation.")
    return ordered
