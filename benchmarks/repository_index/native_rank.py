"""Rust graph/PageRank candidate with the existing weighted reference semantics."""
import rustworkx as rx
from lib.repo_map import rank
from lib.repo_map.outline import _is_test_path

def scores(repo,chat_rel_paths=None,*,tol=1e-6,max_iter=100):
    graph=rx.PyDiGraph(multigraph=False);indices={}
    def node(name):
        if name not in indices:indices[name]=graph.add_node(name)
        return indices[name]
    for ident in set(repo.definitions):
        definers=repo.definitions.get(ident,set())
        referencers=repo.references.get(ident,set())-definers
        if not definers or not referencers:continue
        target=node('ident:'+ident)
        for path in referencers:graph.add_edge(node('file:'+path),target,len(definers))
        for path in definers:graph.add_edge(target,node('file:'+path),len(referencers))
    if not indices:return {}
    personalization={indices['file:'+path]:50.0 for path in chat_rel_paths or ()
                     if 'file:'+path in indices} or None
    try:
        result=rx.pagerank(graph,weight_fn=float,personalization=personalization,
                          nstart={index:1.0 for index in indices.values()},tol=tol,max_iter=max_iter)
    except Exception:
        return rank._fallback_scores(repo,set(repo.definitions),repo.references,repo.definitions)
    return {name[5:]:result[index]*(.2 if _is_test_path(name[5:]) else 1.)
            for name,index in indices.items() if name.startswith('file:')}
