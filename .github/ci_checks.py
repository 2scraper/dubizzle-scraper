#!/usr/bin/env python3
"""
CI checks that are too long to live inside the workflow YAML.

They started out as heredocs in `.github/workflows/tests.yml` and moved here
for one practical reason: a Python block nested inside YAML inside a shell
`run:` needs three levels of quoting to stay intact, and shell-quoted regexes
like '(ws|wss)://[^ "'"'"']+' do not survive being copied through a browser.
A separate .py file is copy-paste safe, runs locally, and can be read on its
own.

Run any of these from the repo root:

    python .github/ci_checks.py --help-check
    python .github/ci_checks.py --sample-check
    python .github/ci_checks.py --secret-check
    python .github/ci_checks.py --all

Each prints what it looked at and exits non-zero on failure.
"""

import argparse
import csv
import json
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Engine libraries are deliberately absent in CI — the offline suite does not
# need them. An ImportError naming one of these is expected, not a failure.
ENGINE_LIBS = ("playwright", "pyppeteer", "selenium", "webdriver_manager")

CLIS = ["playwright_scraper.py", "puppeteer_scraper.py", "selenium_scraper.py",
        "scraper_api_client.py", "fingerprint_client.py", "env_config.py"]

SAMPLE_FILES = ("sample_output.json", "sample_output.csv")

# Phrases that show up in hand-written or template sample data. The point of
# committing a sample is that it came from a real run; a placeholder teaches
# readers field names and value shapes that do not exist.
FABRICATION_MARKERS = ("sample-product-", "example brand", "sample product",
                       "product description text", "lorem ipsum",
                       "your_api_key", "123456789")

# A URL carrying real credentials — the shape is scheme://something:something@host
# (deliberately not spelled out as an example here: this file scans itself, and
# an illustrative credential in a comment is a false positive that turns the
# build red for no reason. It happened on the first run.)
CREDENTIALLED_URL = re.compile(r"(?:ws|wss|https?)://[^\s\"'/]+:[^\s\"'/]+@")

# Documented placeholders and test values, which are SUPPOSED to look like the
# real thing — that is the point of them. Each entry earns its place by being
# in a line whose job is to show the shape of a credential or to prove the
# masker removes one; a real secret matches none of these.
#
# Kept as an explicit list rather than a loose pattern so that adding one is a
# decision. The alternative — a regex broad enough to cover them all — would
# also cover a real login.
CREDENTIAL_ALLOWED = (
    # documentation placeholders
    "USER:PASS", "user:pass", "ACCOUNT:PASSWORD", "LOGIN:PASSWORD",
    # This repo's April 2026 prototype README documented a proxy URL as
    # `http://username:password@…`. That commit is in the history and cannot
    # be removed from it, so --history-check would fail forever on a literal
    # placeholder — which would teach everyone to ignore the one check that
    # exists to be read exactly once, before publishing. Allowed by NAME, so
    # a real login still fails.
    "username:password",
    "{login}", "{user}", "password}@", "***", "u:p@h",
    "login:password@host:port",     # the shape a refusal message prints
    "user:secret@",                 # the proxy-pool masking fixtures
    "u:supersecret@", "login:supersecret@",   # the redaction fixtures
    "u:pass@h1", "u:pass@h2",       # the global-masking fixture
    "only:1",                       # a one-exit pool fixture
)

# A 2captcha API key is a 32-character hex string.
#
# So is this site's own ad identifier, and it is PUBLIC: every listing URL
# dubizzle publishes ends in `---{32 hex}`, and the committed fixtures,
# sample output and documentation are full of them by design. A pattern that
# cannot tell the two apart fails on its own repository on day one — which is
# how a check stops being read (§17) — so the ad-id shape is subtracted from
# the text BEFORE the scan rather than added to a context allowlist. A bare
# 32-hex string anywhere else still fails, which is the point.
HEX32 = re.compile(r"\b[0-9a-f]{32}\b")

# The three contexts this site publishes its own 32-hex identifiers in. Each
# is subtracted from a line BEFORE the scan, so a bare 32-hex anywhere else
# still fails — which is the point, and is what keeps this from becoming a
# check nobody reads.
#
#   1. the tail of every ad URL, `…-2-536---ac0df37b…`
#   2. the `uuid` / `listing_uuid` field carrying that same value
#   3. the filename of every photo on dbz-images.dubizzle.com
#
# 2 and 3 were invisible until the scan stopped filtering by suffix: `.json`
# was not in the old allowlist, so neither `sample_output.json` nor
# `fixtures_generated.json` had ever been opened by this check.
SITE_PUBLIC_IDS = (
    re.compile(r"---[0-9a-f]{32}\b"),
    re.compile(r'"(?:listing_)?uuid"\s*:\s*"[0-9a-f]{32}"'),
    re.compile(r"dbz-images\.dubizzle\.com/[^\s\"']*?[0-9a-f]{32}"),
)


