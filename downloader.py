import os
import http.cookiejar
import urllib.request
import threading
import shutil
from tqdm import tqdm

def fast_download(label, url, output_file, user_agent, cookie_file, num_parts=16, bar_pos=0):
    """
    Parallel range-request downloader using a cookie file and tqdm progress bar.
    """
    # Load cookies from file
    cj = http.cookiejar.MozillaCookieJar(cookie_file)
    try:
        cj.load(ignore_discard=True, ignore_expires=True)
    except Exception as e:
        print(f"Warning: could not load {cookie_file}: {e}")

    # Setup custom opener with cookie support
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    opener.addheaders = [("User-Agent", user_agent)]
    
    tqdm.write(f"⬇  {label}: probing file size...")

    # HEAD/GET request to get total size
    try:
        with opener.open(url, timeout=15) as r:
            total = int(r.headers.get("Content-Length", 1))
    except Exception as e:
        tqdm.write(f"   {label} probe failed ({e}), trying single connection...")
        total = 0

    if total <= 1:
        tqdm.write(f"   Falling back to single-connection download for {label}...")
        try:
            with opener.open(url, timeout=60) as r, open(output_file, "wb") as f:
                shutil.copyfileobj(r, f)
            tqdm.write(f"   ✓ {label} done.")
            return True
        except Exception as e:
            tqdm.write(f"   ✗ {label} failed: {e}")
            return False

    size_mb = total / (1024 * 1024)
    # print(f"   Size: {size_mb:.1f} MB  → splitting into {num_parts} parallel parts...")

    # Divide into num_parts byte ranges
    chunk = (total + num_parts - 1) // num_parts
    ranges = []
    for i in range(num_parts):
        start = i * chunk
        end = min(start + chunk - 1, total - 1)
        if start <= total - 1:
            ranges.append((i, start, end))

    actual_parts = len(ranges)
    temp_files = [f"{output_file}.part{i}" for i in range(actual_parts)]
    errors = [None] * actual_parts # type: list[Optional[Exception]]
    done_count = [0]
    lock = threading.Lock()

    # Progress bar setup
    pbar = tqdm(total=actual_parts, desc=f"   {label}", unit="part", position=bar_pos, leave=True)

    def fetch_part(idx, start, end):
        try:
            req = urllib.request.Request(url)
            req.add_header("Range", f"bytes={start}-{end}")
            with opener.open(req, timeout=60) as r, open(temp_files[idx], "wb") as f:
                shutil.copyfileobj(r, f)
            with lock:
                done_count[0] += 1
                pbar.update(1)
        except Exception as e:
            errors[idx] = e
            # tqdm.write(f"   [{label}] part {idx} ERROR: {e}")

    threads = [threading.Thread(target=fetch_part, args=(i, s, e)) for i, s, e in ranges]
    for t in threads: t.start()
    for t in threads: t.join()
    pbar.close()

    if any(errors):
        tqdm.write(f"   ✗ {label}: {sum(1 for e in errors if e)} part(s) failed.")
        return False

    # Concatenate parts into final file
    tqdm.write(f"   Assembling {label}...")
    with open(output_file, "wb") as out:
        for tf in temp_files:
            if os.path.exists(tf):
                with open(tf, "rb") as part:
                    shutil.copyfileobj(part, out)
                os.remove(tf)

    tqdm.write(f"   ✓ {label} saved → {output_file}")
    return True
