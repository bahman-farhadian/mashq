#!/usr/bin/env python3
"""
utils/build_german_vocab.py

Reads data/word_lists/word_to_json.txt (one German word per line),
looks up each word on Wiktionary (1 req/s), and writes a LexiLoop-
compatible JSON file.

For nouns, the word field includes the singular with article and the
plural form, comma-separated: "das Auto, die Autos"

Rate limits are handled transparently — the script waits and retries
indefinitely. It never skips a word due to a temporary API error.
After processing, a verification step confirms every input word is
present in the output.

Usage (from the repo root):
    python3 utils/build_german_vocab.py <output_json_path>

Example:
    python3 utils/build_german_vocab.py data/word_lists/bahman_german_a1.json
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# Paths are always relative to the repo root (one level above utils/).
_ROOT         = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INPUT_FILE    = os.path.join(_ROOT, 'data', 'word_lists', 'word_to_json.txt')
PROGRESS_FILE = os.path.join(_ROOT, 'data', 'word_lists', '.vocab_build_progress.json')

DELAY        = 1.0   # seconds between successful API calls (1 req/s)
BACKOFF_BASE = 5     # first wait after a rate-limit or transient error
BACKOFF_MAX  = 120   # ceiling on exponential backoff

ARTICLE = {'m': 'der', 'f': 'die', 'n': 'das'}


# ------------------------------------------------------------
# HTTP — infinite retry on rate-limit / network errors
# ------------------------------------------------------------

def http_get(url, timeout=10):
    """Fetch a JSON URL, retrying indefinitely on 429 / 503 / network errors.

    Returns the parsed JSON dict, or None if the resource does not exist
    (404) or an unrecoverable parse error occurs.
    """
    req = urllib.request.Request(
        url,
        headers={'User-Agent': 'LexiLoop-vocab-builder/1.0 (educational)'},
    )
    wait = BACKOFF_BASE
    while True:
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())

        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None  # Word not on Wiktionary — not an error
            if e.code in (429, 503):
                print(f'    rate limited ({e.code}) — waiting {wait}s, then retrying…',
                      flush=True)
                time.sleep(wait)
                wait = min(wait * 2, BACKOFF_MAX)
                continue
            # Other HTTP errors (5xx, etc.) — treat as transient
            print(f'    HTTP {e.code} — waiting {wait}s, then retrying…', flush=True)
            time.sleep(wait)
            wait = min(wait * 2, BACKOFF_MAX)
            continue

        except urllib.error.URLError as e:
            print(f'    network error ({e.reason}) — waiting {wait}s, then retrying…',
                  flush=True)
            time.sleep(wait)
            wait = min(wait * 2, BACKOFF_MAX)
            continue

        except (json.JSONDecodeError, ValueError):
            return None  # Malformed response — give up on this URL


# ------------------------------------------------------------
# Wiktionary lookups
# ------------------------------------------------------------

_INFLECTION_RE = re.compile(
    r'^(plural|genitive|dative|accusative|nominative|inflected|'
    r'past tense|present tense|alternative form|archaic form)',
    re.I,
)


def en_definition(word):
    """Return (part_of_speech, definition_text) for the German entry, or None."""
    url = ('https://en.wiktionary.org/api/rest_v1/page/definition/'
           + urllib.parse.quote(word))
    data = http_get(url)
    if not data:
        return None
    for entry in data.get('de', []):
        for defn in entry.get('definitions', []):
            text = re.sub(r'<[^>]+>', '', defn.get('definition', '')).strip()
            if not text or _INFLECTION_RE.match(text):
                continue
            return entry.get('partOfSpeech', ''), text
    return None


def de_noun_info(word):
    """Return (article, plural) for a German noun via de.wiktionary, or (None, None)."""
    url = ('https://de.wiktionary.org/w/api.php?action=parse&format=json'
           f'&page={urllib.parse.quote(word)}&prop=wikitext')
    data = http_get(url)
    if not data:
        return None, None
    wikitext = data.get('parse', {}).get('wikitext', {}).get('*', '')

    article = None
    for pat in (r'\|Genus\s*=\s*([mfn])', r'\|Genus 1\s*=\s*([mfn])'):
        m = re.search(pat, wikitext)
        if m:
            article = ARTICLE.get(m.group(1))
            break

    plural = None
    for pat in (r'\|Nominativ Plural\s*=\s*([^\n|{]+)',
                r'\|Nominativ Plural 1\s*=\s*([^\n|{]+)'):
        m = re.search(pat, wikitext)
        if m:
            candidate = m.group(1).strip()
            if candidate and candidate not in ('—', '-', '–'):
                plural = candidate
            break

    return article, plural


# ------------------------------------------------------------
# Progress file
# ------------------------------------------------------------

def write_progress(processed, total, words, running=True, output=None):
    data = {'running': running, 'processed': processed, 'total': total, 'words': words}
    if output:
        data['output'] = output
    try:
        with open(PROGRESS_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False)
    except OSError:
        pass


# ------------------------------------------------------------
# Verify: every input word must appear in the output JSON
# ------------------------------------------------------------

def _base_form(raw):
    """Strip leading article from a word string for comparison purposes."""
    m = re.match(r'^(der|die|das)\s+(.+)$', raw.strip(), re.I)
    return m.group(2).lower() if m else raw.strip().lower()


def verify_output(input_words, output_path):
    """Compare input words against the output JSON and report any gaps."""
    if not os.path.exists(output_path):
        print('\n⚠  Output file not found — nothing to verify.', flush=True)
        return

    with open(output_path, encoding='utf-8') as f:
        results = json.load(f)

    # Build a set of base forms that are present in the output
    found_bases = set()
    for entry in results:
        for part in entry.get('word', '').split(','):
            found_bases.add(_base_form(part))

    missing = [w for w in input_words if _base_form(w) not in found_bases]

    print()
    if missing:
        print(f'⚠  {len(missing)} input word(s) not found in output:')
        for w in missing:
            print(f'   - {w}')
    else:
        print(f'✓  All {len(input_words)} input words are present in the output.')


# ------------------------------------------------------------
# Main
# ------------------------------------------------------------

def main():
    if len(sys.argv) < 2:
        print(f'Usage: python3 {sys.argv[0]} <output_json_path>', file=sys.stderr)
        sys.exit(1)

    output_path = sys.argv[1]

    if not os.path.exists(INPUT_FILE):
        print(f'Input file not found: {INPUT_FILE}', file=sys.stderr)
        write_progress(0, 0, [], running=False)
        sys.exit(1)

    with open(INPUT_FILE, encoding='utf-8') as f:
        raw_words = [ln.strip() for ln in f if ln.strip() and not ln.startswith('#')]

    total = len(raw_words)
    print(f'{total} words  →  {output_path}', flush=True)
    write_progress(0, total, [])

    results = []

    for i, raw in enumerate(raw_words, 1):
        # Strip a leading article so the lookup hits the bare noun
        m = re.match(r'^(der|die|das)\s+(.+)$', raw, re.I)
        word = m.group(2) if m else raw

        pos, defn, article, plural = None, '', None, None

        # English definition (en.wiktionary REST API)
        result = en_definition(word)
        time.sleep(DELAY)

        if result:
            pos, defn = result

        # Gender + plural for nouns (de.wiktionary wikitext)
        if pos and 'noun' in pos.lower():
            article, plural = de_noun_info(word)
            time.sleep(DELAY)

        # Build the final word field
        if article:
            singular = f'{article} {word}'
            word_field = f'{singular}, die {plural}' if plural else singular
        else:
            word_field = word

        entry = {'word': word_field, 'definition': defn}
        results.append(entry)

        status = '✓' if defn else '?'
        print(f'[{i}/{total}] {status}  {raw} → {word_field}: {defn[:55]}', flush=True)
        write_progress(i, total, results)

    # Write final output
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    print(f'\nSaved {len(results)} words → {output_path}', flush=True)
    write_progress(total, total, results, running=False, output=output_path)

    # Verify nothing was lost
    verify_output(raw_words, output_path)


if __name__ == '__main__':
    main()