def _without_site_ids(line):
    """A line with this site's own published identifiers taken out.

    Two passes, and the second is what makes this precise rather than broad.
    The first removes the identifier in the CONTEXTS the site publishes it
    in. The second removes those exact VALUES anywhere else on the same line
    — because a row that has already shown a hex as the ad's public id in its
    URL is not also carrying it as a separate secret, and in CSV that is
    exactly what happens: the URL column and the `listing_uuid` column hold
    the same string, one of them with no surrounding context at all.

    Anything left is a 32-hex the line never justified, and it still fails.
    """
    known = set()
    for pattern in SITE_PUBLIC_IDS:
        for match in pattern.finditer(line):
            known.update(HEX32.findall(match.group(0)))
        line = pattern.sub("SITE-AD-ID", line)
    for value in known:
        line = line.replace(value, "SITE-AD-ID")
    return line


# Contexts in which a 32-hex string is plainly not a key.
HEX32_ALLOWED = ("sha", "hash", "nonce", "example", "md5", "digest",
                 "checksum")

# Files the BARE-HEX rule is not applied to, and the reason it is not.
#
# These are verbatim site markup and verbatim run output. This site emits
# 32-hex identifiers in at least five public contexts — the tail of an ad
# URL, the `uuid` field, a photo filename, the `location_list.uuids` array,
# and Imperva's own resource token — so a bare-hex rule over them produces
# hundreds of findings that are all correct data. A check that cries wolf 221
# times is a check somebody switches off, and then it protects nothing.
#
# What covers them instead is STRONGER, not weaker, because it looks for the
# shape of a secret rather than the shape of a hex string:
#
#   * every rule below still applies here — a credentialled URL and a
#     key-shaped field both fail in these files;
#   * `make_fixtures.py` refuses to write a fixture whose scrub left an
#     agent's name, a per-seller UUID or a key-shaped value in it;
#   * `smoke_test.py` re-scans the whole committed fixture corpus for JWTs,
#     access tokens, API keys, Sentry DSNs, session ids, emails and proxy
#     credentials, and FAILS if the corpus it scanned was empty.
GENERATED_DATA_FILES = ("fixtures_generated.json", "sample_output.json",
                        "sample_output.csv")

# A secret sitting in a field named like one. This is what the bare-hex rule
# was reaching for, said precisely, and it applies to EVERY tracked file
# including the generated ones.
KEY_SHAPED_FIELD = re.compile(
    r'"(?:[a-zA-Z_-]*(?:api[_-]?key|apikey|secret|token|password|'
    r'client[_-]?key|access[_-]?key|site[_-]?key))"\s*[:=]\s*'
    r'"(?!REDACTED-|your_|\{|\*\*\*)[A-Za-z0-9_-]{16,}"', re.I)

# History findings that have been LOOKED AT and cleared, each with its
# reason. This exists because the history scan is a pre-publication gate: a
# later commit cannot reach what a published tag and a merged PR's refs
# already hold, so the decision has to be made once, before the repo goes
# public — and a decision that is not written down gets made again by the
# next person, differently.
#
# An entry here is a claim that someone read the blob. Anything not listed
# still fails.
HISTORY_DECIDED = {
    # The legacy README's JSON example, from the pre-rewrite repo (commit
    # "Add files via upload", 2026-04-14). It is a dubizzle LISTING id — the
    # same value the site publishes at the end of that ad's own URL — not a
    # credential. No key has ever been in this repository.
    "66966b05b2b746e48835c2b9791ecf7e":
        "a dubizzle listing id in the legacy README's JSON example, not a key",
}

