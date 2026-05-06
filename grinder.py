#!/usr/bin/env python3
"""
Ardinal Grinder — autonomous epoch grinding with multi-wallet parallel execution.
Polls for new epochs, solves riddles via any OpenAI-compatible LLM API,
commits/reveals/inscribes across all configured wallets in parallel.

Requirements:
    - ardi-agent v0.5.13+ (https://github.com/awp-worknet/ardi-skill)
    - awp-wallet v1.5.0+
    - Python 3.9+
    - An OpenAI-compatible LLM API endpoint (OpenRouter, local proxy, etc.)

Usage:
    python3 grinder.py                          # run forever, all wallets from config
    python3 grinder.py --epochs 5               # run N epochs then stop
    python3 grinder.py --dry-run                # solve riddles but don't commit on-chain
    python3 grinder.py --config my_config.json  # use custom config file

Configuration:
    Copy config.example.json → config.json and fill in your details.
    See README.md for full setup guide.
"""

import subprocess
import json
import time
import sys
import os
import argparse
import concurrent.futures
from datetime import datetime, timezone
from pathlib import Path

# ── Config loading ──────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent.resolve()
DEFAULT_CONFIG_PATH = SCRIPT_DIR / "config.json"

def load_config(config_path=None):
    """Load configuration from JSON file."""
    path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    if not path.exists():
        print(f"ERROR: Config file not found: {path}")
        print(f"Copy config.example.json → config.json and fill in your details.")
        sys.exit(1)
    with open(path) as f:
        return json.load(f)

# ── Globals (set after config load) ─────────────────────────────────────────
CONFIG = {}
WALLETS = []
LOG_FILE = ""

TOP_N = 5  # max commits per wallet per epoch (contract limit)
POLL_INTERVAL = 10  # seconds between epoch polls
REVEAL_WAIT = 135  # seconds after commit deadline before revealing
VRF_WAIT = 90  # seconds after reveal before inscribing
VRF_RETRY_WAIT = 60  # seconds between inscribe retries
VRF_MAX_RETRIES = 3

# ── Logging ─────────────────────────────────────────────────────────────────
_log_fh = None

def _get_log_fh():
    global _log_fh
    if _log_fh is None:
        _log_fh = open(LOG_FILE, "a", buffering=1)
    return _log_fh

def log(msg, level="INFO"):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    line = f"[{ts}] [{level}] {msg}"
    print(line, flush=True)
    try:
        _get_log_fh().write(line + "\n")
    except Exception:
        pass

def log_epoch(epoch, msg, level="INFO"):
    log(f"[E{epoch}] {msg}", level)

# ── Shell helpers ───────────────────────────────────────────────────────────
def run_ardi(args, wallet_env=None, timeout=45):
    """Run ardi-agent with optional wallet env override. Returns parsed JSON or None."""
    env = os.environ.copy()
    if wallet_env:
        env.update(wallet_env)
    try:
        result = subprocess.run(
            ["ardi-agent"] + args,
            capture_output=True, text=True, timeout=timeout, env=env,
        )
        output = result.stdout.strip()
        if not output:
            return None
        return json.loads(output)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, Exception) as e:
        log(f"ardi-agent {' '.join(args)} failed: {e}", "ERROR")
        return None

# ── LLM solver ──────────────────────────────────────────────────────────────
def solve_riddles(riddles):
    """Send riddles to LLM, return dict of {wordId: answer}."""
    import urllib.request

    llm = CONFIG["llm"]
    base_url = llm["base_url"].rstrip("/")
    model = llm["model"]
    api_key = llm.get("api_key", "")

    # Build prompt
    riddle_lines = []
    for r in riddles:
        riddle_lines.append(
            f"- wordId={r['wordId']} language={r['language']} rarity={r['rarity']} power={r['power']}\n"
            f"  Riddle: {r['riddle']}"
        )
    riddles_text = "\n".join(riddle_lines)

    prompt = f"""You are solving riddles for the Ardinals competition. Each riddle has exactly ONE correct answer — a single canonical word in the riddle's language.

Rules:
- Answer must be a SINGLE WORD (no phrases, no explanations)
- Answer must be in the riddle's language (en→English, de→German, fr→French, ja→Japanese, ko→Korean, zh→Chinese)
- For Japanese: use the most common form (e.g. hiragana for native words, katakana for loanwords, kanji if standard)
- For Korean: use standard hangul
- For Chinese: use simplified characters
- For German: capitalize nouns as standard

Riddles to solve:
{riddles_text}

Respond ONLY with a JSON object mapping wordId to answer. Example:
{{"1234": "example", "5678": "例え"}}

JSON response:"""

    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1,
        "max_tokens": 300,
    }).encode()

    # Build headers — support various API auth styles
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
        headers["x-api-key"] = api_key  # some proxies need this too
    # Allow extra headers from config
    for k, v in llm.get("extra_headers", {}).items():
        headers[k] = v

    req = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=payload,
        headers=headers,
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
            content = data["choices"][0]["message"]["content"].strip()
            # Extract JSON from response (handle markdown code blocks)
            if "```" in content:
                content = content.split("```")[1]
                if content.startswith("json"):
                    content = content[4:]
                content = content.strip()
            answers = json.loads(content)
            return {str(k): str(v) for k, v in answers.items()}
    except Exception as e:
        log(f"LLM solve failed: {e}", "ERROR")
        return {}

