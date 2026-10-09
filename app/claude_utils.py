"""Calls to the Claude API with caching, a daily budget, and cost logging.

Every function returns None (never raises) when a call fails, so one bad
document cannot crash the daily run.
"""
import hashlib
import json
import logging
from datetime import date, datetime

from app.config import get_anthropic_key, load_config
from app.db import run_query, run_write

log = logging.getLogger(__name__)

# Dollars per million tokens: (input, output). MUST be checked against the current
# Anthropic pricing page. Last checked 2026-10-06 in the Claude API docs.
# Haiku 5.5 costs more ($0.50 / $2.50) for prompts over 100K tokens.
PRICES = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
}
HAIKU_LONG_PROMPT_PRICES = (0.50, 2.50)
HAIKU_LONG_PROMPT_TOKENS = 100000

JSON_RULES = ("Respond with a single valid JSON object and nothing else. "
              "If information is not in the text, use null. Do not guess.")


def get_client():
    """Create the Anthropic client from the API key; None if no key is configured."""
    import anthropic
    key = get_anthropic_key()
    if key is None:
        log.error("No Anthropic API key found (set BIOTECH_ANTHROPIC_API_KEY)")
        return None
    return anthropic.Anthropic(api_key=key)


def estimate_cost(model, input_tokens, output_tokens):
    """Estimated dollars for one call. Unknown models are priced like Sonnet to be safe."""
    input_price, output_price = PRICES.get(model, PRICES["claude-sonnet-5-5"])
    if model == "claude-haiku-5-5" and input_tokens > HAIKU_LONG_PROMPT_TOKENS:
        input_price, output_price = HAIKU_LONG_PROMPT_PRICES
    return (input_tokens * input_price + output_tokens * output_price) / 1_000_000


def make_cache_key(task_name, model, system_prompt, user_text):
    """SHA-256 hash that identifies one exact request."""
    text = "\x1f".join([task_name, model, system_prompt, user_text])
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def get_cached_response(cache_key):
    """Return the stored response string for a cache key, or None."""
    rows = run_query("SELECT response_json FROM claude_cache WHERE cache_key = ?", (cache_key,))
    return rows[0]["response_json"] if rows else None


def save_cached_response(cache_key, response_text):
    """Store a successful response so the same input is never processed twice."""
    run_write("INSERT OR REPLACE INTO claude_cache (cache_key, response_json, created_at) "
              "VALUES (?, ?, ?)", (cache_key, response_text, datetime.now().isoformat(timespec="seconds")))


def get_todays_spend():
    """Total estimated Claude dollars spent today."""
    rows = run_query("SELECT COALESCE(SUM(est_cost_usd), 0) AS total FROM claude_usage "
                     "WHERE run_date = ?", (date.today().isoformat(),))
    return rows[0]["total"]


def is_over_budget():
    """True (with a printed warning) if today's spend has reached the daily budget."""
    budget = load_config()["claude"]["daily_budget_usd"]
    spent = get_todays_spend()
    if spent >= budget:
        print(f"WARNING: Claude budget reached (${spent:.2f} of ${budget:.2f}). Skipping call.")
        return True
    return False


def log_usage(task_name, model, response):
    """Record token counts and estimated cost for one API response."""
    input_tokens = response.usage.input_tokens
    output_tokens = response.usage.output_tokens
    run_write("INSERT INTO claude_usage (run_date, task_name, model, input_tokens, output_tokens, "
              "est_cost_usd) VALUES (?, ?, ?, ?, ?, ?)",
              (date.today().isoformat(), task_name, model, input_tokens, output_tokens,
               estimate_cost(model, input_tokens, output_tokens)))


def send_request(task_name, model, system_prompt, messages, max_tokens, effort):
    """One API call. Returns the response text, or None on refusal/truncation/error."""
    client = get_client()
    if client is None or is_over_budget():
        return None
    try:
        response = client.messages.create(
            model=model, max_tokens=max_tokens, system=system_prompt, messages=messages,
            output_config={"effort": effort})  # effort controls how much the model "thinks"
    except Exception as error:
        log.error("%s: Claude call failed (%s)", task_name, error)
        return None
    log_usage(task_name, model, response)
    if response.stop_reason in ("refusal", "max_tokens"):
        log.error("%s: stopped with %s", task_name, response.stop_reason)
        return None
    return "".join(block.text for block in response.content if block.type == "text")


def parse_json_text(text):
    """Parse JSON from model output, tolerating ```json fences. Returns None if invalid."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
    try:
        return json.loads(cleaned)
    except ValueError:
        return None


def call_claude_json(task_name, model, system_prompt, user_text, max_tokens=None, effort="low"):
    """Send text to Claude, ask for JSON only, return a Python object (or None on failure).

    Checks the cache first, logs usage and cost, and refuses to call past the daily budget.
    """
    if max_tokens is None:
        max_tokens = load_config()["claude"]["max_tokens_default"]
    full_system = system_prompt + "\n\n" + JSON_RULES
    cache_key = make_cache_key(task_name, model, full_system, user_text)
    cached = get_cached_response(cache_key)
    if cached is not None:
        return json.loads(cached)

    messages = [{"role": "user", "content": user_text}]
    text = send_request(task_name, model, full_system, messages, max_tokens, effort)
    if text is None:
        return None
    result = parse_json_text(text)
    if result is None:
        # One retry: show the bad answer and ask again for JSON only.
        messages += [{"role": "assistant", "content": text},
                     {"role": "user", "content": "That was not valid JSON. Reply with valid JSON only."}]
        text = send_request(task_name, model, full_system, messages, max_tokens, effort)
        result = parse_json_text(text) if text else None
    if result is None:
        log.error("%s: could not get valid JSON from Claude", task_name)
        return None
    save_cached_response(cache_key, json.dumps(result))
    return result


def call_claude_text(task_name, model, system_prompt, user_text, max_tokens=None, effort="medium"):
    """Like call_claude_json but returns plain markdown text (used for memos)."""
    if max_tokens is None:
        max_tokens = load_config()["claude"]["max_tokens_default"]
    cache_key = make_cache_key(task_name, model, system_prompt, user_text)
    cached = get_cached_response(cache_key)
    if cached is not None:
        return json.loads(cached)["text"]
    messages = [{"role": "user", "content": user_text}]
    text = send_request(task_name, model, system_prompt, messages, max_tokens, effort)
    if text is None:
        return None
    save_cached_response(cache_key, json.dumps({"text": text}))
    return text


if __name__ == "__main__":
    print(f"Claude spend today: ${get_todays_spend():.4f}")
    print(f"API key found: {get_anthropic_key() is not None}")
