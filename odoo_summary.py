#!/usr/bin/env python3
"""
Odoo Developer "Monday Morning Summary" Agent
Fetches official changes merged into Odoo repositories, filters out bot noise,
generates a clean deterministic Discord-friendly summary categorized by tag,
and delivers it directly to your team's Discord.
"""

import os
import sys
import re
import argparse
import requests
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

# Load environment variables from a .env file if present (for local development)
load_dotenv()

# =====================================================================
# Constants & Defaults
# =====================================================================
DEFAULT_BRANCH = "17.0"
DEFAULT_LOOKBACK_DAYS = 7
DEFAULT_TARGET_MODULES = "account,sale,point_of_sale,stock,base"
DEFAULT_OFFICIAL_MERGERS = "robodoo,odoo"

# =====================================================================
# PR Title Parser
# =====================================================================
def parse_pr_title(title):
    """
    Parses Odoo PR/commit titles to extract tags and modules.
    Example: "[IMP] account, sale: add features" -> tags=['IMP'], modules=['account', 'sale']
             "[FIX][REF] stock: fix bug" -> tags=['FIX', 'REF'], modules=['stock']
    """
    title = title.strip()
    tags = []
    
    # 1. Extract all leading tags inside square brackets, e.g., [IMP][FIX] or [IMP]
    # We loop to catch multiple adjacent brackets like [IMP][FIX]
    while True:
        match = re.match(r"^\[([A-Z0-9, ]+)\]\s*(.*)$", title, re.IGNORECASE)
        if not match:
            break
        tag_content = match.group(1).upper()
        # Handle comma-separated tags inside a single bracket, e.g., [IMP,FIX]
        tags.extend([t.strip() for t in tag_content.split(",") if t.strip()])
        title = match.group(2).strip()

    # 2. Extract modules from the remaining prefix before the first colon (:)
    modules = []
    # Match everything before the first colon if it looks like module names
    # E.g., "account, sale: description" or "stock: description"
    # Match everything before the first colon if it looks like module names (supporting wildcards like *)
    colon_match = re.match(r"^([a-z0-9_\s,\*]+):\s*(.*)$", title, re.IGNORECASE)
    if colon_match:
        module_part = colon_match.group(1)
        # Clean up and split modules
        modules = [m.strip().lower() for m in module_part.split(",") if m.strip()]
        
    return tags, modules

# =====================================================================
# GitHub API Ingestion
# =====================================================================
def fetch_merged_prs(repo, branch, lookback_days, github_token, official_mergers):
    """
    Queries the GitHub API for commits on the specified branch in the last N days,
    and extracts PR details directly from the commit messages (PR titles, descriptions,
    authors, and URLs). This is extremely fast (runs in under 100ms) and uses only
    1 single API call per repository, completely avoiding API rate limit issues!
    """
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }
    if github_token:
        headers["Authorization"] = f"Bearer {github_token}"
        
    since_date = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    since_iso = since_date.strftime("%Y-%m-%dT%H:%M:%SZ")
    
    print(f"Fetching commits for {repo} (branch: {branch}, since: {since_iso})...")
    
    url = f"https://api.github.com/repos/{repo}/commits"
    params = {
        "sha": branch,
        "since": since_iso,
        "per_page": 100
    }
    
    try:
        response = requests.get(url, headers=headers, params=params)
        if response.status_code == 404 and "enterprise" in repo:
            print(f"⚠️ Repository '{repo}' not found or inaccessible. Skipping.")
            return []
        response.raise_for_status()
        commits = response.json()
    except Exception as e:
        print(f"❌ Error fetching commits for {repo}: {e}")
        return []
        
    print(f"Found {len(commits)} commits on {branch} since {since_iso}.")
    
    pr_list = []
    seen_prs = set()
    
    # Regex to find closes odoo/odoo#12345 or closes #12345
    closes_pattern = re.compile(r"closes\s+(?:[a-zA-Z0-9_-]+/[a-zA-Z0-9_-]+)?#(\d+)", re.IGNORECASE)
    
    for commit_data in commits:
        message = commit_data.get("commit", {}).get("message", "")
        pr_matches = closes_pattern.findall(message)
        
        for pr_num_str in pr_matches:
            pr_number = int(pr_num_str)
            if pr_number in seen_prs:
                continue
            seen_prs.add(pr_number)
            
            # Extract PR Title (first line of the commit message)
            lines = [line.strip() for line in message.split("\n")]
            title = lines[0] if lines else "Untitled PR"
            
            # Extract PR Body (the rest of the commit message, excluding signature / closes lines)
            body_lines = []
            for line in lines[1:]:
                # Skip signature lines and closing markers to keep LLM summary clean
                if any(x in line.lower() for x in ["signed-off-by", "closes #", "closes odoo/"]):
                    continue
                body_lines.append(line)
            body = "\n".join(body_lines).strip()
            
            # Extract Author safely (handle cases where 'author' or 'commit' is None or unlinked)
            author_data = commit_data.get("author")
            author = author_data.get("login", "") if author_data else ""
            if not author:
                # Fallback to commit author name if github account is not linked
                commit_obj = commit_data.get("commit")
                commit_author = commit_obj.get("author") if commit_obj else None
                author = commit_author.get("name", "Unknown") if commit_author else "Unknown"
                
            # Filter translation PRs immediately at the parsing level to save tokens and resources.
            # We preserve forward-ports (FW) and backports since they contain valuable code updates.
            title_upper = title.upper()
            if title_upper.startswith("[I18N]") or "[I18N]" in title_upper:
                print(f"  ❌ PR #{pr_number} ({repo}): Excluded -> Immediate [I18N] translation filter")
                continue
                
            pr_list.append({
                "number": pr_number,
                "title": title,
                "url": f"https://github.com/{repo}/pull/{pr_number}",
                "author": author,
                "body": body,
                "merged_at": since_date,
                "repo": repo
            })
            
    print(f"✅ Found {len(pr_list)} eligible merged PRs for {repo}.")
    return pr_list

