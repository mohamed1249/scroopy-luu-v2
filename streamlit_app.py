"""Run with: python -m streamlit run streamlit_app.py"""
import json
from collections import Counter
from datetime import datetime
import streamlit as st
from ui_client import make_payload, submit_scrape
from settings import DEFAULT_DATABASE

st.set_page_config(page_title="Scroopy-Luu", page_icon="🕸️", layout="wide")
st.title("🕸️ Scroopy-Luu")
st.caption("Collect webpage and PDF data for training models and building AI systems. Choose what to extract, inspect the results, and prepare your dataset.")

with st.sidebar:
    st.header("Connection")
    st.caption("Local Flask API · 127.0.0.1:8080")
    api_key = st.text_input("Scrape API key", type="password", key="api_key",
                            help="Use the SCROOPY_API_KEY you configured in the Flask terminal. It is not included in reports or exports.")
    database = st.text_input("MongoDB database", value="", placeholder=DEFAULT_DATABASE, key="database",
                            help="Use an existing database or enter a new name. A blank name uses scroopyluu.")
    st.caption(f"Saving to: {database.strip() or DEFAULT_DATABASE}. New databases are created on the first write. Flask and MongoDB must be running.")
    with st.expander("Start the services"):
        st.code('Get-Service MongoDB\n# If stopped, in Administrator PowerShell:\nStart-Service MongoDB', language="powershell")
        st.code('python -m flask --app API:app --no-debug run --host 127.0.0.1 --port 8080', language="powershell")
    if st.button("Clear displayed results"):
        for name in ("last_result", "result_json", "last_run_time", "last_status"):
            st.session_state.pop(name, None)
        st.info("Display cleared. Stored database records are unchanged.")

with st.form("scrape_form"):
    st.subheader("What would you like to collect?")
    urls = st.text_area("URLs — one per line", value="https://quotes.toscrape.com/", height=110, key="urls")
    left, right = st.columns(2)
    with left:
        scope = st.radio("Text selection", ["Full page", "Main content"], horizontal=True, key="scope")
        st.caption("Full preserves repeated text. Main prefers main/article content and may fall back to the body.")
        webpage = st.checkbox("Extract webpage text", value=True, key="webpage")
        pdfs = st.checkbox("Download and extract linked PDFs", value=False, key="pdfs")
        image_mode = st.selectbox("Images", ["off", "references", "download"], key="image_mode")
        st.caption("References save URLs, alt text and captions. Download also keeps image files locally.")
    with right:
        subpages = st.checkbox("Include linked pages", value=False, key="subpages")
        unlimited = st.checkbox("Unlimited pages", value=False, key="unlimited")
        max_pages = st.number_input("Maximum pages across starting URLs", min_value=1, value=20, step=1, key="max_pages")
        max_levels = st.number_input("Crawl levels (starting page = 1)", min_value=1, value=3, step=1, key="max_levels")
        same_site = st.checkbox("Stay on the starting websites", value=True, key="same_site")
        st.caption("3 levels = starting page → tab pages → linked posts. Unlimited ignores the page-count field; download budgets still apply. URLs are fetched once across the crawl.")
    with st.expander("Download limits and storage"):
        first, second = st.columns(2)
        max_file = first.number_input("Webpage/PDF file limit (MiB)", min_value=1, value=25, step=1, key="max_file")
        max_run = second.number_input("Webpage/PDF run limit (MiB)", min_value=1, value=100, step=1, key="max_run")
        max_image_mb = st.number_input("Per-image limit (MiB)", min_value=1, value=5, key="max_image_mb")
        max_images_mb = st.number_input("Separate image budget (MiB)", min_value=1, value=25, key="max_images_mb")
        max_images = st.number_input("Maximum image download attempts", min_value=1, value=100, key="max_images")
        keep_files = st.checkbox("Keep working files for inspection", value=False, key="keep_files")
        include_text = st.checkbox("Include webpage text in results", value=True, key="include_text")
        st.caption("Default: save to MongoDB, remove temporary files. Failed saves retain compressed recovery copies. PDF text is stored in MongoDB; this report shows PDF processing outcomes.")
    submitted = st.form_submit_button("Start scraping", type="primary")

if submitted:
    try:
        if not api_key.strip():
            raise ValueError("Enter your scrape API key in the sidebar.")
        payload = make_payload(urls, database=database, mode="full" if scope == "Full page" else "main",
                               webpage=webpage, pdfs=pdfs, subpages=subpages, max_pages=None if unlimited else max_pages,
                               max_levels=max_levels, same_site=same_site,
                               max_download_mb=max_file, max_run_download_mb=max_run,
                               keep_files=keep_files, include_text=include_text, image_mode=image_mode,
                               max_image_mb=max_image_mb, max_images_mb=max_images_mb, max_images=max_images)
        with st.spinner("Scraping and saving… This request can take several minutes."):
            status, result = submit_scrape(payload, api_key.strip())
        st.session_state.last_result = result
        st.session_state.last_status = status
        st.session_state.last_run_time = datetime.now().strftime("%H:%M:%S")
        st.session_state.result_json = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except (ValueError, RuntimeError) as exc:
        st.error(str(exc))
        if "last_result" in st.session_state:
            st.caption("The results below belong to the previous completed request.")

