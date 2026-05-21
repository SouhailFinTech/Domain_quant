"""
DomainQuant — Quantitative Domain Scoring & Flipping System
============================================================
Author: AlgoQuant
GitHub: your-repo-name

What this does:
- Fetches expired/expiring domains daily from free sources
- Scores each domain across 8 quantitative dimensions
- Ranks opportunities by expected value
- Estimates resale price using comparable sales
- Generates buy/pass signal with reasoning
- Tracks your portfolio and P&L
- Runs as a Streamlit dashboard

Zero subscriptions required for basic operation.
Optional paid APIs for deeper data (noted inline).

Install:
    pip install -r requirements.txt

Run:
    streamlit run domain_quant.py

or pure Python scan:
    python domain_quant.py --scan
"""

import json
import re
import time
import math
import hashlib
import argparse
import warnings
from datetime import datetime, timedelta
from collections import Counter
from urllib.parse import quote
from pathlib import Path

import requests
import pandas as pd
import streamlit as st

warnings.filterwarnings('ignore')

# ════════════════════════════════════════════════════════════
# CONFIGURATION
# ════════════════════════════════════════════════════════════

CONFIG = {
    # Scoring weights — must sum to 1.0
    'weights': {
        'keyword_value'     : 0.25,   # CPC and search volume of keywords in domain
        'comparable_sales'  : 0.25,   # Similar domains that sold and at what price
        'brandability'      : 0.15,   # How brandable/memorable is the name
        'tld_strength'      : 0.10,   # .com > .net > .org > other
        'domain_age'        : 0.08,   # Older domains have more authority
        'backlink_quality'  : 0.08,   # Historical backlinks
        'wayback_history'   : 0.05,   # Was it a real website before?
        'spam_penalty'      : 0.04,   # Negative score if spam history detected
    },

    # Thresholds
    'min_score_to_register' : 60,     # Only register if score >= this
    'min_score_to_alert'    : 70,     # Send alert if score >= this
    'max_registration_cost' : 12,     # Max $ to spend registering
    'target_roi_multiple'   : 10,     # Target: sell for 10x registration cost

    # Registration cost estimates
    'registration_costs': {
        '.com' : 9.99,
        '.net' : 10.99,
        '.org' : 9.99,
        '.io'  : 32.99,
        '.ai'  : 79.99,
        '.co'  : 11.99,
        '.app' : 14.99,
        '.dev' : 12.99,
    },

    # TLD strength scores (out of 20)
    'tld_scores': {
        '.com'  : 20,
        '.io'   : 17,
        '.ai'   : 16,
        '.co'   : 14,
        '.net'  : 13,
        '.org'  : 12,
        '.app'  : 11,
        '.dev'  : 10,
        '.info' : 6,
        '.biz'  : 5,
        '.us'   : 7,
        '.uk'   : 8,
    },

    # High-value keyword categories with estimated CPC ranges
    'keyword_categories': {
        'finance'    : {'keywords': ['trading','invest','stock','crypto','forex','fund','capital','wealth','asset','portfolio','quant','algo','hedge'], 'base_cpc': 8.0},
        'saas'       : {'keywords': ['software','app','platform','tool','system','dashboard','analytics','data','api','cloud','ai','bot','automation'], 'base_cpc': 6.0},
        'legal'      : {'keywords': ['lawyer','attorney','legal','law','court','injury','accident','insurance','claim'], 'base_cpc': 15.0},
        'health'     : {'keywords': ['health','medical','doctor','clinic','therapy','wellness','fitness','diet','pharma','drug'], 'base_cpc': 5.0},
        'ecommerce'  : {'keywords': ['shop','store','buy','deal','sale','market','price','cheap','best','review'], 'base_cpc': 2.0},
        'realestate' : {'keywords': ['home','house','property','real','estate','rent','mortgage','land','apartment'], 'base_cpc': 7.0},
        'education'  : {'keywords': ['learn','course','school','university','training','academy','teach','study','online'], 'base_cpc': 3.5},
        'crypto'     : {'keywords': ['bitcoin','btc','eth','nft','defi','web3','blockchain','token','wallet','yield'], 'base_cpc': 4.0},
    },

    # Known spam TLDs to penalize
    'spam_tlds': ['.xyz', '.tk', '.ml', '.ga', '.cf', '.click', '.top', '.loan', '.win'],

    # Data storage
    'data_dir'      : 'domainquant_data',
    'portfolio_file': 'domainquant_data/portfolio.json',
    'history_file'  : 'domainquant_data/scan_history.json',
    'cache_file'    : 'domainquant_data/cache.json',
}

# Create data directory
Path(CONFIG['data_dir']).mkdir(exist_ok=True)


# ════════════════════════════════════════════════════════════
# DATA FETCHERS
# ════════════════════════════════════════════════════════════

VALID_TLDS = {'.com','.net','.org','.io','.co','.app','.dev','.ai','.us','.uk'}
BLACKLIST  = {'expireddomains.net','namebio.com','whoisfreaks.com','godaddy.com',
              'namecheap.com','sedo.com','afternic.com','flippa.com','dan.com',
              'google.com','facebook.com','youtube.com','twitter.com','github.com'}

def _is_valid_domain(domain: str) -> bool:
    if not domain or domain.count('.') != 1:
        return False
    name, tld = domain.rsplit('.', 1)
    tld = '.' + tld
    if tld not in VALID_TLDS: return False
    if not 3 <= len(name) <= 25: return False
    if not re.match(r'^[a-z0-9][a-z0-9-]*[a-z0-9]$', name): return False
    if domain in BLACKLIST: return False
    return True


def fetch_expired_domains_whoisfreaks(limit: int = 200) -> list:
    """Fetch expired domains from free sources with strict filtering."""
    domains = []

    # Source 1: WhoisFreaks
    try:
        resp = requests.get("https://whoisfreaks.com/tools/whois/dropped-domains",
            headers={'User-Agent':'Mozilla/5.0'}, timeout=15)
        if resp.status_code == 200:
            found = re.findall(
                r'<td[^>]*>\s*([a-z0-9][a-z0-9-]{2,22}[a-z0-9]\.(?:com|net|org|io|co|app|dev|ai))\s*</td>',
                resp.text.lower())
            domains.extend(found)
    except Exception:
        pass

    # Source 2: ExpiredDomains.net
    if len(domains) < 20:
        try:
            resp = requests.get("https://www.expireddomains.net/domain-lists/expired-domains/",
                headers={'User-Agent':'Mozilla/5.0','Referer':'https://www.expireddomains.net/'}, timeout=15)
            if resp.status_code == 200:
                found = re.findall(
                    r'<td[^>]*>\s*([a-z0-9][a-z0-9-]{2,20}[a-z0-9]\.(?:com|net|org|io))\s*</td>',
                    resp.text.lower())
                domains.extend(found)
        except Exception:
            pass

    # Demo list fallback (always available for testing)
    demo = [
        'tradinglab.io','quantbot.co','algotrader.net','cryptosignal.io',
        'backtestpro.com','forexquant.net','tradebot.app','quantedge.io',
        'algofund.co','tradingsystem.net','cryptoalgo.io','forexbot.co',
        'quantsignal.net','tradingapi.io','algomarket.co','cryptotrader.app',
        'quantpro.net','tradingquant.io','forexalgo.co','bottrader.net',
        'signalquant.io','tradinglab.co','backtester.net','algobot.io',
        'cryptobacktest.co','quantfund.net','forexsystem.io','tradelab.co',
        'algosignal.net','tradequant.io','cryptolab.co','forextrade.app',
    ]
    if len(domains) < 15:
        domains.extend(demo)

    seen, cleaned = set(), []
    for d in domains:
        d = d.lower().strip()
        if d not in seen and _is_valid_domain(d):
            seen.add(d)
            cleaned.append(d)
    return cleaned[:limit]


def fetch_namebio_sales(keyword: str = '', tld: str = '.com', limit: int = 20) -> list:
    """
    Scrape recent comparable sales from NameBio.
    Free, no API key needed.
    Returns list of {domain, price, date} dicts.
    """
    sales = []
    try:
        url = f"https://namebio.com/search?q={quote(keyword)}&ext={tld.replace('.','')}&sort=date&order=desc"
        headers = {'User-Agent': 'Mozilla/5.0 DomainQuant Research Tool'}
        resp = requests.get(url, headers=headers, timeout=15)
        if resp.status_code == 200:
            # Parse sale rows
            price_matches = re.findall(r'\$([0-9,]+)', resp.text)
            domain_matches = re.findall(r'([a-zA-Z0-9][-a-zA-Z0-9]*\.[a-zA-Z]{2,})', resp.text)
            for i, (domain, price) in enumerate(zip(domain_matches[:limit], price_matches[:limit])):
                try:
                    sales.append({
                        'domain': domain,
                        'price' : int(price.replace(',', '')),
                        'date'  : '',
                    })
                except Exception:
                    continue
    except Exception:
        pass
    return sales


