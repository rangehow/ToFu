"""Write a corpus document as the shared competitor input, or print a field.

Usage: python -m evaluations.slides_bench.corpus_input <doc_id> <out.md>
       python -m evaluations.slides_bench.corpus_input --field lang <doc_id>
"""

import sys

from evaluations.slides_bench.corpus import load_document, write_input

if __name__ == '__main__':
    if sys.argv[1] == '--field':
        print(load_document(sys.argv[3])[sys.argv[2]])
    else:
        write_input(sys.argv[1], sys.argv[2])
