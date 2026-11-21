import os
import re
import json
import time
import http.cookiejar
from urllib.request import Request, build_opener, HTTPCookieProcessor
from urllib.error import URLError, HTTPError
from urllib.parse import quote_plus, urlparse, parse_qs
from bs4 import BeautifulSoup as bs
from flask import Flask, request, render_template, url_for
from werkzeug.middleware.proxy_fix import ProxyFix

app = Flask(__name__)
app.config['TEMPLATES_AUTO_RELOAD'] = True
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
HEADERS = {
    'User-Agent': USER_AGENT,
    'Accept-Language': 'en-US,en;q=0.9',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
}

# Cookie jar to maintain session cookies and prevent Flipkart 301 redirect loops
cookie_jar = http.cookiejar.CookieJar()
url_opener = build_opener(HTTPCookieProcessor(cookie_jar))


def is_direct_url(query: str) -> bool:
    """Check if query is a direct Flipkart product or review URL."""
    q = query.strip().lower()
    return (
        'flipkart.com' in q
        or q.startswith('http://')
        or q.startswith('https://')
        or '/p/' in q
        or '/product-reviews/' in q
    )


def normalize_url(raw_url: str) -> str:
    """Normalize input URL into a valid full HTTPS Flipkart URL."""
    url = raw_url.strip()
    if url.startswith('http://') or url.startswith('https://'):
        return url
    if url.startswith('www.'):
        return 'https://' + url
    if url.startswith('/'):
        return 'https://www.flipkart.com' + url
    return 'https://www.flipkart.com/' + url.lstrip('/')


def clean_product_url(url: str) -> str:
    """Strip unnecessary tracking query params, preserving the product path and canonical pid."""
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    clean_query = ""
    if 'pid' in qs:
        clean_query = f"pid={qs['pid'][0]}"
    path = parsed.path
    netloc = parsed.netloc or "www.flipkart.com"
    scheme = parsed.scheme or "https"
    if clean_query:
        return f"{scheme}://{netloc}{path}?{clean_query}"
    return f"{scheme}://{netloc}{path}"


def get_review_url(url: str) -> str:
    """Transform a product URL (/p/) to a dedicated review URL (/product-reviews/)."""
    cleaned = clean_product_url(url)
    if '/product-reviews/' in cleaned:
        return cleaned
    if '/p/' in cleaned:
        return cleaned.replace('/p/', '/product-reviews/')
    return cleaned


def extract_product_name(soup: bs, fallback_url: str) -> str:
    """Extract clean product name from HTML <title> or URL slug."""
    if soup and soup.title and soup.title.string:
        title = soup.title.string.strip()
        if 'Reviews:' in title:
            return title.split('Reviews:')[0].strip()
        if '|' in title:
            candidate = title.split('|')[0].strip()
            candidate = re.sub(r'\s+Reviews?$', '', candidate, flags=re.IGNORECASE).strip()
            if candidate:
                return candidate
        cleaned = re.sub(r'\|\s*Flipkart\.com.*$', '', title, flags=re.IGNORECASE).strip()
        cleaned = re.sub(r'\s+Reviews?$', '', cleaned, flags=re.IGNORECASE).strip()
        if cleaned:
            return cleaned

    parsed = urlparse(fallback_url)
    parts = [p for p in parsed.path.split('/') if p]
    if parts:
        slug = parts[0]
        return slug.replace('-', ' ').title()

    return "Flipkart Product"


