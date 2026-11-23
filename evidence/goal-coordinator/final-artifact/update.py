#!/usr/bin/env python3
"""Apply additions and removals to a filename set and return sorted JSON."""

import argparse
import json


def parse_filenames(value):
    return {filename for filename in value.split(",") if filename}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("existing", help="Comma-separated existing filenames")
    parser.add_argument("additions", help="Comma-separated filenames to add")
    parser.add_argument("removals", help="Comma-separated filenames to remove")
    args = parser.parse_args()

    filenames = parse_filenames(args.existing)
    filenames.update(parse_filenames(args.additions))
    filenames.difference_update(parse_filenames(args.removals))
    print(json.dumps({"filenames": sorted(filenames)}))


if __name__ == "__main__":
    main()
