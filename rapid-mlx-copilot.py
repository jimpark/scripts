#!/usr/bin/env python3
"""
rapid-mlx-copilot.py

Interactive launcher that:
  1. Reads this machine's RAM and chip.
  2. Lists rapid-mlx model aliases, estimates each one's memory working set,
     and shows ONLY the models this machine can actually run.
  3. Tags recommended models for [general] / [planning] / [coding] use.
  4. Lets you pick one. If it's already serving, it's reused. If a *different*
     model is serving, that server is stopped and the chosen one is started.
     Models that aren't downloaded yet are pulled on demand.
  5. Configures the GitHub Copilot CLI to use the local server and launches it.

The "can this machine run it" filter uses an LLM-Calc-style memory estimate
(weights + KV-cache for the configured context + OS overhead) compared against
this machine's total RAM, so it adapts to whatever machine it runs on.
"""

import os
import re
import sys
import shutil
import signal
import subprocess
import time
import urllib.request

# ─── Tunables ────────────────────────────────────────────────────────────────
# RAM estimation model, borrowed from LLM-Calc (RayFernando1337/LLM-Calc):
#   required_RAM = weights + KV-cache(context) + OS overhead
#     weights      = params_B × (bits / 8)                 GB
#     KV-cache     = context_tokens × KV_CACHE_MB_PER_TOKEN GB-equivalent
#     OS overhead  = OS_OVERHEAD_GB                         GB
# A model is "runnable" if required_RAM ≤ this machine's total RAM. Lower the
# context or raise the OS overhead to be more conservative; raise the context
# to size the KV cache for longer conversations.
KV_CACHE_MB_PER_TOKEN = 0.5       # LLM-Calc's per-token KV-cache estimate
OS_OVERHEAD_GB = 2.0              # RAM reserved for macOS (LLM-Calc default)
CONTEXT_TOKENS = 16384           # context length to size the KV cache for
                                  # (Copilot's prompts are large; ~16k is a
                                  #  realistic working context)

PORT = 8000
PREFILL_STEP_SIZE = 4096          # speeds up the cold prefill of Copilot's prompt
LEAVE_SERVER_RUNNING = True       # keep the server warm after Copilot exits
LOG_FILE = os.path.join(os.getcwd(), "rapid_mlx.log")

# Recommendation preferences, best-first. The launcher tags the first runnable
# alias in each list as the ★ pick for that category, and loosely tags the rest
# by keyword. Edit freely to taste.
CATEGORY_PREFS = {
    "coding":   ["qwen3.6-27b-4bit", "devstral-v2-24b-4bit", "devstral-24b-4bit",
                 "qwen3.5-27b-4bit", "qwen3.5-9b-8bit", "qwen3.5-9b-4bit",
                 "qwen3.5-4b-8bit"],
    "planning": ["qwen3.6-27b-4bit", "qwen3.5-27b-4bit", "glm4.5-air-4bit",
                 "deepseek-r1-32b-4bit", "nemotron-30b-4bit", "qwen3.5-9b-8bit",
                 "qwen3.5-9b-4bit"],
    "general":  ["qwen3.5-9b-8bit", "qwen3.5-9b-4bit", "gemma-4-12b-4bit",
                 "llama-3.1-8b-8bit", "qwen3.5-4b-8bit", "gemma3-12b-4bit"],
}
# Loose keyword tagging for everything else (substring match on the alias).
CATEGORY_KEYWORDS = {
    "coding":   ["coder", "devstral", "codestral", "qwopus", "deepseek-v4", "qwen3.6"],
    "planning": ["-r1", "qwq", "glm4.5-air", "minimax", "kimi", "nemotron",
                 "deepseek-r1"],
    "general":  ["gemma", "llama", "hermes", "phi", "ministral", "mistral",
                 "granite", "qwen3.5-9b", "qwen3.5-4b", "bonsai"],
}

# ─── ANSI helpers ────────────────────────────────────────────────────────────
def c(txt, code):
    return f"\033[{code}m{txt}\033[0m" if sys.stdout.isatty() else txt

BOLD, DIM, GREEN, YELLOW, CYAN, RED = "1", "2", "32", "33", "36", "31"


