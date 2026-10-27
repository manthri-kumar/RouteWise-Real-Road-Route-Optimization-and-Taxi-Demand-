"""NetworkX graph-mining and routing algorithms used by RouteWise."""
import networkx as nx
from data_generator import FARE_BASE, FARE_PER_KM

OPTIMIZE_WEIGHTS = {"distance":"distance_km", "time":"time_min", "fare":"fare"}

def path_stats(graph, path, optimize="distance"):
    km=sum(graph[u][v]["distance_km"] for u,v in zip(path,path[1:])); minutes=sum(graph[u][v]["time_min"] for u,v in zip(path,path[1:])); fare=FARE_BASE+FARE_PER_KM*km
    costs={"distance":km,"time":minutes,"fare":fare}
    return {"path":path,"distance_km":round(km,1),"time_min":round(minutes,1),"estimated_fare_inr":round(fare),"optimization_cost":round(costs[optimize],1),"optimized_for":optimize}

def find_routes(graph, source, target, optimize="distance", limit=3):
    if source==target:
        return [{"path":[source],"distance_km":0.0,"time_min":0.0,"estimated_fare_inr":0,"optimization_cost":0.0,"optimized_for":optimize,"rank":1,"explanation":"Source and destination are the same; no travel is required."}]
    try:
        paths=nx.shortest_simple_paths(graph,source,target,weight=OPTIMIZE_WEIGHTS[optimize]); routes=[path_stats(graph,p,optimize) for _,p in zip(range(limit),paths)]
    except (nx.NetworkXNoPath,nx.NodeNotFound): return []
    if not routes:return routes
    best=routes[0]; units={"distance":"km","time":"min","fare":"INR"}; labels={"distance":"simulated distance","time":"simulated travel time","fare":"distance-based estimated fare"}
    for i,route in enumerate(routes,1):
        route["rank"]=i; delta=round(route["optimization_cost"]-best["optimization_cost"],1)
        route["explanation"]=(f"Recommended: minimum {labels[optimize]} at {route['optimization_cost']} {units[optimize]}." if i==1 else f"Alternative {i}: {delta:+g} {units[optimize]} versus the recommended route; it remains a distinct valid path.")
    return routes

def simulate_closure(graph, source, target, edge, optimize="distance"):
    before=find_routes(graph,source,target,optimize,1); changed=graph.copy()
    if not changed.has_edge(*edge): raise ValueError("The selected road connection does not exist.")
    changed.remove_edge(*edge); after=find_routes(changed,source,target,optimize,1); before=before[0] if before else None; after=after[0] if after else None
    delta=None
    if before and after: delta={"distance_km":round(after["distance_km"]-before["distance_km"],1),"time_min":round(after["time_min"]-before["time_min"],1),"estimated_fare_inr":after["estimated_fare_inr"]-before["estimated_fare_inr"]}
    return {"closed_edge":list(edge),"before":before,"after":after,"delta":delta,"unreachable":bool(before and not after)}

def demand_communities(demand):
    aggregate=nx.Graph(); aggregate.add_nodes_from(demand.nodes())
    for u,v,d in demand.edges(data=True):
        if aggregate.has_edge(u,v): aggregate[u][v]["weight"]+=d["trips"]
        else: aggregate.add_edge(u,v,weight=d["trips"])
    groups=sorted(nx.community.louvain_communities(aggregate,weight="weight",seed=42),key=lambda g:(-len(g),sorted(g)[0])); membership={node:i+1 for i,g in enumerate(groups) for node in g}
    return groups,membership

def critical_roads(graph):
    original=dict(nx.all_pairs_dijkstra_path_length(graph,weight="distance_km")); rows=[]
    for u,v in graph.edges():
        trial=graph.copy(); trial.remove_edge(u,v); disconnected=not nx.is_connected(trial); impact=None
        if not disconnected:
            impact=sum(sum(nx.single_source_dijkstra_path_length(trial,a,weight="distance_km")[b]-original[a][b] for b in graph.nodes()) for a in graph.nodes())/2
        rows.append({"source":u,"target":v,"road":f"{u} — {v}","disconnects_network":disconnected,"total_extra_km_all_pairs":None if impact is None else round(impact,1)})
    return sorted(rows,key=lambda r:(r["disconnects_network"],r["total_extra_km_all_pairs"] or 0),reverse=True)

def analysis_summary(road,demand):
    groups,membership=demand_communities(demand); strength={n:int(demand.in_degree(n,weight="trips")+demand.out_degree(n,weight="trips")) for n in demand.nodes()}
    result={"demand_strength":[{"location":n,"trips":s} for n,s in sorted(strength.items(),key=lambda x:x[1],reverse=True)],"communities":[{"id":i+1,"members":sorted(g)} for i,g in enumerate(groups)],"membership":membership,"methodology":{"communities":"Louvain detection on undirected aggregated synthetic trip counts; counts are relationship strength."}}
    if road is not None:
        between=nx.betweenness_centrality(road,weight="distance_km")
        result.update({"centrality":[{"location":n,"score":round(s,4)} for n,s in sorted(between.items(),key=lambda x:x[1],reverse=True)],"critical_connections":critical_roads(road)})
    return result
