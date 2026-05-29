#!/usr/bin/env python3
"""Convert a value between binary, decimal, octal, hex and base64.

The canonical internal representation is a sequence of raw bytes. Every
supported format is treated as one particular *encoding* of those bytes, so a
conversion is always "decode the input into bytes, then re-encode as output".

    bin     bits, grouped/padded to whole bytes (8 bits each)
    hex     two hex digits per byte, zero-padded to even length
    oct     octal of the big-endian integer value of the bytes
    dec     base-10 of the big-endian integer value of the bytes
    base64  standard RFC 4648 base64 of the bytes

Input comes from the positional argument or, if omitted, from stdin.
Output is written to stdout followed by a newline.

Notes / caveats of the raw-bytes model:
  * dec and oct go through the *integer value* of the bytes, so they cannot
    preserve leading zero-bytes (0x00ff and 0xff both read back as 255).
    bin, hex and base64 preserve the exact byte sequence.
  * Negative numbers have no byte representation and are rejected.

Examples:
    baseconv.py --from hex    --to base64 48656c6c6f      ->  SGVsbG8=
    echo SGVsbG8= | baseconv.py --from base64 --to hex    ->  48656c6c6f
    baseconv.py --from dec    --to bin    65535           ->  1111111111111111
    baseconv.py --from hex    --to oct    ff              ->  377

Exit status:
    0   success
    1   invalid input for the chosen --from format
    2   usage error (bad/missing arguments; handled by argparse)
"""
import argparse
import base64
import sys

__version__ = "1.0.0"

FORMATS = ("bin", "dec", "oct", "hex", "base64")


def int_to_bytes(n: int) -> bytes:
    """Minimal big-endian byte representation of a non-negative integer."""
    if n < 0:
        raise ValueError("negative values are not supported")
    if n == 0:
        return b"\x00"
    return n.to_bytes((n.bit_length() + 7) // 8, "big")


def decode(value: str, fmt: str) -> bytes:
    """Parse an input string in the given format into raw bytes."""
    value = value.strip()
    if fmt == "bin":
        bits = "".join(value.split())
        if bits.lower().startswith("0b"):
            bits = bits[2:]
        if not bits:
            return b""
        if any(c not in "01" for c in bits):
            raise ValueError("binary input may contain only 0 and 1")
        bits = bits.zfill((len(bits) + 7) // 8 * 8)  # left-pad to whole bytes
        return int(bits, 2).to_bytes(len(bits) // 8, "big")
    if fmt == "hex":
        h = "".join(value.split())
        if h.lower().startswith("0x"):
            h = h[2:]
        if len(h) % 2:
            h = "0" + h  # left-pad to whole bytes
        return bytes.fromhex(h)
    if fmt == "oct":
        return int_to_bytes(int(value, 8))
    if fmt == "dec":
        return int_to_bytes(int(value, 10))
    if fmt == "base64":
        return base64.b64decode("".join(value.split()), validate=True)
    raise ValueError(f"unknown format: {fmt}")


def encode(data: bytes, fmt: str) -> str:
    """Render raw bytes into the given format."""
    if fmt == "bin":
        return "".join(f"{b:08b}" for b in data)
    if fmt == "hex":
        return data.hex()
    if fmt == "oct":
        return format(int.from_bytes(data, "big"), "o") if data else "0"
    if fmt == "dec":
        return str(int.from_bytes(data, "big")) if data else "0"
    if fmt == "base64":
        return base64.b64encode(data).decode("ascii")
    raise ValueError(f"unknown format: {fmt}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="baseconv.py",
        description="Convert a value between bin, dec, oct, hex and base64. "
                    "Each format is treated as an encoding of an underlying "
                    "byte sequence.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "formats:\n"
            "  bin      bits, padded to whole bytes (e.g. 0110100001101001)\n"
            "  dec      base-10 integer value of the bytes (e.g. 26729)\n"
            "  oct      octal integer value of the bytes (e.g. 064151)\n"
            "  hex      two hex digits per byte (e.g. 6869, '0x' optional)\n"
            "  base64   standard RFC 4648 base64 (e.g. aGk=)\n"
            "\n"
            "input:\n"
            "  Pass VALUE as an argument, or omit it to read from stdin.\n"
            "  Surrounding whitespace and 0b/0x prefixes are ignored.\n"
            "\n"
            "examples:\n"
            "  baseconv.py --from hex --to base64 48656c6c6f\n"
            "  echo SGVsbG8= | baseconv.py --from base64 --to hex\n"
            "  baseconv.py --from dec --to bin 65535\n"
            "\n"
            "note:\n"
            "  dec and oct use the integer value of the bytes and therefore\n"
            "  drop leading zero-bytes; bin, hex and base64 are byte-exact.\n"
        ),
    )
    parser.add_argument("--from", dest="src", required=True, choices=FORMATS,
                        metavar="FORMAT",
                        help="input format: %(choices)s")
    parser.add_argument("--to", dest="dst", required=True, choices=FORMATS,
                        metavar="FORMAT",
                        help="output format: %(choices)s")
    parser.add_argument("value", nargs="?", metavar="VALUE",
                        help="value to convert (default: read from stdin)")
    parser.add_argument("--version", action="version",
                        version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)

    value = args.value if args.value is not None else sys.stdin.read()

    try:
        data = decode(value, args.src)
        result = encode(data, args.dst)
    except (ValueError, base64.binascii.Error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
