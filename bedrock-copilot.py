#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "litellm[proxy]",
# ]
# ///
"""bedrock-copilot.py

Launch the GitHub Copilot CLI against a model on AWS Bedrock.

It discovers the Bedrock text models your AWS credentials can reach, lets you
pick one and a reasoning-effort level, stands up a local LiteLLM proxy that
presents that model as an OpenAI-compatible endpoint, points the Copilot CLI's
"bring your own key" (BYOK) settings at the proxy, and tears the proxy down
again when Copilot exits.

  Copilot CLI  ──BYOK──▶  LiteLLM proxy (localhost)  ──boto3──▶  AWS Bedrock

Requires on PATH (the script checks these and errors if any are missing):
  uv        runs this script and installs litellm — https://docs.astral.sh/uv
            (`brew install uv`); only if you launch it with `uv run` / the
            shebang rather than a Python that already has litellm.
  litellm   the proxy. Auto-installed when run via uv; otherwise
            `pip install 'litellm[proxy]'`.
  copilot   GitHub Copilot CLI — `brew install copilot-cli`
            (https://github.com/github/copilot-cli). NOT `gh copilot`.
  aws       AWS CLI v2 — `brew install awscli`. Needed for credential checks,
            model discovery, and the access check; not required if you pass
            both --model and --skip-validation.
  op        1Password CLI — `brew install 1password-cli`. Only needed when you
            use the --op-* flags.

Credentials are never hardcoded. They are resolved, in order, from:
  1. an AWS profile / SSO         (--profile, or $AWS_PROFILE — recommended)
  2. 1Password                    (--op-key-ref / --op-secret-ref secret refs)
  3. ambient environment          ($AWS_ACCESS_KEY_ID / $AWS_SECRET_ACCESS_KEY)
  4. an interactive hidden prompt (getpass)
Whatever the source, boto3 inside LiteLLM does the actual Bedrock auth, so SSO
and short-lived STS credentials work too.

Model + effort:
  Copilot's BYOK is single-model: the model is fixed for the session (relaunch
  to change it), but the effort level CAN be changed live inside Copilot. Pass
  --model / --effort to skip the menus, or run with neither to pick
  interactively. Effort is also baked into the proxy config as `reasoning_effort`
  so Bedrock honours it regardless of what Copilot forwards.

  List the models yourself with:
    aws bedrock list-foundation-models --by-output-modality TEXT \\
        --by-inference-type ON_DEMAND --region <REGION>

Examples:
    bedrock-copilot.py --profile dev
    bedrock-copilot.py --profile dev --model bedrock/anthropic.claude-3-5-sonnet-20241022-v2:0 --effort high
    bedrock-copilot.py --op-key-ref "op://Private/AWS/access key id" \\
                       --op-secret-ref "op://Private/AWS/secret access key"
    bedrock-copilot.py -- --help          # forward args to copilot

Exit status:
    0   Copilot ran (its own exit code is not propagated)
    1   an error (missing tool, bad creds, no models, proxy failed to start)
    2   usage error (handled by argparse)
    130 interrupted (Ctrl+C)
"""
import argparse
import getpass
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request

IS_WINDOWS = os.name == "nt"
DEFAULT_REGION = "us-east-1"
DEFAULT_PORT = 4000
EFFORT_LEVELS = ("none", "low", "medium", "high", "xhigh", "max")
DEFAULT_EFFORT = "medium"
# Copilot's effort levels are richer than LiteLLM's reasoning_effort, which only
# understands low/medium/high (and "none" => no extended thinking). Collapse the
# extremes so Bedrock still gets a valid value.
EFFORT_TO_REASONING = {
    "none": None, "low": "low", "medium": "medium",
    "high": "high", "xhigh": "high", "max": "high",
}


# ─── ANSI helpers ────────────────────────────────────────────────────────────
def c(txt, code):
    return f"\033[{code}m{txt}\033[0m" if sys.stdout.isatty() else txt


BOLD, DIM, GREEN, YELLOW, CYAN, RED = "1", "2", "32", "33", "36", "31"


def die(msg):
    print(c(f"error: {msg}", RED), file=sys.stderr)
    sys.exit(1)


# ─── Prerequisites ───────────────────────────────────────────────────────────
# Tool -> how to install it, surfaced both in --help and in the missing-tool error.
INSTALL_HINTS = {
    "copilot": "GitHub Copilot CLI — `brew install copilot-cli` "
               "(https://github.com/github/copilot-cli). Not `gh copilot`.",
    "litellm": "LiteLLM proxy — auto-installed when run via `uv run`; "
               "otherwise `pip install 'litellm[proxy]'`.",
    "aws": "AWS CLI v2 — `brew install awscli` (https://aws.amazon.com/cli/).",
    "op": "1Password CLI — `brew install 1password-cli`.",
}