def die(msg):
    print(c(f"error: {msg}", RED), file=sys.stderr)
    sys.exit(1)


# ─── System info ─────────────────────────────────────────────────────────────
def total_ram_gib():
    try:
        b = int(subprocess.check_output(["sysctl", "-n", "hw.memsize"]).strip())
        return b / (1024 ** 3)
    except Exception:
        try:
            return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / (1024 ** 3)
        except Exception:
            return 0.0


def chip_name():
    try:
        return subprocess.check_output(
            ["sysctl", "-n", "machdep.cpu.brand_string"]).decode().strip()
    except Exception:
        return "unknown"


# ─── rapid-mlx introspection ─────────────────────────────────────────────────
def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True).stdout


def list_model_aliases():
    """Parse `rapid-mlx models` -> [{alias, tools, reasoning}]."""
    out = run(["rapid-mlx", "models"])
    models = []
    for line in out.splitlines():
        s = line.strip()
        if not s or s.startswith(("─", "-", "Alias", "Available")):
            continue
        parts = s.split()
        if len(parts) < 2 or not re.match(r"^[a-z0-9]", parts[0]):
            continue
        alias = parts[0]
        tools = parts[1] if len(parts) > 1 else "—"
        reasoning = parts[2] if len(parts) > 2 else "—"
        models.append({"alias": alias, "tools": tools, "reasoning": reasoning})
    return models


def cached_aliases():
    out = run(["rapid-mlx", "ls"])
    cached = set()
    for line in out.splitlines():
        s = line.strip()
        if not s or s.startswith(("─", "Alias", "Cached", "Total", "Tip")):
            continue
        parts = s.split()
        if parts and re.match(r"^[a-z0-9]", parts[0]):
            cached.add(parts[0])
    return cached


def running_servers():
    """Parse `rapid-mlx ps` -> [{pid, port, model}]."""
    out = run(["rapid-mlx", "ps"])
    servers = []
    for line in out.splitlines():
        s = line.strip()
        if not s or s.startswith(("─", "-", "PID")) or "No rapid-mlx" in s:
            continue
        parts = s.split()
        if len(parts) >= 3 and parts[0].isdigit():
            servers.append({"pid": int(parts[0]), "port": int(parts[1]),
                            "model": parts[2]})
    return servers


# ─── Memory estimation ───────────────────────────────────────────────────────
def parse_size_b(alias):
    """Billions of params from the alias, ignoring the bit-width (…bit)."""
    m = re.search(r"(\d+(?:\.\d+)?)b(?![a-z])", alias.lower())
    return float(m.group(1)) if m else None


def parse_bits(alias):
    a = alias.lower()
    m = re.search(r"(\d+)bit", a)
    if m:
        return int(m.group(1))
    if "mxfp4" in a or "nvfp4" in a:
        return 4
    if "dwq" in a:
        return 4
    if "-ud" in a:
        return 5          # unsloth dynamic ≈ mixed ~4-5 bit
    if "unpacked" in a:
        return 16         # full precision
    return 16             # unknown precision -> assume fp16 (conservative)


def required_ram_gib(size_b, bits):
    """LLM-Calc memory model: weights + KV-cache(context) + OS overhead."""
    weights = size_b * bits / 8.0
    kv_cache = CONTEXT_TOKENS * KV_CACHE_MB_PER_TOKEN / 1000.0
    return weights + kv_cache + OS_OVERHEAD_GB


# ─── Categorisation ──────────────────────────────────────────────────────────
def categorize(models, runnable_aliases):
    """Return {alias: {tags}} and {category: best_alias}."""
    tags = {m["alias"]: set() for m in models}
    best = {}
    for cat, keys in CATEGORY_KEYWORDS.items():
        for m in models:
            a = m["alias"].lower()
            if m["alias"] in runnable_aliases and any(k in a for k in keys):
                tags[m["alias"]].add(cat)
        # ★ best = first preferred alias that is runnable
        for pref in CATEGORY_PREFS[cat]:
            if pref in runnable_aliases:
                best[cat] = pref
                tags[pref].add(cat)
                break
    # coder tool-format implies coding capability
    for m in models:
        if m["tools"] == "qwen3_coder_xml" and m["alias"] in runnable_aliases:
            tags[m["alias"]].add("coding")
    return tags, best


