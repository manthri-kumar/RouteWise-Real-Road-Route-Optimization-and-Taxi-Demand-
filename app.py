"""RouteWise Flask application: synthetic demand analytics + real-road routing."""
from io import StringIO
import os
from flask import Flask, Response, jsonify, render_template, request
from data_generator import build_demand_graph, make_trips
from graph_analysis import analysis_summary, demand_communities
from routing_service import InvalidCoordinates, LOCATIONS, OSRMClient, OSMRoadService, RoutingUnavailable, choose_routes

app=Flask(__name__); TRIPS=make_trips(); DEMAND=build_demand_graph(TRIPS)
DEMAND_ANALYSIS=analysis_summary(None,DEMAND); ROADS=OSMRoadService(); OSRM=OSRMClient()
ROADS.load(allow_download=os.getenv("ROUTEWISE_OFFLINE","0")!="1")
ROAD_ANALYSIS_CACHE=None

def ok(data,meta=None):
    payload={"ok":True,"data":data}
    if meta is not None:payload["meta"]=meta
    return jsonify(payload)
def error(message,status=400,code="invalid_request",details=None):
    body={"code":code,"message":message}
    if details:body["details"]=details
    return jsonify({"ok":False,"error":body}),status
def endpoint_coordinates(prefix):
    name=request.args.get(prefix,"").strip(); lat=request.args.get(f"{prefix}_lat"); lon=request.args.get(f"{prefix}_lon")
    if lat not in (None,"") or lon not in (None,""):
        if lat in (None,"") or lon in (None,""): raise InvalidCoordinates(f"Both {prefix}_lat and {prefix}_lon are required.")
        from routing_service import validate_coordinates
        return validate_coordinates(lat,lon), name or "Custom point"
    if name not in LOCATIONS: raise InvalidCoordinates(f"Unknown {prefix} location '{name}'.")
    return LOCATIONS[name],name
def route_request():
    source,source_name=endpoint_coordinates("source"); target,target_name=endpoint_coordinates("target"); optimize=request.args.get("optimize","distance")
    if optimize not in {"distance","time","fare"}: raise ValueError("optimize must be distance, time, or fare.")
    return source,target,source_name,target_name,optimize

@app.get("/")
def index():return render_template("index.html")
@app.get("/api/overview")
def overview():
    pickups=TRIPS.pickup.value_counts(); hourly=TRIPS.groupby(TRIPS.datetime.dt.hour).size().reindex(range(24),fill_value=0); top=TRIPS.groupby(["pickup","dropoff"]).size().sort_values(ascending=False).head(8); groups,_=demand_communities(DEMAND); road_status=ROADS.status()
    return ok({"kpis":{"trips":len(TRIPS),"locations":len(LOCATIONS),"road_nodes":road_status["nodes"],"road_edges":road_status["edges"],"communities":len(groups),"average_synthetic_fare_inr":round(float(TRIPS.estimated_fare_inr.mean()))},"pickup_rankings":[{"location":n,"trips":int(v)} for n,v in pickups.items()],"demand_by_hour":[{"hour":int(h),"trips":int(v)} for h,v in hourly.items()],"top_routes":[{"source":p[0],"target":p[1],"trips":int(v)} for p,v in top.items()],"road_status":road_status,"datasets":{"taxi_demand":"Deterministic synthetic trips (seed 42).","road_network":"OpenStreetMap drivable-road graph when cache/download is available.","route_service":"OSRM public driving profile for fastest/fare candidate routes; no live traffic."}})
@app.get("/api/network")
def network():
    membership=DEMAND_ANALYSIS["membership"]
    return ok({"directed":True,"nodes":[{"id":n,**d,"community_id":membership[n]} for n,d in DEMAND.nodes(data=True)],"edges":[{"source":u,"target":v,"trips":d["trips"]} for u,v,d in DEMAND.edges(data=True)],"dataset":"synthetic_demand","meaning":"Directed edges are synthetic taxi origin-destination demand, never physical roads."})
@app.get("/api/road-network")
def road_network():
    status=ROADS.status(); candidates=[]
    if status["available"]:
        try:candidates=ROADS.candidate_edges()
        except Exception as exc:status["warning"]=str(exc)
    return ok({"status":status,"locations":[{"name":n,"lat":p[0],"lon":p[1]} for n,p in LOCATIONS.items()],"closure_edges":candidates,"dataset":"real_osm_roads","meaning":"Directed OSM MultiDiGraph preserving node IDs, parallel edges, and one-way direction. Basemap tiles visualize roads; this endpoint does not serialize the full cached graph."})
@app.get("/api/landmark-network")
def landmark_network():
    try:return ok(ROADS.landmark_network())
    except RoutingUnavailable as exc:return error(str(exc),503,"road_graph_unavailable",ROADS.status())
