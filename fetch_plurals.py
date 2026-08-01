import json
import urllib.request
import urllib.error
import time
import threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

# --- Configuration ---
OLLAMA_URL = "http://192.168.8.5:11434/api/chat"
MODEL = "gemma4:12b"
MAX_CONCURRENT_REQUESTS = 8
MAX_TEST_WORDS = 16
STATE_FILE = "plural_state.json"
NOUNS_DIR = Path("data/word_lists/german/vocabulary")
GPU_POWER_WATTS = 180

# --- State & Locking ---
state_lock = threading.Lock()

if Path(STATE_FILE).exists():
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        state = json.load(f)
else:
    state = {}

if "stats" not in state:
    state["stats"] = {
        "total_input_tokens": 0,
        "total_output_tokens": 0,
        "total_time_seconds": 0.0,
        "total_energy_kwh": 0.0,
        "total_saved_usd": 0.0
    }

def save_state():
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)

def format_json(metadata, items):
    metadata_json = json.dumps(metadata, indent=2, ensure_ascii=False)
    metadata_json_indented = metadata_json.replace('\n', '\n  ')
    items_json_list = [json.dumps(item, ensure_ascii=False) for item in items]
    if not items_json_list:
        items_str = "[]"
    else:
        items_str = "[\n    " + ",\n    ".join(items_json_list) + "\n  ]"
    return f'{{\n  "metadata": {metadata_json_indented},\n  "items": {items_str}\n}}\n'

def fetch_plural(word):
    # Check if we already fetched this word in a previous run
    with state_lock:
        if word in state:
            return state[word]

    prompt = f"Provide the plural form for the following German noun with its definite article. If the noun has no plural, return the exact string 'uncountable'. Input: '{word}'. Expected JSON format: {{\"plural\": \"die Bücher\"}}"
    payload = {
        "model": MODEL,
        "messages": [
            {
                "role": "system",
                "content": "You are a German language expert. Return ONLY valid raw JSON. Do not use Markdown code blocks. Do not include any text outside the JSON."
            },
            {
                "role": "user",
                "content": prompt
            }
        ],
        "options": {
            "num_gpu": 999,
            "num_ctx": 1024,
            "num_predict": 2048,
            "temperature": 0.1
        },
        "stream": False
    }

    req = urllib.request.Request(OLLAMA_URL, data=json.dumps(payload).encode('utf-8'), headers={'Content-Type': 'application/json'})
    
    max_retries = 3
    for attempt in range(max_retries):
        try:
            with urllib.request.urlopen(req, timeout=600) as response:
                data = json.loads(response.read().decode('utf-8'))
                
                # --- Metrics Math ---
                eval_ns = data.get("eval_duration", 0) + data.get("prompt_eval_duration", 0)
                duration_s = eval_ns / 1_000_000_000
                
                input_tokens = data.get("prompt_eval_count", 0)
                output_tokens = data.get("eval_count", 0)
                tokens = input_tokens + output_tokens
                
                energy_j = GPU_POWER_WATTS * duration_s
                energy_kwh = energy_j / 3_600_000
                
                # API Pricing: $0.10 per 1M input, $0.30 per 1M output
                saved_usd = (input_tokens / 1_000_000 * 0.10) + (output_tokens / 1_000_000 * 0.30)
                
                content = data.get("message", {}).get("content", "").strip()
                
                # Strip markdown if model still includes it by mistake
                if content.startswith("```json"):
                    content = content[7:]
                if content.startswith("```"):
                    content = content[3:]
                if content.endswith("```"):
                    content = content[:-3]
                content = content.strip()
                
                result = json.loads(content)
                plural = result.get("plural", "")
                
                if plural and plural.lower() == "uncountable":
                    new_word = "uncountable"
                elif plural:
                    new_word = f"{word}, {plural}"
                else:
                    new_word = word
                    
                # --- Thread-Safe State Update ---
                with state_lock:
                    state[word] = new_word
                    state["stats"]["total_input_tokens"] += input_tokens
                    state["stats"]["total_output_tokens"] += output_tokens
                    state["stats"]["total_time_seconds"] += duration_s
                    state["stats"]["total_energy_kwh"] += energy_kwh
                    state["stats"]["total_saved_usd"] += saved_usd
                    save_state()
                    
                print(f"✅ Processed: {word} -> {new_word} | ⏱️ {duration_s:.2f}s | 🪙 {tokens} tokens | ⚡ {energy_kwh:.6f} kWh | 💰 Saved: ${saved_usd:.5f}")
                return new_word
                
        except Exception as e:
            raw_out = repr(content) if 'content' in locals() else "[No response, failed early]"
            print(f"⚠️ Attempt {attempt + 1} failed for '{word}': {e} | Raw output: {raw_out}")
            if attempt == max_retries - 1:
                print(f"❌ Giving up on '{word}' after {max_retries} attempts.")
                return None
            time.sleep(2) # brief pause before retry