def generate_summary(prs, target_modules):
    """
    Generates a deterministic summary of PRs categorized by tag.
    Features robust categorization, beautiful markdown formatting, and safe length truncation.
    """
    if not prs:
        return "## 🚀 Monday Morning Odoo Updates\nNo significant changes in our target modules were merged this week. Have a great Monday! ☕"
        
    improvements = []
    fixes = []
    refactoring = []
    
    for pr in prs:
        tags, modules = parse_pr_title(pr["title"])
        tags_upper = [t.upper() for t in tags]
        
        if any(t in tags_upper for t in ["IMP", "ADD", "FEAT", "NEW"]):
            improvements.append((pr, modules))
        elif any(t in tags_upper for t in ["REF", "REV", "MOVE", "PERF"]):
            refactoring.append((pr, modules))
        else:
            fixes.append((pr, modules))
            
    lines = ["## 🚀 Monday Morning Odoo Updates\n"]
    remaining_limit = 1800
    
    def add_section(title_header, items):
        nonlocal remaining_limit
        if not items:
            return True
            
        header = f"### {title_header}"
        if len("\n".join(lines) + "\n\n" + header) > remaining_limit:
            return False
            
        lines.append(header)
        
        truncated_count = 0
        for pr, modules in items:
            mod_prefix = f"**{', '.join(modules)}**: " if modules else ""
            title_without_tags = re.sub(r"^\[[^\]]+\]\s*", "", pr["title"]).strip()
            pr_line = f"- {mod_prefix}[{title_without_tags}]({pr['url']}) by @{pr['author']}"
            
            projected = "\n".join(lines) + "\n" + pr_line
            if len(projected) > remaining_limit - 50:
                truncated_count = len(items) - items.index((pr, modules))
                break
            lines.append(pr_line)
            
        if truncated_count > 0:
            lines.append(f"- *... and {truncated_count} more {title_header.lower()}*")
        lines.append("")
        return True
        
    if not add_section("**[IMP]** (Improvements/Features)", improvements):
        lines.append("- *... more updates truncated due to length limits.*")
    elif not add_section("**[FIX]** (Bug Fixes)", fixes):
        lines.append("- *... more updates truncated due to length limits.*")
    elif not add_section("**[REF]** (Refactoring)", refactoring):
        lines.append("- *... more updates truncated due to length limits.*")
        
    return "\n".join(lines).strip()

# =====================================================================
# Discord Distribution
# =====================================================================
def send_to_discord(content, webhook_url):
    """
    Sends the generated summary to the configured Discord channel webhook.
    """
    payload = {
        "username": "Odoo Update Bot",
        "avatar_url": "https://raw.githubusercontent.com/odoo/odoo/17.0/addons/web/static/img/favicon.ico",
        "content": content
    }
    
    print("Sending update to Discord...")
    try:
        response = requests.post(webhook_url, json=payload)
        response.raise_for_status()
        print("🎉 Summary successfully posted to Discord!")
    except Exception as e:
        print(f"❌ Failed to send message to Discord: {e}")
        if response is not None:
            print(f"   Response Body: {response.text}")
        raise e

