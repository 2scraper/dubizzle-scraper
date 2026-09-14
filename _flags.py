"""The engines' flag sets, against the family contract and against each
other, in BOTH directions (§17 #2)."""
import re, subprocess, sys

ENGINES = ["playwright_scraper.py", "puppeteer_scraper.py", "selenium_scraper.py"]

# §9's list, verbatim.
CONTRACT = set("""
--url --pages --category --format --out --delay --retries --retry-delay
--concurrency --proxy --proxy-file --proxy-rotate --proxy-shuffle
--proxy-block-retries --twocaptcha-key --captcha-api --solve-captcha
--min-score --cdp-endpoint --allow-empty --dump-html --headless --headful
""".split())

# Banned on a scraper: each could disagree with the URL it was given.
# `--locale` is NOT here: it exists on the Playwright engine, sets what the
# browser claims about itself, and cannot change the page — the language is a
# path segment on this site and the price is AED for every visitor. It is in
# DOCUMENTED_DIFFERENCES below instead.
BANNED = {"--country", "--antidetect", "--lang"}

# Differences between the engines that the README states and the engines'
# own docstrings repeat. Closing one of these silently would be a change in
# behaviour, so the check fails on an UNDOCUMENTED difference in either
# direction rather than on any difference at all.
DOCUMENTED_DIFFERENCES = {
    "puppeteer_scraper.py": {
        "--fingerprint", "--fp-country", "--fp-tags", "--locale",
        "--chromium-path", "--version",
    },
    "selenium_scraper.py": {"--locale"},
}


def flags(path):
    out = subprocess.run([sys.executable, path, "--help"],
                         capture_output=True, text=True).stdout
    return set(re.findall(r"(--[a-z0-9][a-z0-9-]*)", out))


sets = {e: flags(e) for e in ENGINES}
bad = 0

for e, f in sets.items():
    missing = CONTRACT - f
    if missing:
        bad += 1
        print("%s is missing contract flags: %s" % (e, sorted(missing)))
    banned = BANNED & f
    if banned:
        bad += 1
        print("%s carries a banned flag: %s" % (e, sorted(banned)))

base = sets[ENGINES[0]]
for e in ENGINES[1:]:
    allowed = DOCUMENTED_DIFFERENCES.get(e, set())
    only_a = (base - sets[e]) - allowed
    only_b = (sets[e] - base) - allowed
    if only_a or only_b:
        bad += 1
        print("%s differs from %s: only-in-playwright=%s only-in-%s=%s"
              % (e, ENGINES[0], sorted(only_a), e, sorted(only_b)))

print("contract flags: %d; engines agree: %s" % (len(CONTRACT), "no" if bad else "yes"))
sys.exit(1 if bad else 0)
