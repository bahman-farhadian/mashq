import json
import urllib.request
import urllib.error
import time
import threading
import argparse
import sys
import os
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

# --- CLI Arguments ---
parser = argparse.ArgumentParser(description="Fetch German plurals from local Gemma 4:12b")
parser.add_argument("--test", type=int, default=0, help="Test mode: Process exactly N words total, then stop (0 = process everything).")
args = parser.parse_args()

# --- Configuration ---
OLLAMA_URL = "http://192.168.8.5:11434/api/chat"
MODEL = "gemma4:12b"
MAX_CONCURRENT_REQUESTS = 8
MAX_TEST_WORDS = args.test
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
    
if "processed_words" not in state:
    state["processed_words"] = []

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

def fetch_plural(word, definition, filepath):
    prompt = f"Provide the plural form for the following German noun with its definite article. If the noun has no plural, return the exact string 'uncountable'. You MUST return a JSON object.\nWord: '{word}'\nContext Definition: '{definition}'\nExpected JSON format: {{\"plural\": \"die Bücher\"}}"
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
                
                saved_usd = (input_tokens / 1_000_000 * 0.10) + (output_tokens / 1_000_000 * 0.30)
                
                content = data.get("message", {}).get("content", "").strip()
                
                if content.startswith("```json"):
                    content = content[7:]
                if content.startswith("```"):
                    content = content[3:]
                if content.endswith("```"):
                    content = content[:-3]
                content = content.strip()
                
                if not content:
                    raise ValueError("Model returned an empty string instead of JSON.")
                
                result = json.loads(content)
                plural = result.get("plural", "")
                
                is_uncountable = False
                if plural and plural.lower() == "uncountable":
                    new_word = word # Uncountable remains original
                    is_uncountable = True
                elif plural:
                    new_word = f"{word}, {plural}"
                else:
                    new_word = word
                    
                # --- Thread-Safe On-The-Fly Update ---
                with state_lock:
                    # 1. Update Global State
                    state["processed_words"].append(new_word)
                    state["stats"]["total_input_tokens"] += input_tokens
                    state["stats"]["total_output_tokens"] += output_tokens
                    state["stats"]["total_time_seconds"] += duration_s
                    state["stats"]["total_energy_kwh"] += energy_kwh
                    state["stats"]["total_saved_usd"] += saved_usd
                    save_state()
                    
                    # 2. Update Main JSON File on the fly (if it changed)
                    if new_word != word:
                        with open(filepath, "r", encoding="utf-8") as f:
                            file_data = json.load(f)
                        
                        items = file_data.get("items", [])
                        for item in items:
                            if item.get("word") == word:
                                item["word"] = new_word
                                break
                                
                        with open(filepath, "w", encoding="utf-8") as f:
                            f.write(format_json(file_data.get("metadata", {}), items))
                    
                    # 3. Create/Update Uncountable JSON File on the fly
                    if is_uncountable:
                        uf_path = filepath.parent / filepath.name.replace("_part", "_uncountable_part")
                        if uf_path.exists():
                            with open(uf_path, "r", encoding="utf-8") as uf:
                                u_data = json.load(uf)
                            u_items = u_data.get("items", [])
                        else:
                            # Generate from original file metadata
                            with open(filepath, "r", encoding="utf-8") as f:
                                orig_data = json.load(f)
                            u_data = {"metadata": orig_data.get("metadata", {}).copy()}
                            u_data["metadata"]["name"] = u_data["metadata"]["name"].replace(" Part", " Uncountable Part")
                            u_items = []
                            
                        # Avoid duplicates
                        if not any(u.get("word") == word for u in u_items):
                            u_items.append({"word": word, "definition": definition})
                            with open(uf_path, "w", encoding="utf-8") as uf:
                                uf.write(format_json(u_data.get("metadata", {}), u_items))
                    
                print(f"Processed: {word} -> {new_word} | Time: {duration_s:.2f}s | Tokens: {tokens} | Energy: {energy_kwh:.6f} kWh | Saved: ${saved_usd:.5f}")
                return new_word
                
        except Exception as e:
            raw_out = repr(content) if 'content' in locals() else "[No response, failed early]"
            print(f"Attempt {attempt + 1} failed for '{word}': {e} | Raw output: {raw_out}")
            if attempt == max_retries - 1:
                print(f"Giving up on '{word}' after {max_retries} attempts.")
                return None
            time.sleep(2) # brief pause before retry

def process_files():
    target_files = [f for f in NOUNS_DIR.rglob("*/noun/*.json") if "_uncountable" not in f.name]
    
    futures = []
    words_queued = 0
    
    executor = ThreadPoolExecutor(max_workers=MAX_CONCURRENT_REQUESTS)
    
    try:
        for filepath in target_files:
            if MAX_TEST_WORDS > 0 and words_queued >= MAX_TEST_WORDS:
                break
                
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
                
            items = data.get("items", [])
            for item in items:
                if MAX_TEST_WORDS > 0 and words_queued >= MAX_TEST_WORDS:
                    break
                    
                word = item.get("word", "")
                definition = item.get("definition", "")
                
                with state_lock:
                    is_cached = word in state.get("processed_words", [])
                
                if not is_cached:
                    futures.append(executor.submit(fetch_plural, word, definition, filepath))
                    words_queued += 1
        
        if MAX_TEST_WORDS > 0:
            print(f"Test Mode Active: Queued exactly {words_queued} words globally.")
        else:
            print(f"Production Mode: Queued {words_queued} uncached words for processing.")
            
        for future in as_completed(futures):
            future.result()
            
    except Exception as exc:
        print(f"Task generated an exception: {exc}")
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    print("\n--- GLOBAL STATS ---")
    print(f"Total GPU Time: {state['stats']['total_time_seconds']:.2f}s")
    print(f"Total Input Tokens: {state['stats']['total_input_tokens']}")
    print(f"Total Output Tokens: {state['stats']['total_output_tokens']}")
    print(f"Total Energy Used: {state['stats']['total_energy_kwh']:.6f} kWh")
    print(f"Total API Savings: ${state['stats']['total_saved_usd']:.5f}")

if __name__ == "__main__":
    try:
        process_files()
    except KeyboardInterrupt:
        print("\n\nScript interrupted by user (Ctrl+C). Shutting down immediately!")
        os._exit(1)
