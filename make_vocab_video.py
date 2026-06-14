# -*- coding: utf-8 -*-
"""
make_vocab_video.py - generate a vocabulary-drill video from a LexiLoop word
list, using macOS 'say' for German audio and ffmpeg for the video.

For each word in the list, the output video shows the German word and its
English meaning on a dark-grey background, with the German audio spoken
several times in a row (audio only - no English narration).

Requires ffmpeg/ffprobe (e.g. `brew install ffmpeg`) and macOS 'say'.
Standard library only - no pip install / virtualenv needed.

Run directly, e.g.:
    python3 make_vocab_video.py --user bahman --lang german --limit 5
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile

import lexiloop as ll

FONT_FILE = "/System/Library/Fonts/Supplemental/Arial Unicode.ttf"
VIDEO_SIZE = "1280x720"
BACKGROUND_COLOR = "0x303030"
GAP_SECONDS = 0.6


def escape_drawtext(text):
    """Escapes a string for safe use inside an ffmpeg drawtext filter."""
    text = text.replace("\\", "\\\\")
    text = text.replace(":", "\\:")
    text = text.replace("'", "’")
    text = text.replace("%", "\\%")
    return text


def run(cmd):
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"Command failed: {' '.join(cmd)}\n{result.stderr}")
    return result


def meaning_for(entry):
    definition = entry.get("definition")
    if isinstance(definition, list):
        return definition[0] if definition else ""
    if isinstance(definition, str):
        return definition
    return ""


def make_word_clip(entry, voice, repeats, tmpdir, index):
    word = entry["word"]
    meaning = meaning_for(entry)

    raw_aiff = os.path.join(tmpdir, f"{index}_raw.aiff")
    say_cmd = ["say", "-o", raw_aiff]
    if voice:
        say_cmd += ["-v", voice]
    say_cmd.append(word)
    run(say_cmd)

    raw_wav = os.path.join(tmpdir, f"{index}_raw.wav")
    run(["ffmpeg", "-y", "-i", raw_aiff, "-ar", "44100", "-ac", "1", raw_wav])

    # Repeat the word's audio `repeats` times, with a short silence between.
    repeated_wav = os.path.join(tmpdir, f"{index}_repeated.wav")
    filter_parts = [f"[1:a]atrim=duration={GAP_SECONDS}[sil]"]
    concat_inputs = []
    for i in range(repeats):
        concat_inputs.append("[0:a]")
        if i != repeats - 1:
            concat_inputs.append("[sil]")
    filter_parts.append(
        "".join(concat_inputs) + f"concat=n={len(concat_inputs)}:v=0:a=1[aout]"
    )
    run([
        "ffmpeg", "-y",
        "-i", raw_wav,
        "-f", "lavfi", "-i", f"anullsrc=channel_layout=mono:sample_rate=44100",
        "-filter_complex", ";".join(filter_parts),
        "-map", "[aout]", repeated_wav,
    ])

    duration = run([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", repeated_wav,
    ]).stdout.strip()

    word_text = escape_drawtext(word)
    meaning_text = escape_drawtext(meaning)
    drawtext = (
        f"drawtext=fontfile={FONT_FILE}:text='{word_text}':fontcolor=white:"
        f"fontsize=72:x=(w-text_w)/2:y=(h/2)-60"
    )
    if meaning_text:
        drawtext += (
            f",drawtext=fontfile={FONT_FILE}:text='{meaning_text}':fontcolor=white:"
            f"fontsize=40:x=(w-text_w)/2:y=(h/2)+40"
        )

    clip_path = os.path.join(tmpdir, f"{index}_clip.mp4")
    run([
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"color=c={BACKGROUND_COLOR}:s={VIDEO_SIZE}:d={duration}",
        "-i", repeated_wav,
        "-vf", drawtext,
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-shortest", clip_path,
    ])
    return clip_path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user", required=True, help="Username (word list owner).")
    parser.add_argument("--lang", default="german", help="Word list language (default: german).")
    parser.add_argument("--word-list", help="Path to the word list JSON (default: data/word_lists/<user>_<lang>.json).")
    parser.add_argument("--output", default="vocab_video.mp4", help="Output video path.")
    parser.add_argument("--limit", type=int, help="Only process the first N words.")
    parser.add_argument("--repeats", type=int, default=4, help="How many times to say each word (default: 4).")
    args = parser.parse_args()

    user = ll.sanitize_name(args.user, "user")
    lang = ll.sanitize_name(args.lang, "lang")
    word_list_path = args.word_list or ll.word_list_path(user, lang)

    if not os.path.exists(word_list_path):
        print(f"Error: word list not found: {word_list_path}", file=sys.stderr)
        sys.exit(1)

    with open(word_list_path, "r", encoding="utf-8") as f:
        entries = json.load(f)

    if args.limit:
        entries = entries[:args.limit]

    if not entries:
        print("Error: word list is empty.", file=sys.stderr)
        sys.exit(1)

    voice = ll.voice_for_language(lang)
    print(f"Voice: {voice or '(system default)'}")
    print(f"Words: {len(entries)}")

    with tempfile.TemporaryDirectory() as tmpdir:
        clip_paths = []
        for i, entry in enumerate(entries):
            print(f"  [{i + 1}/{len(entries)}] {entry['word']}")
            clip_paths.append(make_word_clip(entry, voice, args.repeats, tmpdir, i))

        concat_list = os.path.join(tmpdir, "concat.txt")
        with open(concat_list, "w", encoding="utf-8") as f:
            for clip_path in clip_paths:
                f.write(f"file '{clip_path}'\n")

        run([
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0", "-i", concat_list,
            "-c", "copy", args.output,
        ])

    print(f"Done: {args.output}")


if __name__ == "__main__":
    main()