# ─── HTTP helpers ────────────────────────────────────────────────────────────
def server_ready(port):
    try:
        with urllib.request.urlopen(f"http://localhost:{port}/v1/models", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def served_model_id(port):
    try:
        import json
        with urllib.request.urlopen(f"http://localhost:{port}/v1/models", timeout=3) as r:
            data = json.load(r)
        return data["data"][0]["id"]
    except Exception:
        return None


# ─── Server lifecycle ────────────────────────────────────────────────────────
def stop_server(pid):
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    for _ in range(20):
        try:
            os.kill(pid, 0)
            time.sleep(0.5)
        except ProcessLookupError:
            return


def start_server(model, tools_parser):
    cmd = ["rapid-mlx", "serve", model, "--port", str(PORT),
           "--enable-auto-tool-choice", "--tool-call-parser", tools_parser,
           "--no-thinking", "--prefill-step-size", str(PREFILL_STEP_SIZE)]
    print(c(f"\nStarting: {' '.join(cmd)}", DIM))
    log = open(LOG_FILE, "w")
    # start_new_session so the server survives after this script / Copilot exit
    subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT,
                     start_new_session=True)
    print(f"Waiting for the model to load (log: {LOG_FILE}) …")
    for _ in range(600):
        if server_ready(PORT):
            print(c("Server ready.", GREEN))
            return
        time.sleep(1)
    die("server did not become ready in time — check the log.")


def stop_one(srv):
    print(c(f"Stopping {srv['model']} (pid {srv['pid']}, port {srv['port']})…",
            YELLOW))
    stop_server(srv["pid"])
    if any(s["pid"] == srv["pid"] for s in running_servers()):
        print(c(f"  pid {srv['pid']} did not exit — escalating to SIGKILL.", RED))
        try:
            os.kill(srv["pid"], signal.SIGKILL)
        except ProcessLookupError:
            pass
    print(c(f"  stopped {srv['model']}.", GREEN))


def select_servers_to_stop(running, target):
    """Resolve a --stop target to a list of running servers, or prompt for one.

    target may be None (interactive), "all", a model alias, a PID, or a port.
    """
    if target is None:
        print(c("\n  Running rapid-mlx servers:", BOLD))
        for i, s in enumerate(running, 1):
            print(f"  {i:>2}  {s['model']:<26} pid {s['pid']:<7} port {s['port']}")
        print(f"  {'a':>2}  stop ALL of the above")
        choice = input(c("\nPick a server to stop (#, a for all, q to quit): ",
                         BOLD)).strip().lower()
        if choice in ("q", "quit", "exit", ""):
            return []
        if choice in ("a", "all"):
            return running
        if choice.isdigit() and 1 <= int(choice) <= len(running):
            return [running[int(choice) - 1]]
        die("invalid selection.")

    if target.lower() == "all":
        return running
    matches = [s for s in running
               if target == s["model"]
               or (target.isdigit() and int(target) in (s["pid"], s["port"]))]
    if not matches:
        die(f"no running server matches {target!r} "
            f"(by model alias, pid, or port).")
    return matches


def do_stop(target):
    running = running_servers()
    if not running:
        print(c("No rapid-mlx servers are running.", DIM))
        return
    for srv in select_servers_to_stop(running, target):
        stop_one(srv)


def ensure_downloaded(alias, cached):
    if alias in cached:
        return
    print(c(f"\n'{alias}' is not downloaded yet — pulling it now "
            f"(this may be several GB)…", YELLOW))
    rc = subprocess.run(["rapid-mlx", "pull", alias]).returncode
    if rc != 0:
        die(f"failed to download {alias}.")


# ─── Menu ────────────────────────────────────────────────────────────────────
def build_runnable(models, budget):
    runnable, skipped = [], 0
    for m in models:
        size_b = parse_size_b(m["alias"])
        if size_b is None:
            skipped += 1
            continue
        bits = parse_bits(m["alias"])
        ram = required_ram_gib(size_b, bits)
        m = {**m, "size_b": size_b, "bits": bits, "ram": ram}
        if ram <= budget:
            runnable.append(m)
        else:
            skipped += 1
    runnable.sort(key=lambda x: (-x["size_b"], x["bits"]))
    return runnable, skipped


