"""
JPMS Issue Miner In Github
----------------
1) Finds Java repositories on GitHub that contain a `module-info.java` file.
2) Searches each repository's issues for JPMS-related discussions.
3) Saves the results to CSV and JSON (and can resume if interrupted).

Requirements:
    pip install requests

Usage:
    export GITHUB_TOKEN=ghp_xxx          # Linux / macOS
    set GITHUB_TOKEN=ghp_xxx             # Windows (cmd)
    python jpms_miner.py --max-repos 100
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
    "module-info",
    '"module system"',
    "jigsaw",
    '"module path"',
    '"automatic module"',
    '"split package"',
    "Automatic-Module-Name",
]

# Size buckets (bytes) used to get past GitHub's 1000-results-per-query limit
SIZE_BUCKETS = ["<200", "200..400", "400..700", "700..1200", "1200..2500", ">2500"]


class GitHubClient:
    def __init__(self, token):
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        })

    def get(self, url, params=None):
        """GET request with automatic waiting when the rate limit is hit."""
        while True:
            resp = self.session.get(url, params=params, timeout=30)

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

            if resp.status_code == 422:  # e.g. repo is private/blocked for search
                return None

            resp.raise_for_status()
            return resp.json()


def find_repos_with_module_info(gh, max_repos):
    """Step 1: code search for module-info.java files and collect unique repos."""
    repos = {}
    for size in SIZE_BUCKETS:
        query = f"filename:module-info.java language:Java size:{size}"
        print(f"🔍 Searching code: {query}")
        page = 1
        while page <= 10:  # GitHub returns at most 1000 results (10 pages x 100)
            data = gh.get(f"{API}/search/code",
                          {"q": query, "per_page": 100, "page": page})
            time.sleep(7)  # code search allows ~10 requests per minute
            if not data or not data.get("items"):
                break

            for item in data["items"]:
                repo = item["repository"]
                if repo["fork"]:
                    continue
                full_name = repo["full_name"]
                if full_name not in repos:
                    repos[full_name] = {
                        "repo": full_name,
                        "url": repo["html_url"],
                        "module_info_path": item["path"],
                    }
                    if len(repos) >= max_repos:
                        return list(repos.values())

            print(f"   page {page}: {len(repos)} unique repos so far")
            if len(data["items"]) < 100:
                break
            page += 1
    return list(repos.values())


def search_jpms_issues(gh, repo_full_name):
    """Step 2: search a repo's issues for JPMS-related keywords."""
    keywords = " OR ".join(JPMS_KEYWORDS)
    query = f"repo:{repo_full_name} is:issue ({keywords})"
    results = []
    page = 1
    while True:
        data = gh.get(f"{API}/search/issues",
                      {"q": query, "per_page": 100, "page": page})
        time.sleep(2.1)  # issue search allows ~30 requests per minute
        if not data or not data.get("items"):
            break

        for issue in data["items"]:
            text = f"{issue['title']} {issue.get('body') or ''}".lower()
            matched = [k.strip('"') for k in JPMS_KEYWORDS
                       if k.strip('"').lower() in text]
            results.append({
                "repo": repo_full_name,
                "issue_number": issue["number"],
                "title": issue["title"],
                "state": issue["state"],
                "url": issue["html_url"],
                "created_at": issue["created_at"],
                "comments": issue["comments"],
                "labels": ", ".join(l["name"] for l in issue["labels"]),
                "matched_keywords": ", ".join(matched),
            })

        if len(data["items"]) < 100 or page >= 10:
            break
        page += 1
    return results


def load_progress(path):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {"repos": [], "done": [], "issues": []}


def save_progress(path, progress):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(progress, f, ensure_ascii=False, indent=2)


def save_csv(path, issues):
    if not issues:
        return
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=issues[0].keys())
        writer.writeheader()
        writer.writerows(issues)


def main():
    parser = argparse.ArgumentParser(description="Mine JPMS-related issues from GitHub")
    parser.add_argument("--max-repos", type=int, default=100,
                        help="max number of repos to collect (default: 100)")
    parser.add_argument("--out", default="jpms_results",
                        help="output file name prefix (default: jpms_results)")
    args = parser.parse_args()

    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        sys.exit("❌ Please set the GITHUB_TOKEN environment variable first.")

    gh = GitHubClient(token)
    progress_file = f"{args.out}.json"
    progress = load_progress(progress_file)

    # Step 1: find repositories (skipped if already found in a previous run)
    if not progress["repos"]:
        progress["repos"] = find_repos_with_module_info(gh, args.max_repos)
        save_progress(progress_file, progress)
    print(f"\n📦 {len(progress['repos'])} repositories with module-info.java\n")

    # Step 2: search issues in each repository
    for i, repo in enumerate(progress["repos"], 1):
        name = repo["repo"]
        if name in progress["done"]:
            continue
        print(f"[{i}/{len(progress['repos'])}] {name}", end=" ... ")
        issues = search_jpms_issues(gh, name)
        print(f"{len(issues)} JPMS issues")

        progress["issues"].extend(issues)
        progress["done"].append(name)
        save_progress(progress_file, progress)  # save after every repo

    # Step 3: save final results
    save_csv(f"{args.out}.csv", progress["issues"])
    repos_with_issues = len({x["repo"] for x in progress["issues"]})
    print(f"\n✅ Done! {len(progress['issues'])} issues from "
          f"{repos_with_issues} repositories")
    print(f"   → {args.out}.csv  (open in Excel)")
    print(f"   → {args.out}.json (full data + progress)")


if __name__ == "__main__":
    main()