# Raw captures that HAVE been committed at some point, each with the decision
# taken about it. A blob in history cannot be removed by a later commit — a
# merged PR's refs and any published tag keep it — so this is the record the
# pre-publication step asks for: read it once, decide once, before the repo
# goes public.
CAPTURES_IN_HISTORY_DECIDED = {
    # Committed by the v0.1.0 squash merge, because `--dump-html live_results`
    # writes FILES (`live_results.page1`) and .gitignore only covered the
    # DIRECTORY form. Both removed in the commit after, and both are in
    # history for good.
    #
    # Read before deciding. Each is one motors listing page as the site
    # served it, and carries: 26 per-seller UUIDs (opaque tokens the site
    # ships to every visitor), 12 copies of its public Algolia search key, 2
    # Sentry public keys, and ~820 of its own 32-hex ad identifiers — all of
    # it data dubizzle publishes to anyone who loads the page.
    #
    # It carries NOTHING OF OURS: no 2Captcha key, no proxy credential, no
    # session cookie, no Bearer token, no email address. Verified by pattern
    # before the decision was taken. A motors page also carries no
    # `agent_profile`, so no individual is named.
    #
    # Decision: not a leak, and not worth recreating the repository over.
    # What it IS is 3 MB of unscrubbed markup that this project's own rules
    # keep out, so the gitignore was widened, the working-tree scan stopped
    # filtering by suffix, and a tracked-capture rule was added — see
    # CAPTURE_SHAPES.
    "live_results.page1":
        "one motors listing page, site-public data only, nothing of ours",
    "live_results.page2":
        "one motors listing page, site-public data only, nothing of ours",
}

# Suffixes the HISTORY scan walks. It reads blobs out of git, where a
# binary is expensive to decode and useless to grep, so it stays narrow.
SCANNED_SUFFIXES = (".py", ".md", ".txt", ".yml", ".yaml", ".example")

# The WORKING-TREE scan is the opposite: it reads whatever is TRACKED, at any
# suffix, and that is not tidiness.
#
# A suffix allowlist is a scanner that cannot see the thing most likely to
# leak. `--dump-html live_results` writes `live_results.page1` — no suffix
# the list knew — and a merge committed two of them, 1.5 MB each, carrying 26
# per-seller UUIDs, 12 copies of the site's Algolia key and its Sentry keys.
# The check that exists to stop exactly that ran, passed, and never opened
# them.
#
# So the rule inverted: scan every tracked file, skip only what cannot be
# grepped.
BINARY_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf",
                   ".zip", ".gz", ".tar", ".whl", ".woff", ".woff2", ".ttf")

# A raw page dump has no business being tracked at all — it is 1.5 MB of
# someone else's session material, and scrubbing it is `make_fixtures.py`'s
# job. Matched on SHAPE rather than on the two names that got through once.
CAPTURE_SHAPES = (
    re.compile(r"\.page\d+$"),
    re.compile(r"_debug\.(?:html|png)$"),
    re.compile(r"^live_results\."),
    re.compile(r"^captures?/"),
)


def _git_files(*flags):
    out = subprocess.run(["git", "ls-files", "-z", *flags], cwd=REPO,
                         capture_output=True, text=True)
    if out.returncode != 0:
        return
    for rel in out.stdout.split("\0"):
        if rel:
            yield REPO / rel


def tracked_files():
    """Only what git already tracks. Used by the raw-capture rule, whose
    question is literally "is this committed"."""
    return _git_files("--cached")


def scanned_files():
    """What the CONTENT rules read: tracked files PLUS new files that are not
    ignored.

    Asked of GIT rather than of the disk, and the two flags are the whole
    design:

      --cached            what is committed, which is what can leak;
      --others            what is new, so a secret is caught BEFORE it is
                          added rather than after;
      --exclude-standard  which drops everything `.gitignore` covers — a
                          developer's own `.env`, their captures and their
                          run output are EXPECTED beside the scripts, and a
                          check that went red on them would be red on every
                          machine that had ever run the scraper for real,
                          which is the machine most likely to run it.
    """
    for path in _git_files("--cached", "--others", "--exclude-standard"):
        if not path.is_file() or path.suffix.lower() in BINARY_SUFFIXES:
            continue
        yield path


def help_check():
    failed = []
    for name in CLIS:
        script = REPO / name
        if not script.is_file():
            print(f"missing  {name}")
            failed.append(name)
            continue
        result = subprocess.run([sys.executable, str(script), "--help"],
                                capture_output=True, text=True, cwd=REPO)
        if result.returncode == 0:
            print(f"ok       {name}")
            continue
        blob = result.stdout + result.stderr
        if "ModuleNotFoundError" in blob and any(lib in blob for lib in ENGINE_LIBS):
            print(f"skipped  {name} (engine library not installed here)")
            continue
        print(f"FAILED   {name}\n{blob}")
        failed.append(name)
    return failed


