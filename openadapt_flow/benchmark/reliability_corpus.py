"""Corpus of diverse public web apps for the compile-reliability study.

Selection bias (stated plainly here and in RELIABILITY.md): these are
PUBLIC, no-auth (or demo-credential) web apps reachable headlessly. The
real enterprise/desktop/Citrix targets — the ones behind SSO, VPNs, and
anti-automation walls — are exactly the apps this method cannot reach this
way, so they are UNREPRESENTED. Public apps are a generalization proxy, not
the target population.

Diversity is deliberate: login forms, e-commerce browse/cart, multi-widget
public forms, todo/CRUD apps, dense tables, a Swagger API console, native
widgets (date picker, native <select>), a canvas app, and a few known-hard
anti-bot/consent sites — across React, Vue, jQuery, Bootstrap, server-
rendered, and static frameworks. A handful are chosen specifically to
STRESS the compiler (dense/table-heavy/widget-heavy).

Each entry carries an ARM-INDEPENDENT ground-truth check (DOM/URL) where a
clean one exists; a few genuinely lack one and are labelled
``self_reported``.
"""

from __future__ import annotations

from openadapt_flow.benchmark.reliability import AppSpec, Step, Verify


def _c(selector: str, desc: str = "") -> Step:
    return Step(action="click", selector=selector, description=desc)


def _t(text: str, param: str | None = None, desc: str = "") -> Step:
    return Step(action="type", text=text, param=param, description=desc)


def _k(key: str, desc: str = "") -> Step:
    return Step(action="press", key=key, description=desc)