@app.get("/api/trips")
def trips():
    try:page=max(1,int(request.args.get("page",1))); per=min(100,max(1,int(request.args.get("per_page",10))))
    except ValueError:return error("page and per_page must be integers.")
    start=(page-1)*per; records=TRIPS.iloc[start:start+per].copy(); records["datetime"]=records.datetime.dt.strftime("%Y-%m-%d %H:%M")
    return ok(records.to_dict("records"),{"page":page,"per_page":per,"total":len(TRIPS),"pages":(len(TRIPS)+per-1)//per,"dataset":"synthetic_demand"})
@app.get("/api/route")
def route():
    try:
        source,target,source_name,target_name,optimize=route_request()
        if source==target:
            route={"rank":1,"distance_km":0,"duration_min":0,"estimated_fare_inr":0,"geometry":{"type":"LineString","coordinates":[[source[1],source[0]],[source[1],source[0]]]},"steps":[],"optimized_for":optimize,"optimization_cost":0,"provider":"No routing required","explanation":"Source and destination coordinates are identical."}; routes=[route]
        elif optimize=="distance":
            best=ROADS.shortest(source,target); best.update({"rank":1,"steps":[],"explanation":"Recommended: NetworkX Dijkstra minimum-length path on the cached directed OSM graph."}); routes=[best]
            try:
                osrm_routes=choose_routes(OSRM.route(source,target),"distance")
                for candidate in osrm_routes:
                    if abs(candidate["distance_km"]-best["distance_km"])>.02:routes.append({**candidate,"rank":len(routes)+1,"explanation":"OSRM-returned driving alternative; the NetworkX OSM path remains the minimum-length recommendation."})
                    if len(routes)>=3:break
            except RoutingUnavailable:pass
        else:routes=choose_routes(OSRM.route(source,target),optimize)
        for index,item in enumerate(routes):item["route_id"]=f"{source_name}::{target_name}::{optimize}::{index+1}"
        return ok({"source":{"name":source_name,"lat":source[0],"lon":source[1]},"target":{"name":target_name,"lat":target[0],"lon":target[1]},"optimize":optimize,"routes":routes,"provider_metadata":{"road_graph":"OpenStreetMap directed MultiDiGraph","routing":"NetworkX Dijkstra for distance; OSRM candidate routes for time/fare","live_traffic":False},"limitations":"OSM/OSRM map data and driving estimates are not live traffic. OSRM alternatives are a limited candidate set, not a global guarantee."})
    except InvalidCoordinates as exc:return error(str(exc),400,"invalid_coordinates")
    except ValueError as exc:return error(str(exc),400,"invalid_request")
    except RoutingUnavailable as exc:return error(str(exc),503,"routing_unavailable",ROADS.status())
@app.get("/api/closure")
def closure():
    try:
        source,target,_,_,optimize=route_request()
        if optimize!="distance":return error("Hypothetical closures use NetworkX OSM shortest-distance routing only; OSRM is not informed of local closures.",400,"unsupported_closure_optimization")
        edge=(request.args.get("edge_u"),request.args.get("edge_v"),request.args.get("edge_key"))
        if any(value in (None,"") for value in edge):return error("edge_u, edge_v, and edge_key are required.",400,"invalid_edge")
        return ok(ROADS.closure(source,target,edge))
    except InvalidCoordinates as exc:return error(str(exc),400,"invalid_coordinates")
    except (ValueError,TypeError) as exc:return error(str(exc),400,"invalid_edge")
    except RoutingUnavailable as exc:return error(str(exc),503,"routing_unavailable",ROADS.status())
@app.get("/api/analysis")
def analysis():
    global ROAD_ANALYSIS_CACHE
    road_analysis=ROAD_ANALYSIS_CACHE; road_error=None
    if ROADS.graph is not None and road_analysis is None:
        try:road_analysis=ROADS.analysis(); ROAD_ANALYSIS_CACHE=road_analysis
        except Exception as exc:road_error=str(exc)
    return ok({"synthetic_demand":{"communities":DEMAND_ANALYSIS["communities"],"demand_strength":DEMAND_ANALYSIS["demand_strength"],"methodology":DEMAND_ANALYSIS["methodology"]["communities"]},"real_road_network":road_analysis,"road_error":road_error or ROADS.error,"separation":"Louvain and trip rankings use synthetic demand. Road centrality and connectivity use the cached real OSM graph."})
@app.get("/api/trips/download")
def download_trips():
    stream=StringIO(); TRIPS.to_csv(stream,index=False)
    return Response(stream.getvalue(),mimetype="text/csv",headers={"Content-Disposition":"attachment; filename=routewise_synthetic_trips.csv"})
if __name__=="__main__":app.run(debug=True,port=5000)
