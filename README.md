# Ardinal Grinder

Autonomous grinder for [AWP Ardinals](https://ardinals.com). Solves riddles with an LLM, commits/reveals/inscribes across multiple wallets.

## How It Works

Every ~6 min a new epoch opens with 15 riddles. Guess the word correctly → enter a VRF lottery → mint an Ardinal NFT on Base.

This script automates the full cycle:
1. **Poll** for new epochs
2. **Solve** top 5 riddles using any OpenAI-compatible LLM
3. **Commit** answers on-chain (all wallets in parallel)
4. **Reveal** answers after commit window closes
5. **Inscribe** if you win the VRF lottery

**~4-5 epochs/hour**, ~10 lottery tickets per epoch with 2 wallets.

## Requirements

- Python 3.9+ (no pip install needed — stdlib only)
- [ardi-agent v0.5.13+](https://github.com/awp-worknet/ardi-skill)
- [awp-wallet v1.5.0+](https://github.com/awp-worknet/awp-wallet)
- Base ETH for gas (~0.01 ETH lasts hundreds of epochs)
- An OpenAI-compatible LLM API key

## Setup

### 1. Install ardi-agent & awp-wallet

Follow the [ardi-skill setup guide](https://github.com/awp-worknet/ardi-skill). Verify with:

```bash
ardi-agent preflight
```

### 2. Configure

```bash
cp config.example.json config.json
nano config.json
```

**Single wallet:**
```json
{
  "llm": {
    "base_url": "https://openrouter.ai/api/v1",
    "model": "CHOOSE UR MODEL",
    "api_key": "sk-or-v1-YOUR_KEY"
  },
  "wallets": [
    { "name": "W1", "session_id": null }
  ]
}
```

**Multiple wallets** (more wallets = more lottery tickets):
```json
{
  "llm": {
    "base_url": "https://openrouter.ai/api/v1",
    "model": "CHOOSE UR MODEL",
    "api_key": "sk-or-v1-YOUR_KEY"
  },
  "wallets": [
    { "name": "W1", "session_id": null },
    { "name": "W2", "session_id": "ardi-2" },
    { "name": "W3", "session_id": "ardi-3" }
  ]
}
```

To create additional wallets:
```bash
AWP_SESSION_ID=ardi-2 awp-wallet create
AWP_SESSION_ID=ardi-2 ardi-agent preflight   # register + check balance
# Fund the address shown in preflight output with Base ETH
```

### 3. Run

```bash
# Test first (solves riddles, doesn't commit on-chain)
python3 grinder.py --dry-run

# Run for real
python3 grinder.py

# Run N epochs then stop
python3 grinder.py --epochs 10

# Run in background (recommended)
nohup python3 -u grinder.py >> grinder.log 2>&1 &
```

### 4. Monitor

```bash
tail -f grinder.log          # live log
grep "STATS" grinder.log     # summary stats
grep "WIN" grinder.log       # check wins
grep "DONE" grinder.log      # per-epoch results
```

## LLM Options

Any OpenAI-compatible `/v1/chat/completions` endpoint works:

| Provider | Config |
|----------|--------|
| **OpenRouter** | `"base_url": "https://openrouter.ai/api/v1"` |
| **OpenAI** | `"base_url": "https://api.openai.com/v1"` |
| **Ollama (local)** | `"base_url": "http://localhost:11434/v1"`, `"api_key": ""` |

Recommended models: `claude-sonnet-4`, `gpt-4o`, `deepseek-v3`. The model needs decent multilingual capability (riddles are in EN/DE/FR/JA/KO/ZH).

If your API needs extra headers, add them:
```json
{
  "llm": {
    "base_url": "https://your-proxy.com/v1",
    "model": "claude-sonnet-4",
    "api_key": "your-key",
    "extra_headers": { "x-custom-header": "value" }
  }
}
```

## Odds

With ~50-100 candidates per word (current competition):

| Wallets | Tickets/epoch | Win chance/epoch |
|---------|--------------|-----------------|
| 1 | 5 | ~5-10% |
| 2 | 10 | ~10-19% |
| 3 | 15 | ~14-26% |
| 5 | 25 | ~22-40% |

All agents answer correctly — winning is pure VRF luck. More tickets = better odds.

## Troubleshooting

| Issue | Cause | Fix |
|-------|-------|-----|
| "No commit-able epoch" | Between epochs | Normal, script polls every 10s |
| "Already committed" | Another process committed this epoch | Stop other grinder/daemon |
| "LLM solve failed" | Bad API key or endpoint | Test with `curl` (see below) |
| "Reveal reverted" | Wrong answer (rare) | Normal, LLM occasionally misses |
| Low gas warning | ardi-agent warns at 0.003 ETH | Ignore until < 0.001 ETH |

Test your LLM endpoint:
```bash
curl YOUR_BASE_URL/chat/completions \
  -H "Authorization: Bearer YOUR_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"YOUR_MODEL","messages":[{"role":"user","content":"Say hi"}]}'
```

## License

MIT
