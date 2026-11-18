"""
JPMS Issue Miner In GitHub
--------------------------
1) Scans popular Java repositories on GitHub (most stars first) and keeps
   the ones that contain a `module-info.java` file (modular projects).
2) Searches each modular repository's issues for JPMS-related discussions.
3) Saves the results to CSV and JSON (and can resume if interrupted).

Requirements:
    pip install requests

Usage:
    export GITHUB_TOKEN=ghp_xxx          # Linux / macOS
    set GITHUB_TOKEN=ghp_xxx             # Windows (cmd)
    python jpms_miner.py --scan 10000 --min-stars 50
"""

import argparse
import csv
import json
import os
import sys
import time

import requests

API = "https://api.github.com"

# Keywords that indicate an issue is about the Java Platform Module System
JPMS_KEYWORDS = [
    "JPMS",
    '"module-info"',
    '"module system"',
    "jigsaw",
    '"module path"',
    '"automatic module"',
    '"split package"',
    '"Automatic-Module-Name"',
    "modulepath",
    '"named module"',
]

# GitHub allows at most 5 AND/OR/NOT operators per search query,
# so the keywords are searched in small groups.
KEYWORDS_PER_QUERY = 4

CSV_FIELDS = ["repo", "stars", "issue_number", "title", "state", "url",
              "created_at", "comments", "labels", "matched_keywords"]


class GitHubClient:
    def __init__(self, token):
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        })
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"

    def get(self, url, params=None):
        """GET request with automatic waiting when the rate limit is hit."""
        while True:
            try:
                resp = self.session.get(url, params=params, timeout=60)
            except requests.RequestException as e:
                print(f"  ⚠️ Network error ({e}), retrying in 10 seconds...")
                time.sleep(10)
                continue

            if resp.status_code in (403, 429):
                remaining = resp.headers.get("X-RateLimit-Remaining")
                reset = resp.headers.get("X-RateLimit-Reset")
                retry_after = resp.headers.get("Retry-After")
                if retry_after:
                    wait = int(retry_after)
                elif remaining == "0" and reset:
                    wait = max(int(reset) - int(time.time()), 0) + 2
                else:
                    wait = 60
                print(f"  ⏳ Rate limit reached, waiting {wait} seconds...")
                time.sleep(wait)
                continue

            if resp.status_code == 422:
                print(f"  ⚠️ GitHub rejected the query: "
                      f"{resp.json().get('message', '')} {params}")
                return None

            if resp.status_code in (404, 409, 451):  # not found / empty / blocked
                return None

            if resp.status_code >= 500:
                print(f"  ⚠️ GitHub server error {resp.status_code}, retrying...")
                time.sleep(10)
                continue

            resp.raise_for_status()
            return resp.json()


def has_module_info(gh, repo):
    """Check the repo's file tree for a module-info.java file."""
    tree = gh.get(f"{API}/repos/{repo['full_name']}/git/trees/"
                  f"{repo['default_branch']}", {"recursive": "1"})
    if not tree:
        return None
    for item in tree.get("tree", []):
        if item["path"].endswith("module-info.java"):
            return item["path"]

    # Very large repos return a truncated tree; fall back to code search
    if tree.get("truncated"):
        data = gh.get(f"{API}/search/code",
                      {"q": f"repo:{repo['full_name']} filename:module-info.java"})
        time.sleep(6)  # code search allows ~10 requests per minute
        if data and data.get("items"):
            return data["items"][0]["path"]
    return None


def iterate_java_repos(gh, min_stars, upper_stars=None):
    """Yield Java repos from most to least stars.

    GitHub returns max 1000 results per query, so the star range is moved
    down step by step (e.g. stars 5000..80000, then 3000..5000, ...).
    """
    while True:
        stars = f"{min_stars}..{upper_stars}" if upper_stars else f">={min_stars}"
        query = f"language:Java stars:{stars} fork:false archived:false"
        print(f"\n🔍 Searching repositories: {query}")

        lowest = None
        for page in range(1, 11):
            data = gh.get(f"{API}/search/repositories",
                          {"q": query, "sort": "stars", "order": "desc",
                           "per_page": 100, "page": page})
            time.sleep(2.1)  # search API allows ~30 requests per minute
            if not data or not data.get("items"):
                break
            for repo in data["items"]:
                lowest = repo["stargazers_count"]
                yield repo
            if len(data["items"]) < 100:
                return  # no more repos in this range → finished

        if lowest is None or lowest <= min_stars:
            return
        # next window: everything below (and including) the lowest star count
        upper_stars = lowest if lowest != upper_stars else lowest - 1


