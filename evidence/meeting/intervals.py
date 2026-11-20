import json
import sys


def union_minutes(intervals):
    if not intervals:
        return 0

    ordered = iter(sorted(intervals))
    start, end = next(ordered)
    total = 0
    for next_start, next_end in ordered:
        if next_start <= end:
            end = max(end, next_end)
        else:
            total += end - start
            start, end = next_start, next_end
    return total + (end - start)


if __name__ == "__main__":
    intervals = json.loads(sys.argv[1])
    print(json.dumps({"minutes": union_minutes(intervals)}))
