# JPMS Issue Miner

A Python script that mines GitHub for Java projects using the
**Java Platform Module System (JPMS)** and collects issues related to it.

## How it works
1. Finds Java repositories that contain a `module-info.java` file (GitHub Code Search).
2. Searches each repository's issues for JPMS-related keywords
   (`JPMS`, `module-info`, `jigsaw`, `split package`, ...).
3. Saves the results to `jpms_results.csv` and `jpms_results.json`.

## Setup
```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

Create a GitHub Personal Access Token (no special scopes needed) and set it:
```bash
export GITHUB_TOKEN=your_token   # Windows (cmd): set GITHUB_TOKEN=your_token
```

## Usage
```bash
python jpms_miner.py --max-repos 50
python jpms_miner.py --max-repos 200 --out my_results
```

If the script is interrupted, run it again — it resumes from where it stopped.
Delete the `.json` output file to start a fresh search.
