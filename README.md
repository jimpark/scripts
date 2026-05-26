# baseconv

Convert a value between **binary**, **decimal**, **octal**, **hex**, and **base64** — any format to any other.

## How it works

The script models every value as an underlying sequence of **raw bytes**. Each
format is just one way of encoding those bytes, so a conversion is always:

> decode the input (`--from`) into bytes → re-encode as the output (`--to`)

| Format   | Meaning                                                        | Example (`hi`)       |
| -------- | -------------------------------------------------------------- | -------------------- |
| `bin`    | Bits, padded to whole bytes (8 bits each)                      | `0110100001101001`   |
| `dec`    | Base-10 of the big-endian integer value of the bytes           | `26729`              |
| `oct`    | Octal of the big-endian integer value of the bytes             | `64151`              |
| `hex`    | Two hex digits per byte, zero-padded to even length            | `6869`               |
| `base64` | Standard RFC 4648 base64                                        | `aGk=`               |

## Usage

```
python baseconv.py --from <FORMAT> --to <FORMAT> [VALUE]
```

- `VALUE` may be passed as an argument, or **omitted to read from stdin**.
- Output is written to **stdout**, followed by a newline.
- Surrounding whitespace and `0b` / `0x` prefixes on the input are ignored.

Run `python baseconv.py --help` for the full reference.

## Examples

```sh
# hex -> base64
python baseconv.py --from hex --to base64 48656c6c6f
# -> SGVsbG8=

# base64 -> hex, reading from stdin
echo SGVsbG8= | python baseconv.py --from base64 --to hex
# -> 48656c6c6f

# decimal -> binary
python baseconv.py --from dec --to bin 65535
# -> 1111111111111111

# hex -> octal
python baseconv.py --from hex --to oct ff
# -> 377
```

## Notes & caveats

- **`dec` and `oct` go through the integer value** of the bytes, so they
  cannot preserve leading zero-bytes (`0x00ff` and `0xff` both read back as
  `255`). `bin`, `hex`, and `base64` are byte-exact and round-trip losslessly.
- **Negative numbers** have no byte representation and are rejected.

## Exit status

| Code | Meaning                                              |
| ---- | ---------------------------------------------------- |
| `0`  | Success                                              |
| `1`  | Invalid input for the chosen `--from` format         |
| `2`  | Usage error (bad or missing arguments)               |

## Requirements

Python 3.6+ (standard library only; no dependencies).