def require_tools(args):
    """Error out (listing every gap) if a tool this run will use is missing.

    copilot + litellm are always needed. aws is needed unless the run avoids it
    entirely (--model given AND --skip-validation). op is only needed for --op-*.
    """
    needed = ["copilot", "litellm"]
    if not (args.model and args.skip_validation):
        needed.append("aws")
    if args.op_key_ref or args.op_secret_ref or args.op_token_ref:
        needed.append("op")

    missing = [t for t in needed if not shutil.which(t)]
    if missing:
        lines = [f"required tool(s) not found on PATH: {', '.join(missing)}", ""]
        lines += [f"  {t}: {INSTALL_HINTS[t]}" for t in missing]
        die("\n".join(lines))


# ─── Credentials ─────────────────────────────────────────────────────────────
def op_read(ref):
    """Read a single secret from 1Password via `op read <secret-reference>`."""
    if not shutil.which("op"):
        die("--op-* given but the 1Password CLI ('op') is not on PATH.")
    try:
        out = subprocess.run(["op", "read", ref], capture_output=True, text=True,
                             check=True)
        return out.stdout.strip()
    except subprocess.CalledProcessError as exc:
        die(f"`op read {ref}` failed: {exc.stderr.strip()}")


def resolve_credentials(args):
    """Return an env overlay for AWS auth, using the first available source.

    Precedence: AWS profile > 1Password > ambient env vars > hidden prompt. When
    a profile is used we set nothing but AWS_PROFILE and let boto3's credential
    chain (incl. SSO) do the rest, so no secret is ever materialised here.
    """
    env = {"AWS_REGION": args.region, "AWS_DEFAULT_REGION": args.region}

    profile = args.profile or os.environ.get("AWS_PROFILE")
    if profile:
        env["AWS_PROFILE"] = profile
        print(c(f"Using AWS profile '{profile}' (boto3 credential chain).", DIM))
        return env

    if args.op_key_ref or args.op_secret_ref:
        if not (args.op_key_ref and args.op_secret_ref):
            die("--op-key-ref and --op-secret-ref must be given together.")
        env["AWS_ACCESS_KEY_ID"] = op_read(args.op_key_ref)
        env["AWS_SECRET_ACCESS_KEY"] = op_read(args.op_secret_ref)
        if args.op_token_ref:
            env["AWS_SESSION_TOKEN"] = op_read(args.op_token_ref)
        print(c("Loaded AWS credentials from 1Password.", DIM))
        return env

    if os.environ.get("AWS_ACCESS_KEY_ID") and os.environ.get("AWS_SECRET_ACCESS_KEY"):
        print(c("Using AWS credentials from the environment.", DIM))
        return env  # already in os.environ; inherited by children

    print(c("No AWS profile or credentials found — enter them "
            "(input is hidden):", YELLOW))
    env["AWS_ACCESS_KEY_ID"] = getpass.getpass("  AWS_ACCESS_KEY_ID: ").strip()
    env["AWS_SECRET_ACCESS_KEY"] = getpass.getpass("  AWS_SECRET_ACCESS_KEY: ").strip()
    token = getpass.getpass("  AWS_SESSION_TOKEN (optional, Enter to skip): ").strip()
    if token:
        env["AWS_SESSION_TOKEN"] = token
    if not (env["AWS_ACCESS_KEY_ID"] and env["AWS_SECRET_ACCESS_KEY"]):
        die("an access key id and secret access key are required.")
    return env


def aws(env, *args, check=True):
    """Run an `aws` subcommand with the resolved creds; return CompletedProcess."""
    return subprocess.run(["aws", *args], env={**os.environ, **env},
                         capture_output=True, text=True, check=check)


def validate_credentials(env):
    """Prove the resolved credentials work with `aws sts get-caller-identity`."""
    if not shutil.which("aws"):
        print(c("Skipping credential check: AWS CLI not on PATH.", DIM))
        return
    print(c("Validating AWS credentials …", DIM))
    proc = aws(env, "sts", "get-caller-identity", check=False)
    if proc.returncode != 0:
        die("AWS credential check failed (sts get-caller-identity):\n"
            f"{proc.stderr.strip()}")
    print(c("Credentials OK.", GREEN))


