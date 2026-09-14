import subprocess, sys
for f in ("playwright_scraper.py", "puppeteer_scraper.py", "selenium_scraper.py",
          "scraper_api_client.py", "diff_runs.py", "env_config.py",
          "fingerprint_client.py", "proxy_pool.py", "captcha_solver.py"):
    r = subprocess.run([sys.executable, f, "--help"], capture_output=True, text=True)
    tail = (r.stderr or r.stdout).strip().splitlines()
    print("%-24s rc=%s  %s" % (f, r.returncode, "OK" if r.returncode == 0 else tail[-2:]))
