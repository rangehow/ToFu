"""Production traversal candidate for comparison with immutable Git evidence.

Capture the actual callable before the runner temporarily selects its reference.
The extraction algorithm has one owner: lib.repo_map.outline.
"""
from index import outline

extract = outline._extract_treesitter_tags