def parse_initial_state(html_text: str):
    """Locate and decode the window.__INITIAL_STATE__ hydration JSON payload."""
    soup = bs(html_text, 'html.parser')
    script_content = None

    is_script = soup.find('script', id='is_script')
    if is_script and is_script.string and 'window.__INITIAL_STATE__' in is_script.string:
        script_content = is_script.string
    else:
        for s in soup.find_all('script'):
            if s.string and 'window.__INITIAL_STATE__' in s.string:
                script_content = s.string
                break

    if not script_content and 'window.__INITIAL_STATE__' in html_text:
        idx = html_text.find('window.__INITIAL_STATE__')
        if idx != -1:
            raw = html_text[idx:]
            end_idx = raw.find('</script>')
            script_content = raw[:end_idx] if end_idx != -1 else raw

    if script_content:
        raw = script_content.strip()
        if 'window.__INITIAL_STATE__' in raw:
            raw = raw.split('window.__INITIAL_STATE__', 1)[1]
            raw = raw.lstrip(' \t=:')
        raw = raw.rstrip(' \t\n\r;')
        try:
            return json.loads(raw), soup
        except Exception as e:
            print(f"Error parsing window.__INITIAL_STATE__ JSON: {e}")

    return None, soup


def collect_reviews_from_json(node, results, seen_signatures):
    """Recursively search JSON hydration tree for customer review objects."""
    if isinstance(node, dict):
        candidates = [node]
        if 'value' in node and isinstance(node['value'], dict):
            candidates.append(node['value'])

        for cand in candidates:
            author = cand.get('author') or cand.get('authorName') or cand.get('reviewerName')
            rating = cand.get('rating')
            title = cand.get('title') or cand.get('heading')
            text = cand.get('text') or cand.get('description') or cand.get('reviewText')

            # Ensure this is a genuine user review, not general page metadata
            if (author or rating is not None) and (text or title):
                # Filter out pure page meta (author is None and rating is None)
                if author is None and rating is None:
                    continue

                if isinstance(author, str) or isinstance(title, str) or isinstance(text, str):
                    username = str(author).strip() if isinstance(author, str) and author else "Flipkart Customer"
                    r_str = str(rating).strip() if rating is not None and not isinstance(rating, (dict, list)) else "N/A"
                    head = str(title).strip() if isinstance(title, str) else ""
                    desc = str(text).strip() if isinstance(text, str) else ""

                    if desc or head:
                        sig = (username, head, desc[:60])
                        if sig not in seen_signatures:
                            seen_signatures.add(sig)
                            results.append({
                                'username': username,
                                'rating': r_str,
                                'commentHead': head,
                                'description': desc
                            })

        for v in node.values():
            collect_reviews_from_json(v, results, seen_signatures)
    elif isinstance(node, list):
        for item in node:
            collect_reviews_from_json(item, results, seen_signatures)


def collect_reviews_from_dom(soup: bs, seen_signatures: set):
    """HTML DOM scraper fallback for extracting customer reviews."""
    results = []
    comment_boxes = (
        soup.find_all('div', {'class': '_16PBlm'})
        or soup.find_all('div', {'class': 'col EPCmJX'})
        or soup.find_all('div', {'class': 'cPHDOP'})
        or soup.find_all('div', {'class': '_27M-vq'})
        or soup.find_all('div', {'class': 'row _27M-vq'})
    )

    for comment in comment_boxes:
        user_tags = comment.find_all('p', {'class': re.compile(r'(_2sc7ZR|_2NsDsF|AwS1CA|_2V5EHH)')})
        username = user_tags[0].text.strip() if user_tags else "Flipkart Customer"

        rating_tags = comment.find_all('div', {'class': re.compile(r'(_3LWZlK|XQDdHH|_1BLPMq|Ga3i8K)')})
        rating = rating_tags[0].text.strip() if rating_tags else "N/A"

        head_tags = comment.find_all('p', {'class': re.compile(r'(_2-N8zT|z9E0q0)')})
        comment_head = head_tags[0].text.strip() if head_tags else ""

        desc_tags = comment.find_all('div', {'class': re.compile(r'(t-ZTKy|ZmyHeo)')})
        description = desc_tags[0].text.strip() if desc_tags else ""

        if description or comment_head:
            sig = (username, comment_head, description[:60])
            if sig not in seen_signatures:
                seen_signatures.add(sig)
                results.append({
                    'username': username,
                    'rating': rating,
                    'commentHead': comment_head,
                    'description': description
                })

    return results