def print_menu(runnable, tags, best, cached, running_models, total, budget):
    cap = "" if abs(budget - total) < 0.5 else c(f" · budget {budget:.0f}G", YELLOW)
    print(c(f"\n  {chip_name()} · {total:.0f} GiB RAM", BOLD) + cap +
          c(f" · sizing for {CONTEXT_TOKENS/1024:.0f}k context "
            f"(+{OS_OVERHEAD_GB:.0f}G OS) — LLM-Calc model", BOLD))
    if best:
        rec = "  ".join(f"{cat}: {c(best[cat], CYAN)}" for cat in
                        ("general", "planning", "coding") if cat in best)
        print(c("  Recommended → ", DIM) + rec)
    print()
    print(c(f"  {'#':>2}  {'model':<26} {'~RAM':>7}  {'disk':<5} tags", DIM))
    for i, m in enumerate(runnable, 1):
        a = m["alias"]
        mark = c("●", GREEN) if a in cached else c("○", DIM)  # downloaded vs pull
        live = c(" (running)", GREEN) if a in running_models else ""
        tag_str = ""
        for cat in ("general", "planning", "coding"):
            if cat in tags[a]:
                star = "★" if best.get(cat) == a else ""
                tag_str += c(f" {star}[{cat}]", YELLOW if star else DIM)
        print(f"  {i:>2}  {a:<26} {m['ram']:>5.1f}G  {mark:<5}{tag_str}{live}")
    print(c("\n  ● downloaded   ○ will download   ★ top pick for that use", DIM))


# ─── CLI parsing ─────────────────────────────────────────────────────────────
USAGE = """\
usage: rapid-mlx-copilot.py [options] [-- copilot args…]

  -s, --serve-only     Start/reuse the chosen model and wait, do not launch
                       Copilot (attach from another terminal).
      --stop [TARGET]  Stop running rapid-mlx server(s) and exit. TARGET is an
                       optional model alias, PID, port, or "all"; with no TARGET
                       you pick one interactively. Stops nothing else.
      --context N      Context length used to size the KV cache in the RAM
                       estimate (accepts e.g. 16384 or 16k). Default: %d.
      --budget X       Cap usable RAM for the "runnable" filter: a number in
                       GB (e.g. 24) or a percentage of total (e.g. 80%%).
                       Default: all of this machine's RAM.
  -h, --help           Show this help.

Any other args are forwarded to `copilot`."""


def parse_tokens(s):
    s = s.strip().lower()
    try:
        return int(float(s[:-1]) * 1024) if s.endswith("k") else int(s)
    except ValueError:
        die(f"invalid --context value: {s!r}")


def parse_budget(s, total):
    s = s.strip().lower()
    try:
        if s.endswith("%"):
            return total * float(s[:-1]) / 100.0
        return float(s.rstrip("gb"))
    except ValueError:
        die(f"invalid --budget value: {s!r}")


