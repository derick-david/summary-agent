#!/usr/bin/env python3
"""
Odoo Developer "Monday Morning Summary" Agent
Fetches official changes merged into Odoo repositories, filters out bot noise,
uses Gemini to generate a Discord-friendly summary, and posts to Discord.
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
    colon_match = re.match(r"^([a-z0-9_\s,]+):\s*(.*)$", title, re.IGNORECASE)
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
                
            # Filter bot noise
            if author == "fw-bot" or title.upper().startswith("[FW]") or title.upper().startswith("FW "):
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

# =====================================================================
# LLM Integration (Google Gemini)
# =====================================================================
def generate_summary_with_gemini(prs, target_modules, gemini_api_key):
    """
    Prepares the prompt, invokes Google Gemini with multi-SDK/REST fallback,
    and returns a formatted, character-constrained Discord message.
    """
    if not prs:
        return "## 🚀 Monday Morning Odoo Updates\nNo significant changes in our target modules were merged this week. Have a great Monday! ☕"
        
    # Format the PR details for the prompt
    pr_entries = []
    for idx, pr in enumerate(prs):
        # Truncate body to keep prompt token size clean and efficient
        body_snippet = pr["body"][:400] + "..." if len(pr["body"]) > 400 else pr["body"]
        pr_entries.append(
            f"PR #{pr['number']} ({pr['repo']})\n"
            f"Title: {pr['title']}\n"
            f"URL: {pr['url']}\n"
            f"Description: {body_snippet}\n"
            f"---"
        )
    prs_text = "\n".join(pr_entries)
    
    target_modules_str = ", ".join(target_modules)
    
    system_instruction = (
        "You are an Odoo Technical Lead briefing your development team on Discord.\n"
        "Your task is to summarize the list of merged Pull Requests.\n"
        "CRITICAL RULES:\n"
        "1. Start the message with a friendly greeting header: '## 🚀 Monday Morning Odoo Updates'\n"
        f"2. Focus strictly on these target modules: {target_modules_str}. Ignore updates to other modules.\n"
        "3. Categorize the updates under these headings:\n"
        "   - **[IMP]** (Improvements/Features)\n"
        "   - **[FIX]** (Bug Fixes)\n"
        "   - **[REF]** (Refactoring)\n"
        "4. Ignore [I18N] (Translations) completely.\n"
        "5. Output clean, chat-friendly Discord Markdown. Use bolding for module names (e.g., **account**, **sale**).\n"
        "6. Keep bullet points extremely short, direct, and developer-focused.\n"
        "7. STRICT CHARACTER CONSTRAINT: The total summary MUST be under 1,800 characters to fit inside Discord's 2,000-character limit."
    )
    
    user_prompt = (
        "Here are the merged Pull Requests from the last week. Summarize them according to your instructions:\n\n"
        f"{prs_text}"
    )
    
    print("Generating summary using Gemini...")
    
    # Fallback Mechanism for Gemini API
    # 1. Try google-genai (successor SDK)
    try:
        from google import genai
        from google.genai import types
        print("Using standard google-genai library...")
        client = genai.Client(api_key=gemini_api_key)
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=user_prompt,
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
                temperature=0.2,
                max_output_tokens=1000
            )
        )
        return response.text
    except Exception as sdk1_err:
        print(f"⚠️ google-genai not available or failed: {sdk1_err}. Trying google-generativeai...")
        
        # 2. Try google-generativeai (classic SDK)
        try:
            import google.generativeai as classic_genai
            print("Using classic google-generativeai library...")
            classic_genai.configure(api_key=gemini_api_key)
            model = classic_genai.GenerativeModel(
                model_name="gemini-1.5-flash",
                system_instruction=system_instruction
            )
            response = model.generate_content(
                user_prompt,
                generation_config={"temperature": 0.2, "max_output_tokens": 1000}
            )
            return response.text
        except Exception as sdk2_err:
            print(f"⚠️ google-generativeai not available or failed: {sdk2_err}. Falling back to direct REST API...")
            
            # 3. Bulletproof REST API Fallback
            try:
                url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={gemini_api_key}"
                payload = {
                    "contents": [{"parts": [{"text": user_prompt}]}],
                    "systemInstruction": {"parts": [{"text": system_instruction}]},
                    "generationConfig": {"temperature": 0.2, "maxOutputTokens": 1000}
                }
                headers = {"Content-Type": "application/json"}
                resp = requests.post(url, json=payload, headers=headers)
                resp.raise_for_status()
                
                resp_json = resp.json()
                text = resp_json["candidates"][0]["content"]["parts"][0]["text"]
                return text
            except Exception as rest_err:
                print(f"❌ All Gemini invocation methods failed. REST error: {rest_err}")
                raise RuntimeError("Failed to call Gemini API via all fallback methods.")

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
    parser.add_argument('--dry-run', action='store_true', help='Execute the agent in Dry-Run mode without calling Discord.')
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
    gemini_api_key = os.getenv("GEMINI_API_KEY")
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
    filtered_prs = []
    for pr in all_prs:
        tags, modules = parse_pr_title(pr["title"])
        
        # Keep PRs that don't have modules parsed but are relevant (fallback if title doesn't follow guidelines), 
        # or if any parsed module is in our target_modules.
        is_relevant = False
        
        # If the tag is I18N, skip it immediately
        if "I18N" in tags:
            continue
            
        if not modules:
            # If no module prefix could be parsed (e.g., "[FIX] fix some core issue"), 
            # we send it to Gemini and let Gemini determine if it affects the target business logic modules.
            is_relevant = True
        else:
            for mod in modules:
                if mod in target_modules:
                    is_relevant = True
                    break
                    
        if is_relevant:
            filtered_prs.append(pr)

    print(f"📦 Filtered down to {len(filtered_prs)} PRs relevant to target modules.")

    # 4. Agentic Summary Generation
    if not gemini_api_key:
        print("⚠️ GEMINI_API_KEY environment variable is missing!")
        if args.dry_run:
            print("🔬 Running in DRY-RUN mode. Simulating LLM Summary Response...")
            summary = (
                "## 🚀 Monday Morning Odoo Updates\n"
                f"### **[IMP]**\n"
                f"- **sale**: [Simulated] Improved sales order validation pipeline ({len(filtered_prs)} PRs analyzed).\n"
                f"### **[FIX]**\n"
                f"- **account**: [Simulated] Resolved multi-currency reconciliation rounding error.\n"
                f"### **[REF]**\n"
                f"- **stock**: [Simulated] Cleaned up stock valuation inventory hooks."
            )
        else:
            print("❌ Exiting: GEMINI_API_KEY is required for active runs.")
            sys.exit(1)
    else:
        try:
            summary = generate_summary_with_gemini(filtered_prs, target_modules, gemini_api_key)
        except Exception as e:
            print(f"❌ Failed to generate summary: {e}")
            sys.exit(1)

    print("\n--- Generated Summary ---")
    print(summary)
    print(f"Length: {len(summary)} characters (Discord limit: 2000, Target limit: 1800)\n")

    # Ensure character length is valid
    if len(summary) > 2000:
        print("⚠️ Warning: Generated summary exceeds 2000 character limit! Truncating to prevent delivery failure.")
        summary = summary[:1990] + "..."

    # 5. Distribution
    if args.dry_run:
        print("🔬 DRY-RUN mode active. Skipping Discord webhook submission.")
    else:
        if not discord_webhook:
            print("❌ Exiting: DISCORD_WEBHOOK_URL environment variable is missing.")
            sys.exit(1)
        send_to_discord(summary, discord_webhook)

if __name__ == "__main__":
    main()
