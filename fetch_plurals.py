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
MAX_TEST_FILES = 1  # Number of files to process before stopping (set to 99999 for full run)
MAX_TEST_WORDS = 16 # Number of words to process per file (set to 0 for production to process all 64 words)
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
    
if "completed_files" not in state:
    state["completed_files"] = []

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

def fetch_plural(word, definition):
    prompt = f"Provide the plural form for the following German noun with its definite article. If the noun has no plural, return the exact string 'uncountable'.\nWord: '{word}'\nContext Definition: '{definition}'\nExpected JSON format: {{\"plural\": \"die Bücher\"}}"
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
    # Gather all target files, ignoring files with "_uncountable" in the name
    target_files = [f for f in NOUNS_DIR.rglob("*/noun/*.json") if "_uncountable" not in f.name]
    files_processed = 0
    
    for filepath in target_files:
        if files_processed >= MAX_TEST_FILES:
            break
            
        with state_lock:
            if filepath.name in state.get("completed_files", []):
                continue
                
        print(f"\n📂 Processing file: {filepath.name}")
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
            
        items = data.get("items", [])
        
        # Find which words need fetching
        futures = []
        words_queued = 0
        with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_REQUESTS) as executor:
            for item in items:
                if MAX_TEST_WORDS > 0 and words_queued >= MAX_TEST_WORDS:
                    break
                    
                word = item.get("word", "")
                definition = item.get("definition", "")
                
                with state_lock:
                    is_cached = word in state
                
                if not is_cached:
                    futures.append(executor.submit(fetch_plural, word, definition))
                    words_queued += 1
            
            # Wait for all LLM queries for this file to finish
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as exc:
                    print(f"Task generated an exception: {exc}")

        # Re-build the items list with the LLM results
        regular_items = []
        uncountable_items = []
        
        for item in items:
            w = item.get("word", "")
            if w in state:
                if state[w] == "uncountable":
                    # Uncountable: copy item to sibling file, but keep original untouch in main file
                    uncountable_items.append(dict(item))
                    regular_items.append(item)
                else:
                    # Regular: replace word with pluralized version
                    item["word"] = state[w]
                    regular_items.append(item)
            else:
                regular_items.append(item)
                
        # Write modified main file
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(format_json(data.get("metadata", {}), regular_items))
            
        # Write uncountable sibling file (if any uncountable words were found)
        if uncountable_items:
            uncountable_filepath = filepath.parent / filepath.name.replace("_part", "_uncountable_part")
            u_metadata = dict(data.get("metadata", {}))
            u_metadata["name"] = u_metadata["name"].replace(" Part", " Uncountable Part")
            with open(uncountable_filepath, "w", encoding="utf-8") as uf:
                uf.write(format_json(u_metadata, uncountable_items))
            print(f"📝 Generated {uncountable_filepath.name} with {len(uncountable_items)} uncountable words.")
            
        # Mark file as atomically completed (only if we processed the whole file, not in test mode)
        if MAX_TEST_WORDS == 0:
            with state_lock:
                state.setdefault("completed_files", []).append(filepath.name)
                save_state()
            print(f"✅ Successfully completed file: {filepath.name}")
        else:
            print(f"⚠️ Test mode: File updated but NOT marked as completed in state file.")
            
        files_processed += 1
        
    print("\n--- GLOBAL STATS ---")
    print(f"Total GPU Time: {state['stats']['total_time_seconds']:.2f}s")
    print(f"Total Input Tokens: {state['stats']['total_input_tokens']}")
    print(f"Total Output Tokens: {state['stats']['total_output_tokens']}")
    print(f"Total Energy Used: {state['stats']['total_energy_kwh']:.6f} kWh")
    print(f"Total API Savings: ${state['stats']['total_saved_usd']:.5f}")

if __name__ == "__main__":
    process_files()