# ─── Main ────────────────────────────────────────────────────────────────────
def main():
    global CONTEXT_TOKENS
    total = total_ram_gib()
    budget = total
    serve_only = False
    stop_requested = False
    stop_target = None
    copilot_args = []

    argv = sys.argv[1:]
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--serve-only", "-s"):
            serve_only = True
        elif a == "--stop" or a.startswith("--stop="):
            stop_requested = True
            if "=" in a:
                stop_target = a.split("=", 1)[1]
            elif i + 1 < len(argv) and not argv[i + 1].startswith("-"):
                stop_target = argv[i + 1]
                i += 1
        elif a in ("-h", "--help"):
            print(USAGE % CONTEXT_TOKENS)
            return
        elif a == "--":                       # everything after -- is copilot's
            copilot_args.extend(argv[i + 1:])
            break
        elif a == "--context" or a.startswith("--context="):
            val = a.split("=", 1)[1] if "=" in a else (argv[i + 1] if i + 1 < len(argv) else die("--context requires a value"))
            i += 0 if "=" in a else 1
            CONTEXT_TOKENS = parse_tokens(val)
        elif a == "--budget" or a.startswith("--budget="):
            val = a.split("=", 1)[1] if "=" in a else (argv[i + 1] if i + 1 < len(argv) else die("--budget requires a value"))
            i += 0 if "=" in a else 1
            budget = parse_budget(val, total)
        else:
            copilot_args.append(a)
        i += 1

    needed = ("rapid-mlx",) if (serve_only or stop_requested) \
        else ("rapid-mlx", "copilot")
    for tool in needed:
        if not shutil.which(tool):
            die(f"'{tool}' not found on PATH.")

    if stop_requested:
        do_stop(stop_target)
        return

    models = list_model_aliases()
    if not models:
        die("could not read model list from `rapid-mlx models`.")
    cached = cached_aliases()
    running = running_servers()
    running_models = {s["model"] for s in running}

    runnable, skipped = build_runnable(models, budget)
    if not runnable:
        die("no models fit in this machine's RAM — try lowering CONTEXT_TOKENS "
            "or OS_OVERHEAD_GB, or use a machine with more RAM.")
    runnable_aliases = {m["alias"] for m in runnable}
    tags, best = categorize(models, runnable_aliases)

    print_menu(runnable, tags, best, cached, running_models, total, budget)
    if skipped:
        print(c(f"  ({skipped} models hidden: too large for this machine "
                f"or unknown size)", DIM))

    # Selection
    choice = input(c("\nPick a model number (or q to quit): ", BOLD)).strip()
    if choice.lower() in ("q", "quit", "exit", ""):
        print("Aborted.")
        return
    if not choice.isdigit() or not (1 <= int(choice) <= len(runnable)):
        die("invalid selection.")
    selected = runnable[int(choice) - 1]
    alias = selected["alias"]
    parser = selected["tools"] if selected["tools"] != "—" else "hermes"

    # Reuse / replace running servers
    reuse_port = None
    for s in running:
        if s["model"] == alias and server_ready(s["port"]):
            reuse_port = s["port"]
        else:
            print(c(f"Stopping running server: {s['model']} "
                    f"(pid {s['pid']}, port {s['port']})", YELLOW))
            stop_server(s["pid"])

    global PORT
    if reuse_port is not None:
        PORT = reuse_port
        print(c(f"Reusing already-running '{alias}' on port {PORT}.", GREEN))
    else:
        ensure_downloaded(alias, cached)
        start_server(alias, parser)

    model_id = served_model_id(PORT) or alias
    copilot_env = {
        "COPILOT_PROVIDER_BASE_URL": f"http://localhost:{PORT}/v1",
        "COPILOT_PROVIDER_API_KEY": "dummy-key",
        "COPILOT_PROVIDER_WIRE_API": "completions",
        "COPILOT_MODEL": model_id,
        "COPILOT_OFFLINE": "1",
    }

    # Serve-only: keep the server up and explain how to attach Copilot.
    if serve_only:
        print("-" * 50)
        print(c(f"Serve-only: '{model_id}' is running at "
                f"http://localhost:{PORT}/v1", GREEN))
        print("Attach Copilot from another terminal, either:")
        print(f"  1) run:  {os.path.basename(sys.argv[0])}   "
              f"# auto-reuses this server")
        print("  2) or export these, then run 'copilot':")
        for k, v in copilot_env.items():
            print(f"       export {k}=\"{v}\"")
        srv = next((s for s in running_servers() if s["port"] == PORT), None)
        if srv:
            print(c(f"\nStop the server with:  kill {srv['pid']}", DIM))
        return

    # Configure + launch Copilot
    env = os.environ.copy()
    env.update(copilot_env)
    print(c(f"\nLaunching Copilot with model '{model_id}' …", BOLD))
    print(c("NOTE: the FIRST question may take ~1-2 min while the model prefills "
            "Copilot's large prompt (one-time; cached afterwards).", DIM))
    print("-" * 50)
    subprocess.run(["copilot", *copilot_args], env=env)
    print("-" * 50)

    if LEAVE_SERVER_RUNNING and reuse_port is None:
        print(c(f"Server for '{alias}' left running on port {PORT} "
                f"(warm cache).", GREEN))
        srv = next((s for s in running_servers() if s["port"] == PORT), None)
        if srv:
            print(c(f"Stop it with:  kill {srv['pid']}", DIM))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nAborted.")