CORPUS: list[AppSpec] = [
    # ---- login / auth demos ------------------------------------------------
    AppSpec(
        id="herokuapp_login",
        name="The Internet — Form Authentication",
        url="https://the-internet.herokuapp.com/login",
        category="login_form",
        framework="server-rendered",
        description="Type username + password, submit; land on secure area.",
        steps=[
            _c("#username", "focus username"),
            _t("tomsmith", param="username"),
            _c("#password", "focus password"),
            _t("SuperSecretPassword!", param="password"),
            _c("button[type=submit]", "Login"),
        ],
        verify=Verify(kind="dom_text_contains", selector="#flash", value="secure area"),
        params={"username": "tomsmith", "password": "SuperSecretPassword!"},
    ),
    AppSpec(
        id="saucedemo_login_addcart",
        name="Sauce Labs Demo — login then add to cart",
        url="https://www.saucedemo.com/",
        category="ecommerce",
        framework="React",
        description="Login with demo creds, add first product to cart, open cart.",
        steps=[
            _c("#user-name", "focus username"),
            _t("standard_user", param="username"),
            _c("#password", "focus password"),
            _t("secret_sauce", param="password"),
            _c("#login-button", "Login"),
            _c("button[id^=add-to-cart]", "Add first product to cart"),
            _c(".shopping_cart_link", "Open cart"),
        ],
        verify=Verify(
            kind="dom_text_contains", selector=".shopping_cart_badge", value="1"
        ),
        params={"username": "standard_user", "password": "secret_sauce"},
        notes="Standard e-commerce browse->cart; demo credentials are public.",
    ),
    AppSpec(
        id="quotes_login",
        name="Quotes to Scrape — login",
        url="https://quotes.toscrape.com/login",
        category="login_form",
        framework="server-rendered",
        description="Login (accepts any credentials), land back with a Logout link.",
        steps=[
            _c("#username", "focus username"),
            _t("demo_user", param="username"),
            _c("#password", "focus password"),
            _t("demo_pass", param="password"),
            _c("input[type=submit]", "Sign in"),
        ],
        verify=Verify(kind="dom_visible", selector="a[href='/logout']"),
        params={"username": "demo_user", "password": "demo_pass"},
    ),
    # ---- multi-widget public forms ----------------------------------------
    AppSpec(
        id="httpbin_form",
        name="httpbin — pizza order form",
        url="https://httpbin.org/forms/post",
        category="form",
        framework="static",
        description="Fill name, pick a size radio + a topping, add comment, submit.",
        steps=[
            _c("input[name=custname]", "focus customer name"),
            _t("Ada Lovelace", param="custname"),
            _c("input[value=medium]", "size = medium"),
            _c("input[value=bacon]", "topping = bacon"),
            _c("textarea[name=comments]", "focus comments"),
            _t("extra crispy please", param="comments"),
            _c("button", "Submit order"),
        ],
        verify=Verify(kind="url_contains", value="/post"),
        params={"custname": "Ada Lovelace", "comments": "extra crispy please"},
        notes="Real multi-widget form (text + radio + checkbox + textarea).",
    ),
    AppSpec(
        id="formy_form",
        name="Formy — complete web form",
        url="https://formy-project.herokuapp.com/form",
        category="form",
        framework="Bootstrap",
        description="Fill first/last name, tick a radio + checkbox, submit.",
        steps=[
            _c("#first-name", "focus first name"),
            _t("Grace", param="first"),
            _c("#last-name", "focus last name"),
            _t("Hopper", param="last"),
            _c("#radio-button-1", "role radio"),
            _c("#checkbox-1", "experience checkbox"),
            _c(".btn.btn-lg.btn-primary", "Submit"),
        ],
        verify=Verify(
            kind="dom_text_contains", selector=".alert", value="successfully submitted"
        ),
        params={"first": "Grace", "last": "Hopper"},
    ),
    AppSpec(
        id="demoqa_textbox",
        name="DemoQA — Text Box (ad-heavy)",
        url="https://demoqa.com/text-box",
        category="form",
        framework="React",
        description="Fill name/email/address and submit; ad iframes reflow layout.",
        steps=[
            _c("#userName", "focus full name"),
            _t("Alan Turing", param="name"),
            _c("#userEmail", "focus email"),
            _t("alan@example.com", param="email"),
            _c("#currentAddress", "focus address"),
            _t("Bletchley Park", param="address"),
            _c("#submit", "Submit"),
        ],
        verify=Verify(
            kind="dom_text_contains", selector="#output", value="Alan Turing"
        ),
        params={
            "name": "Alan Turing",
            "email": "alan@example.com",
            "address": "Bletchley Park",
        },
        notes="STRESS: third-party ad iframes shift element positions between load and interaction.",
    ),
    AppSpec(
        id="seleniumbase_demo",
        name="SeleniumBase — demo page",
        url="https://seleniumbase.io/demo_page",
        category="form",
        framework="static",
        description="Type into a text field, tick a checkbox, click a button.",
        steps=[
            _c("#myTextInput", "focus text input"),
            _t("hello world", param="text"),
            _c("#checkBox1", "tick checkbox"),
            _c("#myButton", "click button"),
        ],
        verify=Verify(
            kind="dom_value_equals", selector="#myTextInput", value="hello world"
        ),
        params={"text": "hello world"},
    ),
    # ---- todo / CRUD -------------------------------------------------------
    AppSpec(
        id="todomvc_react",
        name="TodoMVC — React",
        url="https://todomvc.com/examples/react/dist/",
        category="crud",
        framework="React",
        description="Add two todos via the input + Enter.",
        steps=[
            _c(".new-todo", "focus new-todo"),
            _t("Buy milk", param="todo1"),
            _k("Enter"),
            _t("Walk the dog", param="todo2"),
            _k("Enter"),
        ],
        verify=Verify(kind="dom_count", selector=".todo-list li", count=2),
        params={"todo1": "Buy milk", "todo2": "Walk the dog"},
    ),
    AppSpec(
        id="todomvc_playwright",
        name="TodoMVC — Playwright/Vue demo",
        url="https://demo.playwright.dev/todomvc/",
        category="crud",
        framework="Vue",
        description="Add two todos via the input + Enter.",
        steps=[
            _c(".new-todo", "focus new-todo"),
            _t("Write report", param="todo1"),
            _k("Enter"),
            _t("Ship it", param="todo2"),
            _k("Enter"),
        ],
        verify=Verify(kind="dom_count", selector=".todo-list li", count=2),
        params={"todo1": "Write report", "todo2": "Ship it"},
    ),
    AppSpec(
        id="herokuapp_add_remove",
        name="The Internet — Add/Remove Elements",
        url="https://the-internet.herokuapp.com/add_remove_elements/",
        category="crud",
        framework="server-rendered",
        description="Click 'Add Element' three times; three Delete buttons appear.",
        steps=[
            _c("button[onclick='addElement()']", "Add #1"),
            _c("button[onclick='addElement()']", "Add #2"),
            _c("button[onclick='addElement()']", "Add #3"),
        ],
        verify=Verify(kind="dom_count", selector=".added-manually", count=3),
    ),
    # ---- search / navigate -------------------------------------------------
    AppSpec(
        id="wikipedia_search",
        name="Wikipedia — search + navigate",
        url="https://en.wikipedia.org/wiki/Main_Page",
        category="search",
        framework="Vue-typeahead",
        description="Type a query in the search box and press Enter.",
        steps=[
            _c("#searchInput", "focus search"),
            _t("Automation", param="query"),
            _k("Enter"),
        ],
        verify=Verify(kind="url_contains", value="Automation"),
        params={"query": "Automation"},
        notes="STRESS: Vue typeahead may intercept Enter / suggest-navigate.",
    ),
    AppSpec(
        id="duckduckgo_search",
        name="DuckDuckGo — HTML (no-JS) search",
        url="https://html.duckduckgo.com/html/",
        category="search",
        framework="server-rendered",
        description="Type a query and submit; results list appears.",
        steps=[
            _c("input[name=q]", "focus query"),
            _t("openadapt", param="query"),
            _k("Enter"),
        ],
        verify=Verify(kind="dom_count", selector=".result", count=1),
        params={"query": "openadapt"},
        notes="KNOWN-HARD: the html endpoint returns zero results to headless "
        "chromium (bot mitigation), so the search 'succeeds' vacuously — a "
        "genuine wrong_action (replay reports success, no results reached).",
    ),
    AppSpec(
        id="quotes_tag",
        name="Quotes to Scrape — filter by tag",
        url="https://quotes.toscrape.com/",
        category="navigation",
        framework="server-rendered",
        description="Click a tag link to filter quotes.",
        steps=[_c(".tag-item a", "click first tag")],
        verify=Verify(kind="url_contains", value="/tag/"),
    ),
    AppSpec(
        id="books_browse",
        name="Books to Scrape — open a product",
        url="https://books.toscrape.com/",
        category="ecommerce",
        framework="static",
        description="Click the first book to open its product page.",
        steps=[_c("article.product_pod h3 a", "open first book")],
        verify=Verify(kind="dom_visible", selector=".product_main h1"),
    ),
    AppSpec(
        id="hn_navigate",
        name="Hacker News — dense list nav",
        url="https://news.ycombinator.com/",
        category="navigation",
        framework="server-rendered",
        description="Click the 'new' nav link on the dense front page.",
        steps=[_c("a[href='newest']", "click 'new'")],
        verify=Verify(kind="url_contains", value="newest"),
        notes="STRESS: dense, tabular, live-changing content.",
    ),
    AppSpec(
        id="automationexercise_products",
        name="Automation Exercise — products (ad-heavy)",
        url="https://www.automationexercise.com/",
        category="ecommerce",
        framework="Bootstrap",
        description="Navigate to the Products page.",
        steps=[_c("a[href='/products']", "click Products")],
        verify=Verify(kind="url_contains", value="/products"),
        notes="STRESS: third-party ads.",
    ),
    AppSpec(
        id="selenium_docs_nav",
        name="Selenium.dev — docs nav",
        url="https://www.selenium.dev/",
        category="navigation",
        framework="Hugo/static",
        description="Click the Documentation nav link.",
        steps=[_c("a[href*='documentation']", "click Documentation")],
        verify=Verify(kind="url_contains", value="documentation"),
    ),
    # ---- widget / table stress --------------------------------------------
    AppSpec(
        id="datatables_filter",
        name="DataTables — dense table filter",
        url="https://datatables.net/",
        category="table",
        framework="jQuery/DataTables",
        description="Type into the table search box to filter rows.",
        steps=[
            _c(".dt-search input", "focus search"),
            _t("London", param="query"),
        ],
        verify=Verify(
            kind="dom_value_equals", selector=".dt-search input", value="London"
        ),
        params={"query": "London"},
        notes="STRESS: dense data table with live client-side filtering.",
    ),
    AppSpec(
        id="petstore_expand",
        name="Swagger Petstore — expand operation",
        url="https://petstore.swagger.io/",
        category="dashboard",
        framework="Swagger-UI/React",
        description="Click an operation summary to expand it.",
        steps=[_c(".opblock-summary", "expand first operation")],
        verify=Verify(kind="dom_visible", selector=".opblock.is-open"),
        notes="STRESS: very dense API console UI; an EU cookie-consent overlay "
        "(ch2) now intercepts pointer events, so the expand click lands on the "
        "overlay and 'succeeds' vacuously — a genuine consent-wall wrong_action.",
    ),
    AppSpec(
        id="herokuapp_tables_sort",
        name="The Internet — sortable data table",
        url="https://the-internet.herokuapp.com/tables",
        category="table",
        framework="server-rendered",
        description="Click the Last Name column header to sort the table.",
        steps=[_c("#table1 thead th:nth-child(1)", "sort by Last Name")],
        verify=Verify(
            kind="dom_text_contains",
            selector="#table1 tbody tr:first-child",
            value="Bach",
        ),
        notes="STRESS: table header sort; ascending Last-Name sort floats 'Bach' to the top row.",
    ),
    AppSpec(
        id="jqueryui_datepicker",
        name="jQuery UI — date picker widget",
        url="https://jqueryui.com/resources/demos/datepicker/default.html",
        category="widget",
        framework="jQuery UI",
        description="Open the date picker and click a day.",
        steps=[
            _c("#datepicker", "open datepicker"),
            _c("a.ui-state-default", "pick a day"),
        ],
        verify=Verify(kind="dom_value_nonempty", selector="#datepicker"),
        notes="STRESS: pop-up calendar widget rendered on click.",
    ),
    AppSpec(
        id="herokuapp_dropdown",
        name="The Internet — native <select> dropdown",
        url="https://the-internet.herokuapp.com/dropdown",
        category="widget",
        framework="server-rendered",
        description="Focus a native select and choose an option via keyboard.",
        steps=[
            _c("#dropdown", "focus select"),
            _k("ArrowDown", "move to Option 1"),
            _k("Enter", "commit selection"),
        ],
        verify=Verify(kind="dom_value_equals", selector="#dropdown", value="1"),
        notes="STRESS: native OS <select> popup is invisible to screenshots; keyboard path only.",
    ),
    AppSpec(
        id="herokuapp_inputs",
        name="The Internet — number input",
        url="https://the-internet.herokuapp.com/inputs",
        category="form",
        framework="server-rendered",
        description="Type a number into the input.",
        steps=[
            _c("input[type=number]", "focus number input"),
            _t("42", param="value"),
        ],
        verify=Verify(
            kind="dom_value_equals", selector="input[type=number]", value="42"
        ),
        params={"value": "42"},
    ),
    AppSpec(
        id="herokuapp_keypress",
        name="The Internet — key presses",
        url="https://the-internet.herokuapp.com/key_presses",
        category="widget",
        framework="server-rendered",
        description="Focus the target and press a key; result text updates.",
        steps=[
            _c("#target", "focus target"),
            _k("A", "press A"),
        ],
        verify=Verify(kind="dom_text_contains", selector="#result", value="A"),
    ),
    AppSpec(
        id="herokuapp_checkboxes",
        name="The Internet — checkboxes",
        url="https://the-internet.herokuapp.com/checkboxes",
        category="widget",
        framework="server-rendered",
        description="Tick the first (unchecked) checkbox.",
        steps=[_c("#checkboxes input:first-child", "tick first checkbox")],
        verify=Verify(kind="dom_checked", selector="#checkboxes input:first-child"),
    ),
    AppSpec(
        id="excalidraw_canvas",
        name="Excalidraw — canvas app",
        url="https://excalidraw.com/",
        category="canvas",
        framework="Canvas/React",
        description="Select the rectangle tool, then click on the canvas.",
        steps=[
            _c("[title*=Rectangle], [aria-label*=Rectangle]", "select rectangle tool"),
            Step(action="click", selector="canvas", description="click on canvas"),
        ],
        verify=Verify(kind="self_reported"),
        notes="STRESS: canvas has no DOM to assert against; self-reported only.",
    ),
    # ---- known-hard: anti-bot / consent -----------------------------------
    AppSpec(
        id="google_search",
        name="Google — search (anti-bot/consent)",
        url="https://www.google.com/",
        category="search",
        framework="proprietary",
        description="Type a query and press Enter (expected consent/anti-bot).",
        steps=[
            _c("textarea[name=q]", "focus query"),
            _t("openadapt", param="query"),
            _k("Enter"),
        ],
        verify=Verify(kind="url_contains", value="q=openadapt"),
        params={"query": "openadapt"},
        notes="KNOWN-HARD: consent interstitial / bot detection likely.",
    ),
    AppSpec(
        id="bing_search",
        name="Bing — search (consent)",
        url="https://www.bing.com/",
        category="search",
        framework="proprietary",
        description="Type a query and press Enter (expected consent banner).",
        steps=[
            _c("#sb_form_q", "focus query"),
            _t("openadapt", param="query"),
            _k("Enter"),
        ],
        verify=Verify(kind="url_contains", value="q=openadapt"),
        params={"query": "openadapt"},
        notes="KNOWN-HARD: cookie-consent overlay may block clicks.",
    ),
    AppSpec(
        id="w3schools_form",
        name="W3Schools — HTML forms page (consent/iframe)",
        url="https://www.w3schools.com/html/html_forms.asp",
        category="form",
        framework="iframe-heavy",
        description="Type into the first text input on a consent+iframe page.",
        steps=[
            _c("input[type=text]", "focus first text input"),
            _t("First", param="value"),
        ],
        verify=Verify(
            kind="dom_value_equals", selector="input[type=text]", value="First"
        ),
        params={"value": "First"},
        notes="KNOWN-HARD: cookie-consent wall + heavy ad iframes.",
    ),
]
