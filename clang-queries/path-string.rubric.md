# std::filesystem::path string() audit — investigation packet

Each finding below is a call to `.string()` or `.generic_string()` where the
receiver's static type is genuinely `std::filesystem::path` (verified by Clang's
AST, not text matching).

## Your task
For every finding, decide which bucket it belongs to and recommend an action.

1. **Display / logging** — path rendered into a message/log only.
   e.g. `MFLOG_ERROR("can't remove {}", p.string())`.
   Usually fine; flag only if logs must be stable UTF-8 across platforms.
2. **URI construction** — path spliced into a URI/URL.
   e.g. `"file:///" + p.generic_string()`.
   High priority: needs UTF-8 + percent-encoding review.
3. **Passed into file / archive / external API** — value crosses an API boundary.
   e.g. `export_zip_archive(doc, f.generic_string())`.
   Review per-API: does the callee want native bytes or UTF-8?
4. **Test / comparison** — comparing path spellings, often deliberately using
   `generic_string()` for `/`-normalized output. Usually intentional; confirm.

## Encoding hazard (C++23)
`string()` returns the platform-native encoding (UTF-8 on POSIX/macOS, but the
active code page / potentially lossy on Windows); `generic_string()` only
normalizes separators, not encoding. If a site needs guaranteed UTF-8, the fix
is usually to switch to `u8string()` / `generic_u8string()`, which return
`std::u8string` (`char8_t`) and require an explicit `char8_t*`->`char*`
reinterpret at the API boundary.

## For each finding, output
- bucket (1-4)
- whether it's an encoding risk (and why)
- recommended action (leave as-is / switch method / add conversion helper / manual review)
- confidence

---