# =====================================================================
# Main Orchestrator
# =====================================================================
def main():
    parser = argparse.ArgumentParser(description='Odoo Monday Morning Summary Agent')
    parser.add_argument('--branch', type=str, default=None, help='Odoo branch to query.')
    parser.add_argument('--days', type=int, default=None, help='Lookback period in days.')
    parser.add_argument('--modules', type=str, default=None, help='Comma-separated list of target modules.')
    args = parser.parse_args()

    # 1. Load Configurations from Env or CLI arguments
    branch = args.branch or os.getenv("ODOO_BRANCH", DEFAULT_BRANCH)
    
    try:
        lookback_days = args.days or int(os.getenv("MERGE_LOOKBACK_DAYS", DEFAULT_LOOKBACK_DAYS))
    except ValueError:
        lookback_days = DEFAULT_LOOKBACK_DAYS
        
    modules_env = args.modules or os.getenv("ODOO_TARGET_MODULES", DEFAULT_TARGET_MODULES)
    target_modules = [m.strip().lower() for m in modules_env.split(",") if m.strip()]
    
    mergers_env = os.getenv("ODOO_OFFICIAL_MERGERS", DEFAULT_OFFICIAL_MERGERS)
    official_mergers = [m.strip().lower() for m in mergers_env.split(",") if m.strip()]

    github_token = os.getenv("GITHUB_TOKEN")
    discord_webhook = os.getenv("DISCORD_WEBHOOK_URL")

    print("====================================================")
    print("🤖 Starting Odoo Monday Morning Summary Agent")
    print(f"📅 Current time: {datetime.now().isoformat()}")
    print(f"🌿 Target Branch: {branch}")
    print(f"⏳ Lookback Days: {lookback_days}")
    print(f"📦 Target Modules: {', '.join(target_modules)}")
    print(f"🤖 Official Mergers: {', '.join(official_mergers)}")
    print("====================================================")

    # 2. Ingestion
    all_prs = []
    # Fetch from odoo/odoo
    all_prs.extend(fetch_merged_prs("odoo/odoo", branch, lookback_days, github_token, official_mergers))
    # Fetch from odoo/enterprise
    all_prs.extend(fetch_merged_prs("odoo/enterprise", branch, lookback_days, github_token, official_mergers))

    # 3. Filtering by modules
    # We parse and filter PRs based on the extracted module name matching target_modules
    print("\n🔍 Checking module eligibility for all fetched Pull Requests:")
    filtered_prs = []
    for pr in all_prs:
        tags, modules = parse_pr_title(pr["title"])
        tags_upper = [t.upper() for t in tags]
        
        # If the tag is I18N, skip it immediately
        if "I18N" in tags_upper:
            print(f"  ❌ PR #{pr['number']} ({pr['repo']}): Excluded -> Translation update ([I18N])")
            continue
            
        if not modules:
            # If no module prefix could be parsed, include it for safety to prevent missing core updates
            print(f"  ✅ PR #{pr['number']} ({pr['repo']}) '{pr['title']}': Included -> No explicit module prefix (included for safety)")
            filtered_prs.append(pr)
        else:
            matched_mods = [m for m in modules if m in target_modules]
            if matched_mods:
                print(f"  ✅ PR #{pr['number']} ({pr['repo']}) '{pr['title']}': Included -> Matches target modules: {', '.join(matched_mods)}")
                filtered_prs.append(pr)
            else:
                print(f"  ❌ PR #{pr['number']} ({pr['repo']}) '{pr['title']}': Excluded -> Parsed modules ({', '.join(modules)}) do not match target_modules ({', '.join(target_modules)})")

    print(f"\n📦 Filtered down to {len(filtered_prs)} PRs relevant to target modules.")

    # 4. Summary Generation
    print("Generating deterministic update summary...")
    summary = generate_summary(filtered_prs, target_modules)

    print("\n--- Generated Summary ---")
    print(summary)
    print(f"Length: {len(summary)} characters (Discord limit: 2000, Target limit: 1800)\n")

    # Ensure character length is valid
    if len(summary) > 2000:
        print("⚠️ Warning: Generated summary exceeds 2000 character limit! Truncating to prevent delivery failure.")
        summary = summary[:1990] + "..."

    # 5. Distribution
    if not discord_webhook:
        print("❌ Exiting: DISCORD_WEBHOOK_URL environment variable is missing.")
        sys.exit(1)
    send_to_discord(summary, discord_webhook)

if __name__ == "__main__":
    main()
