"""Export a deck the Tofu pipeline refused to publish, for diagnosis only.

Usage: python -m evaluations.slides_bench.force_export <run-dir>/tofu/<doc>

A deck whose layout-QA or visual-QA gate refused publication has authored
pages but no PPTX. The benchmark exports those pages as they stand
(``forced.pptx``) so the judge can still score their content; evaluate.py
labels them undelivered and keeps them out of the delivered averages.
"""

import os
import sys

from lib.slides.export_pptx import export_pptx
from lib.slides.pptd import parse_deck

if __name__ == '__main__':
    base = sys.argv[1]
    deck = parse_deck(os.path.join(base, 'deck', 'deck.pptd'))
    export_pptx(deck, os.path.join(base, 'forced.pptx'))
