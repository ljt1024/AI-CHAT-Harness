import csv
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import sys


def summarize(path):
    path = Path(path).resolve()
    path.relative_to(Path('/workspace'))
    with path.open(newline='', encoding='utf-8') as source:
        reader = csv.DictReader(source)
        columns = reader.fieldnames or []
        totals = {name: Decimal(0) for name in columns}
        numeric = set(columns)
        count = 0
        for row in reader:
            count += 1
            for name in list(numeric):
                try:
                    value = Decimal(row[name])
                    if not value.is_finite():
                        raise InvalidOperation
                    totals[name] += value
                except (InvalidOperation, TypeError):
                    numeric.remove(name)
    return {'rows': count, 'columns': columns,
            'numeric_totals': {name: str(totals[name]) for name in columns if name in numeric}}


if __name__ == '__main__':
    print(json.dumps(summarize(sys.argv[1])))