# ── Commit (sequential per wallet, parallel across wallets) ─────────────────
def commit_wallet(wallet, word_id, answer):
    """Commit a single word for a single wallet."""
    result = run_ardi(
        ["commit", "--word-id", str(word_id), "--answer", answer],
        wallet_env=wallet.get("env"),
    )
    if result and result.get("status") == "ok":
        return (wallet["name"], word_id, True, "ok")
    msg = result.get("message", "unknown error") if result else "no response"
    return (wallet["name"], word_id, False, msg)

def commit_single_wallet(wallet, word_answers):
    """Commit all words for a single wallet SEQUENTIALLY (state file race condition fix)."""
    results = []
    for wid, ans in word_answers:
        result = commit_wallet(wallet, wid, ans)
        results.append(result)
        time.sleep(1)
    return results

def commit_all(wallets, word_answers):
    """Commit words — sequential per wallet, parallel across wallets."""
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(wallets)) as pool:
        futures = {}
        for wallet in wallets:
            f = pool.submit(commit_single_wallet, wallet, word_answers)
            futures[f] = wallet["name"]
        for f in concurrent.futures.as_completed(futures):
            results.extend(f.result())
    return results

# ── Reveal (sequential per wallet, parallel across wallets) ─────────────────
def reveal_wallet_word(wallet, epoch, word_id):
    """Reveal a single word for a single wallet."""
    result = run_ardi(
        ["reveal", "--epoch", str(epoch), "--word-id", str(word_id)],
        wallet_env=wallet.get("env"),
    )
    if result and result.get("status") == "ok":
        return (wallet["name"], word_id, True, "ok")
    msg = result.get("message", "unknown error") if result else "no response"
    return (wallet["name"], word_id, False, msg)

def reveal_single_wallet(wallet, epoch, word_ids, committed_map):
    """Reveal all words for a single wallet SEQUENTIALLY (state file race condition fix)."""
    results = []
    for wid in word_ids:
        if (wallet["name"], wid) in committed_map:
            result = reveal_wallet_word(wallet, epoch, wid)
            results.append(result)
            time.sleep(1)
    return results

def reveal_all(wallets, epoch, word_ids, committed_map):
    """Reveal words — sequential per wallet, parallel across wallets."""
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(wallets)) as pool:
        futures = {}
        for wallet in wallets:
            f = pool.submit(reveal_single_wallet, wallet, epoch, word_ids, committed_map)
            futures[f] = wallet["name"]
        for f in concurrent.futures.as_completed(futures):
            results.extend(f.result())
    return results

# ── Inscribe (parallel) ────────────────────────────────────────────────────
def inscribe_wallet_word(wallet, epoch, word_id):
    """Inscribe a single word for a single wallet."""
    result = run_ardi(
        ["inscribe", "--epoch", str(epoch), "--word-id", str(word_id)],
        wallet_env=wallet.get("env"),
    )
    if not result:
        return (wallet["name"], word_id, "error", "no response")
    msg = result.get("message", "")
    if "VRF pending" in msg:
        return (wallet["name"], word_id, "pending", msg)
    elif "Better luck" in msg:
        return (wallet["name"], word_id, "lost", msg)
    elif "winner" in msg.lower() and "not us" not in msg.lower():
        return (wallet["name"], word_id, "WON", msg)
    elif "inscribed" in msg.lower() or "minted" in msg.lower() or "token" in msg.lower():
        return (wallet["name"], word_id, "WON", msg)
    else:
        return (wallet["name"], word_id, "unknown", msg)