if "last_result" not in st.session_state:
    st.info("Ready when you are. Start with one webpage and PDFs disabled.")
else:
    result = st.session_state.last_result
    report = result["report"]
    st.divider()
    st.subheader("Results")
    if report.get("images", {}).get("mode", "off") != "off":
        st.caption(f"Images: {report['images']['mode']} · {report['images'].get('received_body_bytes', 0) / 1024:.1f} KiB received. Downloaded files are retained in resources/images.")
    st.caption(f"Last completed response: {st.session_state.last_run_time} · Run {report.get('run_id', 'unknown')}")
    if report.get("pull_id"):
        st.caption(f"Saved pull: {report['pull_id']}")
        st.caption("Same URLs and settings refresh this pull. Successful pages are updated; earlier pages remain if a refresh fails or does not reach them.")
    if st.session_state.last_status >= 400:
        st.error("The run encountered a server/storage failure. Successful extraction and recovery details are shown below.")
    elif isinstance(result.get("errors"), list) and result["errors"]:
        st.warning("Completed with issues. Review the activity and page details.")
    else:
        st.success("Scraping completed and results saved.")
    if isinstance(result.get("errors"), list):
        for error in result["errors"]:
            st.text(str(error))
    summary = report.get("summary", {})
    cols = st.columns(5)
    for column, name in zip(cols, ("attempted", "success", "empty", "failed", "skipped")):
        column.metric(name.capitalize(), summary.get(name, 0))
    st.caption("Counts cover webpage fetching. PDF and database outcomes appear in Activity.")
    skip_counts = Counter(page.get("skip_reason", "unspecified") for page in report.get("pages", [])
                          if page.get("outcome") == "skipped")
    if skip_counts.get("page_limit"):
        st.warning(f"{skip_counts['page_limit']} links were not fetched because the page limit was reached. "
                   "Increase Maximum pages across starting URLs or enable Unlimited pages. Pages are explored level by level.")
    if skip_counts.get("run_download_budget_exhausted"):
        st.warning("The run download budget was exhausted. Some pages were not requested.")

    if report.get("crawl"):
        crawl = report["crawl"]
        cap = "Unlimited" if crawl.get("max_pages") is None else str(crawl["max_pages"])
        st.caption(f"Crawl: {crawl['max_levels']} levels · Page cap: {cap} · Duplicate links avoided: {crawl.get('duplicate_links_avoided', 0)}")
    downloads = report.get("downloads", {})
    storage = report.get("storage", {})
    st.caption(f"Downloaded body data: {downloads.get('received_body_bytes', 0) / 1024:.1f} KiB · Retained files: {storage.get('retained_bytes', 0) / 1024:.1f} KiB")
    if storage.get("recovery_needed"):
        st.warning("Some documents need recovery. Their compressed copies remain in the run folder.")
    if report.get("files_retained"):
        st.text("Run folder: " + report.get("workspace", ""))
    st.download_button("Download JSON report", data=st.session_state.result_json,
                       file_name=f"scroopy-{report.get('run_id', 'report')}.json", mime="application/json")
    pages_tab, activity_tab, raw_tab = st.tabs(["Pages & text", "Activity", "Full report"])
    with pages_tab:
        pages = report.get("pages", [])
        if not pages:
            st.info("No webpage text was requested or collected. Check Activity for PDF results.")
        for index, page in enumerate(pages):
            reason_labels = {"page_limit": "page limit reached", "unsupported_scheme": "non-web link",
                             "run_download_budget_exhausted": "download budget exhausted",
                             "outside_site": "outside starting websites", "duplicate_redirect": "already fetched redirect target"}
            reason = page.get("skip_reason")
            status_label = page.get("outcome", "unknown").upper()
            if reason:
                status_label += " - " + reason_labels.get(reason, reason)
            with st.expander(f"{status_label} · {page.get('url', '')}", expanded=len(pages) == 1):
                st.caption(f"Level {page.get('depth', 0) + 1} · HTTP {page.get('http_status')} · Region: {page.get('selected_region')} · {page.get('character_count', 0)} characters · {page.get('duration_ms', 0)} ms")
                if page.get("error"):
                    st.json(page["error"])
                if page.get("skip_reason"):
                    st.info(page["skip_reason"])
                if page.get("images"):
                    st.caption("Image resources (URLs, captions and local file paths)")
                    st.dataframe(page["images"], hide_index=True)
                if page.get("text") is not None:
                    st.text_area("Extracted text", value=page["text"], height=250, disabled=True,
                                 key=f"text_{report.get('run_id')}_{index}")
                elif page.get("outcome") in ("success", "empty"):
                    st.caption("Text was omitted from this response. Check the saved database document.")
    with activity_tab:
        st.caption("Events arrive when the request finishes; this is not a live progress feed.")
        st.dataframe(report.get("events", []), hide_index=True)
    with raw_tab:
        st.json(result, expanded=False)