def scan_repositories(gh, progress, progress_file, scan_limit, min_stars):
    """Step 1: scan repos and keep those with module-info.java."""
    scanned = set(progress["scanned"])
    found_names = {r["repo"] for r in progress["repos"]}
    upper = progress.get("last_stars")

    for repo in iterate_java_repos(gh, min_stars, upper):
        name = repo["full_name"]
        if name in scanned:
            continue
        if len(scanned) >= scan_limit:
            break

        scanned.add(name)
        progress["scanned"].append(name)
        progress["last_stars"] = repo["stargazers_count"]

        if repo["has_issues"] and name not in found_names:
            path = has_module_info(gh, repo)
            if path:
                found_names.add(name)
                progress["repos"].append({
                    "repo": name,
                    "url": repo["html_url"],
                    "stars": repo["stargazers_count"],
                    "module_info_path": path,
                })
                print(f"   ✔ {name} ({repo['stargazers_count']} ⭐)")

        if len(scanned) % 50 == 0:
            print(f"   ... scanned {len(scanned)}/{scan_limit}, "
                  f"modular: {len(progress['repos'])}")
            save_progress(progress_file, progress)

    progress["scan_finished"] = True
    save_progress(progress_file, progress)


def search_jpms_issues(gh, repo):
    """Step 2: search a repo's issues for JPMS-related keywords."""
    found = {}
    groups = [JPMS_KEYWORDS[i:i + KEYWORDS_PER_QUERY]
              for i in range(0, len(JPMS_KEYWORDS), KEYWORDS_PER_QUERY)]

    for group in groups:
        query = f"repo:{repo['repo']} is:issue ({' OR '.join(group)})"
        for page in range(1, 11):
            data = gh.get(f"{API}/search/issues",
                          {"q": query, "per_page": 100, "page": page})
            time.sleep(2.1)  # search API allows ~30 requests per minute
            if not data or not data.get("items"):
                break

            for issue in data["items"]:
                if issue["number"] in found:
                    continue
                text = f"{issue['title']} {issue.get('body') or ''}".lower()
                matched = [k.strip('"') for k in JPMS_KEYWORDS
                           if k.strip('"').lower() in text]
                found[issue["number"]] = {
                    "repo": repo["repo"],
                    "stars": repo["stars"],
                    "issue_number": issue["number"],
                    "title": issue["title"],
                    "state": issue["state"],
                    "url": issue["html_url"],
                    "created_at": issue["created_at"],
                    "comments": issue["comments"],
                    "labels": ", ".join(l["name"] for l in issue["labels"]),
                    "matched_keywords": ", ".join(matched),
                }
            if len(data["items"]) < 100:
                break
    return list(found.values())


def load_progress(path):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if "scanned" in data:  # ignore files from older versions
            return data
    return {"scanned": [], "repos": [], "scan_finished": False,
            "done": [], "issues": []}


def save_progress(path, progress):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(progress, f, ensure_ascii=False, indent=2)


def save_csv(path, rows, fields):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser(description="Mine JPMS-related issues from GitHub")
    parser.add_argument("--scan", type=int, default=1000,
                        help="how many Java repos to scan (default: 1000)")
    parser.add_argument("--min-stars", type=int, default=50,
                        help="only scan repos with at least this many stars (default: 50)")
    parser.add_argument("--out", default="jpms_results",
                        help="output file name prefix (default: jpms_results)")
    args = parser.parse_args()

    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        sys.exit("❌ Please set the GITHUB_TOKEN environment variable first.")

    gh = GitHubClient(token)
    progress_file = f"{args.out}.json"
    progress = load_progress(progress_file)

    # Step 1: find modular repositories
    if not progress["scan_finished"] or len(progress["scanned"]) < args.scan:
        progress["scan_finished"] = False
        scan_repositories(gh, progress, progress_file, args.scan, args.min_stars)

    repos = progress["repos"]
    save_csv(f"{args.out}_repos.csv", repos,
             ["repo", "url", "stars", "module_info_path"])
    print(f"\n📦 Scanned {len(progress['scanned'])} repos → "
          f"{len(repos)} modular (have module-info.java)\n")

    # Step 2: search issues in each modular repository
    for i, repo in enumerate(repos, 1):
        if repo["repo"] in progress["done"]:
            continue
        print(f"[{i}/{len(repos)}] {repo['repo']}", end=" ... ", flush=True)
        issues = search_jpms_issues(gh, repo)
        print(f"{len(issues)} JPMS issues")

        progress["issues"].extend(issues)
        progress["done"].append(repo["repo"])
        save_progress(progress_file, progress)  # save after every repo

    # Step 3: save final results
    save_csv(f"{args.out}.csv", progress["issues"], CSV_FIELDS)
    repos_with_issues = len({x["repo"] for x in progress["issues"]})
    print(f"\n✅ Done! {len(progress['issues'])} issues from "
          f"{repos_with_issues} repositories")
    print(f"   → {args.out}.csv        (JPMS issues, open in Excel)")
    print(f"   → {args.out}_repos.csv  (modular repositories)")
    print(f"   → {args.out}.json       (full data + progress)")


if __name__ == "__main__":
    main()