def sample_check():
    failed = []
    for name in SAMPLE_FILES:
        if not (REPO / name).is_file():
            failed.append(f"{name} is missing — regenerate it from a real run")
    if failed:
        return failed

    rows = json.loads((REPO / "sample_output.json").read_text(encoding="utf-8"))
    if not rows:
        return ["sample_output.json is empty — a run that found nothing is not a sample"]

    blob = json.dumps(rows).lower()
    hits = [m for m in FABRICATION_MARKERS if m in blob]
    if hits:
        failed.append(f"sample_output.json looks fabricated: {hits}")

    # The committed sample doubles as a schema test: rename a field in the code
    # and forget the sample, and this fails rather than the docs going stale.
    sys.path.insert(0, str(REPO))
    from dataclasses import asdict

    from output_writer import Product
    expected = list(asdict(Product()).keys())

    for i, row in enumerate(rows):
        if list(row.keys()) != expected:
            failed.append(f"sample_output.json row {i}: columns differ from "
                          f"output_writer.Product")
            break

    with (REPO / "sample_output.csv").open(newline="", encoding="utf-8") as handle:
        header = next(csv.reader(handle))
    if header != expected:
        failed.append("sample_output.csv header differs from output_writer.Product")

    if not failed:
        discounted = sum(1 for r in rows if r.get("original_price"))
        print(f"ok       {len(rows)} rows, {len(expected)} columns, "
              f"{discounted} discounted, schema matches")
    return failed


