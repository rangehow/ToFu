"""Emit retained per-case outcomes and child identity properties from JUnit."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import xml.etree.ElementTree as ET


def main() -> None:
    for path in sys.argv[1:]:
        root = ET.parse(path).getroot()
        for case in root.iter('testcase'):
            status = next((tag for tag in ('failure', 'error', 'skipped')
                           if case.find(tag) is not None), 'passed')
            print(json.dumps({
                'receipt': 'sql-authority-case.v1',
                'case': case.get('classname', '') + '::' + case.get('name', ''),
                'status': status,
                'properties': {item.get('name'): item.get('value')
                               for item in case.findall('properties/property')},
            }, sort_keys=True), flush=True)
        print(json.dumps({'receipt': 'sql-authority-junit.v1',
                          'report': Path(path).name,
                          'cases': sum(1 for _ in root.iter('testcase'))}), flush=True)


if __name__ == '__main__':
    main()
