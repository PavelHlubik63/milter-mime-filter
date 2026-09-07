#!/usr/bin/env python3
"""Generate mail_filter emoji_regex.inc from official Unicode emoji data."""
from __future__ import annotations
import argparse
import pathlib
import re
import sys
import urllib.request

# The two files this generator reads, and where they come from. Spelled out
# here so that the URLs can be fetched by hand -- with wget, or from a host
# that reaches unicode.org when this one does not -- and so that anyone
# reading the output knows exactly what it was made from:
#
#   https://www.unicode.org/Public/17.0.0/ucd/emoji/emoji-data.txt
#   https://www.unicode.org/Public/17.0.0/ucd/emoji/emoji-variation-sequences.txt
#
# VERSION is the Unicode version, not the "Unicode Emoji" version: the path
# is under /Public/<Unicode version>/ucd/emoji/. The two numbering schemes
# have tracked each other for years, which is why the distinction is easy to
# miss and worth writing down.
#
# A version that has not been published yet does not fail. It redirects:
#
#   https://www.unicode.org/Public/18.0.0/ucd/emoji/emoji-data.txt
#     -> 302 -> http://www.unicode.org/Public/draft/ucd/emoji/emoji-data.txt
#
# and urlretrieve follows that quietly, over plain http, so asking for a
# future version yields draft data that changes under you between runs
# without anything saying so. Check the "Date:" line in the header of the
# downloaded file, which is kept in --download-dir for exactly this reason.
UNICODE_BASE = "https://www.unicode.org/Public/{version}"

def parse_codepoint_range(field: str) -> tuple[int, int]:
	field = field.strip()
	if ".." in field:
		lo, hi = field.split("..", 1)
		return int(lo, 16), int(hi, 16)
	cp = int(field, 16)
	return cp, cp

def merge_ranges(ranges) -> list[tuple[int, int]]:
	items = sorted(ranges)
	if not items:
		return []
	merged = [[items[0][0], items[0][1]]]
	for lo, hi in items[1:]:
		prev = merged[-1]
		if lo <= prev[1] + 1:
			prev[1] = max(prev[1], hi)
		else:
			merged.append([lo, hi])
	return [(lo, hi) for lo, hi in merged]

def load_property(path: pathlib.Path, prop: str) -> list[tuple[int, int]]:
	out = []
	with path.open(encoding="utf-8") as fh:
		for raw in fh:
			line = raw.split("#", 1)[0].strip()
			if not line or ";" not in line:
				continue
			cp_field, property_field = (part.strip() for part in line.split(";", 1))
			if property_field == prop:
				out.append(parse_codepoint_range(cp_field))
	return merge_ranges(out)

def load_emoji_variation_bases(path: pathlib.Path) -> list[tuple[int, int]]:
	bases = set()
	with path.open(encoding="utf-8") as fh:
		for raw in fh:
			line = raw.split("#", 1)[0].strip()
			if not line or ";" not in line:
				continue
			cps_field, style_field = (part.strip() for part in line.split(";", 1))
			if style_field.rstrip(";").strip() != "emoji style":
				continue
			cps = [int(x, 16) for x in cps_field.split()]
			if len(cps) == 2 and cps[1] == 0xFE0F:
				bases.add(cps[0])
	return merge_ranges((cp, cp) for cp in sorted(bases))

def cp_escape(cp: int) -> str:
	return f"\\u{cp:04X}" if cp <= 0xFFFF else f"\\U{cp:08X}"

def class_body(ranges: list[tuple[int, int]]) -> str:
	parts = []
	for lo, hi in ranges:
		parts.append(cp_escape(lo) if lo == hi else f"{cp_escape(lo)}-{cp_escape(hi)}")
	return "".join(parts)

def read_version(path: pathlib.Path) -> str:
	rx = re.compile(r"^# Version:\s*(\S+)")
	with path.open(encoding="utf-8") as fh:
		for line in fh:
			m = rx.match(line)
			if m:
				return m.group(1)
	return "unknown"

def download_unicode(version: str, directory: pathlib.Path):
	directory.mkdir(parents=True, exist_ok=True)
	emoji_data = directory / f"emoji-data-{version}.txt"
	variation = directory / f"emoji-variation-sequences-{version}.txt"
	for url, target in [
		(f"{UNICODE_BASE.format(version=version)}/ucd/emoji/emoji-data.txt", emoji_data),
		(f"{UNICODE_BASE.format(version=version)}/ucd/emoji/emoji-variation-sequences.txt", variation),
	]:
		print(f"Downloading {url}", file=sys.stderr)
		urllib.request.urlretrieve(url, target)
	return emoji_data, variation

