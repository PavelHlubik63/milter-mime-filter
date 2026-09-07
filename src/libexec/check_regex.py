#!/usr/bin/env python3

import sys
import re
import argparse


UNICODE_ESC_RE = re.compile(r"\\U[0-9A-Fa-f]{8}|\\u[0-9A-Fa-f]{4}")

FLAG_MAP = {
	"i": re.IGNORECASE,
	"m": re.MULTILINE,
	"s": re.DOTALL,
	"x": re.VERBOSE,
}


def unescape_pattern(pattern):
	def replace(match):
		return chr(int(match.group(0)[2:], 16))

	return UNICODE_ESC_RE.sub(replace, pattern)


def parse_regex_line(line, filename, lineno):
	line = line.strip()

	if not line or line.startswith("#"):
		return None

	if not line.startswith("/"):
		raise ValueError(
			f"{filename}:{lineno}: expected /regex/flags: {line!r}"
		)

	i = 1
	end = None

	while i < len(line):
		if line[i] == "\\":
			i += 2
			continue

		if line[i] == "/":
			end = i

		i += 1

	if end is None:
		raise ValueError(
			f"{filename}:{lineno}: unterminated regex: {line!r}"
		)

	pattern = line[1:end]
	flags_str = line[end + 1:].strip()

	if any(flag.lower() not in FLAG_MAP for flag in flags_str):
		raise ValueError(
			f"{filename}:{lineno}: invalid flags: {flags_str!r}"
		)

	flags = 0

	for flag in flags_str:
		flags |= FLAG_MAP[flag.lower()]

	try:
		regex = re.compile(unescape_pattern(pattern), flags)
	except re.error as exc:
		raise ValueError(
			f"{filename}:{lineno}: invalid regex /{pattern}/{flags_str}: {exc}"
		) from exc

	return regex, pattern, flags_str, lineno


def load_regexes(filename):
	regexes = []

	with open(filename, encoding="utf-8") as f:
		for lineno, line in enumerate(f, 1):
			entry = parse_regex_line(line, filename, lineno)

			if entry is not None:
				regexes.append(entry)

	return regexes


def process_stream(stream, regexes, show_found=False, reverse=False):
	found_count = 0
	not_found_count = 0

	for raw_line in stream:
		text = raw_line.rstrip("\r\n")

		text = re.sub(
			r"^\*\*\*\*\*SPAM\*\*\*\*\*\s*",
			"",
			text,
			flags=re.IGNORECASE,
		)

		match_entry = None

		for regex, pattern, flags_str, lineno in regexes:
			if regex.search(text):
				match_entry = (pattern, flags_str, lineno)
				break

		if match_entry is None:
			not_found_count += 1

			if not reverse:
				print(f"NOT FOUND: {text}")
		else:
			found_count += 1

			if show_found:
				pattern, flags_str, lineno = match_entry
				print(
					f"FOUND: {text}\n"
					f"\tline {lineno}: /{pattern}/{flags_str}"
				)

	return found_count, not_found_count


def main():
	parser = argparse.ArgumentParser(
		description="Test text lines against mail_filter-style regex file"
	)

	parser.add_argument(
		"text_file",
		help="input text file, or - for stdin",
	)

	parser.add_argument(
		"regex_file",
		help="file containing /regex/flags entries",
	)

	parser.add_argument(
		"--show-found", "-s",
		action="store_true",
		help="also display matched lines and matching regex",
	)

	parser.add_argument(
		"--reverse", "-r",
		action="store_true",
		help=(
			"reverse test logic: matching input lines are errors; "
			"use --show-found to display them"
		),
	)

	args = parser.parse_args()

	try:
		regexes = load_regexes(args.regex_file)
	except (OSError, ValueError) as exc:
		print(f"ERROR: {exc}", file=sys.stderr)
		return 2

	if not regexes:
		print("ERROR: no regexes loaded", file=sys.stderr)
		return 2

	if args.text_file == "-":
		found, not_found = process_stream(
			sys.stdin,
			regexes,
			args.show_found,
			args.reverse,
		)
	else:
		try:
			with open(args.text_file, encoding="utf-8") as f:
				found, not_found = process_stream(
					f,
					regexes,
					args.show_found,
					args.reverse,
				)
		except OSError as exc:
			print(f"ERROR: {exc}", file=sys.stderr)
			return 2

	print(
		f"\nRegexes: {len(regexes)}, "
		f"FOUND: {found}, "
		f"NOT FOUND: {not_found}",
		file=sys.stderr,
	)

	return 1 if (found if args.reverse else not_found) else 0


if __name__ == "__main__":
	sys.exit(main())