def inscribe_all(wallets, epoch, word_ids, committed_map):
    """Inscribe with VRF retry logic."""
    pending = set()
    for wallet in wallets:
        for wid in word_ids:
            if (wallet["name"], wid) in committed_map:
                pending.add((wallet["name"], wid))

    wins = []
    losses = []

    for attempt in range(VRF_MAX_RETRIES + 1):
        if not pending:
            break
        if attempt > 0:
            log(f"  VRF retry {attempt}/{VRF_MAX_RETRIES} for {len(pending)} pending...")
            time.sleep(VRF_RETRY_WAIT)

        results = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(pending) or 1) as pool:
            futures = {}
            for wname, wid in pending:
                wallet = next(w for w in wallets if w["name"] == wname)
                f = pool.submit(inscribe_wallet_word, wallet, epoch, wid)
                futures[f] = (wname, wid)
            for f in concurrent.futures.as_completed(futures):
                results.append(f.result())

        still_pending = set()
        for wname, wid, status, msg in results:
            if status == "WON":
                wins.append((wname, wid, msg))
                log(f"  🎉 {wname} word {wid}: MINTED! {msg}", "WIN")
            elif status == "lost":
                losses.append((wname, wid))
            elif status == "pending":
                still_pending.add((wname, wid))
            else:
                losses.append((wname, wid))
        pending = still_pending

    for wname, wid in pending:
        losses.append((wname, wid))
        log(f"  {wname} word {wid}: VRF never resolved", "WARN")

    return wins, losses

# ── Main epoch loop ─────────────────────────────────────────────────────────
def run_epoch(dry_run=False):
    """Run a single epoch cycle. Returns (epoch_id, wins, total_committed) or None."""

    # 1. Get context (use first wallet to check epoch)
    ctx = run_ardi(["context"], wallet_env=WALLETS[0].get("env"))
    if not ctx or not ctx.get("data") or not ctx["data"].get("riddles"):
        return None

    data = ctx["data"]
    epoch = data["epochId"]
    riddles = data["riddles"]
    commit_deadline = data["commitDeadline"]
    now = int(time.time())
    commit_remaining = commit_deadline - now

    # Need enough time to solve + commit (LLM ~5s + commits ~30s)
    min_window = 15 + (len(WALLETS) * 5)
    if commit_remaining < min_window:
        log_epoch(epoch, f"Commit window too short ({commit_remaining}s < {min_window}s), skipping")
        return None

    log_epoch(epoch, f"STARTED — {len(riddles)} riddles, {commit_remaining}s window, {len(WALLETS)} wallets")

    # 2. Sort by power, pick top N
    riddles_sorted = sorted(riddles, key=lambda x: -x["power"])[:TOP_N]
    log_epoch(epoch, "Top %d: %s" % (
        len(riddles_sorted),
        ", ".join(f"{r['wordId']}({r['rarity'][0]}{r['power']})" for r in riddles_sorted),
    ))

    # 3. Solve via LLM
    t0 = time.time()
    answers = solve_riddles(riddles_sorted)
    solve_time = time.time() - t0
    log_epoch(epoch, f"Solved in {solve_time:.1f}s: {answers}")

    if not answers:
        log_epoch(epoch, "No answers from LLM, skipping", "ERROR")
        return (epoch, [], 0)

    # Build (wordId, answer) pairs
    word_answers = []
    for r in riddles_sorted:
        wid_str = str(r["wordId"])
        if wid_str in answers:
            word_answers.append((r["wordId"], answers[wid_str]))

    if not word_answers:
        log_epoch(epoch, "No matching answers, skipping", "ERROR")
        return (epoch, [], 0)

    if dry_run:
        log_epoch(epoch, f"DRY RUN — would commit: {word_answers}")
        return (epoch, [], 0)

    # 4. Commit all wallets in parallel
    log_epoch(epoch, f"Committing {len(word_answers)} words × {len(WALLETS)} wallets...")
    commit_results = commit_all(WALLETS, word_answers)

    committed_map = set()
    for wname, wid, success, msg in commit_results:
        status_icon = "✅" if success else "❌"
        log_epoch(epoch, f"  {status_icon} {wname} word {wid}: {msg}")
        if success:
            committed_map.add((wname, wid))

    if not committed_map:
        log_epoch(epoch, "No commits succeeded", "ERROR")
        return (epoch, [], 0)

    total_committed = len(committed_map)
    log_epoch(epoch, f"Committed: {total_committed}/{len(word_answers) * len(WALLETS)}")

    # 5. Wait for reveal window
    now = int(time.time())
    wait_reveal = max(0, commit_deadline - now + 5)
    if wait_reveal > 0:
        log_epoch(epoch, f"Waiting {wait_reveal}s for reveal window...")
        time.sleep(wait_reveal)

    # 6. Reveal all in parallel
    word_ids = [wid for wid, _ in word_answers]
    log_epoch(epoch, "Revealing...")
    reveal_results = reveal_all(WALLETS, epoch, word_ids, committed_map)

    revealed = sum(1 for _, _, ok, _ in reveal_results if ok)
    log_epoch(epoch, f"Revealed: {revealed}/{total_committed}")

    # 7. Wait for VRF
    log_epoch(epoch, f"Waiting {VRF_WAIT}s for VRF...")
    time.sleep(VRF_WAIT)

    # 8. Inscribe
    log_epoch(epoch, "Inscribing...")
    wins, losses = inscribe_all(WALLETS, epoch, word_ids, committed_map)

    log_epoch(epoch, f"DONE — {len(wins)} wins, {len(losses)} losses out of {total_committed} commits")
    if wins:
        for wname, wid, msg in wins:
            log_epoch(epoch, f"  🏆 {wname} word {wid}: {msg}", "WIN")

    return (epoch, wins, total_committed)

