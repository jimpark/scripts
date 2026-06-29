# std::filesystem::path from narrow string — investigation packet

Each finding below constructs a `std::filesystem::path` from a narrow `char`
string — `std::string` or `std::string_view` — verified by Clang's AST (the
source argument's static type is a `char` specialization of `basic_string` /
`basic_string_view`, not text matching). This includes both explicit
construction (`fs::path p(s)`, `fs::path p = s`) and *implicit* construction at
a call boundary (`void f(const fs::path&); f(s);`).

## The hazard
`path`'s `char` constructor interprets the bytes in the platform's *native
narrow* encoding. On POSIX/macOS that is UTF-8, so it round-trips; on **Windows**
it is the active code page (ACP), so a UTF-8 `std::string` becomes a mojibake or
lossy path and may fail to open Unicode filenames. The fix is to convert the
`std::string` to `std::u8string` (via the project's helper) and construct the
`path` from that, so the bytes are always treated as UTF-8.

## Your task
For every finding, decide which bucket it belongs to and recommend an action.

1. **UTF-8 string → path** — the source `std::string` holds UTF-8 (most app
   data: JSON, config, network, our own APIs). **High priority:** route through
   the u8string helper.
2. **Already-native / OS-handle bytes → path** — the source came straight from
   an OS API in native encoding (rare). May be correct as-is; confirm origin.
3. **ASCII-only / constant** — the string is provably ASCII (a fixed name, a
   test fixture). Low risk; ACP and UTF-8 agree on ASCII. Confirm it cannot
   carry non-ASCII at runtime.
4. **Ambiguous origin** — encoding of the source can't be determined locally.
   Needs manual review of where the string is produced.

## For each finding, output
- bucket (1-4)
- the source expression and, if determinable, where its bytes originate
- recommended action (convert via u8string helper / leave as-is / manual review)
- confidence

---
