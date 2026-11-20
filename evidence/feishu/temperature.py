import json
import sys


def main():
    celsius = float(sys.argv[1])
    fahrenheit = celsius * (9 / 5) + 32
    if fahrenheit.is_integer():
        fahrenheit = int(fahrenheit)
    print(json.dumps({"fahrenheit": fahrenheit}, allow_nan=False, separators=(",", ":")))


if __name__ == "__main__":
    main()