# ── Entry point ─────────────────────────────────────────────────────────────
def main():
    global CONFIG, WALLETS, LOG_FILE

    parser = argparse.ArgumentParser(
        description="Ardinal Grinder — autonomous epoch grinding with LLM-powered riddle solving",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 grinder.py                          # grind forever
  python3 grinder.py --epochs 10              # grind 10 epochs
  python3 grinder.py --dry-run                # test without committing
  python3 grinder.py --config my_config.json  # custom config
        """,
    )
    parser.add_argument("--config", type=str, default=None, help="Path to config.json (default: ./config.json)")
    parser.add_argument("--epochs", type=int, default=0, help="Run N epochs then stop (0=forever)")
    parser.add_argument("--dry-run", action="store_true", help="Solve riddles but don't commit on-chain")
    args = parser.parse_args()

    # Load config
    CONFIG = load_config(args.config)
    LOG_FILE = str(SCRIPT_DIR / CONFIG.get("log_file", "grinder.log"))

    # Build wallet list
    WALLETS.clear()
    for i, w in enumerate(CONFIG.get("wallets", [{"name": "W1", "session_id": None}])):
        name = w.get("name", f"W{i+1}")
        env = {}
        if w.get("session_id"):
            env["AWP_SESSION_ID"] = w["session_id"]
        WALLETS.append({"name": name, "env": env if env else {}})

    if not WALLETS:
        print("ERROR: No wallets configured. Check config.json.")
        sys.exit(1)

    # Preflight check
    log("=" * 60)
    log("ARDINAL GRINDER")
    log(f"Model: {CONFIG['llm']['model']} @ {CONFIG['llm']['base_url']}")
    log(f"Wallets: {', '.join(w['name'] for w in WALLETS)}")
    log(f"Mode: {'DRY RUN' if args.dry_run else 'LIVE'} | Epochs: {'∞' if args.epochs == 0 else args.epochs}")
    log("=" * 60)

    # Quick preflight
    for wallet in WALLETS:
        pf = run_ardi(["preflight"], wallet_env=wallet.get("env"))
        if pf and pf.get("status") == "ok":
            bal = pf.get("data", {}).get("balance_eth", 0)
            log(f"  ✅ {wallet['name']}: ready (balance: {bal:.4f} ETH)")
        else:
            msg = pf.get("message", "preflight failed") if pf else "no response"
            log(f"  ❌ {wallet['name']}: {msg}", "WARN")

    log("=" * 60)
    log("Grinding started. Ctrl+C to stop.")
    log("=" * 60)

    epochs_done = 0
    total_wins = 0
    total_commits = 0

    try:
        while True:
            result = run_epoch(dry_run=args.dry_run)

            if result is None:
                time.sleep(POLL_INTERVAL)
                continue

            epoch, wins, committed = result
            epochs_done += 1
            total_wins += len(wins)
            total_commits += committed

            log(f"[STATS] Epochs: {epochs_done} | Wins: {total_wins} | Commits: {total_commits} | "
                f"Win rate: {total_wins/max(total_commits,1)*100:.1f}%")

            if args.epochs > 0 and epochs_done >= args.epochs:
                log(f"Target {args.epochs} epochs reached, stopping.")
                break

            time.sleep(POLL_INTERVAL)

    except KeyboardInterrupt:
        log("Interrupted by user")
    finally:
        log("=" * 60)
        log(f"FINAL: {epochs_done} epochs, {total_wins} wins, {total_commits} commits")
        log("=" * 60)

if __name__ == "__main__":
    main()