# ─── Model discovery + selection ─────────────────────────────────────────────
def discover_models(env, region):
    """List on-demand, streaming-capable TEXT foundation models in `region`.

    Returns [{id, provider, name}] sorted by provider then id. Copilot needs
    streaming + tool calling; we can filter on streaming from the API but tool
    support isn't reported, so unusable models may still appear.
    """
    proc = aws(env, "bedrock", "list-foundation-models",
               "--region", region,
               "--by-output-modality", "TEXT",
               "--by-inference-type", "ON_DEMAND",
               "--output", "json", check=False)
    if proc.returncode != 0:
        die("could not list Bedrock models (needs 'bedrock:ListFoundationModels'):\n"
            f"{proc.stderr.strip()}")
    models = []
    for m in json.loads(proc.stdout).get("modelSummaries", []):
        if m.get("modelLifecycle", {}).get("status") != "ACTIVE":
            continue
        if m.get("responseStreamingSupported") is False:
            continue
        models.append({"id": m["modelId"],
                       "provider": m.get("providerName", "?"),
                       "name": m.get("modelName", m["modelId"])})
    models.sort(key=lambda m: (m["provider"].lower(), m["id"]))
    return models


def choose_model(models):
    """Show a numbered menu and return the selected bare Bedrock model id."""
    print(c(f"\n  Bedrock text models reachable in this region ({len(models)}):",
            BOLD))
    print(c("  (listing means the model exists — access is verified next)", DIM))
    print(c(f"\n  {'#':>3}  {'provider':<14} model id", DIM))
    for i, m in enumerate(models, 1):
        provider = f"{m['provider'][:14]:<14}"
        print(f"  {i:>3}  {c(provider, CYAN)} {m['id']}")
    choice = input(c("\nPick a model number (or q to quit): ", BOLD)).strip()
    if choice.lower() in ("q", "quit", "exit", ""):
        print("Aborted.")
        sys.exit(0)
    if not choice.isdigit() or not (1 <= int(choice) <= len(models)):
        die("invalid selection.")
    return models[int(choice) - 1]["id"]


def choose_effort():
    """Prompt for a reasoning-effort level."""
    print(c(f"\n  Effort levels: {', '.join(EFFORT_LEVELS)}", DIM))
    val = input(c(f"Reasoning effort [{DEFAULT_EFFORT}]: ", BOLD)).strip().lower()
    val = val or DEFAULT_EFFORT
    if val not in EFFORT_LEVELS:
        die(f"effort must be one of: {', '.join(EFFORT_LEVELS)}.")
    return val


def verify_access(env, region, model_id):
    """Prove the model is actually invokable with a tiny Converse call.

    Converse uses one message schema across providers (Anthropic, Llama,
    Mistral, …), so this works regardless of the model family — unlike
    invoke-model, whose body is provider-specific.
    """
    print(c(f"Verifying access to {model_id} …", DIM))
    messages = json.dumps([{"role": "user", "content": [{"text": "hi"}]}])
    inference = json.dumps({"maxTokens": 8})
    proc = aws(env, "bedrock-runtime", "converse",
               "--region", region, "--model-id", model_id,
               "--messages", messages, "--inference-config", inference,
               "--output", "json", check=False)
    if proc.returncode != 0:
        die(f"Bedrock access check failed for '{model_id}'. Ensure model access "
            "is granted in this region (incl. any Anthropic first-time-use form) "
            f"and your IAM principal has 'bedrock:InvokeModel':\n{proc.stderr.strip()}")
    print(c("Access OK.", GREEN))


# ─── Proxy lifecycle ─────────────────────────────────────────────────────────
def write_proxy_config(model_id, region, reasoning_effort):
    """Write a minimal LiteLLM config pinning the model (+ effort). Returns path."""
    params = {"model": f"bedrock/{model_id}", "aws_region_name": region}
    if reasoning_effort:
        params["reasoning_effort"] = reasoning_effort
    # Hand-rolled YAML to avoid a PyYAML dependency; values are simple scalars.
    lines = ["model_list:", f"  - model_name: {model_id}", "    litellm_params:"]
    lines += [f"      {k}: {v}" for k, v in params.items()]
    fd, path = tempfile.mkstemp(prefix="bedrock-copilot-", suffix=".yaml")
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(lines) + "\n")
    return path


def proxy_ready(port):
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/health/liveliness", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def start_proxy(config_path, port, env, log_path):
    cmd = ["litellm", "--config", config_path, "--port", str(port)]
    print(c(f"Starting LiteLLM proxy on port {port} …", DIM))
    log = open(log_path, "w")
    # Give litellm its own process group so we can tear down the proxy AND the
    # workers it forks in one shot on cleanup (see stop_proxy).
    group_kwargs = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
                    if IS_WINDOWS else {"start_new_session": True})
    proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT,
                           env={**os.environ, **env}, **group_kwargs)
    print(f"Waiting for the proxy to come up (log: {log_path}) …")
    for _ in range(60):
        if proc.poll() is not None:
            die(f"LiteLLM exited early (code {proc.returncode}). See {log_path}.")
        if proxy_ready(port):
            print(c("Proxy ready.", GREEN))
            return proc
        time.sleep(1)
    stop_proxy(proc)
    die(f"proxy did not become ready in 60s — check {log_path}.")