def process_files():
    # Gather all target files
    target_files = list(NOUNS_DIR.rglob("*/noun/*.json"))
    pending_tasks = []
    
    # Collect up to MAX_TEST_WORDS words to process
    for filepath in target_files:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
            
        items = data.get("items", [])
        for item in items:
            w = item.get("word", "")
            # Only process if it hasn't been pluralized (no comma)
            if w and "," not in w:
                pending_tasks.append((filepath, item))
                if len(pending_tasks) >= MAX_TEST_WORDS:
                    break
        if len(pending_tasks) >= MAX_TEST_WORDS:
            break
            
    print(f"Found {len(pending_tasks)} words to process in test mode.")

    results = []
    # Use ThreadPoolExecutor for concurrent synchronous requests
    with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_REQUESTS) as executor:
        future_to_task = {executor.submit(fetch_plural, item["word"]): (filepath, item) for filepath, item in pending_tasks}
        
        for future in as_completed(future_to_task):
            filepath, item = future_to_task[future]
            try:
                result = future.result()
                results.append((filepath, item, result))
            except Exception as exc:
                print(f"Task generated an exception: {exc}")

    # Now we write the results back to the JSON files
    files_to_update = {}
    for filepath, item, result in results:
        if result:
            files_to_update[filepath] = True

    for filepath in files_to_update:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
        
        items = data.get("items", [])
        regular_items = []
        uncountable_items = []
        
        for item in items:
            w = item.get("word", "")
            if w in state:
                if state[w] == "uncountable":
                    uncountable_items.append(item)
                else:
                    item["word"] = state[w]
                    regular_items.append(item)
            else:
                regular_items.append(item)
                
        # write file back
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(format_json(data.get("metadata", {}), regular_items))
            
        print(f"Updated {filepath.name}")
        
        if uncountable_items:
            uncountable_filepath = filepath.parent / filepath.name.replace("_part", "_uncountable_part")
            if uncountable_filepath.exists():
                with open(uncountable_filepath, "r", encoding="utf-8") as uf:
                    u_data = json.load(uf)
                u_items = u_data.get("items", [])
                
                # Check for duplicates before appending
                existing_words = {u["word"] for u in u_items}
                for item in uncountable_items:
                    if item["word"] not in existing_words:
                        u_items.append(item)
                
                with open(uncountable_filepath, "w", encoding="utf-8") as uf:
                    uf.write(format_json(u_data.get("metadata", {}), u_items))
            else:
                u_metadata = dict(data.get("metadata", {}))
                u_metadata["name"] = u_metadata["name"].replace(" Part", " Uncountable Part")
                with open(uncountable_filepath, "w", encoding="utf-8") as uf:
                    uf.write(format_json(u_metadata, uncountable_items))
            print(f"Created/Updated {uncountable_filepath.name}")
        
    print("\n--- GLOBAL STATS ---")
    print(f"Total GPU Time: {state['stats']['total_time_seconds']:.2f}s")
    print(f"Total Input Tokens: {state['stats']['total_input_tokens']}")
    print(f"Total Output Tokens: {state['stats']['total_output_tokens']}")
    print(f"Total Energy Used: {state['stats']['total_energy_kwh']:.6f} kWh")
    print(f"Total API Savings: ${state['stats']['total_saved_usd']:.5f}")

if __name__ == "__main__":
    process_files()