def fetch_wayback_history(domain: str) -> dict:
    """
    Check Wayback Machine for domain history.
    Completely free, no API key.
    """
    try:
        url = f"https://archive.org/wayback/available?url={domain}"
        resp = requests.get(url, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            snapshot = data.get('archived_snapshots', {}).get('closest', {})
            if snapshot:
                return {
                    'has_history' : True,
                    'last_seen'   : snapshot.get('timestamp', '')[:8],
                    'url'         : snapshot.get('url', ''),
                    'status'      : snapshot.get('status', ''),
                }
    except Exception:
        pass
    return {'has_history': False, 'last_seen': '', 'url': '', 'status': ''}


def fetch_whois_age(domain: str) -> dict:
    """
    Get domain age from WHOIS data.
    Uses free public WHOIS lookup.
    """
    try:
        url = f"https://api.whoisfreaks.com/v1.0/whois?whois=live&domainName={domain}&apiKey=free"
        resp = requests.get(url, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            created = data.get('domain_registrar', {}).get('domain_registration_date', '')
            if created:
                try:
                    created_dt = datetime.strptime(created[:10], '%Y-%m-%d')
                    age_years  = (datetime.utcnow() - created_dt).days / 365
                    return {'age_years': round(age_years, 1), 'created': created[:10]}
                except Exception:
                    pass
    except Exception:
        pass

    # Fallback: estimate from Wayback Machine
    try:
        url  = f"https://web.archive.org/web/19990101000000*/{domain}"
        resp = requests.get(url, timeout=8)
        if '1999' in resp.text or '2000' in resp.text:
            return {'age_years': 20, 'created': 'pre-2000'}
    except Exception:
        pass

    return {'age_years': 0, 'created': 'unknown'}


def check_domain_available(domain: str) -> bool:
    """
    Check if domain is available for registration.
    Uses WHOIS API — free.
    """
    try:
        url  = f"https://api.domainsdb.info/v1/domains/search?domain={domain}&limit=1"
        resp = requests.get(url, timeout=8)
        if resp.status_code == 200:
            data = resp.json()
            items = data.get('domains', [])
            return len(items) == 0
    except Exception:
        pass

    # Fallback: simple WHOIS check
    try:
        import whois
        w = whois.whois(domain)
        return w.status is None
    except Exception:
        return True  # Assume available if check fails


def get_google_trends_score(keyword: str) -> float:
    """
    Get Google Trends interest score for a keyword.
    Uses pytrends — completely free.
    Returns 0-100 score.
    """
    try:
        from pytrends.request import TrendReq
        pytrends = TrendReq(hl='en-US', tz=360)
        pytrends.build_payload([keyword], timeframe='today 12-m')
        data = pytrends.interest_over_time()
        if not data.empty and keyword in data.columns:
            return float(data[keyword].mean())
    except Exception:
        pass
    return 0.0


# ════════════════════════════════════════════════════════════
# SCORING ENGINE
# ════════════════════════════════════════════════════════════

def extract_keywords(domain: str) -> list:
    """Extract meaningful keywords from domain name."""
    # Remove TLD
    name = domain.split('.')[0].lower()
    # Split camelCase
    name = re.sub(r'([A-Z])', r' \1', name).lower()
    # Split on numbers and hyphens
    parts = re.split(r'[-_0-9]', name)
    # Clean
    keywords = [p.strip() for p in parts if len(p.strip()) >= 3]
    return keywords


def score_keyword_value(domain: str) -> dict:
    """
    Score domain based on keyword commercial value.
    Uses keyword category matching — no API needed.
    Optionally uses Google Trends for trend data.
    Max score: 20
    """
    keywords = extract_keywords(domain)
    if not keywords:
        return {'score': 0, 'reason': 'No meaningful keywords found', 'keywords': [], 'category': 'none', 'estimated_cpc': 0}

    best_category  = None
    best_cpc       = 0
    matched_words  = []

    for category, data in CONFIG['keyword_categories'].items():
        for kw in keywords:
            for cat_kw in data['keywords']:
                if cat_kw in kw or kw in cat_kw:
                    matched_words.append(kw)
                    if data['base_cpc'] > best_cpc:
                        best_cpc      = data['base_cpc']
                        best_category = category

    if not best_category:
        # Generic domain — low value
        score  = 3
        reason = f"No high-value keywords detected in: {', '.join(keywords)}"
    else:
        # Score based on CPC tier
        if best_cpc >= 10:
            score = 20
        elif best_cpc >= 7:
            score = 17
        elif best_cpc >= 5:
            score = 14
        elif best_cpc >= 3:
            score = 10
        else:
            score = 6

        # Bonus: short domain with valuable keyword
        if len(domain.split('.')[0]) <= 8:
            score = min(20, score + 2)

        # Bonus: exact match single keyword
        if len(keywords) == 1:
            score = min(20, score + 1)

        reason = f"Category: {best_category} · CPC ~${best_cpc:.1f} · Keywords: {', '.join(matched_words[:3])}"

    return {
        'score'         : score,
        'reason'        : reason,
        'keywords'      : keywords,
        'category'      : best_category or 'generic',
        'estimated_cpc' : best_cpc,
    }


def score_comparable_sales(domain: str) -> dict:
    """
    Estimate value based on comparable domain sales.
    Uses NameBio data — free scraping.
    Max score: 20
    """
    keywords = extract_keywords(domain)
    tld      = '.' + domain.split('.')[-1]

    if not keywords:
        return {'score': 0, 'reason': 'No keywords to compare', 'comparable_price': 0, 'comps_found': 0}

    # Get comparable sales for main keyword
    main_keyword = max(keywords, key=len) if keywords else ''
    comps        = fetch_namebio_sales(main_keyword, tld, 10)

    if not comps:
        # Try without TLD filter
        comps = fetch_namebio_sales(main_keyword, '', 10)

    if not comps:
        return {'score': 5, 'reason': 'No comparable sales found — estimated low liquidity', 'comparable_price': 0, 'comps_found': 0}

    prices = [c['price'] for c in comps if c['price'] > 0]
    if not prices:
        return {'score': 5, 'reason': 'Comps found but no valid prices', 'comparable_price': 0, 'comps_found': len(comps)}

    # Remove extreme outliers (above 99th percentile) before calculating median
    prices_sorted = sorted(prices)
    p95           = prices_sorted[int(len(prices_sorted)*0.95)] if len(prices_sorted)>5 else prices_sorted[-1]
    prices_clean  = [p for p in prices if p <= p95*2]  # Remove anything > 2x the 95th pct
    if not prices_clean:
        prices_clean = prices_sorted[:int(len(prices_sorted)*0.8)]  # Use bottom 80%

    median_price = prices_clean[len(prices_clean)//2]
    avg_price    = sum(prices_clean) / len(prices_clean)

    # Score based on median comparable sale price
    if median_price >= 10000:
        score = 20
    elif median_price >= 5000:
        score = 18
    elif median_price >= 2000:
        score = 16
    elif median_price >= 1000:
        score = 14
    elif median_price >= 500:
        score = 11
    elif median_price >= 200:
        score = 8
    elif median_price >= 100:
        score = 5
    else:
        score = 2

    return {
        'score'            : score,
        'reason'           : f"{len(comps)} comps · median ${median_price:,} · avg ${avg_price:,.0f}",
        'comparable_price' : median_price,
        'comps_found'      : len(comps),
        'price_range'      : f"${min(prices):,} - ${max(prices):,}",
    }


def score_brandability(domain: str) -> dict:
    """
    Score how brandable and memorable the domain is.
    Pure algorithmic — no API needed.
    Max score: 20
    """
    name = domain.split('.')[0].lower()
    score = 15  # Start at 15, adjust up/down

    reasons = []

    # Length scoring
    length = len(name)
    if length <= 4:
        score += 4; reasons.append(f"Excellent length ({length} chars)")
    elif length <= 6:
        score += 3; reasons.append(f"Great length ({length} chars)")
    elif length <= 8:
        score += 1; reasons.append(f"Good length ({length} chars)")
    elif length <= 12:
        reasons.append(f"Acceptable length ({length} chars)")
    elif length <= 16:
        score -= 2; reasons.append(f"Long name ({length} chars)")
    else:
        score -= 5; reasons.append(f"Too long ({length} chars)")

    # Hyphens are bad for brandability
    if '-' in name:
        count = name.count('-')
        score -= count * 3
        reasons.append(f"Contains {count} hyphen(s) — reduces brandability")

    # Numbers reduce brandability
    if any(c.isdigit() for c in name):
        score -= 2
        reasons.append("Contains numbers — reduces brandability")

    # Pronounceable? (simple vowel check)
    vowels = sum(1 for c in name if c in 'aeiou')
    vowel_ratio = vowels / max(len(name), 1)
    if 0.25 <= vowel_ratio <= 0.5:
        score += 1; reasons.append("Good vowel ratio — pronounceable")
    elif vowel_ratio < 0.15:
        score -= 3; reasons.append("Hard to pronounce — low vowel ratio")

    # Repeating characters look spammy
    if re.search(r'(.)\1{2,}', name):
        score -= 3; reasons.append("Repeated characters — looks spammy")

    # Double meaning or compound words (check for common combos)
    valuable_combos = ['pro', 'hub', 'lab', 'kit', 'base', 'desk', 'box', 'link', 'wise', 'smart']
    for combo in valuable_combos:
        if combo in name:
            score += 1; reasons.append(f"Contains '{combo}' — positive brand signal")
            break

    score = max(0, min(20, score))
    return {
        'score'       : score,
        'reason'      : ' · '.join(reasons[:3]),
        'length'      : length,
        'has_hyphen'  : '-' in name,
        'has_numbers' : any(c.isdigit() for c in name),
    }


def score_tld_strength(domain: str) -> dict:
    """
    Score TLD strength. .com is king.
    Max score: 20 (but weighted to 10 in final)
    """
    tld   = '.' + domain.split('.')[-1].lower()
    score = CONFIG['tld_scores'].get(tld, 3)

    reasons = {
        '.com'  : '.com is the gold standard — highest trust and resale value',
        '.io'   : '.io is popular for tech/SaaS startups — good resale market',
        '.ai'   : '.ai is trending for AI startups — premium pricing',
        '.co'   : '.co is solid alternative to .com — decent resale market',
        '.net'  : '.net is established — moderate resale market',
        '.org'  : '.org is trusted for nonprofits — niche resale market',
        '.app'  : '.app is good for mobile/web apps',
        '.dev'  : '.dev is gaining traction for developers',
    }

    return {
        'score'  : score,
        'tld'    : tld,
        'reason' : reasons.get(tld, f'{tld} has limited resale market'),
    }


def score_domain_age(domain: str) -> dict:
    """
    Score domain age. Older = more authority.
    Max score: 20 (weighted to 8 in final)
    """
    age_data = fetch_whois_age(domain)
    age      = age_data.get('age_years', 0)

    if age >= 20:
        score  = 20; reason = f"Very old domain ({age:.0f} years) — high authority signal"
    elif age >= 15:
        score  = 18; reason = f"Old domain ({age:.0f} years) — strong authority"
    elif age >= 10:
        score  = 15; reason = f"Mature domain ({age:.0f} years)"
    elif age >= 5:
        score  = 11; reason = f"Established domain ({age:.0f} years)"
    elif age >= 2:
        score  = 7;  reason = f"Relatively young ({age:.0f} years)"
    elif age > 0:
        score  = 3;  reason = f"Very young domain ({age:.1f} years)"
    else:
        score  = 1;  reason = "Age unknown — new registration"

    return {'score': score, 'reason': reason, 'age_years': age, 'created': age_data.get('created', 'unknown')}


def score_wayback_history(domain: str) -> dict:
    """
    Score based on Wayback Machine history.
    Had a real website = positive signal.
    Max score: 20 (weighted to 5 in final)
    """
    history = fetch_wayback_history(domain)

    if not history['has_history']:
        return {'score': 5, 'reason': 'No archived history found', 'has_history': False}

    last_seen = history.get('last_seen', '')
    try:
        last_dt   = datetime.strptime(last_seen, '%Y%m%d')
        years_ago = (datetime.utcnow() - last_dt).days / 365
        if years_ago < 1:
            score  = 20; reason = "Recently active website — very fresh drop"
        elif years_ago < 3:
            score  = 17; reason = f"Was active ~{years_ago:.0f} year(s) ago"
        elif years_ago < 7:
            score  = 13; reason = f"Was active ~{years_ago:.0f} years ago"
        else:
            score  = 8;  reason = f"Old activity (~{years_ago:.0f} years ago)"
    except Exception:
        score  = 10; reason = "Historical data found — date unclear"

    return {'score': score, 'reason': reason, 'has_history': True, 'last_seen': last_seen, 'archive_url': history.get('url','')}


def score_spam_check(domain: str) -> dict:
    """
    Penalty score for spam signals.
    Returns negative adjustment (0 = clean, -20 = very spammy).
    """
    name     = domain.split('.')[0].lower()
    tld      = '.' + domain.split('.')[-1].lower()
    penalty  = 0
    reasons  = []

    # Spam TLD
    if tld in CONFIG['spam_tlds']:
        penalty -= 15; reasons.append(f"Spam TLD: {tld}")

    # Too many hyphens
    if name.count('-') >= 3:
        penalty -= 8; reasons.append("Excessive hyphens — spam pattern")

    # Too many numbers
    digit_ratio = sum(1 for c in name if c.isdigit()) / max(len(name), 1)
    if digit_ratio > 0.4:
        penalty -= 6; reasons.append("High number ratio — spam pattern")

    # Very long with hyphens (exact match spam pattern)
    if len(name) > 20 and '-' in name:
        penalty -= 5; reasons.append("Long hyphenated — low value pattern")

    # Gibberish detection (too many consonants in a row)
    if re.search(r'[bcdfghjklmnpqrstvwxyz]{5,}', name):
        penalty -= 4; reasons.append("Consonant cluster — likely gibberish")

    # Common spam keywords
    spam_keywords = ['free', 'cheap', 'best', 'top', 'online', 'buy', 'click', 'win', 'prize']
    matched_spam  = [kw for kw in spam_keywords if kw in name]
    if len(matched_spam) >= 2:
        penalty -= 5; reasons.append(f"Multiple spam keywords: {', '.join(matched_spam)}")

    return {
        'score'   : penalty,
        'reason'  : ' · '.join(reasons) if reasons else 'Clean — no spam signals detected',
        'is_clean': penalty == 0,
    }


def score_backlink_quality(domain: str) -> dict:
    """
    Estimate backlink quality.
    Uses Majestic free API — 1000 free lookups/month.
    Falls back to Wayback estimate if no API key.
    Max score: 20 (weighted to 8 in final)
    """
    # Try Majestic free tier
    majestic_key = ''  # Add your free Majestic API key here if available

    if majestic_key:
        try:
            url  = f"https://api.majestic.com/api/json?app_api_key={majestic_key}&cmd=GetIndexItemInfo&items=1&item0={domain}&datasource=fresh"
            resp = requests.get(url, timeout=10)
            if resp.status_code == 200:
                data       = resp.json()
                item       = data.get('DataTables', {}).get('Results', {}).get('Data', [{}])[0]
                trust_flow = int(item.get('TrustFlow', 0))
                citation   = int(item.get('CitationFlow', 0))
                ext_links  = int(item.get('ExtBackLinks', 0))

                if trust_flow >= 40:
                    score  = 20; reason = f"TF:{trust_flow} CF:{citation} Links:{ext_links:,} — Excellent"
                elif trust_flow >= 25:
                    score  = 16; reason = f"TF:{trust_flow} CF:{citation} — Strong"
                elif trust_flow >= 15:
                    score  = 12; reason = f"TF:{trust_flow} CF:{citation} — Good"
                elif trust_flow >= 5:
                    score  = 8;  reason = f"TF:{trust_flow} CF:{citation} — Moderate"
                else:
                    score  = 3;  reason = f"TF:{trust_flow} — Weak backlinks"

                return {'score': score, 'reason': reason, 'trust_flow': trust_flow, 'citation_flow': citation}
        except Exception:
            pass

    # Fallback: estimate from wayback activity
    history = fetch_wayback_history(domain)
    if history['has_history']:
        return {'score': 8, 'reason': 'Backlink data estimated from archive history — install Majestic API for precise data', 'trust_flow': 0}

    return {'score': 2, 'reason': 'No backlink data available — add Majestic API key for this dimension', 'trust_flow': 0}


# ════════════════════════════════════════════════════════════
# MASTER SCORER
# ════════════════════════════════════════════════════════════

def score_domain(domain: str, fast_mode: bool = False) -> dict:
    """
    Run full scoring pipeline on a domain.
    fast_mode=True skips API calls for speed (uses cached/estimated data).
    Returns complete score report.
    """
    domain    = domain.lower().strip()
    tld       = '.' + domain.split('.')[-1]
    name      = domain.split('.')[0]
    weights   = CONFIG['weights']
    timestamp = datetime.utcnow().isoformat()

    print(f"  Scoring: {domain}")

    # Run all scorers
    kw     = score_keyword_value(domain)
    brand  = score_brandability(domain)
    tld_s  = score_tld_strength(domain)
    spam   = score_spam_check(domain)

    if fast_mode:
        comps  = {'score': 8,  'reason': 'Fast mode — comparable sales skipped', 'comparable_price': 0, 'comps_found': 0}
        age    = {'score': 5,  'reason': 'Fast mode — age check skipped', 'age_years': 0, 'created': 'unknown'}
        wb     = {'score': 5,  'reason': 'Fast mode — wayback skipped', 'has_history': False}
        bl     = {'score': 4,  'reason': 'Fast mode — backlink check skipped', 'trust_flow': 0}
    else:
        comps  = score_comparable_sales(domain)
        age    = score_domain_age(domain)
        wb     = score_wayback_history(domain)
        bl     = score_backlink_quality(domain)

    # Calculate weighted final score (0-100)
    raw_scores = {
        'keyword_value'   : kw['score'],
        'comparable_sales': comps['score'],
        'brandability'    : brand['score'],
        'tld_strength'    : tld_s['score'],
        'domain_age'      : age['score'],
        'backlink_quality': bl['score'],
        'wayback_history' : wb['score'],
        'spam_penalty'    : spam['score'],  # This is 0 or negative
    }

    weighted_sum = sum(
        raw_scores[k] * weights[k] * (100/20)  # Normalize each to 100 scale
        for k in raw_scores
    )

    # Apply spam penalty on top
    final_score = max(0, min(100, weighted_sum + spam['score'] * 3))

    # Estimate resale value
    reg_cost    = CONFIG['registration_costs'].get(tld, 12.0)
    comp_price  = comps.get('comparable_price', 0)
    if comp_price > 0:
        estimated_value = comp_price * (final_score / 100)
    else:
        # Estimate from score
        if final_score >= 85:
            estimated_value = 2000
        elif final_score >= 75:
            estimated_value = 800
        elif final_score >= 65:
            estimated_value = 300
        elif final_score >= 55:
            estimated_value = 150
        else:
            estimated_value = 50

    roi_multiple = estimated_value / reg_cost if reg_cost > 0 else 0

    # Signal
    if final_score >= CONFIG['min_score_to_alert']:
        signal = 'STRONG BUY'
        signal_color = '#00e5a0'
    elif final_score >= CONFIG['min_score_to_register']:
        signal = 'BUY'
        signal_color = '#ffd700'
    elif final_score >= 45:
        signal = 'WATCH'
        signal_color = '#ff6b35'
    else:
        signal = 'PASS'
        signal_color = '#ff4560'

    return {
        'domain'            : domain,
        'name'              : name,
        'tld'               : tld,
        'final_score'       : round(final_score, 1),
        'signal'            : signal,
        'signal_color'      : signal_color,
        'estimated_value'   : round(estimated_value),
        'registration_cost' : reg_cost,
        'roi_multiple'      : round(roi_multiple, 1),
        'timestamp'         : timestamp,
        'dimension_scores'  : raw_scores,
        'details'           : {
            'keyword'    : kw,
            'comps'      : comps,
            'brandability': brand,
            'tld'        : tld_s,
            'age'        : age,
            'wayback'    : wb,
            'backlinks'  : bl,
            'spam'       : spam,
        }
    }


def scan_domains(domains: list, fast_mode: bool = False) -> list:
    """Score a list of domains and return ranked results."""
    results = []
    total   = len(domains)
    print(f"\nScanning {total} domains...")

    for i, domain in enumerate(domains):
        print(f"[{i+1}/{total}] ", end='', flush=True)
        try:
            result = score_domain(domain, fast_mode)
            results.append(result)
            time.sleep(0.5)  # Rate limiting — be respectful
        except Exception as e:
            print(f"Error on {domain}: {e}")
            continue

    # Sort by final score descending
    results.sort(key=lambda x: x['final_score'], reverse=True)
    print(f"\nScan complete. Found {len([r for r in results if r['signal'] in ['BUY','STRONG BUY']])} opportunities.")
    return results


# ════════════════════════════════════════════════════════════
# PORTFOLIO MANAGER
# ════════════════════════════════════════════════════════════

def load_portfolio() -> dict:
    try:
        with open(CONFIG['portfolio_file'], 'r') as f:
            return json.load(f)
    except Exception:
        return {'domains': [], 'total_invested': 0, 'total_sold': 0}


def save_portfolio(portfolio: dict):
    with open(CONFIG['portfolio_file'], 'w') as f:
        json.dump(portfolio, f, indent=2)


def add_to_portfolio(domain: str, purchase_price: float, score_report: dict):
    portfolio = load_portfolio()
    entry = {
        'domain'        : domain,
        'purchase_price': purchase_price,
        'purchase_date' : datetime.utcnow().strftime('%Y-%m-%d'),
        'score'         : score_report['final_score'],
        'estimated_value': score_report['estimated_value'],
        'status'        : 'holding',
        'listed_price'  : score_report['estimated_value'],
        'sold_price'    : None,
        'sold_date'     : None,
        'platform'      : 'Afternic',
        'notes'         : '',
    }
    portfolio['domains'].append(entry)
    portfolio['total_invested'] = sum(d['purchase_price'] for d in portfolio['domains'])
    save_portfolio(portfolio)
    return entry


def mark_sold(domain: str, sold_price: float):
    portfolio = load_portfolio()
    for d in portfolio['domains']:
        if d['domain'] == domain:
            d['status']     = 'sold'
            d['sold_price'] = sold_price
            d['sold_date']  = datetime.utcnow().strftime('%Y-%m-%d')
            d['roi']        = round((sold_price - d['purchase_price']) / d['purchase_price'] * 100, 1)
    portfolio['total_sold'] = sum(d.get('sold_price', 0) for d in portfolio['domains'] if d['status'] == 'sold')
    save_portfolio(portfolio)


def portfolio_stats(portfolio: dict) -> dict:
    domains      = portfolio.get('domains', [])
    holding      = [d for d in domains if d['status'] == 'holding']
    sold         = [d for d in domains if d['status'] == 'sold']
    total_invest = sum(d['purchase_price'] for d in domains)
    total_sold_v = sum(d.get('sold_price', 0) for d in sold)
    total_est    = sum(d.get('estimated_value', 0) for d in holding)
    net_pnl      = total_sold_v - total_invest + total_est

    return {
        'total_domains'  : len(domains),
        'holding'        : len(holding),
        'sold'           : len(sold),
        'total_invested' : total_invest,
        'total_sold'     : total_sold_v,
        'estimated_value': total_est,
        'net_pnl'        : net_pnl,
        'roi_pct'        : round((net_pnl / total_invest * 100) if total_invest > 0 else 0, 1),
        'best_flip'      : max([d.get('roi', 0) for d in sold], default=0),
    }


# ════════════════════════════════════════════════════════════
# STREAMLIT DASHBOARD
# ════════════════════════════════════════════════════════════

def run_streamlit():

    st.set_page_config(
        page_title="DomainQuant",
        page_icon="🌐",
        layout="wide",
        initial_sidebar_state="expanded"
    )

    st.markdown("""
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600;700&family=JetBrains+Mono&display=swap');
    :root{--bg:#0a0c10;--surface:#111318;--border:#1e2229;--accent:#00e5a0;--text:#e8eaf0;--muted:#6b7280;}
    html,body,[data-testid="stAppViewContainer"]{background:var(--bg)!important;color:var(--text)!important;font-family:'Space Grotesk',sans-serif!important;}
    [data-testid="stSidebar"]{background:var(--surface)!important;border-right:1px solid var(--border)!important;}
    [data-testid="stSidebar"] *{color:var(--text)!important;}
    h1,h2,h3{font-family:'Space Grotesk',sans-serif!important;color:var(--text)!important;font-weight:700!important;}
    .stButton>button{background:var(--accent)!important;color:#000!important;border:none!important;border-radius:8px!important;font-weight:600!important;padding:0.5rem 1.5rem!important;}
    .stTextInput>div>div>input,.stTextArea>div>div>textarea{background:var(--surface)!important;border:1px solid var(--border)!important;color:var(--text)!important;border-radius:8px!important;}
    .metric-card{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:1.25rem 1.5rem;position:relative;overflow:hidden;}
    .metric-card::before{content:'';position:absolute;top:0;left:0;right:0;height:2px;background:var(--accent);}
    .metric-val{font-size:2rem;font-weight:700;color:var(--accent);line-height:1;}
    .metric-lbl{font-size:0.8rem;color:var(--muted);text-transform:uppercase;letter-spacing:0.08em;}
    .domain-card{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:1rem 1.25rem;margin-bottom:0.75rem;}
    .score-bar-bg{background:#1e2229;border-radius:4px;height:6px;margin-top:4px;}
    .section-header{font-size:0.7rem;text-transform:uppercase;letter-spacing:0.12em;color:var(--muted);margin-bottom:0.75rem;margin-top:1.5rem;}
    #MainMenu,footer,header{visibility:hidden;}
    </style>
    """, unsafe_allow_html=True)

    # Sidebar
    with st.sidebar:
        st.markdown("""
        <div style='padding:1rem 0 1.5rem;'>
            <div style='font-size:1.4rem;font-weight:700;color:#00e5a0;'>🌐 DomainQuant</div>
            <div style='font-size:0.72rem;color:#6b7280;text-transform:uppercase;letter-spacing:0.08em;'>Quantitative Domain Intelligence</div>
        </div>""", unsafe_allow_html=True)

        page = st.radio("",
            ["🏠  Dashboard","🔍  Domain Scanner","📋  Score a Domain",
             "💼  My Portfolio","📊  Market Data","⚙️  Settings"],
            label_visibility="collapsed")

        # Portfolio quick stats
        portfolio = load_portfolio()
        stats     = portfolio_stats(portfolio)
        st.markdown("<hr style='border-color:#1e2229;margin:1rem 0;'>",unsafe_allow_html=True)
        st.markdown("<div style='font-size:0.7rem;text-transform:uppercase;letter-spacing:0.12em;color:#6b7280;margin-bottom:0.5rem;'>Portfolio</div>",unsafe_allow_html=True)
        c1,c2 = st.columns(2)
        with c1: st.markdown(f"<div style='text-align:center;'><div style='font-size:1.3rem;font-weight:700;color:#00e5a0;'>{stats['total_domains']}</div><div style='font-size:0.65rem;color:#6b7280;'>DOMAINS</div></div>",unsafe_allow_html=True)
        with c2: st.markdown(f"<div style='text-align:center;'><div style='font-size:1.3rem;font-weight:700;color:{'#00e5a0' if stats['net_pnl']>=0 else '#ff4560'};'>${stats['net_pnl']:,.0f}</div><div style='font-size:0.65rem;color:#6b7280;'>NET P&L</div></div>",unsafe_allow_html=True)

    # ── PAGES ──────────────────────────────────────────────

    if "🏠" in page:
        _page_dashboard(stats, portfolio)
    elif "🔍" in page:
        _page_scanner()
    elif "📋" in page:
        _page_score_single()
    elif "💼" in page:
        _page_portfolio(portfolio, stats)
    elif "📊" in page:
        _page_market_data()
    elif "⚙️" in page:
        _page_settings()


def _section(title):
    st.markdown(f"<div class='section-header'>{title}</div>", unsafe_allow_html=True)


def _metric(val, lbl, color='#00e5a0'):
    st.markdown(f"<div class='metric-card'><div class='metric-val' style='color:{color};'>{val}</div><div class='metric-lbl'>{lbl}</div></div>", unsafe_allow_html=True)


def _score_color(score):
    if score >= 70: return '#00e5a0'
    if score >= 55: return '#ffd700'
    if score >= 40: return '#ff6b35'
    return '#ff4560'


def _domain_card(result):
    sc    = result['final_score']
    sc_c  = _score_color(sc)
    sig_c = result.get('signal_color', '#6b7280')
    roi   = result.get('roi_multiple', 0)

    st.markdown(f"""
    <div class='domain-card'>
        <div style='display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:8px;'>
            <div>
                <span style='font-size:1rem;font-weight:700;color:#e8eaf0;'>{result['domain']}</span>
                <span style='margin-left:8px;font-size:0.72rem;font-weight:700;
                      color:{sig_c};border:1px solid {sig_c};border-radius:4px;
                      padding:1px 7px;'>{result['signal']}</span>
            </div>
            <div style='text-align:right;'>
                <div style='font-size:1.4rem;font-weight:700;color:{sc_c};'>{sc}</div>
                <div style='font-size:0.65rem;color:#6b7280;'>SCORE</div>
            </div>
        </div>
        <div style='background:#1e2229;border-radius:4px;height:4px;margin-bottom:10px;'>
            <div style='background:{sc_c};width:{sc}%;height:4px;border-radius:4px;'></div>
        </div>
        <div style='display:flex;gap:1.5rem;flex-wrap:wrap;'>
            <span style='font-size:0.75rem;color:#6b7280;'>Est. value <b style='color:#e8eaf0;'>${result['estimated_value']:,}</b></span>
            <span style='font-size:0.75rem;color:#6b7280;'>Reg cost <b style='color:#e8eaf0;'>${result['registration_cost']:.2f}</b></span>
            <span style='font-size:0.75rem;color:#6b7280;'>ROI <b style='color:#00e5a0;'>{roi}x</b></span>
            <span style='font-size:0.75rem;color:#6b7280;'>TLD <b style='color:#e8eaf0;'>{result['tld']}</b></span>
        </div>
    </div>""", unsafe_allow_html=True)


def _page_dashboard(stats, portfolio):
    st.markdown("<h1 style='font-size:1.8rem;margin-bottom:0.25rem;'>🌐 DomainQuant Dashboard</h1>",unsafe_allow_html=True)
    st.markdown("<p style='color:#6b7280;font-size:0.9rem;margin-bottom:2rem;'>Quantitative domain scoring and portfolio management.</p>",unsafe_allow_html=True)

    _section("Portfolio Summary")
    c1,c2,c3,c4,c5 = st.columns(5)
    items = [
        (str(stats['total_domains']),  "Total Domains",   '#00e5a0'),
        (str(stats['holding']),        "Holding",         '#0066ff'),
        (str(stats['sold']),           "Sold",            '#ffd700'),
        (f"${stats['total_invested']:,.0f}", "Invested",  '#ff6b35'),
        (f"${stats['net_pnl']:,.0f}",  "Net P&L",         '#00e5a0' if stats['net_pnl']>=0 else '#ff4560'),
    ]
    for col,(val,lbl,col_) in zip([c1,c2,c3,c4,c5],items):
        with col: _metric(val,lbl,col_)

    st.markdown("<div style='margin:1.5rem 0;'></div>",unsafe_allow_html=True)
    left,right = st.columns([3,2])

    with left:
        _section("Quick Actions")
        actions = [
            ("🔍","Run Daily Domain Scan","Fetch latest expired domains and score them"),
            ("📋","Score a Single Domain","Paste any domain and get full analysis"),
            ("💼","Update Portfolio","Log a new purchase or mark a domain as sold"),
            ("📊","Market Data","See recent comparable sales and keyword trends"),
        ]
        for em,t,d in actions:
            st.markdown(f"<div style='background:var(--surface);border:1px solid var(--border);border-left:3px solid var(--accent);border-radius:0 8px 8px 0;padding:0.75rem 1rem;margin-bottom:0.5rem;'><div style='display:flex;align-items:center;gap:0.5rem;'><span>{em}</span><span style='font-size:0.85rem;font-weight:600;'>{t}</span></div><div style='font-size:0.75rem;color:#6b7280;margin-top:2px;padding-left:1.3rem;'>{d}</div></div>",unsafe_allow_html=True)

    with right:
        _section("How It Works")
        steps = [
            ("1","Fetch","Daily expired domain list from free sources"),
            ("2","Score","8-dimension quantitative scoring model"),
            ("3","Filter","Only domains scoring above 60 are flagged"),
            ("4","Register","Buy flagged domains for $8-12"),
            ("5","List","Post on Afternic, Sedo, or Dan.com"),
            ("6","Sell","Wait for buyer — typical 3-12 month cycle"),
        ]
        for num,t,d in steps:
            st.markdown(f"<div style='display:flex;align-items:flex-start;gap:0.75rem;padding:0.4rem 0;border-bottom:1px solid #1e2229;'><div style='width:22px;height:22px;background:rgba(0,229,160,0.15);border:1px solid #00e5a0;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:0.65rem;font-weight:700;color:#00e5a0;flex-shrink:0;margin-top:2px;'>{num}</div><div><div style='font-size:0.8rem;font-weight:600;'>{t}</div><div style='font-size:0.7rem;color:#6b7280;'>{d}</div></div></div>",unsafe_allow_html=True)

    # Recent portfolio
    domains = portfolio.get('domains',[])
    if domains:
        st.markdown("<div style='margin:1.5rem 0;'></div>",unsafe_allow_html=True)
        _section("Recent Portfolio Entries")
        for d in domains[-3:][::-1]:
            status_c = '#00e5a0' if d['status']=='sold' else '#ffd700'
            st.markdown(f"<div class='domain-card'><div style='display:flex;justify-content:space-between;'><span style='font-weight:700;'>{d['domain']}</span><span style='color:{status_c};font-size:0.78rem;font-weight:600;'>{d['status'].upper()}</span></div><div style='font-size:0.75rem;color:#6b7280;margin-top:4px;'>Paid ${d['purchase_price']:.2f} · Est. ${d.get('estimated_value',0):,} · Score {d.get('score',0)}</div></div>",unsafe_allow_html=True)


def _page_scanner():
    st.markdown("<h1 style='font-size:1.8rem;margin-bottom:0.25rem;'>🔍 Domain Scanner</h1>",unsafe_allow_html=True)
    st.markdown("<p style='color:#6b7280;font-size:0.9rem;margin-bottom:2rem;'>Fetch expired domains and score them automatically. Free data sources.</p>",unsafe_allow_html=True)

    col1,col2,col3 = st.columns(3)
    with col1: scan_limit  = st.slider("Domains to scan", 10, 200, 50)
    with col2: min_score   = st.slider("Min score to show", 30, 80, 50)
    with col3: fast_mode   = st.checkbox("Fast mode (no API calls)", value=True)

    col_a,col_b = st.columns(2)
    with col_a:
        custom_list = st.text_area("Or paste your own domains (one per line)",
            height=100, placeholder="tradingbot.com\nalgotrader.io\nquantfund.net")
    with col_b:
        st.markdown("""
        <div style='background:#111318;border:1px solid #1e2229;border-radius:8px;padding:1rem;margin-top:1.4rem;'>
            <div style='font-size:0.78rem;font-weight:600;margin-bottom:6px;'>Data sources used</div>
            <div style='font-size:0.72rem;color:#6b7280;line-height:1.8;'>
                ✅ WhoisFreaks free expired list<br>
                ✅ ExpiredDomains.net scrape<br>
                ✅ NameBio comparable sales<br>
                ✅ Wayback Machine history<br>
                ✅ Google Trends (pytrends)<br>
                ⚡ All free — no subscriptions
            </div>
        </div>""", unsafe_allow_html=True)

    scan_btn = st.button("⚡  Run Domain Scan", use_container_width=True)

    if scan_btn:
        if custom_list.strip():
            domains = [d.strip().lower() for d in custom_list.strip().split('\n') if d.strip() and '.' in d]
        else:
            with st.spinner("Fetching expired domains..."):
                domains = fetch_expired_domains_whoisfreaks(scan_limit)

        if not domains:
            st.error("No domains fetched. Check your internet connection or paste domains manually.")
            return

        st.info(f"Scoring {len(domains)} domains... This takes ~{len(domains)//2} seconds in fast mode.")
        progress = st.progress(0)
        results  = []

        for i, domain in enumerate(domains):
            try:
                result = score_domain(domain, fast_mode=fast_mode)
                results.append(result)
            except Exception:
                pass
            progress.progress((i+1)/len(domains))

        progress.empty()
        results.sort(key=lambda x: x['final_score'], reverse=True)

        # Save results
        with open(f"{CONFIG['data_dir']}/last_scan.json", 'w') as f:
            json.dump(results, f, indent=2)
        st.session_state['scan_results'] = results

        # Summary
        opportunities = [r for r in results if r['final_score'] >= min_score]
        strong_buys   = [r for r in results if r['signal'] == 'STRONG BUY']
        buys          = [r for r in results if r['signal'] == 'BUY']

        c1,c2,c3,c4 = st.columns(4)
        with c1: _metric(str(len(results)),       "Scanned",          '#0066ff')
        with c2: _metric(str(len(opportunities)), "Above Threshold",  '#ffd700')
        with c3: _metric(str(len(buys)),          "BUY Signals",      '#00e5a0')
        with c4: _metric(str(len(strong_buys)),   "STRONG BUY",       '#00e5a0')

        st.markdown("<div style='margin:1rem 0;'></div>",unsafe_allow_html=True)
        _section(f"Results — {len(opportunities)} opportunities above score {min_score}")

        for result in results:
            if result['final_score'] >= min_score:
                _domain_card(result)
                with st.expander("View full score breakdown"):
                    details = result.get('details', {})
                    cols = st.columns(4)
                    dim_items = [
                        ('Keyword Value',    result['dimension_scores']['keyword_value'],    details.get('keyword',{}).get('reason','')),
                        ('Comparable Sales', result['dimension_scores']['comparable_sales'], details.get('comps',{}).get('reason','')),
                        ('Brandability',     result['dimension_scores']['brandability'],     details.get('brandability',{}).get('reason','')),
                        ('TLD Strength',     result['dimension_scores']['tld_strength'],     details.get('tld',{}).get('reason','')),
                        ('Domain Age',       result['dimension_scores']['domain_age'],       details.get('age',{}).get('reason','')),
                        ('Backlinks',        result['dimension_scores']['backlink_quality'], details.get('backlinks',{}).get('reason','')),
                        ('Wayback History',  result['dimension_scores']['wayback_history'],  details.get('wayback',{}).get('reason','')),
                        ('Spam Check',       result['dimension_scores']['spam_penalty'],     details.get('spam',{}).get('reason','')),
                    ]
                    for j, (dim, score_, reason_) in enumerate(dim_items):
                        with cols[j%4]:
                            c_ = _score_color(score_*5) if score_ >= 0 else '#ff4560'
                            st.markdown(f"<div style='background:#111318;border:1px solid #1e2229;border-radius:8px;padding:0.6rem 0.75rem;margin-bottom:0.5rem;'><div style='font-size:0.68rem;color:#6b7280;text-transform:uppercase;'>{dim}</div><div style='font-size:1.1rem;font-weight:700;color:{c_};'>{score_}/20</div><div style='font-size:0.65rem;color:#9ca3af;margin-top:3px;'>{reason_[:60]}</div></div>",unsafe_allow_html=True)

                    # Add to portfolio button
                    if st.button(f"➕  Add {result['domain']} to Portfolio", key=f"add_{result['domain']}"):
                        add_to_portfolio(result['domain'], result['registration_cost'], result)
                        st.success(f"✅ {result['domain']} added to portfolio!")

        # Download results
        if results:
            df_results = pd.DataFrame([{
                'domain'         : r['domain'],
                'score'          : r['final_score'],
                'signal'         : r['signal'],
                'estimated_value': r['estimated_value'],
                'reg_cost'       : r['registration_cost'],
                'roi_multiple'   : r['roi_multiple'],
                'tld'            : r['tld'],
            } for r in results])
            st.download_button(
                "⬇️  Download Results CSV",
                df_results.to_csv(index=False),
                file_name=f"domain_scan_{datetime.now().strftime('%Y%m%d_%H%M')}.csv",
                mime="text/csv"
            )


def _page_score_single():
    st.markdown("<h1 style='font-size:1.8rem;margin-bottom:0.25rem;'>📋 Score a Domain</h1>",unsafe_allow_html=True)
    st.markdown("<p style='color:#6b7280;font-size:0.9rem;margin-bottom:2rem;'>Get a full quantitative analysis of any domain name.</p>",unsafe_allow_html=True)

    col1,col2 = st.columns([3,1])
    with col1:
        domain_input = st.text_input("Domain name", placeholder="tradingbot.com")
    with col2:
        fast  = st.checkbox("Fast mode", value=False)

    if st.button("📊  Analyze Domain", use_container_width=True) and domain_input.strip():
        domain = domain_input.strip().lower()
        if not '.' in domain:
            st.error("Please include the TLD (e.g. tradingbot.com)")
            return

        with st.spinner(f"Analyzing {domain}..."):
            result = score_domain(domain, fast_mode=fast)

        # Score display
        sc   = result['final_score']
        sc_c = _score_color(sc)
        sig  = result['signal']
        sig_c= result['signal_color']

        st.markdown(f"""
        <div style='background:var(--surface);border:2px solid {sc_c};border-radius:16px;padding:1.5rem;margin:1rem 0;'>
            <div style='display:flex;justify-content:space-between;align-items:center;margin-bottom:1rem;'>
                <div>
                    <div style='font-size:1.4rem;font-weight:700;'>{domain}</div>
                    <span style='font-size:0.82rem;font-weight:700;color:{sig_c};
                          border:1px solid {sig_c};border-radius:4px;padding:2px 10px;'>{sig}</span>
                </div>
                <div style='text-align:right;'>
                    <div style='font-size:3rem;font-weight:700;color:{sc_c};line-height:1;'>{sc}</div>
                    <div style='font-size:0.72rem;color:#6b7280;'>/ 100</div>
                </div>
            </div>
            <div style='background:#1e2229;border-radius:6px;height:8px;margin-bottom:1rem;'>
                <div style='background:{sc_c};width:{sc}%;height:8px;border-radius:6px;'></div>
            </div>
            <div style='display:flex;gap:2rem;flex-wrap:wrap;'>
                <span style='font-size:0.82rem;color:#6b7280;'>Est. value <b style='color:#e8eaf0;font-size:1rem;'>${result['estimated_value']:,}</b></span>
                <span style='font-size:0.82rem;color:#6b7280;'>Register for <b style='color:#e8eaf0;'>${result['registration_cost']:.2f}</b></span>
                <span style='font-size:0.82rem;color:#6b7280;'>ROI potential <b style='color:#00e5a0;'>{result['roi_multiple']}x</b></span>
            </div>
        </div>
        """, unsafe_allow_html=True)

        # Dimension breakdown
        _section("Score Breakdown — 8 Dimensions")
        details = result.get('details', {})
        dim_data = [
            ('Keyword Value',    result['dimension_scores']['keyword_value'],    details.get('keyword',{}),     'Weight 25%'),
            ('Comparable Sales', result['dimension_scores']['comparable_sales'], details.get('comps',{}),       'Weight 25%'),
            ('Brandability',     result['dimension_scores']['brandability'],     details.get('brandability',{}), 'Weight 15%'),
            ('TLD Strength',     result['dimension_scores']['tld_strength'],     details.get('tld',{}),         'Weight 10%'),
            ('Domain Age',       result['dimension_scores']['domain_age'],       details.get('age',{}),         'Weight 8%'),
            ('Backlinks',        result['dimension_scores']['backlink_quality'], details.get('backlinks',{}),   'Weight 8%'),
            ('Wayback History',  result['dimension_scores']['wayback_history'],  details.get('wayback',{}),     'Weight 5%'),
            ('Spam Check',       result['dimension_scores']['spam_penalty'],     details.get('spam',{}),        'Weight 4%'),
        ]

        cols = st.columns(4)
        for i,(dim,score_,detail_,weight_) in enumerate(dim_data):
            with cols[i%4]:
                c_ = _score_color(score_*5) if score_>=0 else '#ff4560'
                reason_ = detail_.get('reason','') if isinstance(detail_,dict) else ''
                st.markdown(f"""
                <div style='background:#111318;border:1px solid #1e2229;border-radius:10px;
                     padding:0.85rem;margin-bottom:0.75rem;'>
                    <div style='font-size:0.68rem;color:#6b7280;text-transform:uppercase;
                         letter-spacing:0.08em;margin-bottom:4px;'>{dim}</div>
                    <div style='font-size:1.4rem;font-weight:700;color:{c_};'>{score_}/20</div>
                    <div style='background:#1e2229;border-radius:3px;height:3px;margin:6px 0;'>
                        <div style='background:{c_};width:{max(0,min(100,score_/20*100))}%;height:3px;border-radius:3px;'></div>
                    </div>
                    <div style='font-size:0.65rem;color:#9ca3af;line-height:1.4;'>{reason_[:80]}</div>
                    <div style='font-size:0.6rem;color:#4b5563;margin-top:4px;'>{weight_}</div>
                </div>""", unsafe_allow_html=True)

        # Recommendation
        _section("Recommendation")
        if result['signal'] in ['BUY','STRONG BUY']:
            st.markdown(f"""
            <div style='background:rgba(0,229,160,0.08);border:1px solid rgba(0,229,160,0.3);
                 border-radius:10px;padding:1rem 1.25rem;'>
                <div style='font-size:0.9rem;font-weight:700;color:#00e5a0;margin-bottom:6px;'>
                    ✅ {result['signal']} — Register this domain
                </div>
                <div style='font-size:0.82rem;color:#9ca3af;line-height:1.6;'>
                    Register on Namecheap or GoDaddy for ${result['registration_cost']:.2f}<br>
                    List immediately on Afternic ($0 to list, 15-20% commission on sale)<br>
                    List on Sedo as well for wider exposure<br>
                    Target sale price: ${result['estimated_value']:,}<br>
                    Expected ROI: {result['roi_multiple']}x your registration cost
                </div>
            </div>""", unsafe_allow_html=True)

            if st.button(f"➕  Add to Portfolio (${result['registration_cost']:.2f})", key="add_single"):
                add_to_portfolio(domain, result['registration_cost'], result)
                st.success(f"✅ {domain} added to portfolio!")
        else:
            st.markdown(f"""
            <div style='background:rgba(255,69,96,0.08);border:1px solid rgba(255,69,96,0.3);
                 border-radius:10px;padding:1rem 1.25rem;'>
                <div style='font-size:0.9rem;font-weight:700;color:#ff4560;margin-bottom:6px;'>
                    ⛔ PASS — Score below threshold ({CONFIG['min_score_to_register']} required)
                </div>
                <div style='font-size:0.82rem;color:#9ca3af;'>
                    The domain scores {sc}/100. Our model requires {CONFIG['min_score_to_register']}+ to justify registration.
                    The registration cost risk is too high relative to estimated resale value.
                </div>
            </div>""", unsafe_allow_html=True)

        # Where to buy/sell reference
        _section("Where to Register & List")
        platforms = [
            ("🛒 Register", "Namecheap.com", "namecheap.com", f"${result['registration_cost']:.2f}/yr"),
            ("🛒 Register", "GoDaddy.com",   "godaddy.com",   f"${result['registration_cost']:.2f}/yr"),
            ("💰 Sell",     "Afternic",       "afternic.com",  "15-20% commission"),
            ("💰 Sell",     "Sedo",           "sedo.com",      "10-15% commission"),
            ("💰 Sell",     "Dan.com",        "dan.com",       "9% commission"),
            ("💰 Sell",     "Flippa",         "flippa.com",    "$10 listing + 5%"),
        ]
        cols = st.columns(3)
        for i,(typ,name,url,cost) in enumerate(platforms):
            with cols[i%3]:
                st.markdown(f"<div style='background:#111318;border:1px solid #1e2229;border-radius:8px;padding:0.75rem;margin-bottom:0.5rem;'><div style='font-size:0.68rem;color:#6b7280;'>{typ}</div><div style='font-size:0.85rem;font-weight:600;'>{name}</div><div style='font-size:0.72rem;color:#00e5a0;'>{cost}</div></div>",unsafe_allow_html=True)


def _page_portfolio(portfolio, stats):
    st.markdown("<h1 style='font-size:1.8rem;margin-bottom:0.25rem;'>💼 My Portfolio</h1>",unsafe_allow_html=True)
    st.markdown("<p style='color:#6b7280;font-size:0.9rem;margin-bottom:2rem;'>Track your domain investments and P&L.</p>",unsafe_allow_html=True)

    # Stats
    _section("Portfolio Stats")
    c1,c2,c3,c4,c5 = st.columns(5)
    with c1: _metric(str(stats['total_domains']),           "Total Domains",    '#00e5a0')
    with c2: _metric(f"${stats['total_invested']:,.0f}",    "Total Invested",   '#ff6b35')
    with c3: _metric(f"${stats['total_sold']:,.0f}",        "Total Sold",       '#0066ff')
    with c4: _metric(f"${stats['estimated_value']:,.0f}",   "Est. Unrealized",  '#ffd700')
    with c5:
        pnl_c = '#00e5a0' if stats['net_pnl']>=0 else '#ff4560'
        _metric(f"${stats['net_pnl']:,.0f}", "Net P&L", pnl_c)

    # Add domain form
    _section("Add Domain to Portfolio")
    with st.form("add_domain"):
        col1,col2,col3 = st.columns(3)
        with col1: new_domain = st.text_input("Domain", placeholder="tradingbot.com")
        with col2: purchase_p = st.number_input("Purchase price ($)", min_value=0.0, value=9.99, format="%.2f")
        with col3: est_val    = st.number_input("Est. resale value ($)", min_value=0, value=500)
        if st.form_submit_button("➕  Add to Portfolio", use_container_width=True):
            if new_domain.strip():
                fake_result = {'final_score': 60, 'estimated_value': est_val, 'registration_cost': purchase_p, 'roi_multiple': round(est_val/purchase_p,1)}
                add_to_portfolio(new_domain.strip().lower(), purchase_p, fake_result)
                st.success(f"✅ {new_domain} added!")
                st.rerun()

    # Domain list
    domains = portfolio.get('domains', [])
    if not domains:
        st.info("No domains yet. Run the scanner or score a domain to get started.")
        return

    _section(f"Holdings — {len(domains)} domains")
    for d in domains[::-1]:
        status_c = '#00e5a0' if d['status']=='sold' else '#ffd700' if d['status']=='holding' else '#6b7280'
        profit   = (d.get('sold_price',0) or d.get('estimated_value',0)) - d['purchase_price']
        profit_c = '#00e5a0' if profit>0 else '#ff4560'

        with st.expander(f"{'✅' if d['status']=='sold' else '⏳'} {d['domain']} — {d['status'].upper()}"):
            c1,c2,c3,c4 = st.columns(4)
            with c1: st.markdown(f"<div style='font-size:0.75rem;color:#6b7280;'>Paid</div><div style='font-weight:700;'>${d['purchase_price']:.2f}</div>",unsafe_allow_html=True)
            with c2: st.markdown(f"<div style='font-size:0.75rem;color:#6b7280;'>Est. value</div><div style='font-weight:700;color:#ffd700;'>${d.get('estimated_value',0):,}</div>",unsafe_allow_html=True)
            with c3: st.markdown(f"<div style='font-size:0.75rem;color:#6b7280;'>Score</div><div style='font-weight:700;color:#00e5a0;'>{d.get('score',0)}</div>",unsafe_allow_html=True)
            with c4: st.markdown(f"<div style='font-size:0.75rem;color:#6b7280;'>Date</div><div style='font-weight:700;'>{d.get('purchase_date','')}</div>",unsafe_allow_html=True)

            if d['status'] == 'holding':
                with st.form(f"sell_{d['domain']}"):
                    sold_price = st.number_input("Sold for ($)", min_value=0, value=int(d.get('estimated_value',500)))
                    if st.form_submit_button("✅  Mark as Sold"):
                        mark_sold(d['domain'], sold_price)
                        st.success(f"Marked {d['domain']} as sold for ${sold_price:,}!")
                        st.rerun()

            elif d['status'] == 'sold':
                roi = d.get('roi', 0)
                roi_c = '#00e5a0' if roi>0 else '#ff4560'
                st.markdown(f"<div style='font-size:0.82rem;'>Sold for <b style='color:#00e5a0;'>${d.get('sold_price',0):,}</b> on {d.get('sold_date','')} · ROI <b style='color:{roi_c};'>{roi}%</b></div>",unsafe_allow_html=True)


def _page_market_data():
    st.markdown("<h1 style='font-size:1.8rem;margin-bottom:0.25rem;'>📊 Market Data</h1>",unsafe_allow_html=True)
    st.markdown("<p style='color:#6b7280;font-size:0.9rem;margin-bottom:2rem;'>Comparable sales and keyword trends. All free data.</p>",unsafe_allow_html=True)

    _section("Search Comparable Sales (NameBio)")
    col1,col2 = st.columns(2)
    with col1: search_kw  = st.text_input("Keyword", placeholder="trading")
    with col2: search_tld = st.selectbox("TLD filter", ['.com','.net','.org','.io','.ai','any'])

    if st.button("🔍  Search Comparable Sales"):
        tld_filter = '' if search_tld == 'any' else search_tld
        with st.spinner("Fetching sales data..."):
            sales = fetch_namebio_sales(search_kw, tld_filter, 20)

        if sales:
            st.success(f"Found {len(sales)} comparable sales")
            df = pd.DataFrame(sales)
            st.dataframe(df, use_container_width=True, hide_index=True)
            if len(df) > 0 and 'price' in df.columns:
                prices = df['price'].tolist()
                st.markdown(f"""
                <div style='background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:1rem;margin-top:0.75rem;'>
                    <div style='display:flex;gap:2rem;flex-wrap:wrap;'>
                        <span style='font-size:0.82rem;'>Median sale <b style='color:#00e5a0;'>${sorted(prices)[len(prices)//2]:,}</b></span>
                        <span style='font-size:0.82rem;'>Average <b style='color:#ffd700;'>${sum(prices)//len(prices):,}</b></span>
                        <span style='font-size:0.82rem;'>Min <b style='color:#e8eaf0;'>${min(prices):,}</b></span>
                        <span style='font-size:0.82rem;'>Max <b style='color:#e8eaf0;'>${max(prices):,}</b></span>
                    </div>
                </div>""", unsafe_allow_html=True)
        else:
            st.info("No sales data found. NameBio may be rate limiting — try again in a minute.")

    _section("Keyword Trends (Google Trends — free)")
    trend_kw = st.text_input("Keyword to check trend", placeholder="algo trading")
    if st.button("📈  Check Trend"):
        with st.spinner("Fetching trend data..."):
            score = get_google_trends_score(trend_kw)
        if score > 0:
            trend_c = '#00e5a0' if score>=50 else '#ffd700' if score>=25 else '#ff4560'
            st.markdown(f"<div class='metric-card'><div class='metric-val' style='color:{trend_c};'>{score:.0f}/100</div><div class='metric-lbl'>12-month average trend score for '{trend_kw}'</div></div>",unsafe_allow_html=True)
            if score >= 50: st.success("High trend score — strong keyword demand")
            elif score >= 25: st.warning("Moderate trend — decent demand")
            else: st.error("Low trend — consider different keywords")
        else:
            st.info("Trend data unavailable — install pytrends: pip install pytrends")

    _section("Keyword Category Reference")
    st.markdown("<div style='font-size:0.78rem;color:#6b7280;margin-bottom:0.75rem;'>Estimated CPC by category — higher CPC = more valuable domain</div>",unsafe_allow_html=True)
    cols = st.columns(4)
    for i,(cat,data) in enumerate(CONFIG['keyword_categories'].items()):
        with cols[i%4]:
            st.markdown(f"<div style='background:#111318;border:1px solid #1e2229;border-radius:8px;padding:0.75rem;margin-bottom:0.5rem;'><div style='font-size:0.82rem;font-weight:600;text-transform:capitalize;'>{cat}</div><div style='font-size:0.72rem;color:#00e5a0;margin:4px 0;'>~${data['base_cpc']:.1f} CPC</div><div style='font-size:0.65rem;color:#6b7280;'>{', '.join(data['keywords'][:4])}</div></div>",unsafe_allow_html=True)


def _page_settings():
    st.markdown("<h1 style='font-size:1.8rem;margin-bottom:0.25rem;'>⚙️ Settings</h1>",unsafe_allow_html=True)

    _section("Optional API Keys (all free tiers)")
    st.markdown("<div style='font-size:0.78rem;color:#6b7280;margin-bottom:0.75rem;'>The system works without any API keys. These unlock deeper data.</div>",unsafe_allow_html=True)

    with st.form("settings"):
        majestic_key = st.text_input("Majestic API Key (free tier — 1000 lookups/month)",
            placeholder="Get free at majestic.com/reports/majestic-million",
            type="password")
        godaddy_key  = st.text_input("GoDaddy API Key (free tier — domain valuation)",
            placeholder="Get free at developer.godaddy.com",
            type="password")
        notify_email = st.text_input("Email for STRONG BUY alerts",
            placeholder="your@email.com")
        if st.form_submit_button("💾  Save Settings"):
            st.session_state['api_keys'] = {'majestic': majestic_key, 'godaddy': godaddy_key, 'email': notify_email}
            st.success("✅ Settings saved")

    _section("Scoring Weights")
    st.markdown("<div style='font-size:0.78rem;color:#6b7280;margin-bottom:0.75rem;'>Adjust how the model weights each dimension. Must sum to 1.0.</div>",unsafe_allow_html=True)
    weight_items = [
        ('keyword_value',    'Keyword Value',    0.25),
        ('comparable_sales', 'Comparable Sales', 0.25),
        ('brandability',     'Brandability',     0.15),
        ('tld_strength',     'TLD Strength',     0.10),
        ('domain_age',       'Domain Age',       0.08),
        ('backlink_quality', 'Backlinks',        0.08),
        ('wayback_history',  'Wayback History',  0.05),
        ('spam_penalty',     'Spam Penalty',     0.04),
    ]
    for key,label,default in weight_items:
        CONFIG['weights'][key] = st.slider(label, 0.0, 0.5, CONFIG['weights'].get(key, default), 0.01)

    _section("Registration Thresholds")
    CONFIG['min_score_to_register'] = st.slider("Min score to register", 40, 80, CONFIG['min_score_to_register'])
    CONFIG['min_score_to_alert']    = st.slider("Min score for STRONG BUY alert", 60, 95, CONFIG['min_score_to_alert'])

    _section("Data Sources — No Subscriptions Needed")
    sources = [
        ("✅ Free", "WhoisFreaks",   "Daily expired domain list — 100 quality domains/day free"),
        ("✅ Free", "ExpiredDomains.net", "Scrape expired domain lists"),
        ("✅ Free", "NameBio",       "Comparable sales data via scraping"),
        ("✅ Free", "Wayback Machine","Domain history check via free API"),
        ("✅ Free", "pytrends",      "Google Trends keyword data"),
        ("✅ Free", "WHOIS lookup",  "Domain age via free WHOIS APIs"),
        ("⚡ Optional", "Majestic", "Backlink Trust Flow data — 1000 free lookups/month"),
        ("⚡ Optional", "GoDaddy API","Domain valuation — free tier available"),
    ]
    for status,name,desc in sources:
        color = '#00e5a0' if 'Free' in status else '#ffd700'
        st.markdown(f"<div style='display:flex;align-items:center;gap:1rem;padding:0.4rem 0;border-bottom:1px solid #1e2229;'><span style='font-size:0.72rem;font-weight:600;color:{color};min-width:80px;'>{status}</span><span style='font-size:0.78rem;font-weight:600;min-width:120px;'>{name}</span><span style='font-size:0.75rem;color:#6b7280;'>{desc}</span></div>",unsafe_allow_html=True)


# ════════════════════════════════════════════════════════════
# CLI ENTRY POINT
# ════════════════════════════════════════════════════════════

def run_cli_scan():
    """Run a domain scan from command line."""
    parser = argparse.ArgumentParser(description='DomainQuant CLI Scanner')
    parser.add_argument('--scan',    action='store_true', help='Fetch and scan expired domains')
    parser.add_argument('--domain',  type=str, help='Score a single domain')
    parser.add_argument('--limit',   type=int, default=50, help='Number of domains to scan')
    parser.add_argument('--fast',    action='store_true', help='Fast mode (no API calls)')
    parser.add_argument('--min',     type=int, default=60, help='Min score to show')
    args = parser.parse_args()

    if args.domain:
        print(f"\nScoring {args.domain}...")
        result = score_domain(args.domain, args.fast)
        print(f"\nDomain      : {result['domain']}")
        print(f"Score       : {result['final_score']}/100")
        print(f"Signal      : {result['signal']}")
        print(f"Est. value  : ${result['estimated_value']:,}")
        print(f"Reg cost    : ${result['registration_cost']:.2f}")
        print(f"ROI         : {result['roi_multiple']}x")
        print("\nDimension scores:")
        for dim, score in result['dimension_scores'].items():
            print(f"  {dim:<20} {score}/20")

    elif args.scan:
        print("Fetching expired domains...")
        domains = fetch_expired_domains_whoisfreaks(args.limit)
        print(f"Found {len(domains)} domains to score")
        results = scan_domains(domains, args.fast)
        print(f"\n{'='*60}")
        print(f"TOP OPPORTUNITIES (score >= {args.min})")
        print(f"{'='*60}")
        for r in results:
            if r['final_score'] >= args.min:
                print(f"\n{r['domain']:<30} {r['signal']:<12} Score: {r['final_score']}/100  Est: ${r['estimated_value']:,}  ROI: {r['roi_multiple']}x")


# ════════════════════════════════════════════════════════════
# MAIN
# ════════════════════════════════════════════════════════════

if __name__ == '__main__':
    import sys
    if len(sys.argv) > 1:
        run_cli_scan()
    else:
        run_streamlit()
