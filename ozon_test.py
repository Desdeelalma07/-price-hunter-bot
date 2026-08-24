from playwright.sync_api import sync_playwright

URL = "https://www.ozon.ru/product/2185831748/"

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)

    page = browser.new_page(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0 Safari/537.36"
        )
    )

    try:
        page.goto(URL, wait_until="domcontentloaded", timeout=60000)

        print("STATUS:", page.url)
        print("TITLE:", page.title())

        print("TEXT:")
        print(page.locator("body").inner_text()[:5000])

    except Exception as e:
        print("ERROR:", repr(e))

    finally:
        browser.close()
