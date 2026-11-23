#!/usr/bin/env python3
"""Find filenames that start with a prefix and return sorted JSON."""

import argparse
import json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prefix", help="Filename prefix")
    parser.add_argument("filenames", nargs="*", help="Candidate filenames")
    args = parser.parse_args()

    matches = sorted(
        filename for filename in args.filenames if filename.startswith(args.prefix)
    )
    print(json.dumps({"matches": matches}))


if __name__ == "__main__":
    main()
