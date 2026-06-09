"""Whisper transcription cleanup — hallucination filtering and repetition removal.

Shared module: canonical copy lives in whisper_dictation/common/. This is a
vendored copy kept in sync by hand (re-copy the file to update). Keep it
dependency-free (stdlib only) and configured via env vars or explicit args.
"""

import os
import re

whisper_language = os.getenv("WHISPER_LANGUAGE", "")

# Repetition removal settings
min_repetitions = int(os.getenv("MIN_REPETITIONS", "6"))  # Minimum repetitions to trigger removal
keep_repetitions = int(os.getenv("KEEP_REPETITIONS", "5"))  # Number of repetitions to keep

# Ignore patterns for transcriptions
ignore_patterns = os.getenv("IGNORE_PATTERNS", "")


def should_ignore_transcription(text, lang=None):
	"""
	Check if transcription should be ignored based on various patterns.
	Returns True if the text should be ignored, False otherwise.
	"""
	if not text:
		return False

	text = text.strip()
	text_lower = text.lower()

	# If user provided custom patterns via env var, use those
	if ignore_patterns:
		if re.search(ignore_patterns, text, re.IGNORECASE):
			return True

	# Default patterns based on language (explicit arg wins over $WHISPER_LANGUAGE)
	lang = str(lang or whisper_language).lower()
	if lang:

		if lang == "cs":
			# Czech patterns - exact matches
			exact_matches = [
				'Děkuji.',
				'Děkuji!',
				'Děkujeme.',
				'Děkujeme!',
				'Konec.',
				'Konec!',
				'Tak.',
				'Óóó!',
				'S...',
				'S',
				'Hm?',
				'Ss.',
				'Ssss!',
				'Ufff...',
				'Mhmmm!',
				'Hmm...',
				'Aaah!',
				'KUH KUH KUH KUH KUH',

			]

			# Czech patterns - substring matches
			substring_matches = [
				'Zdejte se na to, co myslíme.',
				'Zdejte se na návrhu.',
				'Zdejte se na můj kanál!',
				'Zdejte na můj kanál!',
				"http://johnyxcz.blogspot.com",
				"http://johnyxcz.com",
				"Titulky vytvořil",
				"www.hradeckesluzby.cz",
				"www.arkance-systems.cz",
				'www.hradeckralove.org',
				'Nechci to, nechci to, nechci to!',
				"děkujeme za pozornost",
				'Děkujeme za podporu!',
				'Svět!',

			]

			# Check exact matches
			if text_lower in [p.lower() for p in exact_matches]:
				return True

			# Check substring matches
			for pattern in substring_matches:
				if pattern.lower() in text_lower:
					return True

		elif lang == "en":
			# English patterns - exact matches
			exact_matches = [
				"thanks for watching",
				"thanks for watching!",
				"thank you",
				"thank you very much",
				"thank you so much",
				"thank you.",
				"you",
				"bye.",
				"bye-bye."
			]

			# Check exact matches
			if text_lower in [p.lower() for p in exact_matches]:
				return True

			# case-insensitive substring matches
			substring_matches = [
				"thanks for watching"
			]
			# Check substring matches
			for pattern in substring_matches:
				if pattern.lower() in text_lower:
					return True

			# English patterns - regex patterns for more complex matching
			regex_patterns = [
				r"^\s*clear throat\s*$",
				r"^\s*\[.*\]\s*$",  # [anything in brackets]
				r"^\s*\(.*\)\s*$",  # (anything in parentheses)
				r"^\s*\*.*\*\s*$"  # *anything in asterisks*
			]

			# Check regex patterns
			for pattern in regex_patterns:
				if re.match(pattern, text, re.IGNORECASE):
					return True

	return False


def remove_repetitions(text, min_repetitions=min_repetitions, keep_repetitions=keep_repetitions):
	"""
	Remove excessive repetitive patterns from text, keeping only a specified number of repetitions.

	Args:
		text: The input text to process
		min_repetitions: Minimum number of repetitions to consider as excessive (default: 6)
		keep_repetitions: Number of repetitions to keep (default: 5)

	Returns:
		Text with repetitions reduced to keep_repetitions occurrences
	"""
	if not text:
		return text

	# Start with small pattern sizes and work up to larger ones
	# This helps catch both single character and longer phrase repetitions
	max_pattern_length = min(len(text) // min_repetitions, 100)  # Cap at 100 chars for performance

	for pattern_length in range(1, max_pattern_length + 1):
		# Use regex to find consecutive repetitions of patterns
		# The pattern captures any sequence of 'pattern_length' characters
		# and checks if it repeats min_repetitions or more times
		pattern = r'(.{' + str(pattern_length) + r'})(?:\1){' + str(min_repetitions - 1) + r',}'

		# Replace with the pattern repeated keep_repetitions times
		replacement = r'\1' * keep_repetitions
		text = re.sub(pattern, replacement, text)

	return text