def generate(emoji_data: pathlib.Path, variation_data: pathlib.Path) -> str:
	version = read_version(emoji_data)
	default_emoji = load_property(emoji_data, "Emoji_Presentation")
	variation_bases = load_emoji_variation_bases(variation_data)
	default_points = set()
	for lo, hi in default_emoji:
		default_points.update(range(lo, hi + 1))
	variation_only = []
	for lo, hi in variation_bases:
		for cp in range(lo, hi + 1):
			if cp not in default_points:
				variation_only.append((cp, cp))
	variation_only = merge_ranges(variation_only)
	lines = [
		"# mail_filter emoji regex include",
		f"# Generated from official Unicode Emoji {version} data.",
		"#", "# Detection policy:",
		"#   1) Emoji_Presentation=Yes code points are matched directly.",
		"#   2) Text-default emoji-capable symbols are matched only with U+FE0F.",
		"#   3) Keycap emoji sequences are matched explicitly.",
		"#", "# This intentionally does NOT use the broad Emoji=Yes property because",
		"# that property includes ordinary ASCII '#', '*', and digits 0-9.",
		"#", "# Format: one Python-compatible /regex/ per line; loaded by refile:.", "",
		f"/[{class_body(default_emoji)}]/",
		f"/[{class_body(variation_only)}]\\uFE0F/",
		r"/[#*0-9]\uFE0F?\u20E3/", "",
	]
	return "\n".join(lines)

def main() -> int:
	p = argparse.ArgumentParser(
		description=__doc__,
		formatter_class=argparse.RawDescriptionHelpFormatter,
		epilog="""\
Two data files are needed:

  https://www.unicode.org/Public/<VERSION>/ucd/emoji/emoji-data.txt
  https://www.unicode.org/Public/<VERSION>/ucd/emoji/emoji-variation-sequences.txt

where VERSION is a Unicode version such as 17.0.0. There are two ways to
get them:

  --download VERSION
        fetch emoji-data.txt and emoji-variation-sequences.txt for that
        Unicode Emoji version from unicode.org into --download-dir

  --emoji-data FILE --variation-data FILE
        use copies already on this machine, for a host with no route to
        unicode.org or when the files are kept under configuration control

Examples:
  generate_emoji_regex.py --download 18.0.0 -o /tmp/emoji_regex.inc
        generate from the current published version

  generate_emoji_regex.py --emoji-data emoji-data.txt \\
                          --variation-data emoji-variation-sequences.txt \\
                          -o /tmp/emoji_regex.inc
        generate from files fetched earlier

Installing the result:
  Compare it with the file in use before replacing anything, then copy it
  over emoji_regex.inc in the configuration directory and reload:

      diff /etc/mail_filter/emoji_regex.inc /tmp/emoji_regex.inc
      cp /tmp/emoji_regex.inc /etc/mail_filter/emoji_regex.inc
      mail_filter --configtest && systemctl reload mail_filter

  On FreeBSD the directory is /usr/local/etc/mail_filter and the reload is
  "service mail_filter reload".

Why regenerate at all:
  Unicode adds emoji. A file generated from an older version does not match
  the new ones, and a Subject carrying one passes a rule that was meant to
  catch it. The file shipped with this package names the version it was
  generated from on its second line.
""")
	p.add_argument("--emoji-data", type=pathlib.Path, metavar="FILE",
		help="local copy of emoji-data.txt; requires --variation-data")
	p.add_argument("--variation-data", type=pathlib.Path, metavar="FILE",
		help="local copy of emoji-variation-sequences.txt; requires "
		     "--emoji-data")
	p.add_argument("--download", metavar="VERSION",
		help="fetch both data files for this Unicode version (for "
		     "example 17.0.0) from unicode.org instead of reading "
		     "local copies. A version not yet published redirects to "
		     "the draft data -- see the comment above UNICODE_BASE")
	p.add_argument("--download-dir", type=pathlib.Path,
		default=pathlib.Path("."), metavar="DIR",
		help="where --download puts the files it fetches "
		     "(default: the current directory)")
	p.add_argument("-o", "--output", type=pathlib.Path, metavar="FILE",
		help="write the generated include here (default: standard output)")
	args = p.parse_args()
	if args.download:
		if args.emoji_data or args.variation_data:
			p.error("--download cannot be combined with --emoji-data/--variation-data")
		emoji_data, variation_data = download_unicode(args.download, args.download_dir)
	else:
		if not args.emoji_data or not args.variation_data:
			p.error("use --download VERSION or provide both --emoji-data and --variation-data")
		emoji_data, variation_data = args.emoji_data, args.variation_data
	text = generate(emoji_data, variation_data)
	if args.output:
		args.output.write_text(text, encoding="utf-8")
	else:
		sys.stdout.write(text)
	return 0

if __name__ == "__main__":
	raise SystemExit(main())
