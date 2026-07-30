import logging
import os
import sys
from datetime import datetime

LOG_FILE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'tartarus.log'
)

# Configure logger
logger = logging.getLogger('tartarus')
logger.setLevel(logging.DEBUG)

# Formatter
formatter = logging.Formatter('[%(asctime)s] [%(levelname)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')

# File handler
file_handler = logging.FileHandler(LOG_FILE_PATH, encoding='utf-8')
file_handler.setLevel(logging.DEBUG)
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

# Console handler
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setLevel(logging.DEBUG)
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)


def log_session_start(user, lang, session_id, stage, total_questions):
    msg = f"SESSION_START | User: {user} | Lang: {lang} | SessionID: {session_id} | Stage: {stage} | TotalQuestions: {total_questions}"
    logger.info(msg)


def log_question_served(user, session_id, stage, word_id, header_word, prompt, target_answer):
    msg = f"QUESTION_SERVED | User: {user} | SessionID: {session_id} | Stage: {stage} | WordID: {word_id} | Header: '{header_word}' | Prompt: '{prompt}' | Target: '{target_answer}'"
    logger.info(msg)


def log_answer_submitted(user, session_id, stage, word_id, user_input, target_answer, is_correct, result_status, score_after):
    match_str = "MATCH (CORRECT)" if is_correct else "MISMATCH (INCORRECT)"
    msg = f"ANSWER_SUBMITTED | User: {user} | SessionID: {session_id} | Stage: {stage} | WordID: {word_id} | Typed: '{user_input}' | Target: '{target_answer}' | Status: {match_str} [{result_status}] | NewScore: {score_after}"
    logger.info(msg)


def log_drill_step(user, session_id, user_input, target_answer, is_correct):
    msg = f"DRILL_STEP | User: {user} | SessionID: {session_id} | Typed: '{user_input}' | Target: '{target_answer}' | Correct: {is_correct}"
    logger.info(msg)


def log_session_finish(user, session_id, total_questions, correct_count, incorrect_count):
    accuracy = (100.0 * correct_count / total_questions) if total_questions > 0 else 0.0
    msg = f"SESSION_FINISH | User: {user} | SessionID: {session_id} | Total: {total_questions} | Correct: {correct_count} | Incorrect: {incorrect_count} | Accuracy: {accuracy:.1f}%"
    logger.info(msg)