def stop_proxy(proc):
    if proc.poll() is not None:
        return
    print(c("Shutting down LiteLLM proxy …", DIM))

    def signal_group(force):
        # POSIX: signal the whole group; Windows has no killpg (or SIGKILL), so
        # hit the process directly. SIGKILL is referenced only on POSIX.
        if IS_WINDOWS:
            proc.kill() if force else proc.terminate()
        else:
            os.killpg(os.getpgid(proc.pid),
                      signal.SIGKILL if force else signal.SIGTERM)

    try:
        signal_group(force=False)
        proc.wait(timeout=10)
    except (ProcessLookupError, PermissionError):
        pass
    except subprocess.TimeoutExpired:
        try:
            signal_group(force=True)
        except (ProcessLookupError, PermissionError):
            pass


# ─── CLI ─────────────────────────────────────────────────────────────────────
def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="bedrock-copilot.py",
        description="Launch the GitHub Copilot CLI against a model on AWS Bedrock.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Requires on PATH (checked at startup): " +
               ", ".join(INSTALL_HINTS) + ".\n"
               "  " + "\n  ".join(f"{t}: {h}" for t, h in INSTALL_HINTS.items()) +
               "\n\nAnything after `--` is forwarded to `copilot`.",
    )
    parser.add_argument("--model",
                        help="Bedrock model id to use, skipping the menu. Bare id "
                             "(e.g. anthropic.claude-3-5-sonnet-20241022-v2:0) or "
                             "'bedrock/<id>'.")
    parser.add_argument("--effort", choices=EFFORT_LEVELS,
                        help="Reasoning effort, skipping the prompt.")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", DEFAULT_REGION),
                        help=f"AWS region (default: $AWS_REGION or {DEFAULT_REGION})")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"Local proxy port (default: {DEFAULT_PORT})")
    parser.add_argument("--provider-type", default="openai",
                        help="COPILOT_PROVIDER_TYPE (default: openai — matches the "
                             "OpenAI-compatible LiteLLM proxy).")
    parser.add_argument("--profile", help="AWS profile to use (else $AWS_PROFILE).")
    parser.add_argument("--op-key-ref",
                        help="1Password secret reference for the access key id.")
    parser.add_argument("--op-secret-ref",
                        help="1Password secret reference for the secret access key.")
    parser.add_argument("--op-token-ref",
                        help="1Password secret reference for an optional session token.")
    parser.add_argument("--skip-validation", action="store_true",
                        help="Skip the sts + Bedrock access checks.")
    parser.add_argument("copilot_args", nargs="*",
                        help="Arguments forwarded to `copilot` (use `--` first).")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    require_tools(args)

    env = resolve_credentials(args)
    if not args.skip_validation:
        validate_credentials(env)

    # Pick the model: explicit flag, else discover + menu.
    if args.model:
        model_id = args.model[len("bedrock/"):] if args.model.startswith("bedrock/") \
            else args.model
    else:
        models = discover_models(env, args.region)
        if not models:
            die("no on-demand text models found in this region. Newer Claude "
                "models are inference-profile only; pass one explicitly with "
                "--model, or upgrade the AWS CLI for `list-inference-profiles`.")
        model_id = choose_model(models)

    effort = args.effort or choose_effort()
    reasoning = EFFORT_TO_REASONING[effort]

    if not args.skip_validation:
        verify_access(env, args.region, model_id)

    config_path = write_proxy_config(model_id, args.region, reasoning)
    log_fd, log_path = tempfile.mkstemp(prefix="bedrock-copilot-", suffix=".log")
    os.close(log_fd)
    proc = start_proxy(config_path, args.port, env, log_path)

    # Point Copilot's BYOK env at the local proxy. The proxy exposes the model
    # under the exact id we set as model_name, so COPILOT_MODEL matches.
    copilot_env = {
        **os.environ,
        "COPILOT_PROVIDER_BASE_URL": f"http://127.0.0.1:{args.port}/v1",
        "COPILOT_PROVIDER_TYPE": args.provider_type,
        "COPILOT_PROVIDER_API_KEY": "dummy-key",  # proxy needs no real key here
        "COPILOT_MODEL": model_id,
    }

    print(c(f"\nLaunching Copilot · model '{model_id}' · effort '{effort}' "
            "(change effort live in Copilot; relaunch to change model).", BOLD))
    print("-" * 50)
    try:
        subprocess.run(["copilot", "--effort", effort, *args.copilot_args],
                       env=copilot_env)
    finally:
        print("-" * 50)
        stop_proxy(proc)
        for path in (config_path, log_path):
            try:
                os.remove(path)
            except OSError:
                pass
        print(c("Done.", GREEN))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nAborted.")
        raise SystemExit(130)