def secret_check():
    failed = []
    scanned = 0
    for path in scanned_files():
        scanned += 1
        for lineno, line in enumerate(
                path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            rel = path.relative_to(REPO)

            if CREDENTIALLED_URL.search(line) and not any(
                    token in line for token in CREDENTIAL_ALLOWED):
                failed.append(f"{rel}:{lineno} looks like a URL with real "
                              f"credentials in it")

            # Applies everywhere, generated data included — this is the rule
            # the bare-hex one was reaching for, said precisely.
            for hit in KEY_SHAPED_FIELD.findall(line):
                failed.append(f"{rel}:{lineno} has a secret-shaped value in a "
                              f"key-shaped field")

            if rel.name in GENERATED_DATA_FILES:
                continue

            for match in HEX32.findall(_without_site_ids(line)):
                if any(token in line.lower() for token in HEX32_ALLOWED):
                    continue
                # A value already decided for the history scan is decided
                # here too — and this branch is not hypothetical: writing
                # HISTORY_DECIDED down put one of those strings into the
                # working tree, so without it this check failed on the very
                # file that records the decision.
                if match in HISTORY_DECIDED:
                    continue
                failed.append(f"{rel}:{lineno} contains {match[:6]}… — a "
                              f"32-char hex string, the shape of a 2captcha key")

    # A raw capture must not be tracked AT ALL, whatever is in it. This is
    # separate from the content scan on purpose: the two dumps that got
    # through carried nothing of OURS -- no key, no proxy password, no
    # cookie -- so a content rule would have passed them. What was wrong was
    # that they were committed: 3 MB of someone else's session material,
    # unscrubbed, in a repository whose own rules say captures stay out.
    for path in tracked_files():
        rel = path.relative_to(REPO).as_posix()
        if any(shape.search(rel) for shape in CAPTURE_SHAPES):
            failed.append(f"{rel} is a raw page capture and is TRACKED. "
                          f"Captures stay out of the repo; run them through "
                          f"make_fixtures.py, which scrubs them and proves "
                          f"the trim parses identically.")

    if not failed:
        print(f"ok       {scanned} files scanned, nothing credential-shaped, "
              f"no raw capture tracked")
    return failed


def history_check():
    """The same rules, applied to every blob that has EVER existed.

    `secret_check` reads the working tree, which is the right scope for CI:
    it fails a pull request before the mistake lands. This one is for the
    step CI cannot do anything about — publishing.

    A commit on top cannot reach what a published tag and a merged PR's refs
    already hold; those stay attached to the PR and cannot be deleted from
    it. So the decision has to be made BEFORE the repository goes public,
    and afterwards only a fresh repository removes anything. Run this then:

        python .github/ci_checks.py --history-check

    Deliberately NOT part of `--all` and not run by CI. It shells out to git
    once per object, which is fine for a hundred and wasteful on every push,
    and a repo whose history is dirty needs a decision rather than a red
    check.
    """
    try:
        listing = subprocess.run(["git", "rev-list", "--objects", "--all"],
                                 cwd=REPO, capture_output=True, text=True,
                                 check=True).stdout
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        return [f"could not read the git history ({e}) — run this inside a "
                f"clone, not an export"]

    objects = []
    for line in listing.splitlines():
        parts = line.split(None, 1)
        if parts:
            objects.append((parts[0], parts[1] if len(parts) > 1 else ""))

    failed, decided, scanned = [], [], 0

    # RAW CAPTURES THAT HAVE EVER BEEN COMMITTED, reported by name and size
    # whether or not they are still in the tree. Removing one in a later
    # commit does not reach the blob: a merged PR's refs and any published
    # tag keep it, and only a fresh repository removes it. So this is not a
    # pass/fail rule — it is the thing a reader has to make a decision about
    # before the repo goes public, which is the whole reason this scan
    # exists. A decision taken is recorded in CAPTURES_IN_HISTORY_DECIDED.
    for sha, path in objects:
        if not path or not any(s.search(path) for s in CAPTURE_SHAPES):
            continue
        size = subprocess.run(["git", "cat-file", "-s", sha], cwd=REPO,
                              capture_output=True, text=True).stdout.strip()
        note = CAPTURES_IN_HISTORY_DECIDED.get(path)
        if note:
            decided.append(f"{path} ({size} bytes, in history forever) — {note}")
        else:
            failed.append(
                f"{path} ({size} bytes) is a raw page capture that has been "
                f"COMMITTED at some point. A later commit cannot remove the "
                f"blob. Read it, decide, and record the decision in "
                f"CAPTURES_IN_HISTORY_DECIDED — or start a fresh repository.")

    for sha, path in objects:
        if not (path.endswith(SCANNED_SUFFIXES) or path in ("Dockerfile",)):
            continue
        kind = subprocess.run(["git", "cat-file", "-t", sha], cwd=REPO,
                              capture_output=True, text=True).stdout.strip()
        if kind != "blob":
            continue
        scanned += 1
        body = subprocess.run(["git", "cat-file", "blob", sha], cwd=REPO,
                              capture_output=True, text=True,
                              errors="replace").stdout
        for lineno, line in enumerate(body.splitlines(), 1):
            if CREDENTIALLED_URL.search(line) and not any(
                    token in line for token in CREDENTIAL_ALLOWED):
                failed.append(f"{path}:{lineno} (in a past commit) looks like "
                              f"a URL with real credentials in it")
            for match in HEX32.findall(_without_site_ids(line)):
                if any(token in line.lower() for token in HEX32_ALLOWED):
                    continue
                if match in HISTORY_DECIDED:
                    decided.append(f"{path}:{lineno} {match[:6]}… — "
                                   f"{HISTORY_DECIDED[match]}")
                    continue
                failed.append(f"{path}:{lineno} (in a past commit) contains "
                              f"{match[:6]}… — the shape of a 2captcha key")
    for note in sorted(set(decided)):
        print(f"decided  {note}")

    if not failed:
        print(f"ok       {scanned} blob(s) across {len(objects)} object(s) "
              f"that have ever existed — nothing credential-shaped")
    else:
        print("         NOTE: a later commit cannot remove any of these. A "
              "published tag and a merged PR's refs keep them, so this needs "
              "a decision BEFORE the repo goes public.")
    return failed


CHECKS = {"help": help_check, "sample": sample_check,
          "secret": secret_check, "history": history_check}


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--help-check", action="store_true",
                        help="Every shipped CLI answers --help")
    parser.add_argument("--sample-check", action="store_true",
                        help="sample_output.* exist, are real, match the schema")
    parser.add_argument("--secret-check", action="store_true",
                        help="No credentials committed anywhere")
    parser.add_argument("--history-check", action="store_true",
                        help="The same rules over every blob that has EVER "
                             "existed. For before publishing, not for CI — "
                             "see history_check(). Not included in --all.")
    parser.add_argument("--all", action="store_true",
                        help="help, sample and secret. NOT history: that one "
                             "is a pre-publication step, and it shells out to "
                             "git once per object.")
    args = parser.parse_args()

    selected = [name for name in CHECKS
                if getattr(args, f"{name}_check")
                or (args.all and name != "history")]
    if not selected:
        parser.error("pick at least one check, or --all")

    failures = []
    for name in selected:
        print(f"--- {name} check")
        failures += [f"[{name}] {line}" for line in CHECKS[name]()]

    if failures:
        print()
        for line in failures:
            print("FAILED:", line)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
