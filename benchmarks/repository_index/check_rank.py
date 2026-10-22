"""Weighted PageRank, personalization and test-file penalty against canonical NetworkX."""
import math
import random
from index import outline
from lib.repo_map import rank
from native_rank import scores

def check():
    rng=random.Random(20261007)
    for count in range(1,101):
        repo=outline.RepoMap(root='fixture')
        files=[f'source_{index}.py' for index in range(count)]+['tests/test_fixture.py']
        for ident in range(20):
            name=f'symbol_{ident}'
            repo.definitions[name]=set(rng.sample(files,rng.randrange(1,min(8,len(files))+1)))
            repo.references[name]=set(rng.sample(files,rng.randrange(1,len(files)+1)))
        for focus in (None,{files[0]},set(files[:3])):
            expected=rank.sumbd(repo,focus);actual=scores(repo,focus)
            assert expected.keys()==actual.keys()
            assert all(math.isclose(expected[key],actual[key],rel_tol=1e-10,abs_tol=1e-12)
                       for key in expected),(count,max(abs(expected[key]-actual[key]) for key in expected))
    print('300 weighted ranking/personalization cases match NetworkX within declared tolerance')

if __name__=='__main__':check()