def fetch_url(url: str, timeout: int = 10) -> str:
    """Fetch URL with standardized desktop headers and cookie handling."""
    req = Request(url, headers=HEADERS)
    with url_opener.open(req, timeout=timeout) as res:
        return res.read().decode('utf-8', errors='ignore')


@app.route('/', methods=['GET', 'POST'])
def homepage():
    return render_template('index.html')


@app.route('/search', methods=['POST'])
def search():
    search_string = request.form.get('search', '').strip()
    if not search_string:
        return render_template('index.html', response=[], num_of_results=0)

    target_products = []

    # 1. Direct Product or Review URL Input
    if is_direct_url(search_string):
        normalized = normalize_url(search_string)
        target_products.append(normalized)
    else:
        # 2. Keyword Search Query Input
        flipkart_search_url = f"https://www.flipkart.com/search?q={quote_plus(search_string)}"
        try:
            search_page = fetch_url(flipkart_search_url)
            search_soup = bs(search_page, 'html.parser')

            seen_paths = set()
            for a in search_soup.find_all('a', href=True):
                href = a['href']
                if '/p/' in href:
                    base = href.split('?')[0] if '?' in href else href
                    if base not in seen_paths:
                        seen_paths.add(base)
                        full = href if href.startswith('http') else f"https://www.flipkart.com{href}"
                        target_products.append(full)

            # Fallback regex search for /p/ links if DOM anchor structure shifted
            if not target_products:
                matches = re.findall(r'href="(/[a-zA-Z0-9-]+/p/itm[a-zA-Z0-9]+(?:\?[^\s"]*)?)"', search_page)
                for href in matches:
                    base = href.split('?')[0] if '?' in href else href
                    if base not in seen_paths:
                        seen_paths.add(base)
                        target_products.append(f"https://www.flipkart.com{href}")
        except Exception as e:
            print(f"Error fetching search page for query '{search_string}': {e}")
            return render_template('index.html', response=[], num_of_results=0)

    print(f"Identified {len(target_products)} target products for processing.")

    new_list = []
    count = 1
    seen_signatures = set()

    # Limit to top 2 products for fast and responsive scraping
    for product_url in target_products[:2]:
        review_url = get_review_url(product_url)
        print(f"Scraping reviews from: {review_url}")

        page_html = ""
        try:
            page_html = fetch_url(review_url)
        except Exception as e:
            print(f"Failed fetching review URL ({review_url}): {e}")
            # Fallback to direct product URL if /product-reviews/ failed
            if review_url != product_url:
                try:
                    page_html = fetch_url(clean_product_url(product_url))
                except Exception as pe:
                    print(f"Failed fetching product fallback URL ({product_url}): {pe}")

        if not page_html:
            continue

        # Primary extraction: window.__INITIAL_STATE__ JSON
        json_state, soup = parse_initial_state(page_html)
        product_name = extract_product_name(soup, product_url)
        product_reviews = []

        if json_state:
            collect_reviews_from_json(json_state, product_reviews, seen_signatures)

        # Secondary fallback: HTML DOM selectors
        if not product_reviews and soup:
            product_reviews = collect_reviews_from_dom(soup, seen_signatures)

        for rev in product_reviews:
            new_list.append({
                'count': count,
                'username': rev['username'],
                'product_name': product_name,
                'rating': rev['rating'],
                'commentHead': rev['commentHead'],
                'description': rev['description']
            })
            count += 1

        time.sleep(0.5)

    print(f"Extracted {len(new_list)} total verified customer reviews.")
    return render_template('index.html', response=new_list, num_of_results=len(new_list))


if __name__ == '__main__':
    os.environ.pop("WERKZEUG_SERVER_FD", None)
    os.environ.pop("WERKZEUG_RUN_MAIN", None)
    port = int(os.environ.get("PORT", os.environ.get("FLIPKART_PORT", 5208)))
    host = os.environ.get("HOST", "127.0.0.1")
    print(f"🚀 Flipkart Scrapper App running on http://{host}:{port}")
    app.run(host=host, port=port, debug=False, use_reloader=False)
