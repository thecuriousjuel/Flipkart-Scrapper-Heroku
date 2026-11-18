import os
import time
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError
from bs4 import BeautifulSoup as bs
from flask import Flask, request, render_template, url_for
from werkzeug.middleware.proxy_fix import ProxyFix

app = Flask(__name__)
app.config['TEMPLATES_AUTO_RELOAD'] = True
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"


@app.route('/', methods=['GET', 'POST'])
def homepage():
    return render_template('index.html')


@app.route('/search', methods=['POST'])
def search():
    search_string = request.form.get('search', '').strip()
    if not search_string:
        return render_template('index.html', response=[], num_of_results=0)

    flipkart_url = "https://www.flipkart.com/search?q=" + search_string.replace(' ', '+')

    try:
        req = Request(flipkart_url, headers={'User-Agent': USER_AGENT})
        with urlopen(req, timeout=10) as uClient:
            flipkartPage = uClient.read()
    except Exception as e:
        print(f"Error fetching Flipkart search page: {e}")
        return render_template('index.html', response=[], num_of_results=0)

    flipkart_html = bs(flipkartPage, 'html.parser')
    bigboxes = flipkart_html.findAll("a", {"class": "_1fQZEK"})
    if not bigboxes:
        # Fallback to alternate Flipkart card class
        bigboxes = flipkart_html.findAll("a", {"class": "CGtC58"})

    product_list = []
    for i in bigboxes:
        href = i.get('href')
        if href:
            product_list.append(href)

    print('Length of Product List -> ', len(product_list))

    new_list = []
    count = 1
    page = 1
    for product in product_list[:2]:  # Limit to first 2 products for responsive scraping
        product_link = "https://www.flipkart.com" + product

        try:
            prod_req = Request(product_link, headers={'User-Agent': USER_AGENT})
            with urlopen(prod_req, timeout=10) as prodRes:
                product_page = prodRes.read()
            prod_html = bs(product_page, 'html.parser')

            name_tags = prod_html.find_all('span', {'class': 'B_NuCI'}) or prod_html.find_all('span', {'class': 'VU-ZEz'})
            product_name = name_tags[0].text if name_tags else "Product"
            comment_box = prod_html.find_all('div', {'class': '_16PBlm'}) or prod_html.find_all('div', {'class': 'col EPCmJX'})

            page += 1

            if comment_box:
                for comment in comment_box:
                    user_tags = comment.find_all('p', {'class': '_2sc7ZR _2V5EHH'}) or comment.find_all('p', {'class': '_2NsDsF AwS1CA'})
                    username = user_tags[0].text if user_tags else "Anonymous"

                    rating_tags = comment.find_all('div', {'class': '_3LWZlK _1BLPMq'}) or comment.find_all('div', {'class': 'XQDdHH Ga3i8K'})
                    rating = rating_tags[0].text if rating_tags else "N/A"

                    head_tags = comment.find_all('p', {'class': '_2-N8zT'}) or comment.find_all('p', {'class': 'z9E0q0'})
                    comment_head = head_tags[0].text if head_tags else ""

                    desc_tags = comment.find_all('div', {'class': 't-ZTKy'}) or comment.find_all('div', {'class': 'ZmyHeo'})
                    description = desc_tags[0].text if desc_tags else ""

                    mydict = dict(
                        count=count,
                        username=username,
                        product_name=product_name,
                        rating=rating,
                        commentHead=comment_head,
                        description=description
                    )

                    new_list.append(mydict)
                    count += 1
        except Exception as e:
            print(f"Error parsing product page: {e}")

        time.sleep(1)

    return render_template('index.html', response=new_list, num_of_results=len(new_list))


if __name__ == '__main__':
    os.environ.pop("WERKZEUG_SERVER_FD", None)
    os.environ.pop("WERKZEUG_RUN_MAIN", None)
    port = int(os.environ.get("PORT", os.environ.get("FLIPKART_PORT", 5208)))
    host = os.environ.get("HOST", "127.0.0.1")
    print(f"🚀 Flipkart Scrapper App running on http://{host}:{port}")
    app.run(host=host, port=port, debug=False, use_reloader=False)
